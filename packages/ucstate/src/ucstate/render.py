"""The model as source files, for `ucstate import`.

Each object becomes one file in the layout of docs/design.md, section 1. The
files use two-part names, so they build in any catalog. Applying a file to
an empty catalog and reading it back must give the same model, which
is what the round-trip tests check.

Owners are not written. Ownership is outside the Phase 1 model.
"""

from __future__ import annotations

from ucmodel.sqltext import ident, principal, qualified, string

# Delta properties that record a decision rather than a platform default, so
# `import` writes them into the source. Other `delta.*` keys are left to the
# platform. Keys outside these namespaces are user properties and are always
# written.
DECLARABLE_DELTA = {
    "delta.appendOnly",
    "delta.checkpointRetentionDuration",
    "delta.columnMapping.mode",
    "delta.dataSkippingNumIndexedCols",
    "delta.dataSkippingStatsColumns",
    "delta.deletedFileRetentionDuration",
    "delta.enableChangeDataFeed",
    "delta.enableIcebergCompatV2",
    "delta.enableTypeWidening",
    "delta.feature.allowColumnDefaults",
    "delta.feature.catalogManaged",
    "delta.isolationLevel",
    "delta.logRetentionDuration",
    "delta.randomizeFilePrefixes",
    "delta.setTransactionRetentionDuration",
    "delta.targetFileSize",
    "delta.tuneFileSizesForRewrites",
    "delta.universalFormat.enabledFormats",
}
# Declarable only when switched off, since on is the platform default.
DECLARABLE_WHEN_FALSE = {"delta.enableDeletionVectors", "delta.enableRowTracking"}
PLATFORM_NAMESPACES = ("delta.", "spark.", "databricks.", "view.", "io.", "parquet.")
PLATFORM_KEYS = {"collation", "owner"}


def declarable(props: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in props.items()
        if k in DECLARABLE_DELTA
        or (k in DECLARABLE_WHEN_FALSE and v == "false")
        or (not k.startswith(PLATFORM_NAMESPACES) and k not in PLATFORM_KEYS)
    }


def path_for(key: str) -> str:
    kind, name = key.split(":", 1)
    if kind == "schema":
        return f"src/{name}/schema.sql"
    schema, obj = name.split(".", 1)
    return f"src/{schema}/{kind}s/{obj}.sql"


def render(model: dict[str, dict]) -> dict[str, str]:
    """Map each supported object to (path, text). Unsupported objects are skipped."""
    renderers = {
        "schema": _schema,
        "table": _table,
        "view": _view,
        "function": _function,
        "volume": _volume,
    }
    files = {}
    for key, obj in model.items():
        if obj["kind"] in renderers:
            create, *rest = renderers[obj["kind"]](obj)
            files[path_for(key)] = create + ("\n\n" + "\n".join(rest) if rest else "") + "\n"
    return files


# Shared pieces ---------------------------------------------------------------------


def privilege(p: str) -> str:
    return p.replace("_", " ")


def _grants(securable: str, name: str, grants: dict[str, list[str]]) -> list[str]:
    return [
        f"GRANT {', '.join(privilege(p) for p in privileges)} ON {securable} {name} "
        f"TO {principal(who)};"
        for who, privileges in grants.items()
    ]


def tag_list(tags: dict[str, str]) -> str:
    return ", ".join(f"{string(k)} = {string(v)}" for k, v in tags.items())


def _properties(keyword: str, props: dict[str, str]) -> list[str]:
    if not props:
        return []
    lines = [f"  {string(k)} = {string(v)}" for k, v in props.items()]
    return [f"{keyword} (", ",\n".join(lines), ")"]


def sql_type(t: str) -> str:
    """Upper-case simple types. Complex ones keep their case, for field names."""
    head = t.split("(")[0]
    return t.upper() if head.isalpha() and "<" not in t else t


# Objects -------------------------------------------------------------------------------


def _schema(o: dict) -> list[str]:
    name = ident(o["name"])
    head = [f"CREATE SCHEMA {name}"]
    if o["comment"]:
        head.append(f"COMMENT {string(o['comment'])}")
    head += _properties("WITH DBPROPERTIES", declarable(o["properties"]))
    lines = ["\n".join(head) + ";"]
    if o["tags"]:
        lines.append(f"ALTER SCHEMA {name} SET TAGS ({tag_list(o['tags'])});")
    lines += _grants("SCHEMA", name, o["grants"])
    return lines


def _column(c: dict) -> str:
    parts = [ident(c["name"]), sql_type(c["type"])]
    if not c["nullable"]:
        parts.append("NOT NULL")
    if c["generated"]:
        parts.append(f"GENERATED ALWAYS AS ({c['generated']})")
    if c["identity"]:
        i = c["identity"]
        how = "ALWAYS" if i["always"] else "BY DEFAULT"
        parts.append(
            f"GENERATED {how} AS IDENTITY (START WITH {i['start']} INCREMENT BY {i['step']})"
        )
    if c["default"] is not None:
        parts.append(f"DEFAULT {c['default']}")
    if c["comment"]:
        parts.append(f"COMMENT {string(c['comment'])}")
    if c["mask"]:
        parts.append(f"MASK {qualified(c['mask']['function'])}")
        if c["mask"]["using"]:
            parts.append(f"USING COLUMNS ({', '.join(map(ident, c['mask']['using']))})")
    return " ".join(parts)


def column_list(names: list[str]) -> str:
    return ", ".join(ident(n) for n in names)


def constraint(c: dict) -> str:
    """A constraint's definition, after `CONSTRAINT name`."""
    if c["type"] == "primary_key":
        return f"PRIMARY KEY ({column_list(c['columns'])})" + (" RELY" if c["rely"] else "")
    if c["type"] == "foreign_key":
        return (f"FOREIGN KEY ({column_list(c['columns'])})\n"
                f"    REFERENCES {qualified(c['references'])} ({column_list(c['parent_columns'])})")
    return f"CHECK ({c['expression']})"


def _table(o: dict) -> list[str]:
    name = qualified(o["name"])
    elements = [_column(c) for c in o["columns"]]
    later = []
    order = {"primary_key": 0, "foreign_key": 1, "check": 2}
    for cname, c in sorted(o["constraints"].items(), key=lambda kv: (order[kv[1]["type"]], kv[0])):
        text = f"CONSTRAINT {ident(cname)} {constraint(c)}"
        # A CHECK, or an FK to the table itself, can't be declared inline
        # (probes/phase2.py, r4), so it follows the CREATE.
        if c["type"] == "check" or c.get("references") == o["name"]:
            later.append(f"ALTER TABLE {name} ADD {text};")
        else:
            elements.append(text)
    head = [f"CREATE TABLE {name} (", ",\n".join(f"  {e}" for e in elements), ")"]
    if o["partitioning"]:
        head.append(f"PARTITIONED BY ({column_list(o['partitioning'])})")
    if o["clustering"]:
        if o["clustering"].get("auto"):
            head.append("CLUSTER BY AUTO")
        else:
            head.append(f"CLUSTER BY ({', '.join(map(qualified, o['clustering']['columns']))})")
    if o["comment"]:
        head.append(f"COMMENT {string(o['comment'])}")
    head += _properties("TBLPROPERTIES", declarable(o["properties"]))
    if o["row_filter"]:
        f = o["row_filter"]
        head.append(f"WITH ROW FILTER {qualified(f['function'])} ON ({column_list(f['columns'])})")
    lines = ["\n".join(head) + ";", *later]
    if o["tags"]:
        lines.append(f"ALTER TABLE {name} SET TAGS ({tag_list(o['tags'])});")
    for c in o["columns"]:
        if c["tags"]:
            lines.append(
                f"ALTER TABLE {name} ALTER COLUMN {ident(c['name'])} "
                f"SET TAGS ({tag_list(c['tags'])});"
            )
    lines += _grants("TABLE", name, o["grants"])
    return lines


def _view(o: dict) -> list[str]:
    name = qualified(o["name"])
    head = [f"CREATE VIEW {name}"]
    if any(c["comment"] for c in o["columns"]):
        cols = [
            ident(c["name"]) + (f" COMMENT {string(c['comment'])}" if c["comment"] else "")
            for c in o["columns"]
        ]
        head[0] += " ("
        head += [",\n".join(f"  {c}" for c in cols), ")"]
    mode = (o["schema_mode"] or "COMPENSATION").upper().replace("_", " ")
    if mode != "COMPENSATION":
        head.append(f"WITH SCHEMA {mode}")
    if o["comment"]:
        head.append(f"COMMENT {string(o['comment'])}")
    head += _properties("TBLPROPERTIES", declarable(o["properties"]))
    head.append("AS " + o["definition"].rstrip().rstrip(";"))
    lines = ["\n".join(head) + ";"]
    if o["tags"]:
        lines.append(f"ALTER VIEW {name} SET TAGS ({tag_list(o['tags'])});")
    # ALTER VIEW has no ALTER COLUMN; ALTER TABLE tags a view's columns
    # (probes/phase2.py, r1 and r8).
    for c in o["columns"]:
        if c["tags"]:
            lines.append(
                f"ALTER TABLE {name} ALTER COLUMN {ident(c['name'])} "
                f"SET TAGS ({tag_list(c['tags'])});"
            )
    lines += _grants("TABLE", name, o["grants"])
    return lines


def _function(o: dict) -> list[str]:
    name = qualified(o["name"])
    params = []
    for p in o["parameters"]:
        text = f"{ident(p['name'])} {sql_type(p['type'])}"
        if p["default"] is not None:
            text += f" DEFAULT {p['default']}"
        if p["comment"]:
            text += f" COMMENT {string(p['comment'])}"
        params.append(text)
    returns = o["returns"]
    if returns.startswith("TABLE("):
        returns = "TABLE (" + returns[len("TABLE("):]
    else:
        returns = sql_type(returns)
    head = [f"CREATE FUNCTION {name}({', '.join(params)})", f"RETURNS {returns}"]
    if o["language"] != "SQL":
        head.append(f"LANGUAGE {o['language']}")
    head.append("DETERMINISTIC" if o["deterministic"] else "NOT DETERMINISTIC")
    if o["language"] == "SQL" and o["data_access"] == "READS_SQL_DATA":
        head.append("READS SQL DATA")
    if o["comment"]:
        head.append(f"COMMENT {string(o['comment'])}")
    if o["language"] == "SQL":
        head.append(f"RETURN {o['body']}")
    else:
        head.append(f"AS $$\n{o['body']}\n$$")
    return ["\n".join(head) + ";", *_grants("FUNCTION", name, o["grants"])]


def _volume(o: dict) -> list[str]:
    name = qualified(o["name"])
    head = f"CREATE VOLUME {name}" + (f"\nCOMMENT {string(o['comment'])}" if o["comment"] else "")
    lines = [head + ";"]
    if o["tags"]:
        lines.append(f"ALTER VOLUME {name} SET TAGS ({tag_list(o['tags'])});")
    lines += _grants("VOLUME", name, o["grants"])
    return lines
