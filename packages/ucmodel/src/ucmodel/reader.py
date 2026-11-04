"""Read the raw Unity Catalog state of some schemas, over REST only.

The result is the API's own JSON, grouped by schema, plus the tags and
grants that live behind separate endpoints. `normalise` turns it into the
model. Keeping the two apart lets the tests run the normaliser on saved
payloads with no workspace.
"""

from __future__ import annotations

from urllib.parse import quote

from .progress import Progress
from .workspace import UC, Workspace


def read(ws: Workspace, catalog: str, schemas: list[str],
         progress: Progress | None = None) -> dict:
    """Two steps, so the second has a count to show: list each schema's objects, then
    read every object, from all the schemas at once. Each runs eight calls at a time."""
    p = progress or Progress.off()
    with p.step("listing schemas", len(schemas), "schemas") as step:
        listed = dict(zip(schemas, ws.map(lambda name: _list(ws, catalog, name), schemas,
                                          done=lambda name: step.tick(detail=name)),
                          strict=True))
    work = [(schema, group, item) for schema, s in listed.items() if s
            for group in ("tables", "functions", "volumes") for item in s.pop(f"_{group}")]
    with p.step("reading tables, views, functions and volumes", len(work), "objects") as step:
        def one(job: tuple[str, str, dict]) -> dict:
            schema, group, item = job
            full = f"{catalog}.{schema}.{item['name']}"
            if group == "tables":
                return _table(ws, full)
            return _function(ws, full) if group == "functions" else _volume(ws, item)
        found = ws.map(one, work, done=lambda job: step.tick(detail=job[0]))
    for (schema, group, item), value in zip(work, found, strict=True):
        listed[schema][group][item["name"]] = value  # type: ignore[index]
    return {"catalog": catalog, "schemas": listed}


def schemas(ws: Workspace, catalog: str) -> list[str]:
    """The catalog's schemas, except `information_schema`, which the platform keeps."""
    return sorted(s["name"] for s in ws.list_all(f"{UC}/schemas", "schemas", catalog_name=catalog)
                  if s["name"] != "information_schema")


def read_schema(ws: Workspace, catalog: str, schema: str) -> dict | None:
    return read(ws, catalog, [schema])["schemas"][schema]


def _list(ws: Workspace, catalog: str, schema: str) -> dict | None:
    """A schema's own state, with empty groups for its objects, and what is in each
    group under `_tables`, `_functions` and `_volumes`, for `read` to fill in."""
    full = f"{catalog}.{schema}"
    info = ws.schema(full)
    if info is None:
        return None
    scope = {"catalog_name": catalog, "schema_name": schema}
    return {
        "info": info,
        "tags": ws.tags("schemas", full),
        "grants": ws.grants("schema", full),
        "tables": {}, "functions": {}, "volumes": {},
        "_tables": [{"name": t["name"]} for t in ws.list_all(f"{UC}/tables", "tables", **scope)],
        "_functions": [{"name": f["name"]}
                       for f in ws.list_all(f"{UC}/functions", "functions", **scope)],
        "_volumes": ws.list_all(f"{UC}/volumes", "volumes", **scope),
    }


def _table(ws: Workspace, full: str) -> dict:
    info = ws.table(full) or {}
    return {
        "info": info,
        "tags": ws.tags("tables", full),
        "column_tags": {
            c["name"]: tags
            for c in info.get("columns", [])
            if (tags := ws.tags("columns", f"{full}.{c['name']}"))
        },
        "grants": ws.grants("table", full),
    }


def _function(ws: Workspace, full: str) -> dict:
    return {
        "info": ws.get_or_none(f"{UC}/functions/{quote(full)}") or {},
        "grants": ws.grants("function", full),
    }


def _volume(ws: Workspace, info: dict) -> dict:
    full = info["full_name"]
    return {
        "info": info,
        "tags": ws.tags("volumes", full),
        "grants": ws.grants("volume", full),
    }
