"""ER diagrams and lineage of a model, as a static site (docs/diagrams.md,
phases D1 and D2).

Each schema gets a page: a Graphviz drawing of its tables and views, its
lineage graph, and under them every object with its columns, keys and
comments, and where each column's values come from. In the ER drawing:

- a table or view is a box with a row per column, marked PK, FK or mask;
- a foreign key is an edge from the right of the parent's column to the
  left of the child's, with a crow's foot at the child;
- a view has a dashed edge from each table or view its definition reads;
- an object in another schema that one of these edges reaches is a small
  box, which links to that schema's page when the model holds it.

The lineage graph has an edge for each pair of objects the lineage API
links, when one of them is in the schema (`ucmodel.lineage`).

`dot_source` and `lineage_source` are pure, so the tests check the drawings
offline. `write_site` runs Graphviz's `dot` to make the SVGs.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime
from html import escape
from pathlib import Path

from ucmodel.lineage import Lineage
from ucmodel.model import depends_on
from ucmodel.progress import Progress

TABLE, VIEW, STUB = "#dbe8f7", "#e3f1dc", "#f2f2f2"
WIDEST_TYPE = 40  # the characters of a type the drawing shows; the page has it all
DRAWN = ("table", "view")


class DiagramError(Exception):
    pass


def schemas(model: dict[str, dict]) -> list[str]:
    return sorted(o["name"] for k, o in model.items() if k.startswith("schema:"))


def _schema_of(name: str) -> str:
    return name.split(".")[-2]


def _in(model: dict[str, dict], schema: str, kinds: tuple[str, ...]) -> dict[str, dict]:
    return {o["name"]: o for _, o in sorted(model.items())
            if o["kind"] in kinds and o["name"].count(".") == 1
            and _schema_of(o["name"]) == schema}


def _drawn(model: dict[str, dict], name: str) -> dict | None:
    return next((model[k] for k in (f"table:{name}", f"view:{name}") if k in model), None)


def foreign_keys(obj: dict) -> list[tuple[str, dict]]:
    return [(n, c) for n, c in obj.get("constraints", {}).items() if c["type"] == "foreign_key"]


def reads(model: dict[str, dict], key: str, obj: dict) -> list[str]:
    """The tables and views a view's definition reads, as far as the model knows them."""
    if obj["kind"] != "view":
        return []
    return sorted(k.split(":", 1)[1] for k in depends_on(key, obj)
                  if k in model and k.split(":")[0] in DRAWN)


def edges(model: dict[str, dict], schema: str) -> list[tuple[str, str, dict | None]]:
    """Every edge the schema's drawing has: (from, to, foreign key), with no
    foreign key for a view's read. It includes edges to and from other schemas.
    """
    out: list[tuple[str, str, dict | None]] = []
    for key, obj in sorted(model.items()):
        if obj["kind"] not in DRAWN:
            continue
        for _, fk in foreign_keys(obj):
            out.append((fk["references"], obj["name"], fk))
        out += [(name, obj["name"], None) for name in reads(model, key, obj)]
    here = set(_in(model, schema, DRAWN))
    return [e for e in out if e[0] in here or e[1] in here]


def dot_source(model: dict[str, dict], schema: str) -> str:
    here = _in(model, schema, DRAWN)
    lines = [f"digraph {_quote(schema)} {{",
             '  graph [rankdir=LR fontname="Helvetica" nodesep=0.4 ranksep=1.1];',
             '  node [shape=plain fontname="Helvetica" fontsize=10];',
             '  edge [fontname="Helvetica" fontsize=9 color="#555555"];']
    lines += [f"  {_quote(name)} [label=<{_box(obj)}>];" for name, obj in here.items()]
    found = edges(model, schema)
    others = sorted({n for e in found for n in e[:2] if n not in here})
    lines += [f"  {_quote(name)} [{_stub(model, name)}];" for name in others]
    for parent, child, fk in found:
        if fk:
            tail = _quote(parent) + _port(here.get(parent), fk["parent_columns"][0], "e")
            head = _quote(child) + _port(here.get(child), fk["columns"][0], "w")
            tip = (f"{', '.join(fk['columns'])} references {parent} "
                   f"({', '.join(fk['parent_columns'])})")
            lines.append(f"  {tail} -> {head} [dir=both arrowtail=tee arrowhead=crow "
                         f"tooltip={_quote(tip)}];")
        else:
            lines.append(f"  {_quote(parent)} -> {_quote(child)} [style=dashed arrowhead=vee "
                         f"tooltip={_quote(f'{child} reads {parent}')}];")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def lineage_source(model: dict[str, dict], lineage: Lineage, schema: str) -> str | None:
    """The schema's lineage graph, or None when no edge reaches it."""
    here = _in(model, schema, DRAWN)
    found = [e for e in lineage.tables if e.source in here or e.target in here]
    if not found:
        return None
    lines = [f"digraph {_quote(schema)} {{",
             '  graph [rankdir=LR fontname="Helvetica" nodesep=0.3 ranksep=0.9];',
             '  node [shape=box style="rounded,filled" fontname="Helvetica" fontsize=10];',
             '  edge [fontname="Helvetica" fontsize=9 color="#555555"];']
    for name in sorted({n for e in found for n in (e.source, e.target)}):
        obj = here.get(name)
        if obj is None:
            lines.append(f"  {_quote(name)} [{_stub(model, name)}];")
        else:
            fill = VIEW if obj["kind"] == "view" else TABLE
            lines.append(f'  {_quote(name)} [fillcolor="{fill}" URL={_quote("#" + name)} '
                         f"tooltip={_quote(obj['comment'] or name)}];")
    lines += [f"  {_quote(e.source)} -> {_quote(e.target)} "
              f"[tooltip={_quote(f'{e.target} from {e.source}, last {e.seen[:16]}')}];"
              for e in found]
    lines.append("}")
    return "\n".join(lines) + "\n"


def _port(obj: dict | None, column: str, side: str) -> str:
    """A column's port: `c` is its row's left cell and `r` a table row's right one."""
    names = [c["name"] for c in obj["columns"]] if obj else []
    if column not in names:
        return ""
    cell = "r" if side == "e" else "c"
    return f':"{cell}{names.index(column)}":{side}'


def _marks(obj: dict) -> dict[str, list[str]]:
    marks: dict[str, list[str]] = {c["name"]: [] for c in obj["columns"]}
    for c in obj.get("constraints", {}).values():
        mark = {"primary_key": "PK", "foreign_key": "FK"}.get(c["type"])
        if mark is None:
            continue
        for column in c["columns"]:
            if column in marks and mark not in marks[column]:
                marks[column].append(mark)
    for c in obj["columns"]:
        if c.get("mask"):
            marks[c["name"]].append("mask")
    return marks


def _box(obj: dict) -> str:
    """A table has a name, a type and a marks cell per column. A view has no marks."""
    name, view = obj["name"], obj["kind"] == "view"
    link = f'href="#{escape(name)}"'
    rows = [f'<tr><td colspan="{2 if view else 3}" bgcolor="{VIEW if view else TABLE}" {link} '
            f'tooltip="{escape(obj["comment"] or name)}">'
            f'<b>{escape(name)}{" (view)" if view else ""}</b></td></tr>']
    marks = _marks(obj)
    for i, c in enumerate(obj["columns"]):
        kind = c["type"] if len(c["type"]) <= WIDEST_TYPE else c["type"][:WIDEST_TYPE - 1] + "…"
        null = "" if c.get("nullable", True) else ' <font color="#777777">not null</font>'
        row = (f'<tr><td align="left" port="c{i}" {link} '
               f'tooltip="{escape(c["comment"] or c["name"])}">{escape(c["name"])}</td>'
               f'<td align="left">{escape(kind)}{null}</td>')
        if not view:
            row += f'<td align="left" port="r{i}">{" ".join(marks[c["name"]])}</td>'
        rows.append(row + "</tr>")
    return ('<table border="0" cellborder="1" cellspacing="0" cellpadding="3">'
            + "".join(rows) + "</table>")


def _stub(model: dict[str, dict], name: str) -> str:
    style = f'shape=box style="rounded,filled" fillcolor="{STUB}" color="#999999"'
    if _drawn(model, name) is None:
        return f'{style} label={_quote(name)} tooltip="not in the model"'
    return (f"{style} label={_quote(name)} URL={_quote(_href(name))} "
            f"tooltip={_quote(f'on the {_schema_of(name)} page')}")


def _file(schema: str, suffix: str = ".html") -> str:
    return re.sub(r"[^\w.-]", "_", schema) + suffix


def _href(name: str, here: str | None = None) -> str:
    schema = _schema_of(name)
    return f"#{name}" if schema == here else f"{_file(schema)}#{name}"


# The site --------------------------------------------------------------------------------


def svg(source: str) -> str:
    """Graphviz's SVG for a drawing. A warning from `dot` is an error: it means
    the drawing has something `dot` didn't understand."""
    dot = shutil.which("dot")
    if dot is None:
        raise DiagramError("Graphviz isn't installed: brew install graphviz, "
                           "or apt-get install graphviz")
    done = subprocess.run([dot, "-Tsvg"], input=source, capture_output=True, text=True)
    if done.returncode or done.stderr.strip():
        raise DiagramError(f"dot: {done.stderr.strip()}")
    return done.stdout[done.stdout.index("<svg"):]


def write_site(model: dict[str, dict], catalog: str, out: Path, when: datetime,
               lineage: Lineage | None = None, progress: Progress | None = None) -> list[str]:
    """Write the site for a model read from `catalog` at `when`. Returns its schemas.

    Without `lineage`, the pages have no lineage graph and no column lineage.
    """
    names = schemas(model)
    out.mkdir(parents=True, exist_ok=True)
    with (progress or Progress.off()).step("drawing schemas", len(names), "schemas") as step:
        for schema in names:
            step.tick(0, detail=schema)
            drawings: dict[str, str] = {}
            sources = {".dot": dot_source(model, schema) if _in(model, schema, DRAWN) else None,
                       ".lineage.dot": lineage and lineage_source(model, lineage, schema)}
            for suffix, source in sources.items():
                if source:
                    (out / _file(schema, suffix)).write_text(source)
                    drawings[suffix] = svg(source)
            (out / _file(schema)).write_text(_page(model, catalog, schema, names, drawings,
                                                   lineage))
            step.tick()
    (out / "index.html").write_text(_index(model, catalog, names, when))
    return names


STYLE = """
body { font: 14px/1.45 -apple-system, Helvetica, Arial, sans-serif; margin: 2em; color: #222; }
nav a { margin-right: 1em; }
.drawing { overflow: auto; border: 1px solid #ddd; padding: 1em; margin: 1em 0 2em; }
table.list { border-collapse: collapse; margin: 0.5em 0 1em; }
table.list th, table.list td { border: 1px solid #ddd; padding: 0.25em 0.6em; text-align: left;
  vertical-align: top; }
table.list th { background: #f6f6f6; }
h3 { margin-top: 2em; } code, pre { font: 12px Menlo, monospace; }
.dim { color: #777; }
"""


def _html(title: str, nav: str, body: str) -> str:
    return (f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
            f"<title>{escape(title)}</title><style>{STYLE}</style></head>\n"
            f"<body><nav>{nav}</nav>\n{body}\n</body></html>\n")


def _counts(model: dict[str, dict], schema: str) -> str:
    kinds = ("table", "view", "function", "volume")
    found = [(len(_in(model, schema, (k,))), k) for k in kinds]
    return ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for n, k in found if n)


def _index(model: dict[str, dict], catalog: str, names: list[str], when: datetime) -> str:
    rows = "".join(
        f'<tr><td><a href="{_file(s)}">{escape(s)}</a></td>'
        f'<td>{escape(model[f"schema:{s}"]["comment"] or "")}</td>'
        f"<td>{_counts(model, s)}</td></tr>" for s in names)
    body = (f"<h1>{escape(catalog)}</h1>"
            f'<p class="dim">Read from {escape(catalog)} at {when:%Y-%m-%d %H:%M} UTC.</p>'
            f'<table class="list"><tr><th>Schema</th><th>Comment</th><th>Objects</th></tr>'
            f"{rows}</table>")
    return _html(catalog, "", body)


def _page(model: dict[str, dict], catalog: str, schema: str, names: list[str],
          drawings: dict[str, str], lineage: Lineage | None) -> str:
    nav = '<a href="index.html">All schemas</a>' + "".join(
        f'<a href="{_file(s)}">{escape(s)}</a>' for s in names if s != schema)
    comment = model[f"schema:{schema}"]["comment"]
    er, graph = drawings.get(".dot"), drawings.get(".lineage.dot")
    parts = [f"<h1>{escape(catalog)}.{escape(schema)}</h1>",
             f"<p>{escape(comment)}</p>" if comment else "",
             f'<div class="drawing">{er}</div>' if er else
             '<p class="dim">No tables or views.</p>']
    if lineage is not None:
        parts += ["<h2>Lineage</h2>",
                  f'<div class="drawing">{graph}</div>' if graph else
                  '<p class="dim">No lineage recorded.</p>']
    for kind, title in (("table", "Tables"), ("view", "Views"),
                        ("function", "Functions"), ("volume", "Volumes"),
                        ("unsupported", "Outside the model")):
        objects = _in(model, schema, (kind,))
        if objects:
            parts.append(f"<h2>{title}</h2>")
            parts += [_entry(model, schema, obj, lineage) for obj in objects.values()]
    return _html(f"{catalog}.{schema}", nav, "\n".join(parts))


def _entry(model: dict[str, dict], schema: str, obj: dict, lineage: Lineage | None) -> str:
    name, kind = obj["name"], obj["kind"]
    out = [f'<h3 id="{escape(name)}">{escape(name)}</h3>']
    if obj.get("comment"):
        out.append(f"<p>{escape(obj['comment'])}</p>")
    if kind == "unsupported":
        out.append(f'<p class="dim">{escape(obj["type"])}</p>')
    if kind in DRAWN:
        out.append(_columns(model, schema, obj, lineage))
        out += _keys(model, schema, obj)
    if kind == "view":
        names = reads(model, f"view:{name}", obj)
        if names:
            out.append("<p>Reads " + ", ".join(_link(model, n, schema) for n in names) + ".</p>")
        out.append(_details("Definition", obj["definition"]))
    if kind == "function":
        params = ", ".join(f"{p['name']} {p['type']}" for p in obj["parameters"])
        out.append(f"<p><code>{escape(name)}({escape(params)}) returns "
                   f"{escape(obj['returns'])}</code></p>")
        out.append(_details(f"Body ({obj['language']})", obj["body"]))
    return "\n".join(out)


def _columns(model: dict[str, dict], schema: str, obj: dict, lineage: Lineage | None) -> str:
    table = obj["kind"] == "table"
    head = ["Column", "Type", "Comment", "Tags"] + (["Null", "Default", "Mask"] if table else [])
    head += ["From"] if lineage else []
    rows = []
    for c in obj["columns"]:
        cells = [f"<code>{escape(c['name'])}</code>", f"<code>{escape(c['type'])}</code>",
                 escape(c["comment"] or ""),
                 escape(", ".join(f"{k}={v}" if v else k for k, v in c["tags"].items()))]
        if table:
            default = (c["default"] or (c["generated"] and f"generated as {c['generated']}")
                       or ("identity" if c["identity"] else ""))
            mask = c["mask"]["function"] if c["mask"] else ""
            cells += ["" if c["nullable"] else "not null", f"<code>{escape(default)}</code>",
                      escape(mask)]
        if lineage:
            sources = lineage.upstream_columns(f"{obj['name']}.{c['name']}")
            cells.append(", ".join(_column_link(model, e.source, schema) for e in sources))
        rows.append("<tr>" + "".join(f"<td>{x}</td>" for x in cells) + "</tr>")
    head_row = "<tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr>"
    return f'<table class="list">{head_row}{"".join(rows)}</table>'


def _keys(model: dict[str, dict], schema: str, obj: dict) -> list[str]:
    out = []
    for n, c in obj.get("constraints", {}).items():
        if c["type"] == "primary_key":
            out.append(f"<li>Primary key <code>{escape(n)}</code>: "
                       f"{escape(', '.join(c['columns']))}</li>")
        elif c["type"] == "foreign_key":
            out.append(f"<li>Foreign key <code>{escape(n)}</code>: "
                       f"{escape(', '.join(c['columns']))} references "
                       f"{_link(model, c['references'], schema)} "
                       f"({escape(', '.join(c['parent_columns']))})</li>")
        else:
            out.append(f"<li>Check <code>{escape(n)}</code>: "
                       f"<code>{escape(c['expression'])}</code></li>")
    if obj.get("row_filter"):
        out.append(f"<li>Row filter: {escape(obj['row_filter']['function'])}</li>")
    return [f"<ul>{''.join(out)}</ul>"] if out else []


def _link(model: dict[str, dict], name: str, schema: str) -> str:
    """A link to an object's entry, or just its name when the model doesn't hold it."""
    if _drawn(model, name) is None:
        return escape(name)
    return f'<a href="{escape(_href(name, schema))}">{escape(name)}</a>'


def _column_link(model: dict[str, dict], column: str, schema: str) -> str:
    """`schema.table.column`, linked to the table's entry when the model holds it."""
    table = column.rsplit(".", 1)[0]
    if _drawn(model, table) is None:
        return f"<code>{escape(column)}</code>"
    return f'<a href="{escape(_href(table, schema))}"><code>{escape(column)}</code></a>'


def _details(summary: str, text: str) -> str:
    return f"<details><summary>{escape(summary)}</summary><pre>{escape(text)}</pre></details>"
