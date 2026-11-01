# type: ignore
# The probe stays exactly as it ran, so type checkers skip it, as ruff does.
"""Phase 0 probes for docs/design.md.

Every object is synthetic and lives in two throwaway catalogs, `cac_probe` and
`cac_probe_clone`, which `teardown` drops. Run probes by name:

    uv run --with databricks-sdk probes/phase0.py setup p02 p02b p02c p03 p03b \
        p04 p05 p06 p07 p08 p09 p10 p11 p12 p13 teardown

The order matters: p02c needs p02's table, p11 clones the tables from p02
and p04, and p13 reads p11's. `undrop_window` is separate (see its docstring).

Each probe appends its observations to a JSON file (default
`phase0-results.json` in the current directory). A probe step that is
expected to fail says so in its label, so a success there is the finding.
The profile and the warehouse come from `DATABRICKS_CONFIG_PROFILE` and
`DATABRICKS_WAREHOUSE_ID`.
"""

import json
import os
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import (
    ExecuteStatementRequestOnWaitTimeout,
    StatementState,
)

PROFILE = os.environ["DATABRICKS_CONFIG_PROFILE"]
WAREHOUSE = os.environ["DATABRICKS_WAREHOUSE_ID"]
CAT = "cac_probe"
CLONE_CAT = "cac_probe_clone"

w = WorkspaceClient(profile=PROFILE)
results: dict = {}
current: list = []


def record(label, value):
    current.append({"step": label, "result": value})
    text = json.dumps(value, default=str)
    print(f"  {label}: {text[:300]}{'...' if len(text) > 300 else ''}")


def run_sql(stmt, catalog=CAT, schema="p", client=None):
    client = client or w
    r = client.statement_execution.execute_statement(
        statement=stmt,
        warehouse_id=WAREHOUSE,
        catalog=catalog,
        schema=schema,
        wait_timeout="50s",
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
    )
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        r = client.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        msg = r.status.error.message if r.status.error else str(r.status.state)
        return {"error": msg.strip().split("\n")[0][:400]}
    if not r.manifest or not r.manifest.schema or not r.manifest.schema.columns:
        return {"ok": True}
    cols = [c.name for c in r.manifest.schema.columns]
    rows = (r.result.data_array or []) if r.result else []
    return {"rows": [dict(zip(cols, row)) for row in rows]}


def sql(label, stmt, **kw):
    out = run_sql(stmt, **kw)
    record(label, out)
    return out


def rest(path):
    try:
        return w.api_client.do("GET", path)
    except Exception as e:  # noqa: BLE001 - a probe records every failure
        return {"error": str(e)[:400]}


def table(name):
    return rest(f"/api/2.1/unity-catalog/tables/{CAT}.p.{name}")


def table_id(name):
    return table(name).get("table_id")


def props(name, prefixes=("delta.", "cac.")):
    p = table(name).get("properties", {})
    return {k: v for k, v in p.items() if k.startswith(prefixes)}


def columns(name):
    return [
        {
            "name": c["name"],
            "position": c.get("position"),
            "type_text": c.get("type_text"),
            "nullable": c.get("nullable"),
            "metadata": json.loads(c["type_json"]).get("metadata"),
            **({"mask": c["mask"]} if "mask" in c else {}),
        }
        for c in table(name).get("columns", [])
    ]


def tags(entity_type, name):
    try:
        return [t.as_dict() for t in w.entity_tag_assignments.list(entity_type, name)]
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:400]}


def grants(securable_type, name):
    try:
        return w.grants.get(securable_type, name).as_dict()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:400]}


# --------------------------------------------------------------------------


def setup():
    """Probe 1: a catalog created without a location, on default storage."""
    sql("create catalog", f"CREATE CATALOG {CAT} COMMENT 'Phase 0 probes; safe to drop'", catalog=None, schema=None)
    c = rest(f"/api/2.1/unity-catalog/catalogs/{CAT}")
    record("catalog via REST", {k: c.get(k) for k in ("storage_root", "storage_location", "catalog_type", "isolation_mode", "owner", "error")})
    sql("create schema", f"CREATE SCHEMA {CAT}.p", catalog=None, schema=None)


def p02():
    """Probe 2: which attributes REST returns, and where."""
    sql("mask function", "CREATE FUNCTION p.mask_email(email STRING) RETURNS STRING "
        "COMMENT 'probe mask' RETURN concat('***@', substring_index(email, '@', -1))")
    sql("filter function", "CREATE FUNCTION p.region_filter(region STRING) RETURNS BOOLEAN "
        "RETURN region <> 'hidden'")
    sql("create features", """
        CREATE TABLE p.features (
          id BIGINT GENERATED ALWAYS AS IDENTITY (START WITH 100 INCREMENT BY 10),
          email STRING COMMENT 'contact address',
          region STRING DEFAULT 'EU',
          amount DECIMAL(10, 2) NOT NULL,
          amount_x2 DECIMAL(12, 2) GENERATED ALWAYS AS (amount * 2),
          CONSTRAINT features_pk PRIMARY KEY (id)
        )
        CLUSTER BY (region)
        COMMENT 'probe table'
        TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported', 'cac.owner' = 'probe')
    """)
    sql("check constraint", "ALTER TABLE p.features ADD CONSTRAINT amount_nonneg CHECK (amount >= 0)")
    sql("column mask", "ALTER TABLE p.features ALTER COLUMN email SET MASK p.mask_email")
    sql("row filter", "ALTER TABLE p.features SET ROW FILTER p.region_filter ON (region)")
    sql("table tags", "ALTER TABLE p.features SET TAGS ('domain' = 'probe', 'flag')")
    sql("column tags", "ALTER TABLE p.features ALTER COLUMN email SET TAGS ('pii' = 'email')")
    sql("schema tags", "ALTER SCHEMA p SET TAGS ('team' = 'probe')")
    sql("table grant", "GRANT SELECT ON TABLE p.features TO `account users`")
    sql("schema grant", "GRANT USE SCHEMA ON SCHEMA p TO `account users`")
    sql("view", "CREATE VIEW p.features_v (id COMMENT 'the id', region) COMMENT 'probe view' "
        "AS SELECT id, region FROM p.features")
    sql("volume", "CREATE VOLUME p.files COMMENT 'probe volume'")

    t = table("features")
    with open("phase0-features.json", "w") as f:
        json.dump(t, f, indent=2)
    record("table keys", sorted(t.keys()))
    record("columns", columns("features"))
    record("row_filter", t.get("row_filter"))
    record("table_constraints", t.get("table_constraints"))
    record("declared-looking properties", props("features"))
    record("clustering", {k: v for k, v in t.get("properties", {}).items() if "luster" in k})
    v = table("features_v")
    record("view", {k: v.get(k) for k in ("view_definition", "comment", "view_dependencies")}
           | {"columns": [(c["name"], c.get("comment")) for c in v.get("columns", [])]})
    record("view catalogAndNamespace", {k: x for k, x in v.get("properties", {}).items() if "catalogAndNamespace" in k})
    record("table tags (entity-tag-assignments)", tags("tables", f"{CAT}.p.features"))
    record("column tags (entity-tag-assignments)", tags("columns", f"{CAT}.p.features.email"))
    record("schema tags (entity-tag-assignments)", tags("schemas", f"{CAT}.p"))
    record("table grants", grants("table", f"{CAT}.p.features"))
    record("schema grants", grants("schema", f"{CAT}.p"))
    fn = rest(f"/api/2.1/unity-catalog/functions/{CAT}.p.mask_email")
    record("function", {k: fn.get(k) for k in ("routine_body", "routine_definition", "data_type", "full_data_type", "comment", "input_params", "is_deterministic", "sql_data_access")})
    s = rest(f"/api/2.1/unity-catalog/schemas/{CAT}.p")
    record("schema", {k: s.get(k) for k in ("comment", "properties", "owner", "updated_by")})
    vol = rest(f"/api/2.1/unity-catalog/volumes/{CAT}.p.files")
    record("volume", {k: vol.get(k) for k in ("volume_type", "comment", "storage_location", "owner")})
    d = run_sql("DESCRIBE TABLE EXTENDED p.features AS JSON")
    if "rows" in d:
        doc = json.loads(next(iter(d["rows"][0].values())))
        with open("phase0-features-describe.json", "w") as f:
            json.dump(doc, f, indent=2)
        record("DESCRIBE AS JSON keys", sorted(doc.keys()))
        record("DESCRIBE AS JSON columns", [{k: c.get(k) for k in ("name", "default", "comment", "nullable")} | {"type": c.get("type")} for c in doc.get("columns", [])])
    else:
        record("DESCRIBE AS JSON", d)
    for view in ("column_tags", "table_tags", "column_masks", "row_filters", "check_constraints"):
        sql(f"information_schema.{view}", f"SELECT * FROM {CAT}.information_schema.{view}")


def p02b():
    """Probe 2, continued: masks, row filters and table tags, on a table with no CHECK."""
    sql("create secured", "CREATE TABLE p.secured (email STRING, region STRING)")
    sql("column mask", "ALTER TABLE p.secured ALTER COLUMN email SET MASK p.mask_email")
    sql("row filter", "ALTER TABLE p.secured SET ROW FILTER p.region_filter ON (region)")
    sql("CHECK on a masked table (expect error)", "ALTER TABLE p.secured ADD CONSTRAINT region_set CHECK (region IS NOT NULL)")
    sql("table tag with a value", "ALTER TABLE p.secured SET TAGS ('domain' = 'probe')")
    sql("key-only table tag", "ALTER TABLE p.secured SET TAGS ('flag')")
    sql("key-only tag as empty string", "ALTER TABLE p.secured SET TAGS ('flag2' = '')")
    t = table("secured")
    record("row_filters", t.get("row_filters"))
    record("column_masks", {c["name"]: c.get("column_masks") for c in t.get("columns", [])})
    record("table tags", tags("tables", f"{CAT}.p.secured"))
    for view in ("table_tags", "column_masks", "row_filters"):
        sql(f"information_schema.{view}", f"SELECT * FROM {CAT}.information_schema.{view}")
    sql("insert", "INSERT INTO p.secured VALUES ('ada@example.com', 'EU'), ('x@example.com', 'hidden')")
    sql("owner reads through mask and filter", "SELECT * FROM p.secured")


def p03():
    """Probe 3: ADD COLUMN ... DEFAULT on a table that has rows."""
    sql("create", "CREATE TABLE p.defaults (id INT)")
    record("fresh table properties", props("defaults"))
    sql("insert", "INSERT INTO p.defaults VALUES (1), (2)")
    sql("add column with default, feature not enabled (expect error)", "ALTER TABLE p.defaults ADD COLUMN status STRING DEFAULT 'new'")
    sql("enable column defaults", "ALTER TABLE p.defaults SET TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported')")
    sql("add column with default", "ALTER TABLE p.defaults ADD COLUMN status STRING DEFAULT 'new'")
    sql("add NOT NULL column with default", "ALTER TABLE p.defaults ADD COLUMN flag BOOLEAN NOT NULL DEFAULT false")
    sql("existing rows", "SELECT * FROM p.defaults ORDER BY id")
    sql("insert without the column", "INSERT INTO p.defaults (id) VALUES (3)")
    sql("rows after insert", "SELECT * FROM p.defaults ORDER BY id")
    sql("SET NOT NULL with NULL rows (expect error)", "ALTER TABLE p.defaults ALTER COLUMN status SET NOT NULL")
    sql("backfill", "UPDATE p.defaults SET status = 'new' WHERE status IS NULL")
    sql("SET NOT NULL after backfill", "ALTER TABLE p.defaults ALTER COLUMN status SET NOT NULL")
    record("columns after", columns("defaults"))
    record("properties after", props("defaults"))


def p03b():
    """Probe 3, continued: the four-step NOT NULL column with a default."""
    sql("add nullable column", "ALTER TABLE p.defaults ADD COLUMN status STRING")
    sql("set default", "ALTER TABLE p.defaults ALTER COLUMN status SET DEFAULT 'new'")
    sql("existing rows after SET DEFAULT", "SELECT * FROM p.defaults ORDER BY id")
    sql("insert without the column", "INSERT INTO p.defaults (id) VALUES (4)")
    sql("SET NOT NULL with NULL rows (expect error)", "ALTER TABLE p.defaults ALTER COLUMN status SET NOT NULL")
    sql("backfill", "UPDATE p.defaults SET status = DEFAULT WHERE status IS NULL")
    sql("SET NOT NULL after backfill", "ALTER TABLE p.defaults ALTER COLUMN status SET NOT NULL")
    sql("rows", "SELECT * FROM p.defaults ORDER BY id")
    record("columns", columns("defaults"))
    sql("drop default", "ALTER TABLE p.defaults ALTER COLUMN status DROP DEFAULT")
    record("columns after DROP DEFAULT", columns("defaults"))
    record("feature still listed", props("defaults").get("delta.feature.allowColumnDefaults"))


def p04():
    """Probe 4: column mapping, rename and drop."""
    sql("create", "CREATE TABLE p.mapping (a INT, b STRING, c STRING)")
    sql("insert", "INSERT INTO p.mapping VALUES (1, 'x', 'y')")
    sql("rename column before mapping (expect error)", "ALTER TABLE p.mapping RENAME COLUMN b TO b2")
    sql("drop column before mapping (expect error)", "ALTER TABLE p.mapping DROP COLUMN c")
    record("properties before", props("mapping"))
    sql("enable column mapping", "ALTER TABLE p.mapping SET TBLPROPERTIES ('delta.columnMapping.mode' = 'name')")
    sql("rename column", "ALTER TABLE p.mapping RENAME COLUMN b TO b2")
    sql("drop column", "ALTER TABLE p.mapping DROP COLUMN c")
    sql("rows", "SELECT * FROM p.mapping")
    record("properties after", props("mapping"))
    record("columns after", columns("mapping"))
    sql("history", "SELECT version, operation FROM (DESCRIBE HISTORY p.mapping) ORDER BY version")


def p05():
    """Probe 5: type widening."""
    sql("create", "CREATE TABLE p.widen (n INT, d DECIMAL(10, 2), s SMALLINT)")
    sql("insert", "INSERT INTO p.widen VALUES (1, 1.50, 2)")
    sql("widen INT to BIGINT before enabling (expect error)", "ALTER TABLE p.widen ALTER COLUMN n TYPE BIGINT")
    sql("enable type widening", "ALTER TABLE p.widen SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')")
    sql("INT to BIGINT", "ALTER TABLE p.widen ALTER COLUMN n TYPE BIGINT")
    sql("DECIMAL(10,2) to DECIMAL(12,2)", "ALTER TABLE p.widen ALTER COLUMN d TYPE DECIMAL(12, 2)")
    sql("DECIMAL(12,2) to DECIMAL(14,4)", "ALTER TABLE p.widen ALTER COLUMN d TYPE DECIMAL(14, 4)")
    sql("SMALLINT to INT", "ALTER TABLE p.widen ALTER COLUMN s TYPE INT")
    sql("narrow BIGINT to INT (expect error)", "ALTER TABLE p.widen ALTER COLUMN n TYPE INT")
    sql("INT to STRING (expect error)", "ALTER TABLE p.widen ALTER COLUMN s TYPE STRING")
    sql("rows", "SELECT * FROM p.widen")
    record("properties after", props("widen"))
    record("columns after", columns("widen"))


def p06():
    """Probe 6: column order without column mapping."""
    sql("create", "CREATE TABLE p.ordering (a INT, b INT, c INT)")
    sql("insert", "INSERT INTO p.ordering VALUES (1, 2, 3)")
    sql("move c after a", "ALTER TABLE p.ordering ALTER COLUMN c AFTER a")
    sql("move b first", "ALTER TABLE p.ordering ALTER COLUMN b FIRST")
    sql("rows", "SELECT * FROM p.ordering")
    record("columns", [(c["name"], c["position"]) for c in columns("ordering")])
    record("column mapping mode", props("ordering").get("delta.columnMapping.mode"))


def p07():
    """Probe 7: CREATE OR REPLACE on a table with rows, grants, tags and an incoming FK."""
    sql("parent", "CREATE TABLE p.cor_parent (id INT NOT NULL, name STRING, "
        "CONSTRAINT cor_parent_pk PRIMARY KEY (id)) COMMENT 'before'")
    sql("child", "CREATE TABLE p.cor_child (id INT, parent_id INT, "
        "CONSTRAINT cor_child_parent_fk FOREIGN KEY (parent_id) REFERENCES p.cor_parent (id))")
    sql("insert", "INSERT INTO p.cor_parent VALUES (1, 'a')")
    sql("grant", "GRANT SELECT ON TABLE p.cor_parent TO `account users`")
    sql("tag", "ALTER TABLE p.cor_parent SET TAGS ('t' = '1')")
    before = table_id("cor_parent")
    record("table_id before", before)
    sql("CREATE OR REPLACE, PK kept", "CREATE OR REPLACE TABLE p.cor_parent (id INT NOT NULL, name STRING, extra STRING, "
        "CONSTRAINT cor_parent_pk PRIMARY KEY (id)) COMMENT 'after'")
    t = table("cor_parent")
    record("after", {"table_id": t.get("table_id"), "same_id": t.get("table_id") == before,
                     "comment": t.get("comment"), "columns": [c["name"] for c in t.get("columns", [])]})
    record("grants after", grants("table", f"{CAT}.p.cor_parent"))
    record("tags after", tags("tables", f"{CAT}.p.cor_parent"))
    record("child FK after", table("cor_child").get("table_constraints"))
    sql("rows after", "SELECT count(*) AS n FROM p.cor_parent")
    sql("history", "SELECT version, operation FROM (DESCRIBE HISTORY p.cor_parent) ORDER BY version")
    sql("time travel to the insert", "SELECT * FROM p.cor_parent VERSION AS OF 1")
    sql("CREATE OR REPLACE dropping the referenced PK", "CREATE OR REPLACE TABLE p.cor_parent (id INT NOT NULL, name STRING)")
    record("child FK after PK dropped", table("cor_child").get("table_constraints"))


def p08():
    """Probe 8: renaming a table that an FK and a view refer to."""
    sql("parent", "CREATE TABLE p.ren_parent (id INT NOT NULL, CONSTRAINT ren_parent_pk PRIMARY KEY (id))")
    sql("child", "CREATE TABLE p.ren_child (parent_id INT, "
        "CONSTRAINT ren_child_fk FOREIGN KEY (parent_id) REFERENCES p.ren_parent (id))")
    sql("view", "CREATE VIEW p.ren_view AS SELECT id FROM p.ren_parent")
    before = table_id("ren_parent")
    sql("rename", "ALTER TABLE p.ren_parent RENAME TO p.ren_parent2")
    record("same table_id after rename", table_id("ren_parent2") == before)
    record("child FK after rename", table("ren_child").get("table_constraints"))
    sql("query view after rename (expect error)", "SELECT * FROM p.ren_view")
    v = table("ren_view")
    record("view after rename", {"definition": v.get("view_definition"), "dependencies": v.get("view_dependencies")})


def p09():
    """Probe 9: creation and drop order with FKs and views."""
    sql("FK to a missing table (expect error)", "CREATE TABLE p.orphan (x INT, "
        "CONSTRAINT orphan_fk FOREIGN KEY (x) REFERENCES p.not_yet (id))")
    sql("parent without PK", "CREATE TABLE p.nopk_parent (id INT NOT NULL)")
    sql("FK to a parent without PK (expect error)", "CREATE TABLE p.nopk_child (x INT, "
        "CONSTRAINT nopk_fk FOREIGN KEY (x) REFERENCES p.nopk_parent (id))")
    sql("view over a missing table (expect error)", "CREATE VIEW p.early_view AS SELECT * FROM p.not_yet")
    sql("fk parent", "CREATE TABLE p.fk_parent (id INT NOT NULL, CONSTRAINT fk_parent_pk PRIMARY KEY (id))")
    sql("fk child", "CREATE TABLE p.fk_child (pid INT, CONSTRAINT fk_child_fk FOREIGN KEY (pid) REFERENCES p.fk_parent (id))")
    sql("view over fk_parent", "CREATE VIEW p.fk_view AS SELECT id FROM p.fk_parent")
    sql("drop PK an FK uses (expect error)", "ALTER TABLE p.fk_parent DROP PRIMARY KEY")
    sql("drop parent an FK references", "DROP TABLE p.fk_parent")
    record("child FK after parent drop", table("fk_child").get("table_constraints"))
    sql("query view after parent drop (expect error)", "SELECT * FROM p.fk_view")


def p10():
    """Probe 10: UNDROP."""
    sql("create", "CREATE TABLE p.dropme (x INT)")
    sql("insert", "INSERT INTO p.dropme VALUES (1)")
    first = table_id("dropme")
    record("table_id", first)
    sql("drop", "DROP TABLE p.dropme")
    sql("SHOW TABLES DROPPED", f"SHOW TABLES DROPPED IN {CAT}.p")
    sql("UNDROP by name", "UNDROP TABLE p.dropme")
    sql("rows after undrop", "SELECT * FROM p.dropme")
    record("same table_id after undrop", table_id("dropme") == first)
    sql("drop again", "DROP TABLE p.dropme")
    sql("recreate same name", "CREATE TABLE p.dropme (y INT)")
    sql("UNDROP by name while the name is taken (expect error)", "UNDROP TABLE p.dropme")
    sql("UNDROP WITH ID while the name is taken (expect error)", f"UNDROP TABLE WITH ID '{first}'")
    sql("move the new table aside", "ALTER TABLE p.dropme RENAME TO p.dropme_new")
    sql("UNDROP WITH ID", f"UNDROP TABLE WITH ID '{first}'")
    sql("rows after undrop with id", "SELECT * FROM p.dropme")
    sql("drop a view", "CREATE VIEW p.dropme_view AS SELECT 1 AS one")
    sql("drop the view", "DROP VIEW p.dropme_view")
    sql("UNDROP a view (expect error)", "UNDROP TABLE p.dropme_view")


def p11():
    """Probe 11: a shallow clone of a managed table into another catalog."""
    sql("source", "CREATE TABLE p.clone_src (id INT NOT NULL, v STRING, CONSTRAINT clone_src_pk PRIMARY KEY (id)) "
        "COMMENT 'clone source'")
    sql("insert", "INSERT INTO p.clone_src VALUES (1, 'a'), (2, 'b')")
    sql("tag", "ALTER TABLE p.clone_src SET TAGS ('t' = '1')")
    sql("clone catalog", f"CREATE CATALOG {CLONE_CAT} COMMENT 'Phase 0 probes; safe to drop'", catalog=None, schema=None)
    sql("clone schema", f"CREATE SCHEMA {CLONE_CAT}.p", catalog=None, schema=None)
    sql("shallow clone", f"CREATE TABLE {CLONE_CAT}.p.clone_dst SHALLOW CLONE {CAT}.p.clone_src")
    sql("rows in clone", f"SELECT * FROM {CLONE_CAT}.p.clone_dst ORDER BY id")
    dst = rest(f"/api/2.1/unity-catalog/tables/{CLONE_CAT}.p.clone_dst")
    record("clone metadata", {"comment": dst.get("comment"), "table_constraints": dst.get("table_constraints"),
                              "tags": tags("tables", f"{CLONE_CAT}.p.clone_dst")})
    sql("alter clone", f"ALTER TABLE {CLONE_CAT}.p.clone_dst ADD COLUMN extra STRING")
    sql("update clone", f"UPDATE {CLONE_CAT}.p.clone_dst SET v = 'z' WHERE id = 1")
    sql("source unchanged", "SELECT * FROM p.clone_src ORDER BY id")
    sql("shallow clone of a masked, filtered table", f"CREATE TABLE {CLONE_CAT}.p.features_clone SHALLOW CLONE {CAT}.p.features")
    sql("shallow clone of a column-mapped table", f"CREATE TABLE {CLONE_CAT}.p.mapping_clone SHALLOW CLONE {CAT}.p.mapping")


def p12():
    """Probe 12: whether multi-statement transactions cover DDL."""
    sql("create", "CREATE TABLE p.txn (x INT)")
    sql("atomic block, DML only", "BEGIN ATOMIC INSERT INTO p.txn VALUES (1); INSERT INTO p.txn VALUES (2); END")
    sql("rows", "SELECT * FROM p.txn ORDER BY x")
    sql("atomic block that fails", "BEGIN ATOMIC INSERT INTO p.txn VALUES (3); SELECT raise_error('boom'); END")
    sql("rows after failure", "SELECT * FROM p.txn ORDER BY x")
    sql("atomic block with DDL", "BEGIN ATOMIC ALTER TABLE p.txn ADD COLUMN y INT; INSERT INTO p.txn VALUES (4, 4); END")
    record("columns after DDL block", [c["name"] for c in columns("txn")])
    sql("catalog-managed table", "CREATE TABLE p.txn_cm (x INT) TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported')")
    sql("atomic block on catalog-managed table", "BEGIN ATOMIC INSERT INTO p.txn_cm VALUES (1); INSERT INTO p.txn_cm VALUES (2); END")
    sql("failing atomic block on catalog-managed table", "BEGIN ATOMIC INSERT INTO p.txn_cm VALUES (3); SELECT raise_error('boom'); END")
    sql("rows on catalog-managed table", "SELECT * FROM p.txn_cm ORDER BY x")
    sql("atomic DDL on catalog-managed table", "BEGIN ATOMIC ALTER TABLE p.txn_cm ADD COLUMN y INT; INSERT INTO p.txn_cm VALUES (4, 4); END")
    record("catalog-managed columns", [c["name"] for c in columns("txn_cm")])
    sql("non-atomic script that fails midway", "BEGIN INSERT INTO p.txn VALUES (5); SELECT raise_error('boom'); END")
    sql("rows after non-atomic failure", "SELECT * FROM p.txn ORDER BY x")
    sql("enable catalogManaged on an existing table", "ALTER TABLE p.txn SET TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported')")
    record("properties after", {k: v for k, v in props("txn").items() if "catalogManaged" in k})
    sql("atomic MERGE into two tables, then a runtime failure", """
        BEGIN ATOMIC
          MERGE INTO p.txn_cm t USING (SELECT 9 AS x) s ON t.x = s.x WHEN NOT MATCHED THEN INSERT *;
          MERGE INTO p.txn t USING (SELECT 9 AS x) s ON t.x = s.x WHEN NOT MATCHED THEN INSERT *;
          SELECT raise_error('boom');
        END""")
    sql("rows after (9 must be absent from both)", "SELECT 'txn_cm' AS t, x FROM p.txn_cm UNION ALL SELECT 'txn', x FROM p.txn ORDER BY t, x")


def p13():
    """Probe 13: a service principal with an OAuth secret, as CI would use it.

    The secret exists for the length of this function and is never printed.
    The principal and its secret are deleted however the probe ends.
    """
    from databricks.sdk.service.iam import ComplexValue

    sp = sec = None
    try:
        sp = w.service_principals.create(
            display_name="cac-probe-sp",
            active=True,
            entitlements=[ComplexValue(value="workspace-access"), ComplexValue(value="databricks-sql-access")],
        )
        sec = w.service_principal_secrets_proxy.create(sp.id, lifetime="3600s")
        for grant in ("USE CATALOG ON CATALOG cac_probe", "USE SCHEMA ON SCHEMA cac_probe.p",
                      "SELECT ON TABLE cac_probe.p.clone_src"):
            run_sql(f"GRANT {grant} TO `{sp.application_id}`", catalog=None, schema=None)
        spw = WorkspaceClient(host=w.config.host, client_id=sp.application_id,
                              client_secret=sec.secret, auth_type="oauth-m2m")
        record("current user", spw.current_user.me().user_name == sp.application_id)
        record("warehouse query", run_sql("SELECT count(*) AS n FROM cac_probe.p.clone_src",
                                          catalog=None, schema=None, client=spw))
        t = spw.api_client.do("GET", "/api/2.1/unity-catalog/tables/cac_probe.p.clone_src")
        record("REST tables get", [c["name"] for c in t["columns"]])
        record("DDL without the privilege (expect error)", run_sql(
            "CREATE TABLE cac_probe.p.sp_made (x INT)", catalog=None, schema=None, client=spw))
    except Exception as e:  # noqa: BLE001
        text = str(e).replace(sec.secret, "<redacted>") if sec and sec.secret else str(e)
        record("stopped", f"{type(e).__name__}: {text}"[:400])
    finally:
        if sec:
            w.service_principal_secrets_proxy.delete(sp.id, sec.id)
        if sp:
            w.service_principals.delete(sp.id)
        record("principal and secret deleted", bool(sp))


def p02c():
    """Probe 2, continued: identity metadata after inserts, and a CHECK violation."""
    sql("insert", "INSERT INTO p.features (email, amount) VALUES ('ada@example.com', 1.00), ('bo@example.com', 2.00)")
    sql("CHECK violation (expect error)", "INSERT INTO p.features (email, amount) VALUES ('neg@example.com', -1.00)")
    sql("rows", "SELECT id, region, amount_x2 FROM p.features ORDER BY id")
    record("metadata after inserts", {c["name"]: c["metadata"] for c in columns("features")})


def undrop_window():
    """Probe 10, continued: a table left dropped in workspace.default.

    Not part of teardown. Run `SHOW TABLES DROPPED IN workspace.default`
    on later days to see when it disappears.
    """
    kw = {"catalog": "workspace", "schema": "default"}
    sql("create", "CREATE TABLE default.cac_undrop_window (x INT) "
        "COMMENT 'Phase 0: dropped on purpose to measure the UNDROP window'", **kw)
    sql("insert", "INSERT INTO default.cac_undrop_window VALUES (1)", **kw)
    record("table_id", rest("/api/2.1/unity-catalog/tables/workspace.default.cac_undrop_window").get("table_id"))
    sql("drop", "DROP TABLE default.cac_undrop_window", **kw)
    sql("SHOW TABLES DROPPED", "SHOW TABLES DROPPED IN workspace.default", **kw)


def teardown():
    sql("drop clone catalog", f"DROP CATALOG IF EXISTS {CLONE_CAT} CASCADE", catalog=None, schema=None)
    sql("drop probe catalog", f"DROP CATALOG IF EXISTS {CAT} CASCADE", catalog=None, schema=None)


PROBES = {
    f.__name__: f
    for f in (setup, p02, p02b, p02c, p03, p03b, p04, p05, p06, p07, p08, p09, p10, p11, p12, p13, undrop_window, teardown)
}

if __name__ == "__main__":
    out = "phase0-results.json"
    try:
        with open(out) as f:
            results = json.load(f)
    except FileNotFoundError:
        pass
    for name in sys.argv[1:]:
        print(f"== {name}: {PROBES[name].__doc__ or ''}")
        current = []
        PROBES[name]()
        results[name] = current
        with open(out, "w") as f:
            json.dump(results, f, indent=2, default=str)
