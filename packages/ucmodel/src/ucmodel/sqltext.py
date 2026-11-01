"""Text-level SQL helpers: splitting, catalog stripping, comparison and quoting.

None of this parses SQL into a tree. Databricks is the parser (the shadow
catalog). These helpers only work on characters and on sqlglot's tokens,
which keep their offsets, so original formatting survives.
"""

from __future__ import annotations

import re

from sqlglot import Token, TokenType
from sqlglot.dialects.databricks import Databricks

_DIALECT = Databricks()


def tokens(text: str) -> list[Token]:
    return _DIALECT.tokenize(text)


# Splitting ---------------------------------------------------------------------


def _skip(text: str, i: int) -> int | None:
    """If position i opens a quote, comment or $$ body, return the index after it."""
    c = text[i]
    if c in "'\"":
        j = i + 1
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == c:
                return j + 1
            j += 1
        return len(text)
    if c == "`":
        j = text.find("`", i + 1)
        while j != -1 and text.startswith("``", j):
            j = text.find("`", j + 2)
        return len(text) if j == -1 else j + 1
    if text.startswith("--", i):
        j = text.find("\n", i)
        return len(text) if j == -1 else j
    if text.startswith("/*", i):
        j = text.find("*/", i + 2)
        return len(text) if j == -1 else j + 2
    if text.startswith("$$", i):
        j = text.find("$$", i + 2)
        return len(text) if j == -1 else j + 2
    return None


def split_statements(text: str) -> list[str]:
    """Split on semicolons outside quotes, comments and $$ bodies.

    A piece that holds only comments and whitespace is dropped.
    """
    pieces, start, i = [], 0, 0
    while i < len(text):
        after = _skip(text, i)
        if after is not None:
            i = after
        elif text[i] == ";":
            pieces.append(text[start:i])
            start = i = i + 1
        else:
            i += 1
    pieces.append(text[start:])
    return [p.strip() for p in pieces if strip_comments(p).strip()]


def strip_comments(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        after = _skip(text, i)
        if after is None:
            out.append(text[i])
            i += 1
        else:
            if not text.startswith(("--", "/*"), i):
                out.append(text[i:after])
            i = after
    return "".join(out)


# Catalog stripping and comparison ---------------------------------------------------


def _is_name(t: Token) -> bool:
    return t.token_type in (TokenType.VAR, TokenType.IDENTIFIER) or bool(
        re.fullmatch(r"[A-Za-z_]\w*", t.text)
    )


def strip_catalog(text: str, catalog: str) -> str:
    """Remove `catalog.` from every three-part name that starts with it.

    Only `catalog . name . name` with no dot before it qualifies, so a
    two-part `alias.column` is never touched. On text the tokenizer can't
    read, the text comes back unchanged.
    """
    try:
        toks = tokens(text)
    except Exception:  # noqa: BLE001 - leave unreadable text alone
        return text
    cuts = []
    for i, t in enumerate(toks[:-4]):
        if (
            _is_name(t)
            and t.text.lower() == catalog.lower()
            and (i == 0 or toks[i - 1].token_type != TokenType.DOT)
            and toks[i + 1].token_type == TokenType.DOT
            and _is_name(toks[i + 2])
            and toks[i + 3].token_type == TokenType.DOT
            and _is_name(toks[i + 4])
        ):
            cuts.append((t.start, toks[i + 1].end + 1))
    for start, end in reversed(cuts):
        text = text[:start] + text[end:]
    return text


def canonical(text: str | None) -> str | None:
    """A form of SQL text for equality: tokens only, case-folded outside strings.

    Whitespace, comments, keyword case and a trailing semicolon don't
    count. String literals keep their case.
    """
    if text is None:
        return None
    try:
        toks = tokens(text)
    except Exception:  # noqa: BLE001
        return " ".join(text.split())
    parts = [
        repr(t.text) if t.token_type == TokenType.STRING else t.text.lower()
        for t in toks
    ]
    while parts and parts[-1] == ";":
        parts.pop()
    return " ".join(parts)


def two_part_names(text: str | None) -> set[str]:
    """Every `a.b` in the text with no dot on either side, lower-cased.

    This finds the project objects a view or SQL function body reads.
    `alias.column` pairs come back too; callers keep only names they know.
    """
    if not text:
        return set()
    try:
        toks = tokens(text)
    except Exception:  # noqa: BLE001
        return set()

    def name(t: Token) -> bool:
        return t.token_type != TokenType.STRING and _is_name(t)

    found = set()
    for i in range(len(toks) - 2):
        if (
            name(toks[i])
            and toks[i + 1].token_type == TokenType.DOT
            and name(toks[i + 2])
            and (i == 0 or toks[i - 1].token_type != TokenType.DOT)
            and (i + 3 >= len(toks) or toks[i + 3].token_type != TokenType.DOT)
        ):
            found.add(f"{toks[i].text.lower()}.{toks[i + 2].text.lower()}")
    return found


def or_replace(statement: str) -> str:
    """`CREATE ...` as `CREATE OR REPLACE ...`, keeping any leading comments."""
    i = 0
    while i < len(statement):
        if statement[i].isspace():
            i += 1
        elif statement.startswith(("--", "/*"), i):
            i = _skip(statement, i) or len(statement)
        else:
            break
    if not re.match(r"CREATE\s", statement[i:], re.IGNORECASE):
        raise ValueError(f"not a CREATE statement: {statement[:60]!r}")
    return statement[:i + 6] + " OR REPLACE" + statement[i + 6:]


# What a source statement declares ---------------------------------------------------


def declared_properties(statement: str) -> set[str]:
    """Keys named in TBLPROPERTIES or DBPROPERTIES clauses of a statement."""
    toks = tokens(statement)
    keys: set[str] = set()
    i = 0
    while i < len(toks):
        if toks[i].text.upper() in ("TBLPROPERTIES", "DBPROPERTIES") and i + 1 < len(toks) \
                and toks[i + 1].token_type == TokenType.L_PAREN:
            i += 2
            depth, in_key = 0, True
            key: list[str] = []
            while i < len(toks):
                t = toks[i]
                if t.token_type == TokenType.L_PAREN:
                    depth += 1
                elif t.token_type == TokenType.R_PAREN:
                    if depth == 0:
                        if key:
                            keys.add("".join(key))
                        break
                    depth -= 1
                elif t.token_type == TokenType.EQ and depth == 0:
                    keys.add("".join(key))
                    key, in_key = [], False
                elif t.token_type == TokenType.COMMA and depth == 0:
                    if in_key and key:
                        keys.add("".join(key))
                    key, in_key = [], True
                elif in_key:
                    key.append(t.text)
                i += 1
        i += 1
    return keys


def declares_clustering(statement: str) -> bool:
    toks = tokens(statement)
    for i, t in enumerate(toks):
        words = t.text.upper().split()
        if words == ["CLUSTER", "BY"]:
            return True
        if words == ["CLUSTER"] and i + 1 < len(toks) and toks[i + 1].text.upper() == "BY":
            return True
    return False


# Quoting ------------------------------------------------------------------------------

# Spark's ANSI-mode reserved words, which can't be used as bare identifiers.
RESERVED = set("""
ALL AND ANY AS AUTHORIZATION BOTH CASE CAST CHECK COLLATE COLUMN CONSTRAINT CREATE CROSS
CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER DISTINCT ELSE END ESCAPE EXCEPT
FALSE FETCH FILTER FOR FOREIGN FROM FULL GRANT GROUP HAVING IN INNER INTERSECT INTO IS JOIN
LATERAL LEADING LEFT NATURAL NOT NULL OFFSET ON ONLY OR ORDER OUTER OVERLAPS PRIMARY
REFERENCES RIGHT SELECT SESSION_USER SOME TABLE THEN TIME TO TRAILING UNION UNIQUE UNKNOWN
USER USING WHEN WHERE WITH
""".split())


def ident(name: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name.upper() not in RESERVED:
        return name
    return "`" + name.replace("`", "``") + "`"


def qualified(name: str) -> str:
    return ".".join(ident(part) for part in name.split("."))


def string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def principal(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"
