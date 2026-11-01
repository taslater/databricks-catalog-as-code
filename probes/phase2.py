"""Phase 2 probes: what the planner's statements do to state they don't name.

    uv run python -m probes.phase2 --profile my-profile

Runs in ucstate_test.probe_p2, which it drops first and last. Each probe prints
what REST reads afterwards, so the planner's choices rest on observations:

- r1: CREATE OR REPLACE VIEW, on a view with grants, tags and column tags.
- r2: CREATE OR REPLACE FUNCTION, on a function with a grant that a column
  mask uses.
- r3: dropping a PK that an FK in another table references.
- r4: a table whose FK references its own PK, in one CREATE.
- r5: CLUSTER BY AUTO and CLUSTER BY NONE on an existing table.
- r6: ADD COLUMN ... FIRST, and ALTER COLUMN ... FIRST.
- r7: COMMENT ON TABLE for a view.
- r8: tagging a view's column (`ALTER VIEW ... ALTER COLUMN` is a syntax
  error, see r1).
- r9: a self-referencing FK added by ALTER after the CREATE (r4 fails).
"""

from __future__ import annotations

import argparse
import json
import os

from ucmodel.normalise import normalise
from ucmodel.reader import read_schema
from ucmodel.workspace import SqlError, Workspace

CATALOG, SCHEMA = "ucstate_test", "probe_p2"
S = f"{CATALOG}.{SCHEMA}"


def run(ws: Workspace, *statements: str) -> None:
    for s in statements:
        try:
            ws.sql(s)
            print(f"  ok    {s.splitlines()[0][:110]}")
        except SqlError as e:
            print(f"  FAIL  {s.splitlines()[0][:110]}\n        {e.summary}")


def model(ws: Workspace) -> dict:
    raw = {"catalog": CATALOG, "schemas": {SCHEMA: read_schema(ws, CATALOG, SCHEMA)}}
    return normalise(raw)


def show(ws: Workspace, key: str, *attrs: str) -> None:
    obj = model(ws).get(key)
    if obj is None:
        print(f"  {key}: missing")
        return
    for a in attrs:
        value = obj.get(a)
        if a == "columns":
            value = [{k: c[k] for k in ("name", "type", "comment", "tags", "mask") if k in c}
                     for c in value]
        print(f"  {key} {a}: {json.dumps(value)}")


def r1(ws: Workspace) -> None:
    print("r1: CREATE OR REPLACE VIEW keeps grants and tags?")
    run(ws,
        f"CREATE TABLE {S}.base (a INT, b STRING)",
        f"CREATE VIEW {S}.v (a COMMENT 'col a', b) COMMENT 'before' AS SELECT a, b FROM {S}.base",
        f"GRANT SELECT ON TABLE {S}.v TO `account users`",
        f"ALTER VIEW {S}.v SET TAGS ('domain' = 'x')",
        f"ALTER VIEW {S}.v ALTER COLUMN a SET TAGS ('pii' = 'no')")
    show(ws, f"view:{SCHEMA}.v", "comment", "grants", "tags", "columns")
    run(ws, f"CREATE OR REPLACE VIEW {S}.v (a COMMENT 'col a', b) COMMENT 'after' "
            f"AS SELECT a, b FROM {S}.base WHERE a > 0")
    show(ws, f"view:{SCHEMA}.v", "comment", "grants", "tags", "columns", "definition")


def r2(ws: Workspace) -> None:
    print("r2: CREATE OR REPLACE FUNCTION keeps grants, and a mask that uses it?")
    run(ws,
        f"CREATE FUNCTION {S}.m(x STRING) RETURNS STRING RETURN x",
        f"GRANT EXECUTE ON FUNCTION {S}.m TO `account users`",
        f"CREATE TABLE {S}.masked (x STRING MASK {S}.m)")
    show(ws, f"function:{SCHEMA}.m", "grants", "body")
    run(ws, f"CREATE OR REPLACE FUNCTION {S}.m(x STRING) RETURNS STRING RETURN upper(x)")
    show(ws, f"function:{SCHEMA}.m", "grants", "body")
    show(ws, f"table:{SCHEMA}.masked", "columns")
    run(ws, f"CREATE OR REPLACE FUNCTION {S}.m(x STRING, y INT) RETURNS STRING RETURN x")
    show(ws, f"table:{SCHEMA}.masked", "columns")


def r3(ws: Workspace) -> None:
    print("r3: drop a PK that another table's FK references")
    run(ws,
        f"CREATE TABLE {S}.parent (id INT NOT NULL, CONSTRAINT parent_pk PRIMARY KEY (id))",
        f"CREATE TABLE {S}.child (pid INT, "
        f"CONSTRAINT child_fk FOREIGN KEY (pid) REFERENCES {S}.parent (id))",
        f"ALTER TABLE {S}.parent DROP CONSTRAINT parent_pk")
    show(ws, f"table:{SCHEMA}.parent", "constraints")
    show(ws, f"table:{SCHEMA}.child", "constraints")
    run(ws, f"ALTER TABLE {S}.parent DROP CONSTRAINT parent_pk CASCADE")
    show(ws, f"table:{SCHEMA}.parent", "constraints")
    show(ws, f"table:{SCHEMA}.child", "constraints")


def r4(ws: Workspace) -> None:
    print("r4: an FK to the table's own PK, in one CREATE")
    run(ws, f"CREATE TABLE {S}.employee (id INT NOT NULL, manager_id INT, "
            f"CONSTRAINT employee_pk PRIMARY KEY (id), "
            f"CONSTRAINT employee_manager_fk FOREIGN KEY (manager_id) "
            f"REFERENCES {S}.employee (id))")
    show(ws, f"table:{SCHEMA}.employee", "constraints")


def r5(ws: Workspace) -> None:
    print("r5: CLUSTER BY AUTO, then CLUSTER BY NONE, on an existing table")
    run(ws, f"CREATE TABLE {S}.clustered (a INT, b INT) CLUSTER BY (a)",
        f"ALTER TABLE {S}.clustered CLUSTER BY AUTO")
    show(ws, f"table:{SCHEMA}.clustered", "clustering")
    run(ws, f"ALTER TABLE {S}.clustered CLUSTER BY NONE")
    show(ws, f"table:{SCHEMA}.clustered", "clustering")
    run(ws, f"CREATE TABLE {S}.plain (a INT, b INT)", f"ALTER TABLE {S}.plain CLUSTER BY (b)")
    show(ws, f"table:{SCHEMA}.plain", "clustering")


def r6(ws: Workspace) -> None:
    print("r6: ADD COLUMN ... FIRST and ALTER COLUMN ... FIRST")
    run(ws, f"CREATE TABLE {S}.ordered (a INT, b INT)",
        f"INSERT INTO {S}.ordered VALUES (1, 2)",
        f"ALTER TABLE {S}.ordered ADD COLUMN z INT FIRST",
        f"ALTER TABLE {S}.ordered ALTER COLUMN b FIRST",
        f"ALTER TABLE {S}.ordered ADD COLUMN y STRING COMMENT 'why' AFTER a")
    show(ws, f"table:{SCHEMA}.ordered", "columns")
    print(f"  rows: {ws.sql(f'SELECT * FROM {S}.ordered')}")


def r7(ws: Workspace) -> None:
    print("r7: COMMENT ON TABLE for a view")
    run(ws, f"CREATE VIEW {S}.v7 AS SELECT 1 AS one",
        f"COMMENT ON TABLE {S}.v7 IS 'set by COMMENT ON TABLE'")
    show(ws, f"view:{SCHEMA}.v7", "comment")


def r8(ws: Workspace) -> None:
    print("r8: tagging a view's column, three ways")
    run(ws, f"CREATE VIEW {S}.v8 (a, b) AS SELECT 1 AS a, 2 AS b",
        f"ALTER TABLE {S}.v8 ALTER COLUMN a SET TAGS ('pii' = 'alter-table')",
        f"SET TAG ON COLUMN {S}.v8.b `pii` = `set-tag`")
    show(ws, f"view:{SCHEMA}.v8", "columns")
    run(ws, f"ALTER TABLE {S}.v8 ALTER COLUMN a UNSET TAGS ('pii')")
    show(ws, f"view:{SCHEMA}.v8", "columns")


def r9(ws: Workspace) -> None:
    print("r9: a self-referencing FK added after the CREATE")
    run(ws, f"CREATE TABLE {S}.employee9 (id INT NOT NULL, manager_id INT, "
            f"CONSTRAINT employee9_pk PRIMARY KEY (id))",
        f"ALTER TABLE {S}.employee9 ADD CONSTRAINT employee9_manager_fk "
        f"FOREIGN KEY (manager_id) REFERENCES {S}.employee9 (id)")
    show(ws, f"table:{SCHEMA}.employee9", "constraints")


PROBES = [r1, r2, r3, r4, r5, r6, r7, r8, r9]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--warehouse", default=os.environ.get("DATABRICKS_WAREHOUSE_ID"),
                        required="DATABRICKS_WAREHOUSE_ID" not in os.environ,
                        help="a SQL warehouse (default: DATABRICKS_WAREHOUSE_ID)")
    parser.add_argument("only", nargs="*", help="probe names, such as r1 r3")
    args = parser.parse_args()
    ws = Workspace.connect(args.profile, args.warehouse)
    ws.sql(f"DROP SCHEMA IF EXISTS {S} CASCADE")
    ws.sql(f"CREATE SCHEMA {S}")
    try:
        for probe in PROBES:
            if not args.only or probe.__name__ in args.only:
                probe(ws)
    finally:
        ws.sql(f"DROP SCHEMA IF EXISTS {S} CASCADE")


if __name__ == "__main__":
    main()
