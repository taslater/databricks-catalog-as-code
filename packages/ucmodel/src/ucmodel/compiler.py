"""Source DDL to the model, offline: what the shadow catalog would build.

The shadow (`ucstate`'s `shadow.build`) runs the source in an empty catalog
and reads it back, so Databricks decides what each statement means. This
module decides the same thing locally, for the statements it knows, and
raises `Unsupported` for the rest, so that the caller can build a shadow
instead. It doesn't guess: each rule that goes beyond the text written is
pinned by a probe or a saved sample, and `docs/offline.md`, section 3,
lists them.

It reads the source format of `docs/design.md`, section 1: plain `CREATE`s
of schemas, tables, views, functions and volumes, and then tags,
constraints and grants on what was created. Defaults, generated columns,
CHECKs, view queries and function bodies are kept as written, as the
platform keeps them. A view's column types are the one thing derived
rather than read, from its query, by Spark's rules (`_Types`).

The result has the shape of the shadow's model after `declared`: the
properties are only those declared, clustering is None unless declared, and
`meta` is empty.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import TokenType, exp
from sqlglot.errors import ParseError

from .sqltext import _skip, tokens


class Unsupported(Exception):
    """Something the compiler doesn't model. Build a shadow instead."""


def compile_statements(statements: Iterable[tuple[str, str]]) -> dict[str, dict]:
    """The model of `(schema, statement)` pairs, in file order.

    `schema` is the statement's file's schema, which a one-part name
    belongs to.
    """
    build = _Build()
    for schema, statement in statements:
        try:
            build.statement(schema, statement)
        except Unsupported as e:
            raise Unsupported(f"{e}\n    in: {statement.splitlines()[0][:100]}") from None
    return build.finish()


# Words ---------------------------------------------------------------------------------


@dataclass
class _Word:
    text: str  # as written, unquoted: a string's value, a quoted name's name
    kind: str  # "string", "ident", "number", "dollar" (a $$ body) or "word"
    start: int
    end: int  # inclusive

    @property
    def up(self) -> str:
        """The word in upper case, to match keywords. Quoted text never matches."""
        return self.text.upper() if self.kind in ("word", "number") else ""


def _words(sql: str) -> list[_Word]:
    """sqlglot's tokens as words, keeping their offsets.

    A multi-word token such as `PRIMARY KEY` becomes one word per keyword.
    A `$$` body isn't tokenized, since it holds another language.
    """
    out: list[_Word] = []
    i, start = 0, 0
    while i < len(sql):
        after = _skip(sql, i)
        if after is not None and sql.startswith("$$", i):
            out += _tokenize(sql[start:i], start)
            out.append(_Word(sql[i + 2:after - 2], "dollar", i, after - 1))
            start = after
        i = after if after is not None else i + 1
    return out + _tokenize(sql[start:], start)


def _tokenize(sql: str, offset: int) -> list[_Word]:
    out = []
    for t in tokens(sql):
        a, b = t.start + offset, t.end + offset
        if t.token_type == TokenType.STRING:
            out.append(_Word(t.text, "string", a, b))
        elif t.token_type.name.endswith("STRING"):
            raise Unsupported(f"the literal {sql[t.start:t.end + 1][:30]!r}")
        elif t.token_type == TokenType.IDENTIFIER:
            out.append(_Word(t.text, "ident", a, b))
        elif t.token_type == TokenType.NUMBER:
            out.append(_Word(t.text, "number", a, b))
        elif t.text == ">>":  # two nested types closing
            out += [_Word(">", "word", a, a), _Word(">", "word", b, b)]
        else:
            out += [_Word(w, "word", a, b) for w in t.text.split()]
    return out


NAME = re.compile(r"[A-Za-z_]\w*")


class _Cursor:
    def __init__(self, sql: str):
        self.sql = sql
        self.words = _words(sql)
        self.i = 0

    def done(self) -> bool:
        return self.i >= len(self.words)

    def where(self) -> str:
        if self.done():
            return "at the end"
        return f"at {self.sql[self.words[self.i].start:][:40]!r}"

    def peek(self, *seq: str) -> bool:
        ws = self.words[self.i:self.i + len(seq)]
        return len(ws) == len(seq) and all(w.up == s for w, s in zip(ws, seq, strict=True))

    def accept(self, *seq: str) -> bool:
        if self.peek(*seq):
            self.i += len(seq)
            return True
        return False

    def expect(self, *seq: str) -> None:
        if not self.accept(*seq):
            raise Unsupported(f"expected {' '.join(seq)} {self.where()}")

    def next(self) -> _Word:
        if self.done():
            raise Unsupported("the statement ends too soon")
        self.i += 1
        return self.words[self.i - 1]

    def kind(self) -> str | None:
        return None if self.done() else self.words[self.i].kind

    def name(self) -> str:
        w = self.next()
        if w.kind == "ident" or (w.kind == "word" and NAME.fullmatch(w.text)):
            return w.text
        raise Unsupported(f"expected a name at {w.text!r}")

    def dotted(self) -> list[str]:
        parts = [self.name()]
        while self.accept("."):
            parts.append(self.name())
        return parts

    def string(self) -> str:
        w = self.next()
        if w.kind != "string":
            raise Unsupported(f"expected a string at {w.text!r}")
        return w.text

    def integer(self) -> int:
        sign = -1 if self.accept("-") else 1
        w = self.next()
        if w.kind != "number" or not w.text.isdigit():
            raise Unsupported(f"expected a whole number at {w.text!r}")
        return sign * int(w.text)

    def names(self) -> list[str]:
        """`(a, b, ...)`."""
        self.expect("(")
        out = [self.name()]
        while self.accept(","):
            out.append(self.name())
        self.expect(")")
        return out

    def group(self) -> str:
        """A bracketed group's inner text, as written."""
        self.expect("(")
        first, depth = self.i, 1
        while depth:
            w = self.next()
            depth += {"(": 1, ")": -1}.get(w.up, 0)
        return self._span(first, self.i - 2)

    def until(self, *stops: str) -> str:
        """The text up to a `,` or `)` outside brackets, or a stop word, as written."""
        first, depth = self.i, 0
        while not self.done():
            up = self.words[self.i].up
            if depth == 0 and (up in (",", ")") or up in stops):
                break
            depth += {"(": 1, ")": -1}.get(up, 0)
            self.i += 1
        if self.i == first:
            raise Unsupported(f"expected an expression {self.where()}")
        return self._span(first, self.i - 1)

    def rest(self) -> str:
        if self.done():
            raise Unsupported("the statement ends too soon")
        first = self.i
        self.i = len(self.words)
        return self._span(first, self.i - 1)

    def _span(self, first: int, last: int) -> str:
        if last < first:
            return ""
        return self.sql[self.words[first].start:self.words[last].end + 1].strip()


# Types ---------------------------------------------------------------------------------

SIMPLE = {
    "BIGINT": "bigint", "LONG": "bigint", "INT": "int", "INTEGER": "int",
    "SMALLINT": "smallint", "SHORT": "smallint", "TINYINT": "tinyint", "BYTE": "tinyint",
    "BOOLEAN": "boolean", "STRING": "string", "BINARY": "binary", "DATE": "date",
    "TIMESTAMP": "timestamp", "TIMESTAMP_NTZ": "timestamp_ntz", "DOUBLE": "double",
    "FLOAT": "float", "REAL": "float", "VARIANT": "variant",
}
INTEGRAL = ["tinyint", "smallint", "int", "bigint"]
# An integral column as a decimal, in arithmetic with one (Spark's DecimalType.forType).
AS_DECIMAL = {"tinyint": (3, 0), "smallint": (5, 0), "int": (10, 0), "bigint": (20, 0)}


def _type(c: _Cursor) -> str:
    """A type as the platform spells it: lower case, no spaces."""
    w = c.next()
    if w.up in ("DECIMAL", "DEC", "NUMERIC"):
        precision, scale = 10, 0
        if c.accept("("):
            precision = c.integer()
            if c.accept(","):
                scale = c.integer()
            c.expect(")")
        return f"decimal({precision},{scale})"
    if w.up == "ARRAY":
        c.expect("<")
        inner = _type(c)
        c.expect(">")
        return f"array<{inner}>"
    if w.up == "MAP":
        c.expect("<")
        key = _type(c)
        c.expect(",")
        value = _type(c)
        c.expect(">")
        return f"map<{key},{value}>"
    if w.up == "STRUCT":
        c.expect("<")
        fields = []
        while True:
            name = c.name()
            if not NAME.fullmatch(name):
                raise Unsupported(f"the struct field name {name!r}")
            c.accept(":")
            fields.append(f"{name}:{_type(c)}")
            if c.peek("NOT") or c.peek("COMMENT"):
                raise Unsupported("a struct field's NOT NULL or COMMENT")
            if not c.accept(","):
                break
        c.expect(">")
        return f"struct<{','.join(fields)}>"
    if w.up in SIMPLE:
        if c.accept("COLLATE") and c.name().upper() != "UTF8_BINARY":
            raise Unsupported("a collation other than UTF8_BINARY")
        return SIMPLE[w.up]
    raise Unsupported(f"the type {w.text}")


def type_of(text: str) -> str:
    """`type_text` as the platform would spell it, for a type written as `text`."""
    c = _Cursor(text)
    t = _type(c)
    if not c.done():
        raise Unsupported(f"the type {text!r}")
    return t


def _decimal(t: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"decimal\((\d+),(\d+)\)", t)
    return (int(m[1]), int(m[2])) if m else None


def _bounded(precision: int, scale: int) -> str:
    return f"decimal({min(precision, 38)},{min(scale, 38)})"


def _adjusted(precision: int, scale: int) -> str:
    """Spark's DecimalType.adjustPrecisionScale, with precision loss allowed."""
    if precision <= 38:
        return f"decimal({precision},{scale})"
    digits = precision - scale
    return f"decimal(38,{max(38 - digits, min(scale, 6))})"


class _Types:
    """The types of a view's output columns, by Spark's rules, for a few expressions."""

    def __init__(self, sources: dict[str, dict]):
        self.sources = sources  # alias or table name, lower case -> object

    def column(self, col: exp.Column) -> dict:
        if col.args.get("db") or col.args.get("catalog"):
            raise Unsupported(f"the column {col.sql()}: qualify it with its table's alias")
        if col.table:
            if col.table.lower() not in self.sources:
                raise Unsupported(f"the column {col.sql()}")
            candidates = [self.sources[col.table.lower()]]
        else:
            candidates = list(self.sources.values())
        found = [c for o in candidates for c in o["columns"]
                 if c["name"].lower() == col.name.lower()]
        if len(found) != 1:
            raise Unsupported(f"the column {col.sql()}")
        return found[0]

    def of(self, e: exp.Expr) -> str:
        if isinstance(e, exp.Paren):
            return self.of(e.this)
        if isinstance(e, exp.Column) and not isinstance(e.this, exp.Star):
            return self.column(e)["type"]
        if isinstance(e, exp.Count):
            return "bigint"
        if isinstance(e, (exp.Min, exp.Max)):
            return self.of(e.this)
        if isinstance(e, exp.Sum):
            t = self.of(e.this)
            if t in INTEGRAL:
                return "bigint"
            if d := _decimal(t):
                return _bounded(d[0] + 10, d[1])
            if t in ("float", "double"):
                return "double"
        if isinstance(e, exp.Avg):
            t = self.of(e.this)
            if d := _decimal(t):
                return _bounded(d[0] + 4, d[1] + 4)
            if t in INTEGRAL or t in ("float", "double"):
                return "double"
        if isinstance(e, exp.Cast) and not isinstance(e, exp.TryCast):
            return type_of(e.to.sql("databricks"))
        if isinstance(e, exp.Literal):
            return self.literal(e, beside_decimal=False)
        if isinstance(e, (exp.Add, exp.Sub, exp.Mul)):
            return self.arithmetic(e)
        raise Unsupported(f"the type of {e.sql('databricks')[:60]!r}")

    def literal(self, e: exp.Literal, beside_decimal: bool) -> str:
        if e.is_string:
            return "string"
        text = e.this
        if re.fullmatch(r"\d+", text):
            if beside_decimal:  # Spark's DecimalType.fromLiteral
                return f"decimal({len(text.lstrip('0')) or 1},0)"
            return "int" if int(text) < 2**31 else "bigint"
        if m := re.fullmatch(r"(\d*)\.(\d+)", text):
            scale = len(m[2])
            return f"decimal({max(len(m[1].lstrip('0')) + scale, 1)},{scale})"
        raise Unsupported(f"the literal {text}")

    def operand(self, e: exp.Expr, other: exp.Expr) -> str:
        if isinstance(e, exp.Literal) and not e.is_string:
            return self.literal(e, beside_decimal=_decimal(self.of(other)) is not None)
        return self.of(e)

    def arithmetic(self, e: exp.Binary) -> str:
        left, right = self.operand(e.left, e.right), self.operand(e.right, e.left)
        numeric = INTEGRAL + ["float", "double"]
        if not all(t in numeric or _decimal(t) for t in (left, right)):
            raise Unsupported(f"the type of {e.sql('databricks')[:60]!r}")
        if "double" in (left, right):
            return "double"
        if "float" in (left, right):
            return "double" if _decimal(left) or _decimal(right) else "float"
        if left in INTEGRAL and right in INTEGRAL:
            return max(left, right, key=INTEGRAL.index)
        (p1, s1), (p2, s2) = (_decimal(t) or AS_DECIMAL[t] for t in (left, right))
        if isinstance(e, exp.Mul):
            return _adjusted(p1 + p2 + 1, s1 + s2)
        scale = max(s1, s2)
        return _adjusted(max(p1 - s1, p2 - s2) + scale + 1, scale)


# The model ---------------------------------------------------------------------------------


@dataclass
class _View:
    schema: str
    select: exp.Select
    listed: bool  # whether the CREATE named its columns


class _Build:
    def __init__(self) -> None:
        self.model: dict[str, dict] = {}
        self.views: dict[str, _View] = {}

    def statement(self, schema: str, sql: str) -> None:
        c = _Cursor(sql)
        if c.accept("CREATE"):
            if c.accept("SCHEMA") or c.accept("DATABASE"):
                self._schema(c)
            elif c.accept("TABLE"):
                self._table(c, schema)
            elif c.accept("VIEW"):
                self._view(c, schema)
            elif c.accept("FUNCTION"):
                self._function(c, schema)
            elif c.accept("VOLUME"):
                self._volume(c, schema)
            else:
                raise Unsupported(f"CREATE {c.where()}")
        elif c.accept("ALTER"):
            self._alter(c, schema)
        elif c.accept("GRANT"):
            self._grant(c, schema)
        else:
            raise Unsupported(f"the statement {c.where()}")
        if not c.done():
            raise Unsupported(f"unexpected text {c.where()}")

    def _add(self, key: str, obj: dict) -> None:
        kind, name = key.split(":", 1)
        # A table and a view share their names.
        clash = {f"table:{name}", f"view:{name}"} if kind in ("table", "view") else {key}
        if clash & self.model.keys():
            raise Unsupported(f"{name} is created twice")
        self.model[key] = obj

    def _find(self, name: str, kinds: tuple[str, ...]) -> str:
        for kind in kinds:
            if f"{kind}:{name}" in self.model:
                return f"{kind}:{name}"
        raise Unsupported(f"{name} isn't created before this statement")

    # CREATE --------------------------------------------------------------------------

    def _schema(self, c: _Cursor) -> None:
        name = c.name().lower()
        obj: dict[str, Any] = {"kind": "schema", "name": name, "comment": None,
                               "properties": {}, "tags": {}, "grants": {}, "meta": {}}
        while not c.done():
            if c.accept("COMMENT"):
                obj["comment"] = c.string() or None
            elif c.accept("WITH", "DBPROPERTIES") or c.accept("DBPROPERTIES"):
                obj["properties"].update(_properties(c))
            else:
                raise Unsupported(f"the schema clause {c.where()}")
        self._add(f"schema:{name}", obj)

    def _table(self, c: _Cursor, schema: str) -> None:
        name = _object(c, schema)
        if not c.peek("("):
            raise Unsupported("CREATE TABLE ... AS SELECT, whose columns come from its query")
        obj: dict[str, Any] = {
            "kind": "table", "name": name, "comment": None, "columns": [], "constraints": {},
            "partitioning": [], "clustering": None, "row_filter": None, "properties": {},
            "tags": {}, "grants": {}, "meta": {},
        }
        c.expect("(")
        while True:
            if c.accept("CONSTRAINT"):
                cname = _constraint_name(c)
                obj["constraints"][cname] = self._constraint(c, name, schema, inline=True)
            elif any(c.peek(*k) for k in (("PRIMARY", "KEY"), ("FOREIGN", "KEY"), ("CHECK",),
                                         ("UNIQUE",))):
                raise Unsupported("a constraint without a name, which the platform names")
            else:
                obj["columns"].append(self._column(c, schema))
            if not c.accept(","):
                break
        c.expect(")")
        while not c.done():
            if c.accept("USING"):
                if c.name().upper() != "DELTA":
                    raise Unsupported("a table that isn't Delta")
            elif c.accept("PARTITIONED", "BY"):
                obj["partitioning"] = c.names()
            elif c.accept("CLUSTER", "BY"):
                if c.accept("AUTO"):
                    obj["clustering"] = {"auto": True}
                elif not c.accept("NONE"):
                    c.expect("(")
                    paths = [".".join(c.dotted())]
                    while c.accept(","):
                        paths.append(".".join(c.dotted()))
                    c.expect(")")
                    obj["clustering"] = {"columns": paths}
            elif c.accept("COMMENT"):
                obj["comment"] = c.string() or None
            elif c.accept("TBLPROPERTIES"):
                obj["properties"].update(_properties(c))
            elif c.accept("WITH", "ROW", "FILTER"):
                obj["row_filter"] = _row_filter(c, schema)
            else:
                raise Unsupported(f"the table clause {c.where()}")
        self._add(f"table:{name}", obj)

    def _column(self, c: _Cursor, schema: str) -> dict:
        col: dict[str, Any] = {
            "name": c.name(), "type": _type(c), "nullable": True, "comment": None,
            "default": None, "generated": None, "identity": None, "mask": None, "tags": {},
        }
        while not (c.peek(",") or c.peek(")")):
            if c.accept("NOT", "NULL"):
                col["nullable"] = False
            elif c.accept("GENERATED", "ALWAYS", "AS", "IDENTITY"):
                col["identity"] = _identity(c, always=True)
            elif c.accept("GENERATED", "BY", "DEFAULT", "AS", "IDENTITY"):
                col["identity"] = _identity(c, always=False)
            elif c.accept("GENERATED", "ALWAYS", "AS"):
                col["generated"] = c.group()
            elif c.accept("DEFAULT"):
                col["default"] = c.until("COMMENT", "MASK", "NOT", "GENERATED", "CONSTRAINT")
            elif c.accept("COMMENT"):
                col["comment"] = c.string() or None
            elif c.accept("MASK"):
                col["mask"] = _mask(c, schema)
            else:
                raise Unsupported(f"the column clause {c.where()}")
        return col

    def _constraint(self, c: _Cursor, table: str, schema: str, inline: bool) -> dict:
        if c.accept("PRIMARY", "KEY"):
            columns = c.names()
            rely = c.accept("RELY")
            if not rely:
                c.accept("NORELY")
            return {"type": "primary_key", "columns": columns, "rely": rely}
        if c.accept("FOREIGN", "KEY"):
            columns = c.names()
            c.expect("REFERENCES")
            parent = _object(c, schema)
            if not c.peek("("):
                raise Unsupported("a foreign key that doesn't name its parent's columns")
            if inline and parent == table:
                raise Unsupported("a foreign key to its own table inside CREATE TABLE, which the "
                                  "platform refuses (probes/phase2.py, r4)")
            return {"type": "foreign_key", "columns": columns, "references": parent,
                    "parent_columns": c.names()}
        if c.accept("CHECK"):
            if inline:
                raise Unsupported("a CHECK inside CREATE TABLE, which the platform refuses")
            return {"type": "check", "expression": c.group()}
        raise Unsupported(f"the constraint {c.where()}")

    def _view(self, c: _Cursor, schema: str) -> None:
        name = _object(c, schema)
        listed: list[tuple[str, str | None]] | None = None
        if c.accept("("):
            listed = []
            while True:
                column = c.name()
                listed.append((column, c.string() or None if c.accept("COMMENT") else None))
                if not c.accept(","):
                    break
            c.expect(")")
        obj: dict[str, Any] = {"kind": "view", "name": name, "comment": None, "definition": "",
               # An undeclared mode reads back as COMPENSATION (ucstate_prod.shop.order_totals).
               "schema_mode": "COMPENSATION", "columns": [], "properties": {}, "tags": {},
               "grants": {}, "meta": {}}
        while not c.accept("AS"):
            if c.accept("WITH", "SCHEMA"):
                if c.accept("TYPE", "EVOLUTION"):
                    obj["schema_mode"] = "TYPE_EVOLUTION"
                else:
                    mode = c.name().upper()
                    if mode not in ("BINDING", "COMPENSATION", "EVOLUTION"):
                        raise Unsupported(f"the schema mode {mode}")
                    obj["schema_mode"] = mode
            elif c.accept("COMMENT"):
                obj["comment"] = c.string() or None
            elif c.accept("TBLPROPERTIES"):
                obj["properties"].update(_properties(c))
            else:
                raise Unsupported(f"the view clause {c.where()}")
        obj["definition"] = c.rest()
        select = _select(obj["definition"])
        outputs = [_output_name(e) for e in select.expressions]
        if listed is not None and len(listed) != len(outputs):
            raise Unsupported("a column list that doesn't match the query, which the platform "
                              "refuses")
        names = outputs if listed is None else [n for n, _ in listed]
        comments = [None] * len(names) if listed is None else [cm for _, cm in listed]
        obj["columns"] = [{"name": n, "type": None, "comment": cm, "tags": {}}
                          for n, cm in zip(names, comments, strict=True)]
        self._add(f"view:{name}", obj)
        self.views[f"view:{name}"] = _View(schema, select, listed is not None)

    def _function(self, c: _Cursor, schema: str) -> None:
        name = _object(c, schema)
        c.expect("(")
        parameters = []
        while not c.peek(")"):
            p: dict[str, Any] = {"name": c.name(), "type": _type(c), "default": None,
                                 "comment": None}
            while not (c.peek(",") or c.peek(")")):
                if c.accept("DEFAULT"):
                    p["default"] = c.until("COMMENT")
                elif c.accept("COMMENT"):
                    p["comment"] = c.string() or None
                else:
                    raise Unsupported(f"the parameter clause {c.where()}")
            parameters.append(p)
            if not c.accept(","):
                break
        c.expect(")")
        c.expect("RETURNS")
        if c.accept("TABLE"):
            c.expect("(")
            columns = []
            while True:
                columns.append(f"{c.name()} {_type(c)}")
                if not c.accept(","):
                    break
            c.expect(")")
            returns = "TABLE(" + ", ".join(columns) + ")"
        else:
            returns = _type(c)
            if returns not in SIMPLE.values():
                raise Unsupported(f"the return type {returns}: how the platform spells it "
                                  "isn't pinned")
            returns = returns.upper()
        language, comment = "SQL", None
        deterministic: bool | None = None
        access: str | None = None
        while not (c.peek("RETURN") or c.peek("AS")):
            if c.accept("LANGUAGE"):
                language = c.name().upper()
            elif c.accept("DETERMINISTIC"):
                deterministic = True
            elif c.accept("NOT", "DETERMINISTIC"):
                deterministic = False
            elif c.accept("CONTAINS", "SQL"):
                access = "CONTAINS_SQL"
            elif c.accept("READS", "SQL", "DATA"):
                access = "READS_SQL_DATA"
            elif c.accept("COMMENT"):
                comment = c.string() or None
            else:
                raise Unsupported(f"the function clause {c.where()}")
        if language == "SQL":
            c.expect("RETURN")
            body = c.rest()
            if access is None:  # the round-trip sample's big_orders
                access = "READS_SQL_DATA" if _reads(body) else "CONTAINS_SQL"
            deterministic = True if deterministic is None else deterministic
        elif language == "PYTHON":
            c.expect("AS")
            w = c.next()
            if w.kind != "dollar":
                raise Unsupported("a Python body that isn't between $$")
            body = w.text.strip()
            if access is not None:
                raise Unsupported("a Python function that declares SQL access")
            access = "NO_SQL"  # and not deterministic unless declared: the sample's slugify
            deterministic = False if deterministic is None else deterministic
        else:
            raise Unsupported(f"the language {language}")
        self._add(f"function:{name}", {
            "kind": "function", "name": name, "comment": comment, "parameters": parameters,
            "returns": returns, "language": language, "body": body,
            "deterministic": deterministic, "data_access": access, "grants": {}, "meta": {},
        })

    def _volume(self, c: _Cursor, schema: str) -> None:
        name = _object(c, schema)
        obj: dict[str, Any] = {"kind": "volume", "name": name, "comment": None, "tags": {},
                               "grants": {}, "meta": {}}
        if c.accept("COMMENT"):
            obj["comment"] = c.string() or None
        self._add(f"volume:{name}", obj)

    # ALTER and GRANT -------------------------------------------------------------------

    def _alter(self, c: _Cursor, schema: str) -> None:
        if c.accept("SCHEMA") or c.accept("DATABASE"):
            obj = self.model[self._find(c.name().lower(), ("schema",))]
            c.expect("SET", "TAGS")
            obj["tags"].update(_tags(c))
        elif c.accept("VOLUME"):
            obj = self.model[self._find(_object(c, schema), ("volume",))]
            c.expect("SET", "TAGS")
            obj["tags"].update(_tags(c))
        elif c.accept("VIEW"):
            obj = self.model[self._find(_object(c, schema), ("view",))]
            if c.peek("ALTER", "COLUMN"):
                raise Unsupported("ALTER VIEW ... ALTER COLUMN, which the platform refuses "
                                  "(probes/phase2.py, r8)")
            c.expect("SET", "TAGS")
            obj["tags"].update(_tags(c))
        elif c.accept("TABLE"):
            key = self._find(_object(c, schema), ("table", "view"))
            obj = self.model[key]
            if c.accept("ALTER", "COLUMN"):
                column = _column_named(obj, c.name())
                if c.accept("SET", "TAGS"):
                    column["tags"].update(_tags(c))
                elif key.startswith("table:") and c.accept("SET", "MASK"):
                    column["mask"] = _mask(c, schema)
                else:
                    raise Unsupported(f"the column change {c.where()}")
            elif key.startswith("table:") and c.accept("ADD", "CONSTRAINT"):
                cname = _constraint_name(c)
                if cname in obj["constraints"]:
                    raise Unsupported(f"the constraint {cname} is added twice")
                obj["constraints"][cname] = self._constraint(c, obj["name"], schema, inline=False)
            elif key.startswith("table:") and c.accept("SET", "ROW", "FILTER"):
                obj["row_filter"] = _row_filter(c, schema)
            elif key.startswith("table:") and c.accept("SET", "TAGS"):
                obj["tags"].update(_tags(c))
            else:
                raise Unsupported(f"the table change {c.where()}")
        else:
            raise Unsupported(f"ALTER {c.where()}")

    def _grant(self, c: _Cursor, schema: str) -> None:
        privileges = []
        while True:
            words = []
            while not (c.peek(",") or c.peek("ON") or c.done()):
                words.append(c.next().text.upper())
            privileges.append("_".join(words))
            if not c.accept(","):
                break
        c.expect("ON")
        securable = c.next().up
        if securable == "SCHEMA":
            key = self._find(c.name().lower(), ("schema",))
        elif securable in ("TABLE", "VIEW"):
            key = self._find(_object(c, schema), ("table", "view"))
        elif securable in ("FUNCTION", "VOLUME"):
            key = self._find(_object(c, schema), (securable.lower(),))
        else:
            raise Unsupported(f"a grant on {securable}")
        c.expect("TO")
        w = c.next()
        if w.kind not in ("ident", "string", "word"):
            raise Unsupported(f"the principal {w.text!r}")
        grants = self.model[key]["grants"]
        grants[w.text] = sorted(set(grants.get(w.text, [])) | set(privileges))

    # Finishing ---------------------------------------------------------------------------

    def finish(self) -> dict[str, dict]:
        pending = dict(self.views)
        while pending:
            ready = [k for k, v in pending.items() if not (_reads_views(v) & pending.keys())]
            if not ready:
                raise Unsupported(f"views that read each other: {', '.join(sorted(pending))}")
            for key in ready:
                self._derive(key, pending.pop(key))
        for obj in self.model.values():
            if obj["kind"] == "table":
                _check_table(obj)
            for attr in ("tags", "grants", "constraints"):
                if attr in obj:
                    obj[attr] = dict(sorted(obj[attr].items()))
            for column in obj.get("columns", []):
                column["tags"] = dict(sorted(column["tags"].items()))
        return dict(sorted(self.model.items()))

    def _derive(self, key: str, view: _View) -> None:
        """A view's column types, and the comments its columns take from their tables."""
        sources = {}
        for table in _tables(view.select):
            name = f"{(table.db or view.schema).lower()}.{table.name.lower()}"
            found = self.model.get(f"table:{name}") or self.model.get(f"view:{name}")
            if found is None:
                raise Unsupported(f"{key}: it reads {name}, which the source doesn't create")
            sources[(table.alias or table.name).lower()] = found
        types = _Types(sources)
        obj = self.model[key]
        for column, e in zip(obj["columns"], view.select.expressions, strict=True):
            inner = e.unalias()
            column["type"] = types.of(inner)
            if not isinstance(inner, exp.Column):
                continue
            if inner is e and not view.listed:
                # ucstate_prod.shop.order_totals: a named column brings its comment.
                column["comment"] = types.column(inner)["comment"]
            elif column["comment"] is None and types.column(inner)["comment"] is not None:
                raise Unsupported(f"{key}: whether {column['name']} takes the comment of the "
                                  "column it names isn't pinned")


# Pieces -----------------------------------------------------------------------------------


def _object(c: _Cursor, schema: str) -> str:
    parts = [p.lower() for p in c.dotted()]
    if len(parts) == 1:
        return f"{schema.lower()}.{parts[0]}"
    if len(parts) == 2:
        return ".".join(parts)
    raise Unsupported("a three-part name, when the catalog is bound at deploy time")


def _constraint_name(c: _Cursor) -> str:
    name = c.name()
    if name != name.lower():
        raise Unsupported(f"the constraint name {name}: how the platform stores capitals "
                          "isn't pinned")
    return name


def _identity(c: _Cursor, always: bool) -> dict:
    start, step = 1, 1
    if c.accept("("):
        while not c.accept(")"):
            if c.accept("START", "WITH"):
                start = c.integer()
            elif c.accept("INCREMENT", "BY"):
                step = c.integer()
            else:
                raise Unsupported(f"the identity option {c.where()}")
    return {"start": start, "step": step, "always": always}


def _mask(c: _Cursor, schema: str) -> dict:
    function = _object(c, schema)
    return {"function": function, "using": c.names() if c.accept("USING", "COLUMNS") else []}


def _row_filter(c: _Cursor, schema: str) -> dict:
    function = _object(c, schema)
    c.expect("ON")
    return {"function": function, "columns": c.names()}


def _properties(c: _Cursor) -> dict[str, str]:
    c.expect("(")
    out = {}
    while True:
        key = c.string() if c.kind() == "string" else ".".join(c.dotted())
        c.expect("=")
        w = c.next()
        if w.kind in ("string", "number"):
            out[key] = w.text
        elif w.up in ("TRUE", "FALSE"):
            out[key] = w.text.lower()
        else:
            raise Unsupported(f"the property value {w.text!r}")
        if not c.accept(","):
            break
    c.expect(")")
    return out


def _tags(c: _Cursor) -> dict[str, str]:
    """`('k' = 'v', ...)` or `('flag', ...)`. A key alone reads back as ''."""
    c.expect("(")
    out: dict[str, str] = {}
    forms = set()
    while True:
        key = c.string()
        if c.accept("="):
            out[key] = c.string()
            forms.add("pair")
        else:
            out[key] = ""
            forms.add("key")
        if not c.accept(","):
            break
    c.expect(")")
    if len(forms) > 1:
        raise Unsupported("both tag forms in one SET TAGS, which the platform refuses (Phase 0)")
    return out


def _column_named(obj: dict, name: str) -> dict:
    for column in obj["columns"]:
        if column["name"].lower() == name.lower():
            return column
    raise Unsupported(f"{obj['name']} has no column {name}")


def _check_table(obj: dict) -> None:
    """What the platform refuses, or decides itself, in a table that parsed."""
    names = {c["name"].lower() for c in obj["columns"]}
    key_columns = set()
    for name, con in obj["constraints"].items():
        if not {c.lower() for c in con.get("columns", [])} <= names:
            raise Unsupported(f"{obj['name']}: the constraint {name} names a missing column")
        if con["type"] == "primary_key":
            key_columns |= {c.lower() for c in con["columns"]}
    for column in obj["columns"]:
        if column["nullable"] and (column["identity"] or column["name"].lower() in key_columns):
            raise Unsupported(f"{obj['name']}.{column['name']}: a key or identity column without "
                              "NOT NULL, whose nullability the platform decides")
    governed = obj["row_filter"] or any(c["mask"] for c in obj["columns"])
    if governed and any(con["type"] == "check" for con in obj["constraints"].values()):
        raise Unsupported(f"{obj['name']}: a mask or row filter beside a CHECK, which the "
                          "platform refuses (Phase 0)")
    if any(c["default"] is not None for c in obj["columns"]) and \
            obj["properties"].get("delta.feature.allowColumnDefaults") != "supported":
        raise Unsupported(f"{obj['name']}: a default without delta.feature.allowColumnDefaults, "
                          "which the platform refuses (probes/phase4.py, p3)")
    types = _Types({"": obj})
    for column in obj["columns"]:
        if column["generated"] is None:
            continue
        try:
            derived = types.of(sqlglot.parse_one(column["generated"], read="databricks"))
        except (Unsupported, ParseError):
            continue  # can't check it, but the shadow at apply will
        if derived != column["type"]:
            raise Unsupported(f"{obj['name']}.{column['name']}: the type is {column['type']} but "
                              f"its expression's is {derived}, which the platform refuses")


def _select(query: str) -> exp.Select:
    try:
        tree = sqlglot.parse_one(query, read="databricks")
    except ParseError as e:
        raise Unsupported(f"a view query sqlglot can't parse: {str(e)[:80]}") from None
    if not isinstance(tree, exp.Select) or tree.ctes:
        raise Unsupported("a view query that isn't one plain SELECT")
    _tables(tree)
    return tree


def _tables(select: exp.Select) -> list[exp.Table]:
    """The tables a SELECT reads, from FROM and its joins: only named tables."""
    sources = [select.args.get("from_")] + list(select.args.get("joins") or [])
    tables = []
    for s in sources:
        if s is None or not isinstance(s.this, exp.Table) or s.this.args.get("catalog"):
            raise Unsupported("a view that reads a subquery, or nothing, or another catalog")
        tables.append(s.this)
    if not tables or select.args.get("laterals") or any(t.args.get("pivots") for t in tables):
        raise Unsupported("a view that reads through LATERAL VIEW or PIVOT")
    return tables


def _reads_views(view: _View) -> set[str]:
    return {f"view:{(t.db or view.schema).lower()}.{t.name.lower()}" for t in _tables(view.select)}


def _output_name(e: exp.Expr) -> str:
    if isinstance(e, exp.Alias):
        return e.alias
    if isinstance(e, exp.Column) and not isinstance(e.this, exp.Star):
        return e.name
    raise Unsupported(f"the view column {e.sql('databricks')[:40]!r}: name it with AS")


def _reads(body: str) -> bool:
    """Whether a SQL function body reads a table."""
    try:
        return sqlglot.parse_one(body, read="databricks").find(exp.Table) is not None
    except ParseError:
        raise Unsupported("a function body sqlglot can't parse") from None
