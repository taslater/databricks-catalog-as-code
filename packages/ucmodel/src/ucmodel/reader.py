"""Read the raw Unity Catalog state of some schemas, over REST only.

The result is the API's own JSON, grouped by schema, plus the tags and
grants that live behind separate endpoints. `normalise` turns it into the
model. Keeping the two apart lets the tests run the normaliser on saved
payloads with no workspace.
"""

from __future__ import annotations

from urllib.parse import quote

from .workspace import UC, Workspace


def read(ws: Workspace, catalog: str, schemas: list[str]) -> dict:
    return {
        "catalog": catalog,
        "schemas": {name: read_schema(ws, catalog, name) for name in schemas},
    }


def schemas(ws: Workspace, catalog: str) -> list[str]:
    """The catalog's schemas, except `information_schema`, which the platform keeps."""
    return sorted(s["name"] for s in ws.list_all(f"{UC}/schemas", "schemas", catalog_name=catalog)
                  if s["name"] != "information_schema")


def read_schema(ws: Workspace, catalog: str, schema: str) -> dict | None:
    full = f"{catalog}.{schema}"
    info = ws.schema(full)
    if info is None:
        return None
    scope = {"catalog_name": catalog, "schema_name": schema}
    table_names = [t["name"] for t in ws.list_all(f"{UC}/tables", "tables", **scope)]
    function_names = [f["name"] for f in ws.list_all(f"{UC}/functions", "functions", **scope)]
    volumes = ws.list_all(f"{UC}/volumes", "volumes", **scope)
    return {
        "info": info,
        "tags": ws.tags("schemas", full),
        "grants": ws.grants("schema", full),
        "tables": dict(zip(table_names, ws.map(lambda n: _table(ws, f"{full}.{n}"), table_names),
                           strict=True)),
        "functions": dict(zip(function_names,
                              ws.map(lambda n: _function(ws, f"{full}.{n}"), function_names),
                              strict=True)),
        "volumes": {v["name"]: _volume(ws, v) for v in volumes},
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
