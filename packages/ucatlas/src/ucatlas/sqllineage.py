"""Column lineage from SQL text: what the SQL says, before anything runs.

The lineage API (`ucmodel.lineage`) records what ran: which columns fed
which, and when it last saw it. It doesn't record how. This module reads the
SQL that writes each object, a view's definition or a statement in a SQL
file, and works out for each column it writes:

- which source columns feed it, and the expressions in between, from the
  written column back towards its source (`[]` when it is copied as is);
- whether an aggregate is among them.

And for each statement, the objects it reads, which of them it reads only to
join or filter, its WHERE conditions, and each `a.x = b.y` it joins on.

sqlglot does the parsing and the lineage. What this module adds:

- names: a three-part name in the snapshot's catalog becomes the model's
  two-part name, a one-part name takes the schema of the file's last `USE`
  (or the only object of that name in the model), and `read_files('<path>')`
  becomes `files:<path>`;
- `INSERT` maps the query's columns to the target's by position, and
  `MERGE` reads each assignment as `SELECT <value> FROM <using> JOIN <target>`;
- a statement sqlglot can't parse, or a column whose lineage it can't follow,
  is kept as an error on the statement. The explorer shows those, so missing
  lineage reads as missing rather than as nothing feeding the object.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import Node, lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope

from ucmodel.progress import Progress

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
D = "databricks"
AGGREGATE = re.compile(r"(SUM|COUNT|AVG|MIN|MAX|COUNT_IF|ANY_VALUE|FIRST|LAST)\(", re.I)
# A statement that writes an object: when sqlglot can't read one, that's a gap in the lineage.
# Other statements it can't read (a GRANT, an OPTIMIZE) write nothing, and aren't gaps.
_LEAD = re.compile(r"^(?:\s+|--[^\n]*\n?|/\*.*?\*/)*", re.S)
WRITES = re.compile(r"(?:CREATE\b(?!(?:\s+OR\s+REPLACE)?(?:\s+TEMPORARY)?\s+"
                    r"(?:SCHEMA|DATABASE|CATALOG|FUNCTION|VOLUME|CONNECTION|SHARE|RECIPIENT|"
                    r"PROCEDURE|WIDGET|EXTERNAL\s+LOCATION|STORAGE\s+CREDENTIAL)\b)"
                    r"|INSERT\b|MERGE\b|REPLACE\b|COPY\s+INTO\b|APPLY\s+CHANGES\b"
                    r"|AUTO\s+CDC\b)", re.I)
TARGET = re.compile(r"\b(?:INTO|TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                    r"((?:`[^`]+`|[\w$]+)(?:\.(?:`[^`]+`|[\w$]+)){0,2})", re.I)


@dataclass
class ColumnEdge:
    source: str  # schema.table.column, or files:<path>.column, or catalog.schema.table.column
    target: str
    steps: list[str]  # expressions from the target back towards the source; [] = copied as is
    aggregate: bool


@dataclass
class Statement:
    via: str  # the view, or `<file>:<n>`, the file and the statement's number in it
    kind: str  # view, create, insert, merge; or unread, when it couldn't be parsed
    target: str | None  # the object written, None when the statement couldn't be read
    sql: str
    columns: list[ColumnEdge] = field(default_factory=list)
    reads: set[str] = field(default_factory=set)  # every object or external it reads
    filters: list[str] = field(default_factory=list)
    joins: list[tuple[str, str]] = field(default_factory=list)  # column pairs
    errors: list[str] = field(default_factory=list)


def objects(model: dict[str, dict]) -> dict[str, dict]:
    """The model's tables and views, by two-part name."""
    return {o["name"]: o for o in model.values() if o["kind"] in ("table", "view")}


def glot_schema(model: dict[str, dict]) -> dict:
    out: dict = {}
    for name, obj in objects(model).items():
        schema, table = name.split(".", 1)
        out.setdefault(schema, {})[table] = {c["name"]: c["type"] for c in obj["columns"]}
    return out


class _Names:
    """Turns a table reference into the name the model uses."""

    def __init__(self, catalog: str, model: dict[str, dict]):
        self.catalog = catalog
        self.known = set(objects(model))
        self.by_table: dict[str, list[str]] = {}
        for name in self.known:
            self.by_table.setdefault(name.split(".", 1)[1], []).append(name)
        self.schema: str | None = None  # from the last USE SCHEMA in a file
        self.use_catalog: str | None = None

    def resolve(self, t: exp.Table) -> str | None:
        if isinstance(t.this, exp.Anonymous) and t.this.name.lower() == "read_files":
            path = next((a.name for a in t.this.expressions if isinstance(a, exp.Literal)), "?")
            return f"files:{path}"
        if not t.name:
            return None
        cat = t.catalog or (self.use_catalog if t.db else None)
        if t.db:
            if cat and cat != self.catalog:
                return f"{cat}.{t.db}.{t.name}"
            return f"{t.db}.{t.name}"
        if self.schema and (not self.use_catalog or self.use_catalog == self.catalog):
            return f"{self.schema}.{t.name}"
        if len(found := self.by_table.get(t.name, [])) == 1:
            return found[0]
        return t.name

    def rewrite(self, tree: exp.Expr, ctes: set[str]) -> None:
        """Give each table reference its model name, so qualification finds its columns."""
        for t in list(tree.find_all(exp.Table)):
            if t.name in ctes and not t.db:
                continue
            name = self.resolve(t)
            if name is None or name.startswith("files:"):
                continue
            parts = name.split(".")
            t.set("catalog", exp.to_identifier(parts[0]) if len(parts) == 3 else None)
            t.set("db", exp.to_identifier(parts[-2]) if len(parts) >= 2 else None)


def _unread(via: str, sql: str, names: _Names, why: str, view: str | None) -> Statement | None:
    """A statement sqlglot can't read: a gap when it writes an object, else nothing."""
    text = _LEAD.sub("", sql, count=1)
    if not view and not WRITES.match(text):
        return None
    target = view
    if target is None and (m := TARGET.search(text)):
        try:
            target = names.resolve(exp.to_table(m[1].replace("`", ""), dialect=D))
        except SqlglotError:
            target = None
    return Statement(via, "unread", target, sql, errors=[why])


def _table_id(t: exp.Table) -> str | None:
    """The name of an already rewritten table reference."""
    if isinstance(t.this, exp.Anonymous) and t.this.name.lower() == "read_files":
        path = next((a.name for a in t.this.expressions if isinstance(a, exp.Literal)), "?")
        return f"files:{path}"
    if not t.name:
        return None
    return ".".join(p for p in (t.catalog, t.db, t.name) if p)


def _bare(e: exp.Expr) -> exp.Expr:
    return e.this if isinstance(e, exp.Alias) else e


def _paths(node: Node, trail: list[str]) -> Iterator[tuple[Node, list[str]]]:
    """(leaf, steps) for each source column under `node`."""
    if not node.downstream:
        yield node, trail
        return
    e = _bare(node.expression) if isinstance(node.expression, exp.Expr) else None
    # A set operation's node carries its first branch's expression, and each branch is a
    # child with its own: skip the wrapper's, or the first branch's lands on every branch.
    wrapper = any(c.expression is node.expression or c.expression == node.expression
                  for c in node.downstream)
    step = [] if e is None or wrapper or isinstance(e, (exp.Column, exp.Select)) else [e.sql(D)]
    for child in node.downstream:
        yield from _paths(child, trail + [s for s in step if s not in trail])


def _edges(root: Node, target: str) -> list[ColumnEdge]:
    out = []
    for leaf, steps in _paths(root, []):
        if not isinstance(leaf.source, exp.Table):
            continue  # a literal: nothing upstream
        tid = _table_id(leaf.source)
        if tid is None:
            continue
        col = leaf.name.split(".")[-1].strip('"`')
        out.append(ColumnEdge(f"{tid}.{col}", target, steps,
                              any(AGGREGATE.match(s) for s in steps)))
    # sqlglot's order of a node's sources varies from run to run (it comes from a set):
    # sort, so the same SQL always gives the same page
    return sorted(out, key=lambda e: (e.source, e.steps))


def _query_lineage(query: exp.Query, outputs: list[tuple[str, int]] | None, target: str,
                   schema: dict, st: Statement) -> None:
    """Column edges for `outputs`, (target column, position in the select list), all
    from one qualification of the query. With None, the target's columns aren't known:
    each output is named as the query names it, once qualifying has expanded any `*`."""
    try:
        q = qualify(query.copy(), dialect=D, schema=schema, validate_qualify_columns=False,
                    identify=False)
        scope = build_scope(q)
        names = q.named_selects
        nodes = lineage(None, q, schema, dialect=D, scope=scope, copy=False)
    except (SqlglotError, KeyError, ValueError, AttributeError, TypeError) as e:
        st.errors.append(f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
        return
    assert isinstance(nodes, dict)
    if outputs is None:
        outputs = [(n, i) for i, n in enumerate(names)]
    for col, i in outputs:
        node = nodes.get(names[i]) if i < len(names) else None
        if node is None:
            st.errors.append(f"{col}: no lineage for output {i + 1}")
            continue
        st.columns += [e for e in _edges(node, f"{target}.{col}")
                       if not e.source.startswith(f"{target}.")]


def _positions(cols: list[str] | None) -> list[tuple[str, int]] | None:
    return None if cols is None else [(c, i) for i, c in enumerate(cols)]


def _filters(query: exp.Expr) -> list[str]:
    """WHERE conditions that decide which rows arrive: not those of a scalar or
    EXISTS subquery, which only correlate."""
    out = []
    for w in query.find_all(exp.Where):
        sub = w.parent.parent if w.parent else None
        if isinstance(sub, exp.Subquery) and not isinstance(sub.parent, (exp.From, exp.Join)):
            continue
        out.append(w.this.sql(D))
    return out


def _joins(tree: exp.Expr) -> list[tuple[str, str]]:
    """Each `a.x = b.y` in a JOIN ... ON, as two column names. Aliases are resolved per
    statement, which misses one alias used twice for different tables in nested scopes."""
    aliases = {}
    for t in tree.find_all(exp.Table):
        if (tid := _table_id(t)) and "." in tid:
            aliases[t.alias_or_name] = tid
    out = []
    for j in tree.find_all(exp.Join):
        on = j.args.get("on")
        for eq in (on.find_all(exp.EQ) if on else []):
            a, b = eq.this, eq.expression
            if isinstance(a, exp.Column) and isinstance(b, exp.Column) \
                    and a.table in aliases and b.table in aliases:
                pair = sorted([f"{aliases[a.table]}.{a.name}", f"{aliases[b.table]}.{b.name}"])
                out.append((pair[0], pair[1]))
    return out


def _reads(tree: exp.Expr, target: str | None, ctes: set[str]) -> set[str]:
    out = set()
    for t in tree.find_all(exp.Table):
        if t.name in ctes and not t.db:
            continue
        tid = _table_id(t)
        if tid and tid != target:
            out.add(tid)
    return out


def analyse(via: str, sql: str, model: dict[str, dict], catalog: str, *,
            names: _Names | None = None, schema: dict | None = None,
            view: str | None = None) -> Statement | None:
    """The lineage of one statement, or None when it writes no object (DDL, a query,
    `USE`). `view` is set when `sql` is that view's definition."""
    names = names or _Names(catalog, model)
    schema = schema if schema is not None else glot_schema(model)
    columns = {n: [c["name"] for c in o["columns"]] for n, o in objects(model).items()}
    try:
        tree = sqlglot.parse_one(sql, read=D)
    except SqlglotError as e:
        return _unread(via, sql, names, f"sqlglot can't parse it: {str(e).splitlines()[0][:120]}",
                       view)
    if isinstance(tree, exp.Command):
        return _unread(via, sql, names, "sqlglot reads it only as an opaque command", view)
    if isinstance(tree, exp.Use):
        kind = (tree.args.get("kind") or exp.var("SCHEMA")).name.upper()
        t = tree.this
        if kind == "CATALOG":
            names.use_catalog, names.schema = t.name, None
        elif isinstance(t, exp.Table):
            names.use_catalog = t.db or names.use_catalog
            names.schema = t.name
        return None
    ctes = {c.alias for c in tree.find_all(exp.CTE)}
    names.rewrite(tree, ctes)

    if view or isinstance(tree, exp.Query):
        if not view or not isinstance(tree, exp.Query):
            return None  # a bare query in a file writes nothing
        st = Statement(via, "view", view, sql)
        _query_lineage(tree, _positions(columns.get(view)), view, schema, st)
        query: exp.Expr = tree
    elif isinstance(tree, exp.Create) and isinstance(tree.expression, exp.Query):
        dest = tree.this
        table = dest.this if isinstance(dest, exp.Schema) else dest
        target = _table_id(table) if isinstance(table, exp.Table) else None
        if target is None:
            return None
        kind = (tree.args.get("kind") or "").upper()
        st = Statement(via, "view" if kind == "VIEW" else "create", target, sql)
        query = tree.expression
        listed = [i.name for i in dest.expressions] if isinstance(dest, exp.Schema) else []
        _query_lineage(query, _positions(listed or columns.get(target)), target, schema, st)
    elif isinstance(tree, exp.Insert) and isinstance(tree.expression, exp.Query):
        if (with_ := tree.args.get("with_")) and isinstance(tree.expression, exp.Query):
            # `WITH ... INSERT ... SELECT`: the CTEs belong to the query
            tree.expression.set("with_", with_)
            tree.set("with_", None)
        dest = tree.this
        table = dest.this if isinstance(dest, exp.Schema) else dest
        target = _table_id(table) if isinstance(table, exp.Table) else None
        if target is None:
            return None
        st = Statement(via, "insert", target, sql)
        query = tree.expression
        cols = ([i.name for i in dest.expressions] if isinstance(dest, exp.Schema)
                else columns.get(target))
        _query_lineage(query, _positions(cols), target, schema, st)
    elif isinstance(tree, exp.Copy) and isinstance(tree.this, exp.Table):
        target = _table_id(tree.this)
        if target is None:
            return None
        st = Statement(via, "copy", target, sql)  # columns match by name: no expressions to follow
        st.reads = {f"files:{f.name}" for f in tree.args.get("files") or []
                    if isinstance(f, exp.Literal)} or {f"files:{f.sql(D)}" for f in
                                                       tree.args.get("files") or []}
        return st
    elif isinstance(tree, exp.Merge):
        target = _table_id(tree.this) if isinstance(tree.this, exp.Table) else None
        if target is None:
            return None
        st = Statement(via, "merge", target, sql)
        using, on = tree.args["using"], tree.args["on"]
        done: set[str] = set()
        whens = tree.args.get("whens")
        for when in (whens.expressions if whens else []):
            then = when.args.get("then")
            if isinstance(then, exp.Update):
                assigns = [(eq.this.name, eq.expression) for eq in then.expressions
                           if isinstance(eq, exp.EQ)]
            elif isinstance(then, exp.Insert) and isinstance(then.this, exp.Tuple) \
                    and isinstance(then.expression, exp.Tuple):
                assigns = [(c.name, v) for c, v in zip(then.this.expressions,
                                                       then.expression.expressions, strict=False)]
            else:
                continue
            assigns = [(c, v) for c, v in assigns if c not in done]
            if not assigns:
                continue
            done.update(c for c, _ in assigns)
            q = exp.select(*(exp.alias_(v.copy(), f"__c{i}") for i, (_, v) in enumerate(assigns))
                           ).from_(using.copy()).join(tree.this.copy(), on=on.copy())
            _query_lineage(q, [(c, i) for i, (c, _) in enumerate(assigns)], target, schema, st)
        query = tree
    else:
        return None
    st.reads = _reads(tree, st.target, ctes)
    st.filters = _filters(query)
    st.joins = _joins(tree)
    return st


def statements(model: dict[str, dict], catalog: str,
               files: Iterable[tuple[str, str]] = (),
               progress: Progress | None = None) -> list[Statement]:
    """The lineage of every view's definition, and of each statement in `files`,
    (name, text) pairs, that writes an object. A file's `CREATE VIEW` for a view the
    model has is skipped: the definition the catalog holds wins."""
    schema = glot_schema(model)
    files = list(files)
    defined = [(name, obj) for name, obj in sorted(objects(model).items())
               if obj["kind"] == "view" and obj.get("definition")]
    out = []
    with (progress or Progress.off()).step("reading the SQL", len(defined) + len(files),
                                           ("view or file", "views and files")) as step:
        for name, obj in defined:
            st = analyse(name, obj["definition"], model, catalog, schema=schema, view=name)
            if st:
                out.append(st)
            step.tick(detail=name)
        views = {n for n, o in objects(model).items() if o["kind"] == "view"}
        for fname, text in files:
            out += _file_statements(fname, text, model, catalog, schema, views)
            step.tick(detail=fname)
    return out


def _file_statements(fname: str, text: str, model: dict[str, dict], catalog: str,
                     schema: dict, views: set[str]) -> list[Statement]:
    from ucmodel.sqltext import split_statements

    names = _Names(catalog, model)  # a USE lasts to the end of its file
    try:
        parts = split_statements(text)
    except Exception as e:  # noqa: BLE001 - one unreadable file shouldn't stop the rest
        return [Statement(f"{fname}", "unread", None, text[:2000],
                          errors=[f"couldn't split it into statements: {e}"])]
    out = []
    for i, sql in enumerate(parts, 1):
        st = analyse(f"{fname}:{i}", sql, model, catalog, names=names, schema=schema)
        if st and not (st.kind == "view" and st.target in views):
            out.append(st)
    return out
