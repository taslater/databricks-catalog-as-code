"""The explorer: one interactive page for a whole snapshot (docs/explorer.md).

`graph` gathers what the page shows from two sources, each kept apart in the
data so the page can say which one a fact came from:

- the SQL: each view's definition, and the statements in any SQL files given
  (`sqllineage`). It says how each column is computed, and which objects are
  read only to join or filter;
- what ran: the lineage the platform recorded (`ucmodel.lineage`), with when
  it last saw each edge. It covers writes the SQL files don't, such as
  notebooks, but not how.

An edge either source names to an object outside the snapshot, in another
catalog or schema that wasn't read, or files read with `read_files`, gets a
small node of its own, so the edge is drawn rather than dropped.

Nothing here reads the workspace or a warehouse: a snapshot is enough.
`write` lays the graph out (`layout`, with Graphviz) and writes the page.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

from ucmodel.lineage import Lineage
from ucmodel.progress import Progress

from . import layout, sqllineage

ASSETS = files("ucatlas") / "explorer"
PAGE = "index.html"
# The libraries the page loads, and their licenses, written beside it so it works offline.
# The page falls back to jsDelivr for a library that isn't beside it.
VENDOR = ("d3.min.js", "elk.bundled.js", "THIRD-PARTY-NOTICES.txt")


def _grain(comment: str) -> str | None:
    """`One row per order line, ...` gives `order line`."""
    if not comment.lower().startswith("one row per "):
        return None
    return re.split(r":|, with |\. ", comment[len("one row per "):])[0].rstrip(".") or None


def _when(ms: int | None) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seen(s: str) -> str | None:
    """The lineage API's `2026-10-31 04:19:00.0` as ISO 8601, UTC."""
    m = re.match(r"(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)", s or "")
    return f"{m[1]}T{m[2]}Z" if m else None


def _stub(nid: str, catalog_schemas: set[str]) -> dict:
    if nid.startswith("files:"):
        kind, schema, name, comment = "files", "(files)", nid[6:], "Files read with read_files"
    else:
        parts = nid.split(".")
        schema = ".".join(parts[:-1]) or "(unknown)"
        kind, name = "external", parts[-1]
        comment = ("In a schema the snapshot didn't read" if schema in catalog_schemas
                   else "Outside the snapshot")
    return {"id": nid, "kind": kind, "schema": schema, "name": name, "comment": comment,
            "grain": None, "tags": {}, "owner": None, "columns": [], "pk": [], "fks": [],
            "definition": None, "writers": [], "changed": None, "created": None,
            "written": None}


def graph(model: dict[str, dict], catalog: str, taken_at: datetime,
          found: Lineage | None = None, sql_files: list[tuple[str, str]] | None = None,
          *, snapshot: str | None = None, progress: Progress | None = None) -> dict:
    """The page's data: objects, column and table edges, declared keys, joins, and
    where lineage couldn't be read."""
    objects = sqllineage.objects(model)
    schemas_in = {o["name"]: o for o in model.values() if o["kind"] == "schema"}
    nodes: dict[str, dict] = {}
    for name, obj in sorted(objects.items()):
        schema, short = name.split(".", 1)
        pk, fks = [], []
        for cname, c in sorted((obj.get("constraints") or {}).items()):
            if c["type"] == "primary_key":
                pk = list(c["columns"])
            elif c["type"] == "foreign_key":
                fks.append({"name": cname, "columns": c["columns"], "references": c["references"],
                            "parent_columns": c["parent_columns"]})
        comment = obj.get("comment") or ""
        meta = obj.get("meta") or {}
        nodes[name] = {
            "id": name, "kind": obj["kind"], "schema": schema, "name": short,
            "comment": comment, "grain": _grain(comment), "tags": obj.get("tags") or {},
            "owner": meta.get("owner"),
            "columns": [{"name": c["name"], "type": c["type"], "comment": c.get("comment"),
                         "nullable": c.get("nullable", True), "tags": c.get("tags") or {}}
                        for c in obj["columns"]],
            "pk": pk, "fks": fks, "definition": obj.get("definition"), "writers": [],
            "changed": _when(meta.get("updated_at")), "created": _when(meta.get("created_at")),
            "written": None,
        }

    def node(nid: str) -> dict:
        if nid not in nodes:
            nodes[nid] = _stub(nid, set(schemas_in))
        return nodes[nid]

    def column(cid: str) -> None:  # an outside object's columns, as its readers name them
        tid, col = cid.rsplit(".", 1)
        n = node(tid)
        if n["kind"] in ("files", "external") and col not in [c["name"] for c in n["columns"]]:
            n["columns"].append({"name": col, "type": "?", "comment": None, "nullable": True,
                                 "tags": {}})

    sts = sqllineage.statements(model, catalog, sql_files or [], progress)
    column_edges: dict[tuple[str, str, str], dict] = {}
    table_edges: dict[tuple[str, str], dict] = {}
    gaps = []
    for st in sts:
        if st.errors:
            gaps.append({"via": st.via, "target": st.target, "kind": st.kind,
                         "errors": st.errors, "sql": st.sql[:4000]})
        if st.target is None:
            continue
        node(st.target)["writers"].append({"via": st.via, "kind": st.kind, "sql": st.sql,
                                           "filters": st.filters, "errors": st.errors})
        for e in st.columns:
            column(e.source)
            key = (e.source, e.target, st.via)
            column_edges.setdefault(key, {"s": e.source, "t": e.target, "via": st.via,
                                          "steps": e.steps, "agg": e.aggregate,
                                          "static": True, "observed": None})
        fed = {e.source.rsplit(".", 1)[0] for e in st.columns}
        for src in sorted(st.reads):
            node(src)
            table_edges.setdefault((src, st.target), {
                "s": src, "t": st.target, "via": st.via,
                "role": "data" if src in fed else "join", "static": True, "observed": None})
    if found:
        by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for ce in column_edges.values():
            by_pair[ce["s"], ce["t"]].append(ce)
        for seen in found.columns:
            column(seen.source)
            column(seen.target)
            when = _seen(seen.seen)
            if (seen.source, seen.target) in by_pair:
                for ce in by_pair[seen.source, seen.target]:
                    ce["observed"] = when
            else:
                column_edges[(seen.source, seen.target, "lineage")] = {
                    "s": seen.source, "t": seen.target, "via": "lineage", "steps": [],
                    "agg": False, "static": False, "observed": when}
        for seen in found.tables:
            node(seen.source)
            node(seen.target)
            when = _seen(seen.seen)
            if (te := table_edges.get((seen.source, seen.target))) is not None:
                te["observed"] = max(te["observed"] or "", when or "") or None
            else:
                table_edges[(seen.source, seen.target)] = {
                    "s": seen.source, "t": seen.target, "via": "lineage", "role": "data",
                    "static": False, "observed": when}
            target = nodes[seen.target]
            if target["kind"] == "table" and when:
                target["written"] = max(target["written"] or "", when)

    joins: dict[tuple[str, str], dict] = {}
    for st in sts:
        for a, b in st.joins:
            j = joins.setdefault((a, b), {"a": a, "b": b, "code": [], "declared": None})
            if st.via not in j["code"]:
                j["code"].append(st.via)
    fk_edges = []
    for n in list(nodes.values()):
        for fk in n["fks"]:
            node(fk["references"])
            for c, p in zip(fk["columns"], fk["parent_columns"], strict=False):
                a, b = f"{n['id']}.{c}", f"{fk['references']}.{p}"
                fk_edges.append({"s": a, "t": b, "name": fk["name"]})
                pair = (min(a, b), max(a, b))
                j = joins.setdefault(pair, {"a": pair[0], "b": pair[1], "code": [],
                                            "declared": None})
                j["declared"] = fk["name"]

    schema_names = sorted({n["schema"] for n in nodes.values()},
                          key=lambda s: (s.startswith("("), "." in s, s))
    schemas = []
    for s in schema_names:
        info = schemas_in.get(s) or {}
        tags = info.get("tags") or {}
        schemas.append({"name": s, "comment": info.get("comment") or "",
                        "domain": tags.get("domain") or s,
                        "owner": (info.get("properties") or {}).get("owner"),
                        "outside": s not in schemas_in})
    return {
        "meta": {"catalog": catalog, "taken_at": taken_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "snapshot": snapshot, "sqlglot": version("sqlglot"),
                 "lineage": found is not None, "sql_files": len(sql_files or []),
                 "statements": len(sts)},
        "schemas": schemas,
        "nodes": list(nodes.values()),
        "column_edges": list(column_edges.values()),
        "table_edges": list(table_edges.values()),
        "fk_edges": fk_edges,
        "joins": sorted(joins.values(), key=lambda j: (j["a"], j["b"])),
        "gaps": gaps,
    }


def layout_edges(g: dict) -> list[dict]:
    """The edges the layouts are made from: every kind the page can show."""
    out = []
    for e in g["table_edges"]:
        kind = "runtime" if not e["static"] else "join" if e["role"] == "join" else "flow"
        out.append({"s": e["s"], "t": e["t"], "kind": kind})
    for n in g["nodes"]:
        for f in n["fks"]:
            out.append({"s": f["references"], "t": n["id"], "kind": "fk"})
    return out


def write(g: dict, out: Path, *, tries: int | None = None,
          progress: Progress | None = None) -> Path:
    """Lay `g` out and write the page into `out`. Returns the page's path."""
    g = {**g, "layouts": layout.build(g["nodes"], layout_edges(g), tries, progress)}
    out.mkdir(parents=True, exist_ok=True)
    page = (ASSETS / PAGE).read_text(encoding="utf-8")
    for name in VENDOR:
        (out / name).write_bytes((ASSETS / name).read_bytes())
    page = page.replace("/*GRAPH*/null", json.dumps(g, separators=(",", ":")).replace("</", "<\\/"))
    path = out / PAGE
    path.write_text(page, encoding="utf-8")
    return path


def sql_files(paths: list[Path]) -> list[tuple[str, str]]:
    """(name, text) for each `.sql` file given, or under each folder given, in order."""
    out = []
    for p in paths:
        found = sorted(p.rglob("*.sql")) if p.is_dir() else [p]
        for f in found:
            name = str(f.relative_to(p)) if p.is_dir() else f.name
            out.append((name, f.read_text(encoding="utf-8")))
    return out


def counts(g: dict) -> dict[str, int]:
    kinds: dict[str, int] = defaultdict(int)
    for n in g["nodes"]:
        kinds[n["kind"]] += 1
    return {**kinds, "column edges": len(g["column_edges"]), "table edges": len(g["table_edges"]),
            "gaps": len(g["gaps"])}
