"""The compiler against what the platform built, and its rules one at a time."""

import pytest

from ucmodel import compiler, samples
from ucmodel.compiler import Unsupported, compile_statements, type_of
from ucmodel.model import compare
from ucmodel.sqltext import split_statements


def compile_text(text: str, schema: str = "s") -> dict[str, dict]:
    return compile_statements((schema, s) for s in split_statements(text))


def shop() -> dict[str, dict]:
    return compile_text((samples.HERE / "shop.sql").read_text(), "shop")


SCHEMA = "CREATE SCHEMA s;"
TABLE = SCHEMA + """
CREATE TABLE s.t (
  id BIGINT NOT NULL COMMENT 'Key',
  q INT,
  p DECIMAL(10,2),
  wide DECIMAL(38,10),
  x DOUBLE,
  CONSTRAINT t_pk PRIMARY KEY (id)
);
"""


def view_types(select: str) -> dict[str, str]:
    model = compile_text(TABLE + f"CREATE VIEW s.v AS {select} FROM s.t;")
    return {c["name"]: c["type"] for c in model["view:s.v"]["columns"]}


# The oracle ------------------------------------------------------------------------------


def test_shop_compiles_to_what_the_platform_built():
    model = shop()
    assert sorted(model) == sorted(samples.model("shop"))
    assert compare(model, samples.model("shop")) == []


def test_a_wrong_type_rule_fails_the_shop_case(monkeypatch):
    """The known-bad control: an int taken as decimal(1,0) rather than (10,0)."""
    monkeypatch.setitem(compiler.AS_DECIMAL, "int", (1, 0))
    diffs = compare(shop(), samples.model("shop"))
    assert [(d.key, d.path, d.desired, d.live) for d in diffs] == [
        ("view:shop.order_totals", "columns.total.type", "decimal(22,2)", "decimal(31,2)")]


# Types -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("written", "stored"), [
    ("INTEGER", "int"),
    ("LONG", "bigint"),
    ("DECIMAL", "decimal(10,0)"),
    ("NUMERIC(5)", "decimal(5,0)"),
    ("DECIMAL(10, 2)", "decimal(10,2)"),
    ("MAP<STRING, STRING>", "map<string,string>"),
    ("STRUCT<width: DOUBLE, `Height`: DOUBLE>", "struct<width:double,Height:double>"),
    ("ARRAY<ARRAY<INT>>", "array<array<int>>"),
    ("STRING COLLATE UTF8_BINARY", "string"),
])
def test_types_are_spelled_as_the_platform_spells_them(written, stored):
    assert type_of(written) == stored


@pytest.mark.parametrize("written", [
    "VARCHAR(10)", "INTERVAL DAY", "STRUCT<a: INT NOT NULL>", "STRUCT<`a b`: INT>",
    "STRING COLLATE UNICODE",
])
def test_unpinned_types_are_declined(written):
    with pytest.raises(Unsupported):
        type_of(written)


def test_view_types_follow_sparks_rules():
    assert view_types("SELECT id, q * p AS a, sum(q * p) AS b, p * 2 AS c, p + q AS d, "
                      "avg(p) AS e, count(*) AS f, q * q AS g, x * p AS h, "
                      "min(p) AS i, CAST(q AS DECIMAL(5, 1)) AS j, 2.50 * p AS k") == {
        "id": "bigint",
        "a": "decimal(21,2)",  # an int column is decimal(10,0) beside a decimal
        "b": "decimal(31,2)",  # sum adds 10 digits
        "c": "decimal(12,2)",  # a literal has its own precision (Phase 0)
        "d": "decimal(13,2)",
        "e": "decimal(14,6)",
        "f": "bigint",
        "g": "int",
        "h": "double",
        "i": "decimal(10,2)",
        "j": "decimal(5,1)",
        "k": "decimal(14,4)",
    }


def test_decimals_past_38_digits_lose_scale_as_sparks_do():
    assert view_types("SELECT wide * wide AS w, sum(wide) AS s") == {
        "w": "decimal(38,6)", "s": "decimal(38,10)"}


@pytest.mark.parametrize("select", [
    "SELECT coalesce(q, 0) AS a",  # a function outside the rules
    "SELECT q",  # fine on its own, but see below
    "SELECT q / 2 AS a",  # division
    "SELECT q + 1",  # a name the platform makes up
    "SELECT *",
])
def test_view_columns_outside_the_rules_are_declined(select):
    if select == "SELECT q":
        assert view_types(select) == {"q": "int"}
        return
    with pytest.raises(Unsupported):
        view_types(select)


# Views -------------------------------------------------------------------------------------


def test_a_named_column_brings_its_comment_without_a_column_list():
    model = compile_text(TABLE + "CREATE VIEW s.v AS SELECT id, q FROM s.t;")
    assert [c["comment"] for c in model["view:s.v"]["columns"]] == ["Key", None]
    assert model["view:s.v"]["schema_mode"] == "COMPENSATION"


@pytest.mark.parametrize("view", [
    "CREATE VIEW s.v AS SELECT id AS key FROM s.t;",
    "CREATE VIEW s.v (id) AS SELECT id FROM s.t;",
])
def test_an_unpinned_comment_is_declined(view):
    with pytest.raises(Unsupported, match="isn't pinned"):
        compile_text(TABLE + view)


def test_a_column_list_names_the_columns_and_views_can_read_views():
    model = compile_text(TABLE + """
        CREATE VIEW s.w (total COMMENT 'Sum') WITH SCHEMA BINDING AS SELECT sum(a) AS t
        FROM s.v;
        CREATE VIEW s.v AS SELECT q * p AS a FROM s.t;
    """)
    assert model["view:s.w"]["columns"] == [
        {"name": "total", "type": "decimal(31,2)", "comment": "Sum", "tags": {}}]
    assert model["view:s.w"]["schema_mode"] == "BINDING"


def test_a_view_over_something_the_source_doesnt_create_is_declined():
    with pytest.raises(Unsupported, match="doesn't create"):
        compile_text(SCHEMA + "CREATE VIEW s.v AS SELECT a FROM s.missing;")


# Tables ------------------------------------------------------------------------------------


def test_a_table_keeps_what_was_written():
    model = compile_text(SCHEMA + """
        CREATE TABLE t (
          id BIGINT NOT NULL GENERATED BY DEFAULT AS IDENTITY (START WITH 10),
          `Order` STRING DEFAULT 'none' COMMENT 'O\\'Brien',
          amount DECIMAL(10,2),
          doubled DECIMAL(12,2) GENERATED ALWAYS AS ( amount * 2 ),
          CONSTRAINT t_pk PRIMARY KEY (id) RELY
        )
        PARTITIONED BY (`Order`)
        CLUSTER BY AUTO
        TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported', my.flag = true);
        ALTER TABLE t ADD CONSTRAINT positive CHECK (amount > 0);
        ALTER TABLE s.t SET TAGS ('reviewed');
        ALTER TABLE s.t ALTER COLUMN amount SET TAGS ('pii' = 'no');
        GRANT SELECT, MODIFY ON TABLE s.t TO `account users`;
        GRANT SELECT ON TABLE s.t TO `account users`;
    """)
    t = model["table:s.t"]
    assert t["columns"][0]["identity"] == {"start": 10, "step": 1, "always": False}
    assert t["columns"][1]["name"] == "Order"
    assert t["columns"][1]["default"] == "'none'"
    assert t["columns"][1]["comment"] == "O'Brien"
    assert t["columns"][3]["generated"] == "amount * 2"
    assert t["constraints"] == {
        "positive": {"type": "check", "expression": "amount > 0"},
        "t_pk": {"type": "primary_key", "columns": ["id"], "rely": True},
    }
    assert t["partitioning"] == ["Order"]
    assert t["clustering"] == {"auto": True}
    assert t["properties"] == {"delta.feature.allowColumnDefaults": "supported",
                               "my.flag": "true"}
    assert t["tags"] == {"reviewed": ""}
    assert t["columns"][2]["tags"] == {"pii": "no"}
    assert t["grants"] == {"account users": ["MODIFY", "SELECT"]}


@pytest.mark.parametrize(("source", "reason"), [
    ("CREATE TABLE t AS SELECT 1 AS a;", "AS SELECT"),
    ("CREATE TABLE t (a INT, CHECK (a > 0));", "without a name"),
    ("CREATE TABLE t (a INT, CONSTRAINT c CHECK (a > 0));", "inside CREATE TABLE"),
    ("CREATE TABLE t (a INT NOT NULL, b INT, CONSTRAINT t_pk PRIMARY KEY (a),"
     " CONSTRAINT t_fk FOREIGN KEY (b) REFERENCES t (a));", "its own table"),
    ("CREATE TABLE t (a INT, CONSTRAINT t_pk PRIMARY KEY (a));", "nullability"),
    ("CREATE TABLE t (a BIGINT GENERATED ALWAYS AS IDENTITY);", "nullability"),
    ("CREATE TABLE t (a INT NOT NULL, CONSTRAINT T_PK PRIMARY KEY (a));", "capitals"),
    ("CREATE TABLE t (a STRING DEFAULT 'x');", "allowColumnDefaults"),
    ("CREATE TABLE t (a INT, b BIGINT GENERATED ALWAYS AS (a * 2));", "expression's is int"),
    ("CREATE TABLE t (a STRING MASK s.f); ALTER TABLE t ADD CONSTRAINT c CHECK (a <> '');",
     "beside a CHECK"),
    ("CREATE TABLE t (a INT); ALTER TABLE t SET TAGS ('k' = 'v', 'flag');", "both tag forms"),
    ("CREATE TABLE c.s.t (a INT);", "three-part"),
    ("CREATE TABLE t (a INT); CREATE VIEW t AS SELECT a FROM t;", "created twice"),
    ("CREATE TABLE t (a INT) LOCATION 's3://x';", "the table clause"),
    ("CREATE OR REPLACE TABLE t (a INT);", "CREATE"),
    ("ALTER TABLE t ADD COLUMN b INT;", "isn't created"),
])
def test_what_it_doesnt_model_or_the_platform_refuses_is_declined(source, reason):
    with pytest.raises(Unsupported, match=reason):
        compile_text(SCHEMA + source)


# Functions, schemas and volumes -------------------------------------------------------------


def test_functions_get_the_platforms_defaults():
    model = compile_text(SCHEMA + """
        CREATE FUNCTION s.f(d DATE, n INT DEFAULT 1 COMMENT 'How many') RETURNS INT
        RETURN extract(YEAR FROM d) + n;
        CREATE FUNCTION g(x DOUBLE) RETURNS TABLE (a BIGINT, b DECIMAL(10, 2))
        RETURN SELECT a, b FROM s.t WHERE b > x;
        CREATE FUNCTION py(s STRING) RETURNS STRING LANGUAGE PYTHON
        AS $$
        return "it's " + s  # a quote SQL wouldn't close
        $$;
    """)
    f, g, py = model["function:s.f"], model["function:s.g"], model["function:s.py"]
    assert (f["data_access"], f["deterministic"], f["returns"]) == ("CONTAINS_SQL", True, "INT")
    assert f["parameters"][1] == {"name": "n", "type": "int", "default": "1",
                                  "comment": "How many"}
    assert g["data_access"] == "READS_SQL_DATA"
    assert g["returns"] == "TABLE(a bigint, b decimal(10,2))"
    assert (py["data_access"], py["deterministic"], py["language"]) == ("NO_SQL", False, "PYTHON")
    assert py["body"] == "return \"it's \" + s  # a quote SQL wouldn't close"


def test_schemas_and_volumes():
    model = compile_text("""
        CREATE SCHEMA s COMMENT 'Things' WITH DBPROPERTIES ('team' = 'data');
        ALTER SCHEMA s SET TAGS ('domain' = 'sales');
        GRANT USE SCHEMA ON SCHEMA s TO `account users`;
        CREATE VOLUME s.files COMMENT 'Raw';
        ALTER VOLUME s.files SET TAGS ('zone' = 'raw');
        GRANT READ VOLUME, WRITE VOLUME ON VOLUME s.files TO `account users`;
    """)
    assert model["schema:s"] == {
        "kind": "schema", "name": "s", "comment": "Things", "properties": {"team": "data"},
        "tags": {"domain": "sales"}, "grants": {"account users": ["USE_SCHEMA"]}, "meta": {}}
    assert model["volume:s.files"]["grants"] == {"account users": ["READ_VOLUME", "WRITE_VOLUME"]}
    assert model["volume:s.files"]["tags"] == {"zone": "raw"}


def test_the_failing_statement_is_named():
    with pytest.raises(Unsupported, match=r"(?s)the type.*in: CREATE TABLE t \(a VARCHAR"):
        compile_text(SCHEMA + "CREATE TABLE t (a VARCHAR(5));")
