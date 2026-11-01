"""Phase 4 probes: what the hard change classes rest on.

    uv run python -m probes.phase4 --profile my-profile [p1 p2 ...]

Runs in a throwaway catalog, cac_probe_p4, which it drops first and last.

- p1: a `rebuildable` table. CREATE TABLE ... AS SELECT with a column list
  and comments, and what CREATE OR REPLACE ... AS SELECT keeps: table_id,
  grants, tags, a custom property.
- p2: a transition script that replaces a table from its own rows, to
  narrow a type. Whether the rows, table_id, grants and tags survive.
- p3: a column default without `delta.feature.allowColumnDefaults`, set
  on an existing table and declared in a CREATE.
- p4: renaming a table into another schema.
- p5: what a CHECK counts as a violation, for its precheck. Adding one to a
  table with a NULL row, inserting a NULL once it is there, and whether
  `count_if(NOT coalesce(expr, false))` counts the same rows ADD CONSTRAINT
  refuses.
- p6: what a shallow clone keeps, for rehearsal: the PK, an FK, a CHECK,
  comments, tags and grants.
- p7: whether CREATE OR REPLACE ... AS SELECT treats a clone as it treats
  its table. The same replace runs on a table, a shallow clone and a deep
  clone, and each one's constraints are read before and after.
"""

from __future__ import annotations

import argparse
import json
import os

from ucmodel.normalise import normalise
from ucmodel.reader import read_schema
from ucmodel.workspace import SqlError, Workspace

CATALOG = "cac_probe_p4"


def run(ws: Workspace, *statements: str, **kw) -> list[dict] | None:
    rows = None
    for s in statements:
        try:
            rows = ws.sql(s, **kw)
            print(f"  ok    {s.splitlines()[0][:110]}")
        except Exception as e:  # noqa: BLE001 - the API refuses some requests before SQL runs
            summary = e.summary if isinstance(e, SqlError) else f"{type(e).__name__}: {e}"
            print(f"  FAIL  {s.splitlines()[0][:110]}\n        {summary[:240]}")
            rows = None
    return rows


def model(ws: Workspace, schema: str) -> dict:
    return normalise({"catalog": CATALOG, "schemas": {schema: read_schema(ws, CATALOG, schema)}})


def show(ws: Workspace, key: str, schema: str, *fields: str) -> dict:
    m = model(ws, schema)
    obj = m.get(key) or {}
    if not obj:
        print(f"  {key} isn't in the model, which holds " + json.dumps(
            {k: [v.get("kind"), v.get("type")] for k, v in m.items()}))
    print(f"  {key}: " + json.dumps({f: obj.get(f) for f in fields}, sort_keys=True)[:600])
    return obj


def same_id(before: dict, after: dict) -> bool:
    return before.get("meta", {}).get("table_id") == after.get("meta", {}).get("table_id")


def p1(ws: Workspace) -> None:
    print("p1: CREATE TABLE ... AS SELECT, and CREATE OR REPLACE of it")
    s = f"{CATALOG}.p1"
    run(ws, f"CREATE SCHEMA {s}",
        f"CREATE TABLE {s}.src (a INT, b STRING)",
        f"INSERT INTO {s}.src VALUES (1, 'x'), (1, 'y'), (2, 'z')",
        f"CREATE TABLE {s}.listed (a INT COMMENT 'the key', n BIGINT COMMENT 'rows') "
        f"COMMENT 'with a column list' AS SELECT a, count(*) AS n FROM {s}.src GROUP BY a",
        f"CREATE TABLE {s}.r COMMENT 'without one' "
        f"AS SELECT a, count(*) AS n FROM {s}.src GROUP BY a",
        f"ALTER TABLE {s}.r SET TAGS ('domain' = 'x')",
        f"ALTER TABLE {s}.r ALTER COLUMN n SET TAGS ('kind' = 'count')",
        f"GRANT SELECT ON TABLE {s}.r TO `account users`",
        f"ALTER TABLE {s}.r SET TBLPROPERTIES ('cac.rebuild' = 'h1')")
    before = show(ws, "table:p1.listed", "p1", "columns", "comment")
    before = show(ws, "table:p1.r", "p1", "columns", "comment", "tags", "grants", "properties")
    run(ws, f"CREATE OR REPLACE TABLE {s}.r COMMENT 'without one' "
            f"AS SELECT a, count(*) * 2 AS n FROM {s}.src GROUP BY a")
    after = show(ws, "table:p1.r", "p1", "columns", "comment", "tags", "grants", "properties")
    print(f"  table_id kept: {same_id(before, after)}")
    print(f"  rows after: {run(ws, f'SELECT sum(n) AS n FROM {s}.r')}")
    run(ws, f"CREATE OR REPLACE TABLE {s}.listed (a INT COMMENT 'the key', n BIGINT) "
            f"COMMENT 'with a column list' AS SELECT a, count(*) AS n FROM {s}.src GROUP BY a")


def p2(ws: Workspace) -> None:
    print("p2: a table replaced from its own rows, narrowing a type")
    s = f"{CATALOG}.p2"
    run(ws, f"CREATE SCHEMA {s}",
        f"CREATE TABLE {s}.t (id BIGINT NOT NULL COMMENT 'key', v STRING, "
        f"CONSTRAINT t_pk PRIMARY KEY (id)) COMMENT 'narrow me'",
        f"ALTER TABLE {s}.t SET TAGS ('domain' = 'x')",
        f"GRANT SELECT ON TABLE {s}.t TO `account users`",
        f"INSERT INTO {s}.t VALUES (1, 'a'), (2, 'b'), (3, NULL)")
    before = show(ws, "table:p2.t", "p2", "columns", "constraints", "comment", "tags", "grants")
    run(ws, f"CREATE OR REPLACE TABLE {s}.t (id INT NOT NULL COMMENT 'key', v STRING, "
            f"CONSTRAINT t_pk PRIMARY KEY (id)) COMMENT 'narrow me' "
            f"AS SELECT CAST(id AS INT) AS id, v FROM {s}.t")
    after = show(ws, "table:p2.t", "p2", "columns", "constraints", "comment", "tags", "grants")
    print(f"  table_id kept: {same_id(before, after)}")
    print(f"  rows after: {run(ws, f'SELECT count(*) AS n, count(v) AS v FROM {s}.t')}")


def p3(ws: Workspace) -> None:
    print("p3: a column default without allowColumnDefaults")
    s = f"{CATALOG}.p3"
    run(ws, f"CREATE SCHEMA {s}",
        f"CREATE TABLE {s}.t (a INT)",
        f"ALTER TABLE {s}.t ALTER COLUMN a SET DEFAULT 1",
        f"CREATE TABLE {s}.declared (a INT DEFAULT 1)")
    show(ws, "table:p3.t", "p3", "columns", "properties")


def p4(ws: Workspace) -> None:
    print("p4: renaming a table into another schema")
    run(ws, f"CREATE SCHEMA {CATALOG}.p4a", f"CREATE SCHEMA {CATALOG}.p4b",
        f"CREATE TABLE {CATALOG}.p4a.t (a INT)",
        f"ALTER TABLE {CATALOG}.p4a.t RENAME TO {CATALOG}.p4b.t",
        f"SELECT count(*) AS n FROM {CATALOG}.p4b.t")


def p5(ws: Workspace) -> None:
    print("p5: what a CHECK counts as a violation")
    s = f"{CATALOG}.p5"
    run(ws, f"CREATE SCHEMA {s}", f"CREATE TABLE {s}.t (x INT)",
        f"INSERT INTO {s}.t VALUES (1), (NULL)")
    for name, expr in [("plain", "NOT (x > 0)"),
                       ("null-is-a-violation", "NOT coalesce(x > 0, false)")]:
        rows = run(ws, f"SELECT count_if({expr}) AS violations FROM {s}.t")
        print(f"  {name} precheck counts: {rows}")
    run(ws, f"ALTER TABLE {s}.t ADD CONSTRAINT x_positive CHECK (x > 0)  -- expect error: the NULL",
        f"DELETE FROM {s}.t WHERE x IS NULL",
        f"ALTER TABLE {s}.t ADD CONSTRAINT x_positive CHECK (x > 0)",
        f"INSERT INTO {s}.t VALUES (NULL)",
        f"INSERT INTO {s}.t VALUES (-1)  -- expect error",
        f"SELECT count(*) AS n FROM {s}.t")


def p6(ws: Workspace) -> None:
    print("p6: what a shallow clone keeps")
    s, r = f"{CATALOG}.p6", f"{CATALOG}.p6r"
    run(ws, f"CREATE SCHEMA {s}", f"CREATE SCHEMA {r}",
        f"CREATE TABLE {s}.parent (id INT NOT NULL COMMENT 'key', "
        f"CONSTRAINT parent_pk PRIMARY KEY (id)) COMMENT 'parent'",
        f"CREATE TABLE {s}.child (id INT NOT NULL, parent_id INT COMMENT 'up', "
        f"CONSTRAINT child_pk PRIMARY KEY (id), "
        f"CONSTRAINT child_parent_fk FOREIGN KEY (parent_id) REFERENCES {s}.parent (id)) "
        f"COMMENT 'child' TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')",
        f"ALTER TABLE {s}.child ADD CONSTRAINT id_positive CHECK (id > 0)",
        f"ALTER TABLE {s}.child SET TAGS ('domain' = 'x')",
        f"GRANT SELECT ON TABLE {s}.child TO `account users`",
        f"INSERT INTO {s}.parent VALUES (1)", f"INSERT INTO {s}.child VALUES (1, 1)",
        f"CREATE TABLE {r}.parent SHALLOW CLONE {s}.parent",
        f"CREATE TABLE {r}.child SHALLOW CLONE {s}.child")
    show(ws, "table:p6r.child", "p6r", "columns", "constraints", "comment", "tags", "grants",
         "properties")


def p7(ws: Workspace) -> None:
    print("p7: CREATE OR REPLACE ... AS SELECT on a table and on its clones")
    s = f"{CATALOG}.p7"
    run(ws, f"CREATE SCHEMA {s}",
        f"CREATE TABLE {s}.t (id BIGINT NOT NULL COMMENT 'key', v STRING, "
        f"CONSTRAINT t_pk PRIMARY KEY (id)) COMMENT 'original'",
        f"INSERT INTO {s}.t VALUES (1, 'a'), (2, 'b')",
        f"CREATE TABLE {s}.shallow SHALLOW CLONE {s}.t",
        f"CREATE TABLE {s}.deep DEEP CLONE {s}.t")
    for name in ("t", "shallow", "deep"):
        obj = model(ws, "p7").get(f"table:p7.{name}") or {}
        print(f"  {name} before: type {obj.get('meta', {}).get('table_type', '?')}, constraints "
              f"{sorted(obj.get('constraints', {}))}, comment {obj.get('comment')!r}, "
              f"id nullable {[c['nullable'] for c in obj.get('columns', [])][:1]}")
        run(ws, f"CREATE OR REPLACE TABLE {s}.{name} AS SELECT * FROM {s}.{name}")
        obj = model(ws, "p7").get(f"table:p7.{name}") or {}
        print(f"  {name} after:  constraints {sorted(obj.get('constraints', {}))}, comment "
              f"{obj.get('comment')!r}, id nullable "
              f"{[c['nullable'] for c in obj.get('columns', [])][:1]}")
    info = ws.table(f"{s}.deep") or {}
    print(f"  deep clone's table type: {info.get('table_type')}")


PROBES = [p1, p2, p3, p4, p5, p6, p7]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--warehouse", default=os.environ.get("DATABRICKS_WAREHOUSE_ID"),
                        required="DATABRICKS_WAREHOUSE_ID" not in os.environ,
                        help="a SQL warehouse (default: DATABRICKS_WAREHOUSE_ID)")
    parser.add_argument("only", nargs="*", help="probe names, such as p1 p3")
    args = parser.parse_args()
    ws = Workspace.connect(args.profile, args.warehouse)
    ws.sql(f"DROP CATALOG IF EXISTS {CATALOG} CASCADE")
    ws.sql(f"CREATE CATALOG {CATALOG} COMMENT 'Phase 4 probes; dropped when they finish'")
    try:
        for probe in PROBES:
            if not args.only or probe.__name__ in args.only:
                probe(ws)
    finally:
        ws.sql(f"DROP CATALOG IF EXISTS {CATALOG} CASCADE")


if __name__ == "__main__":
    main()
