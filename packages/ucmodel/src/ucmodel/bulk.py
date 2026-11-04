"""Reads over a SQL warehouse: a few queries for a whole catalog, in place of REST calls
per object.

Over REST, each table's tags take one call, its columns' tags one call per column, and
its grants another, and each schema's functions take a list call, which the platform
answers slowly when several run at once. Lineage takes one call per object and one
per column. With a warehouse, five queries do the same:

- `read` reads every tag, every direct grant and every function's name in the catalog
  from `system.information_schema`, in three queries run side by side. Its `Read`
  answers `tags` and `grants` as `Workspace` does, so the reader takes either.
- `lineage` reads table and column lineage from `system.access.table_lineage` and
  `column_lineage`, as the lineage API would answer for the model's objects: edges from
  the last year with one end among them and the other end still there, since the API
  leaves out an edge to a table that was dropped or renamed. An end in a schema that was
  read is there when the model has it; an end anywhere else is looked up over REST, once
  per table. Each edge's `seen` is its latest event, to the nearest second, written as
  the API writes its timestamps. The API's own timestamp is sometimes an earlier event
  of the same edge, by seconds, so `seen` can differ between snapshots read the two ways.

- `history` reads, from `system.query.history` joined to the lineage, the statements
  that wrote each of the model's objects in the last 90 days, one per text with how
  often it ran, who ran it last and from where, the latest ten for each object; and how
  many queries that wrote nothing read each object, by how many people. Query history
  has only statements run on SQL warehouses and serverless compute, and a pipeline's
  refreshes have no statement there. `names` then names the jobs and pipelines those
  statements ran from, and the pipelines that refresh the model's materialized views
  and streaming tables, from `system.lakeflow`, which has a job some time after it is
  created.

What these queries can see is what the caller can see, as over REST. A grant inherited
from a schema or catalog is left out, as the permissions API leaves it out. The system
tables often need an admin's grant, so `ucmodel.snapshot.take` falls back to REST when
a query fails.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from databricks.sdk.errors.base import DatabricksError

from .lineage import Edge, Lineage, Use, Write, _merge, _name
from .model import tabular
from .progress import Progress
from .workspace import Workspace

IS = "system.information_schema"

TAGS = f"""
SELECT 'schemas' AS kind, schema_name AS s, NULL AS o, NULL AS col, tag_name, tag_value
  FROM {IS}.schema_tags WHERE catalog_name = :catalog
UNION ALL
SELECT 'tables', schema_name, table_name, NULL, tag_name, tag_value
  FROM {IS}.table_tags WHERE catalog_name = :catalog
UNION ALL
SELECT 'columns', schema_name, table_name, column_name, tag_name, tag_value
  FROM {IS}.column_tags WHERE catalog_name = :catalog
UNION ALL
SELECT 'volumes', schema_name, volume_name, NULL, tag_name, tag_value
  FROM {IS}.volume_tags WHERE catalog_name = :catalog
"""

GRANTS = f"""
SELECT 'schema' AS kind, schema_name AS s, NULL AS o, grantee, privilege_type
  FROM {IS}.schema_privileges WHERE catalog_name = :catalog AND inherited_from = 'NONE'
UNION ALL
SELECT 'table', table_schema, table_name, grantee, privilege_type
  FROM {IS}.table_privileges WHERE table_catalog = :catalog AND inherited_from = 'NONE'
UNION ALL
SELECT 'function', routine_schema, routine_name, grantee, privilege_type
  FROM {IS}.routine_privileges WHERE routine_catalog = :catalog AND inherited_from = 'NONE'
UNION ALL
SELECT 'volume', volume_schema, volume_name, grantee, privilege_type
  FROM {IS}.volume_privileges WHERE volume_catalog = :catalog AND inherited_from = 'NONE'
"""

FUNCTIONS = f"""
SELECT routine_schema AS s, routine_name AS o
  FROM {IS}.routines WHERE routine_catalog = :catalog
"""

# One row per edge, its latest event. An edge to or from a path or a notebook has no
# table at that end, and the lineage API leaves it out too.
_ENDS = ("source_table_catalog", "source_table_schema", "source_table_name",
         "target_table_catalog", "target_table_schema", "target_table_name")
_WHERE = f"""
WHERE (source_table_catalog = :catalog OR target_table_catalog = :catalog)
  AND {' AND '.join(f'{c} IS NOT NULL' for c in _ENDS)}
  AND event_date >= date_sub(current_date(), 365)"""
TABLE_LINEAGE = f"""
SELECT {', '.join(_ENDS)}, max(event_time) AS seen
  FROM system.access.table_lineage {_WHERE}
 GROUP BY ALL"""
COLUMN_LINEAGE = f"""
SELECT {', '.join(_ENDS)}, source_column_name, target_column_name, max(event_time) AS seen
  FROM system.access.column_lineage {_WHERE}
   AND source_column_name IS NOT NULL AND target_column_name IS NOT NULL
 GROUP BY ALL"""

DAYS = 90  # how far back `history` reads
KEEP = 10  # the most statements `history` keeps for an object, the latest
QUERIES = ("query", "queries")  # a progress unit: "1 query", "2 queries"
WRITES = f"""
WITH wrote AS (
  SELECT statement_id, target_table_schema AS s, target_table_name AS t,
         max(source_table_name IS NOT NULL) AS reads
    FROM system.access.table_lineage
   WHERE target_table_catalog = :catalog AND statement_id IS NOT NULL
     AND event_date >= date_sub(current_date(), {DAYS})
   GROUP BY ALL),
ran AS (
  SELECT w.s, w.t, h.statement_text AS sql, h.statement_type AS type, count(*) AS runs,
         max(h.start_time) AS last, max_by(h.executed_by, h.start_time) AS by,
         max_by(h.client_application, h.start_time) AS client,
         max_by(to_json(h.query_source), h.start_time) AS source, max(w.reads) AS reads
    FROM wrote w JOIN system.query.history h USING (statement_id)
   WHERE h.execution_status = 'FINISHED' AND h.start_time >= date_sub(current_date(), {DAYS})
   GROUP BY w.s, w.t, h.statement_text, h.statement_type)
SELECT *, count(*) OVER (PARTITION BY s, t) AS of FROM ran
QUALIFY row_number() OVER (PARTITION BY s, t ORDER BY last DESC) <= {KEEP}"""
USES = f"""
SELECT source_table_schema AS s, source_table_name AS t,
       count(DISTINCT statement_id) AS queries, count(DISTINCT created_by) AS people,
       max(event_time) AS last
  FROM system.access.table_lineage
 WHERE source_table_catalog = :catalog AND source_table_name IS NOT NULL
   AND target_table_name IS NULL AND statement_id IS NOT NULL
   AND event_date >= date_sub(current_date(), {DAYS})
 GROUP BY ALL"""

NAMES = """
SELECT concat('job:', job_id) AS id, max_by(name, change_time) AS name
  FROM system.lakeflow.jobs WHERE array_contains(split(:jobs, ','), job_id) GROUP BY job_id
UNION ALL
SELECT concat('pipeline:', pipeline_id), max_by(name, change_time)
  FROM system.lakeflow.pipelines WHERE array_contains(split(:pipelines, ','), pipeline_id)
 GROUP BY pipeline_id"""


@dataclass
class Read:
    """What the warehouse read for a catalog. Tags and grants are keyed as
    `Workspace.tags` and `grants` are asked for them, by entity or securable type and
    full name; `functions` names each schema's functions."""
    catalog: str
    tag_map: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    grant_map: dict[tuple[str, str], dict[str, list[str]]] = field(default_factory=dict)
    functions: dict[str, list[str]] = field(default_factory=dict)

    def tags(self, entity_type: str, full_name: str) -> dict[str, str]:
        return dict(self.tag_map.get((entity_type, full_name), {}))

    def grants(self, securable_type: str, full_name: str) -> dict[str, list[str]]:
        found = self.grant_map.get((securable_type, full_name), {})
        return {principal: list(privileges) for principal, privileges in found.items()}

    @classmethod
    def from_rows(cls, catalog: str, tags: list[dict], grants: list[dict],
                  functions: list[dict]) -> Read:
        def full(*parts: str | None) -> str:
            return ".".join([catalog, *(p for p in parts if p is not None)])
        tag_map: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
        for r in tags:  # a tag with no value reads as "", as the tags API gives it
            tag_map[r["kind"], full(r["s"], r["o"], r["col"])][r["tag_name"]] = (
                r["tag_value"] or "")
        privileges: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set))
        for r in grants:
            privileges[r["kind"], full(r["s"], r["o"])][r["grantee"]].add(r["privilege_type"])
        names: dict[str, list[str]] = defaultdict(list)
        for r in functions:
            names[r["s"]].append(r["o"])
        return cls(catalog, dict(tag_map),
                   {key: {p: sorted(v) for p, v in found.items()}
                    for key, found in privileges.items()},
                   {s: sorted(found) for s, found in names.items()})


def read(ws: Workspace, catalog: str, progress: Progress | None = None) -> Read:
    p = progress or Progress.off()
    with p.step("reading tags, grants and functions from information_schema", 3,
                QUERIES) as step:
        tags, grants, functions = ws.map(lambda q: ws.sql(q, params={"catalog": catalog}),
                                         [TAGS, GRANTS, FUNCTIONS], done=lambda q: step.tick())
    return Read.from_rows(catalog, tags, grants, functions)


def lineage(ws: Workspace, catalog: str, model: dict[str, dict],
            progress: Progress | None = None) -> Lineage:
    """The lineage of the model's objects, from the system tables."""
    p = progress or Progress.off()
    with p.step("reading lineage from the system tables", 2, QUERIES) as step:
        tables, columns = ws.map(lambda q: ws.sql(q, params={"catalog": catalog}),
                                 [TABLE_LINEAGE, COLUMN_LINEAGE], done=lambda q: step.tick())
    elsewhere = sorted(outside(catalog, model, tables + columns))
    with p.step("looking up lineage's other ends", len(elsewhere), "tables") as step:
        found = dict(zip(elsewhere, ws.map(lambda name: _columns(ws, catalog, name), elsewhere,
                                           done=lambda name: step.tick()), strict=True))
    return edges(catalog, model, tables, columns, found.get)


def history(ws: Workspace, catalog: str, model: dict[str, dict],
            progress: Progress | None = None) -> tuple[list[Write], list[Use]]:
    """The statements that wrote the model's objects, and how often each was read."""
    p = progress or Progress.off()
    with p.step("reading query history", 2, QUERIES) as step:
        writes, uses = ws.map(lambda q: ws.sql(q, params={"catalog": catalog}), [WRITES, USES],
                              done=lambda q: step.tick())
    return ran(model, writes, uses)


def names(ws: Workspace, model: dict[str, dict], writes: list[Write],
          progress: Progress | None = None) -> dict[str, str]:
    """The names of the jobs and pipelines the writes ran from, and of the pipelines that
    refresh the model's objects, keyed `job:<id>` and `pipeline:<id>`."""
    jobs = sorted({w.source["job_id"] for w in writes if "job_id" in w.source})
    pipelines = sorted({w.source["pipeline_id"] for w in writes if "pipeline_id" in w.source}
                       | {o["meta"]["pipeline_id"] for o in model.values()
                          if (o.get("meta") or {}).get("pipeline_id")})
    if not jobs and not pipelines:
        return {}
    with (progress or Progress.off()).step("naming jobs and pipelines", 1, QUERIES) as step:
        rows = ws.sql(NAMES, params={"jobs": ",".join(jobs), "pipelines": ",".join(pipelines)})
        step.tick()
    return {r["id"]: r["name"] for r in rows if r["name"]}


def ran(model: dict[str, dict], writes: list[dict],
        uses: list[dict]) -> tuple[list[Write], list[Use]]:
    """The rows as writes and uses of the model's objects, latest first."""
    own = {o["name"] for o in model.values() if tabular(o)}
    found = [Write(f"{r['s']}.{r['t']}", r["sql"], r["type"], int(r["runs"]), _iso(r["last"]),
                   r["by"], r["client"], _flat(json.loads(r["source"] or "{}")), int(r["of"]),
                   r["reads"] in (True, "true"))
             for r in writes if f"{r['s']}.{r['t']}" in own]
    used = [Use(f"{r['s']}.{r['t']}", int(r["queries"]), int(r["people"]), _iso(r["last"]))
            for r in uses if f"{r['s']}.{r['t']}" in own]
    latest_first = sorted(found, key=lambda w: w.last, reverse=True)
    return sorted(latest_first, key=lambda w: w.target), sorted(used, key=lambda u: u.target)


def _flat(source: dict, into: dict[str, str] | None = None) -> dict[str, str]:
    """Query history's `query_source`, its nested ids as one level: `job_id`..."""
    out = {} if into is None else into
    for key, value in source.items():
        if isinstance(value, dict):
            _flat(value, out)
        elif value is not None:
            out[key] = str(value)
    return out


def _iso(at: str) -> str:
    """`2026-11-04T17:52:44.012Z` to the second: `2026-11-04T17:52:44Z`."""
    return f"{datetime.fromisoformat(at.replace('Z', '+00:00')):%Y-%m-%dT%H:%M:%SZ}"


def edges(catalog: str, model: dict[str, dict], tables: list[dict], columns: list[dict],
          lookup: Callable[[str], set[str] | None] = lambda name: None) -> Lineage:
    """The lineage the rows give, kept to edges the lineage API would give for the
    model's objects: one end among them, and the other still there. `lookup` gives
    the columns of a table outside the schemas the model read, or None."""
    own = {o["name"]: {c["name"] for c in o["columns"]} for o in model.values() if tabular(o)}
    read_schemas = _schemas(model)

    def columns_at(r: dict, side: str) -> set[str] | None:
        name = _end(catalog, r, side)
        if name in own:
            return own[name]
        if (r[f"{side}_table_catalog"], r[f"{side}_table_schema"]) in (
                (catalog, s) for s in read_schemas):
            return None  # in a schema that was read, and not in the model: gone
        return lookup(name)

    def keep(r: dict, by_column: bool) -> bool:
        if not any(_end(catalog, r, side) in own for side in ("source", "target")):
            return False
        for side in ("source", "target"):
            there = columns_at(r, side)
            if there is None or (by_column and r[f"{side}_column_name"] not in there):
                return False
        return True

    return Lineage(
        _merge([[Edge(_end(catalog, r, "source"), _end(catalog, r, "target"), seen(r["seen"]))
                 for r in tables if keep(r, by_column=False)]]),
        _merge([[Edge(_end(catalog, r, "source", r["source_column_name"]),
                      _end(catalog, r, "target", r["target_column_name"]), seen(r["seen"]))
                 for r in columns if keep(r, by_column=True)]]))


def outside(catalog: str, model: dict[str, dict], rows: list[dict]) -> set[str]:
    """The tables at the rows' ends that aren't in a schema the model read, named as
    edges name them."""
    read_schemas = _schemas(model)
    return {_end(catalog, r, side) for r in rows for side in ("source", "target")
            if not (r[f"{side}_table_catalog"] == catalog
                    and r[f"{side}_table_schema"] in read_schemas)}


def seen(event_time: str) -> str:
    """An event time, `2026-11-04T17:28:20.668Z`, as the lineage API writes its
    timestamps: to the nearest second, `2026-11-04 17:28:21.0`."""
    at = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    at = (at + timedelta(microseconds=500_000)).replace(microsecond=0)
    return f"{at:%Y-%m-%d %H:%M:%S}.0"


def _schemas(model: dict[str, dict]) -> set[str]:
    return {o["name"] for key, o in model.items() if key.startswith("schema:")}


def _end(catalog: str, r: dict, side: str, *column: str) -> str:
    return _name(catalog, r[f"{side}_table_catalog"], r[f"{side}_table_schema"],
                 r[f"{side}_table_name"], *column)


def _columns(ws: Workspace, catalog: str, name: str) -> set[str] | None:
    """A table's columns, or None when it isn't there or can't be seen."""
    full = name if name.count(".") == 2 else f"{catalog}.{name}"
    try:
        info = ws.table(full)
    except DatabricksError:
        return None
    return None if info is None else {c["name"] for c in info.get("columns") or []}
