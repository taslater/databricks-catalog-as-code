"""Unity Catalog lineage, read over REST (docs/diagrams.md, phase D2).

The lineage API needs no warehouse, and has an edge within minutes of the
statement that made it (AGENTS.md, Open questions). Table lineage is one
call per object with columns (a table, a view, a materialized view...), and
column lineage one per column. The API names
objects in three parts. `read` drops the catalog it reads from, as the model
does, so a name in the same catalog matches a model key's.

Two kinds of edge are left out:

- an edge from an object to itself, which `CREATE OR REPLACE ... AS SELECT`
  from the same table leaves;
- edges to notebooks, jobs and paths, which need `include_entity_lineage`.
  None has been seen on this workspace yet (docs/diagrams.md, section 7).

A snapshot keeps what `read` found, as `to_json` writes it. A snapshot read
with a warehouse can also hold, from query history, the statements that wrote
each object (`Write`), how often each was read (`Use`), the queries that read
two or more objects (`Query`), the jobs the writes ran from (`Job`), and, from
the storage metrics, how much each table holds (`Size`); `bulk` reads them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, astuple, dataclass, field
from typing import Any

from .model import tabular
from .progress import Progress
from .workspace import Workspace

API = "/api/2.0/lineage-tracking"


@dataclass(frozen=True, order=True)
class Edge:
    source: str
    target: str
    seen: str  # the API's lineage_timestamp: when a statement last made the edge


@dataclass(frozen=True)
class Write:
    """A statement that wrote an object, as query history has it: one per object and
    statement text, however many times that text ran."""
    target: str  # the object written, named as edges name it
    sql: str
    type: str  # query history's statement type: INSERT, MERGE, CREATE...
    runs: int  # how many times it ran, in the time `bulk` reads
    last: str  # when it last started, as `2026-11-04T17:28:20Z`
    by: str | None  # who ran it last
    client: str | None  # what ran it last, as query history names it: `Databricks CLI`...
    source: dict[str, str]  # where it ran from last: `job_id`, `notebook_id`...
    of: int  # how many statements wrote the object in that time; the latest are kept
    reads: bool  # whether the lineage saw it read a table: False for DDL, `VALUES`...


@dataclass(frozen=True)
class Use:
    """How often an object was read by queries that wrote nothing."""
    target: str
    queries: int
    people: int
    last: str


@dataclass(frozen=True)
class Query:
    """A query that read two or more objects, at least one of the model's, and wrote
    nothing, as query history has it: one per text. Its joins say how they go together."""
    sql: str
    tables: list[str]  # the objects it read, named as edges name them, sorted
    runs: int
    people: int  # how many people ran it
    last: str


@dataclass(frozen=True)
class Size:
    """How much a table holds, from the storage system table's daily metrics."""
    target: str
    bytes: int  # in the table's current version, not counting files kept for time travel
    files: int
    on: str  # the day of the latest metrics, `2026-11-04`
    # (day, bytes) on the first day `bulk` reads, on each day the size changed, and on `on`
    history: list[tuple[str, int]]


@dataclass(frozen=True)
class Task:
    """One task of a job, as the jobs API defines it now."""
    key: str
    depends_on: list[str]  # the keys of the tasks it waits for
    runs: str  # `notebook`, `sql_file`, `sql_query`, `python`, `pipeline`, `job`...
    path: str | None  # the notebook's or file's path, or the query's, pipeline's or job's id


@dataclass(frozen=True)
class Job:
    id: str
    tasks: list[Task]


@dataclass
class Lineage:
    tables: list[Edge] = field(default_factory=list)
    columns: list[Edge] = field(default_factory=list)  # names are `schema.table.column`
    writes: list[Write] | None = None  # None when query history wasn't read
    uses: list[Use] | None = None
    # the names of the jobs and pipelines the writes and the model name by id, keyed
    # `job:<id>` and `pipeline:<id>`; None when they weren't looked up
    names: dict[str, str] | None = None
    queries: list[Query] | None = None  # None when query history wasn't read
    sizes: list[Size] | None = None  # None when the storage metrics weren't read
    jobs: list[Job] | None = None  # the jobs the writes ran from; None when not read

    def upstream_columns(self, column: str) -> list[Edge]:
        return [e for e in self.columns if e.target == column]

    def to_json(self) -> dict:
        """Each edge as `[source, target, seen]`, and writes and uses as objects, when
        query history was read."""
        out: dict = {"tables": [list(astuple(e)) for e in self.tables],
                     "columns": [list(astuple(e)) for e in self.columns]}
        if self.writes is not None:
            out["writes"] = [asdict(w) for w in self.writes]
        if self.uses is not None:
            out["uses"] = [asdict(u) for u in self.uses]
        if self.names is not None:
            out["names"] = dict(sorted(self.names.items()))
        for key in ("queries", "sizes", "jobs"):
            if (found := getattr(self, key)) is not None:
                out[key] = [asdict(x) for x in found]
        return out

    @classmethod
    def from_json(cls, data: dict) -> Lineage:
        def each(key: str, make: Callable[[dict], Any]) -> list | None:
            return None if data.get(key) is None else [make(x) for x in data[key]]
        return cls([Edge(*e) for e in data["tables"]], [Edge(*e) for e in data["columns"]],
                   each("writes", lambda w: Write(**w)), each("uses", lambda u: Use(**u)),
                   data.get("names"), each("queries", lambda q: Query(**q)),
                   each("sizes", lambda z: Size(**{**z, "history": [tuple(p) for p in
                                                                    z["history"]]})),
                   each("jobs", lambda j: Job(j["id"], [Task(**t) for t in j["tasks"]])))


def read(ws: Workspace, catalog: str, model: dict[str, dict],
         progress: Progress | None = None) -> Lineage:
    p = progress or Progress.off()
    objects = [o for o in model.values() if tabular(o)]
    with p.step("reading table lineage", len(objects), "objects") as step:
        tables = ws.map(lambda o: table_edges(
            catalog, o["name"],
            ws.get(f"{API}/table-lineage", table_name=f"{catalog}.{o['name']}")),
            objects, done=lambda o: step.tick(detail=o["name"]))
    columns = [(o["name"], c["name"]) for o in objects for c in o["columns"]]
    with p.step("reading column lineage", len(columns), "columns") as step:
        found = ws.map(lambda tc: column_edges(catalog, *tc, ws.get(
            f"{API}/column-lineage", table_name=f"{catalog}.{tc[0]}", column_name=tc[1])),
            columns, done=lambda tc: step.tick(detail=tc[0]))
    return Lineage(_merge(tables), _merge(found))


def table_edges(catalog: str, name: str, response: dict) -> list[Edge]:
    """The edges in one table-lineage response for `name`, a two-part name."""
    out = []
    for side in ("upstreams", "downstreams"):
        for entry in response.get(side) or []:
            info = entry.get("tableInfo")
            if not info:
                continue
            other = _name(catalog, info["catalog_name"], info["schema_name"], info["name"])
            pair = (other, name) if side == "upstreams" else (name, other)
            out.append(Edge(*pair, info.get("lineage_timestamp", "")))
    return out


def column_edges(catalog: str, table: str, column: str, response: dict) -> list[Edge]:
    """The edges in one column-lineage response for `table.column`."""
    here, out = f"{table}.{column}", []
    for side in ("upstream_cols", "downstream_cols"):
        for c in response.get(side) or []:
            other = _name(catalog, c["catalog_name"], c["schema_name"], c["table_name"],
                          c["name"])
            pair = (other, here) if side == "upstream_cols" else (here, other)
            out.append(Edge(*pair, c.get("lineage_timestamp", "")))
    return out


def _name(catalog: str, other_catalog: str, *parts: str) -> str:
    return ".".join(parts if other_catalog == catalog else (other_catalog, *parts))


def _merge(found: list[list[Edge]]) -> list[Edge]:
    """One edge per pair, the latest seen, without self-edges. Each edge is
    usually found twice: from its source and from its target."""
    latest: dict[tuple[str, str], str] = {}
    for e in (e for edges in found for e in edges if e.source != e.target):
        latest[e.source, e.target] = max(latest.get((e.source, e.target), ""), e.seen)
    return sorted(Edge(s, t, seen) for (s, t), seen in latest.items())
