"""sqllineage.py: column lineage from SQL, with sqlglot, over the synthetic atlas_demo."""

import pytest

import demo
from ucatlas import sqllineage as sl


@pytest.fixture(scope="module")
def model():
    return demo.model()


@pytest.fixture(scope="module")
def by_via(model):
    return {st.via: st for st in sl.statements(model, demo.CATALOG, demo.sql_files())}


def edges(st, column=None):
    return sorted((e.source, e.target) for e in st.columns
                  if column is None or e.target.endswith("." + column))


def analyse(model, sql) -> sl.Statement:
    st = sl.analyse("t.sql:1", sql, model, demo.CATALOG)
    assert st is not None
    return st


def test_every_view_and_every_statement_that_writes_is_read(by_via):
    views = [v for v in by_via if ":" not in v]
    assert views == ["exports.finance_ledger", "exports.partner_feed", "marketing.customer_360",
                     "sales.top_products"]
    assert by_via["build_core.sql:1"].kind == "merge"
    assert by_via["build_core.sql:2"].kind == "insert"
    assert len([v for v in by_via if v.startswith("ingest_web.sql:")]) == 4


def test_an_expression_is_kept_with_every_column_it_reads(by_via):
    st = by_via["build_sales.sql:2"]  # sales.daily_sales
    assert edges(st, "revenue") == [(f"core.order_lines.{c}", "sales.daily_sales.revenue")
                                    for c in ("quantity", "tax", "unit_price")]
    revenue = [e for e in st.columns if e.target.endswith(".revenue")]
    assert {tuple(e.steps) for e in revenue} == {("SUM(l.quantity * l.unit_price + l.tax)",)}
    assert all(e.aggregate for e in revenue)
    # the control: a plain copy has no steps and isn't an aggregate
    sku = [e for e in st.columns if e.target == "sales.daily_sales.sku"]
    assert [(e.source, e.steps, e.aggregate) for e in sku] == [("core.order_lines.sku", [], False)]


def test_each_branch_of_a_union_keeps_its_own_expression(by_via):
    st = by_via["build_core.sql:2"]  # core.orders, web UNION ALL store
    keys = {e.source: e.steps for e in st.columns if e.target == "core.orders.order_key"}
    assert keys == {"raw_web.orders.order_id": ["CONCAT('web-', o.order_id)"],
                    "raw_pos.transactions.txn_id": ["CONCAT('pos-', t.txn_id)"]}


def test_a_merge_follows_its_insert_and_update_clauses(by_via):
    st = by_via["build_core.sql:1"]  # MERGE INTO core.customers
    assert st.target == "core.customers"
    assert edges(st, "first_seen") == [("raw_crm.contacts.created_at", "core.customers.first_seen"),
                                       ("raw_web.accounts.signup_at", "core.customers.first_seen")]
    assert ("raw_crm.contacts.region", "core.customers.region") in edges(st)


def test_a_table_read_only_to_join_or_filter_is_read_but_feeds_no_column(by_via):
    st = by_via["build_sales.sql:2"]  # joins core.orders only for its date, store and channel
    assert st.reads == {"core.order_lines", "core.orders"}
    assert st.filters == ["o.channel = 'store'"]
    assert st.joins == [("core.order_lines.order_key", "core.orders.order_key")]


def test_ctes_before_an_insert_are_followed(by_via):
    st = by_via["finance_close.sql:1"]  # INSERT ... WITH web AS (...), store AS (...)
    assert ("raw_pos.transactions.total_gross", "finance.recognized_revenue.gross") in edges(st)
    assert ("raw_web.order_items.price", "finance.recognized_revenue.gross") in edges(st)


def test_files_read_with_read_files_are_a_source(by_via):
    st = by_via["ingest_web.sql:1"]
    assert st.reads == {"files:/Volumes/landing/web/orders/"}
    assert ("files:/Volumes/landing/web/orders/.order_id", "raw_web.orders.order_id") in edges(st)


def test_copy_into_reads_its_files(model):
    st = analyse(model, "COPY INTO raw_pos.refunds FROM '/Volumes/landing/pos/refunds/' "
                        "FILEFORMAT = CSV")
    assert (st.kind, st.target, st.reads) == ("copy", "raw_pos.refunds",
                                              {"files:/Volumes/landing/pos/refunds/"})


def test_a_statement_sqlglot_cant_parse_is_a_gap_on_what_it_writes(by_via):
    # dim_contacts.sql is the known-bad control: APPLY CHANGES doesn't parse
    st = by_via["dim_contacts.sql:1"]
    assert (st.kind, st.target) == ("unread", "raw_crm.contacts")
    assert st.errors and st.errors[0].startswith("sqlglot can't parse it")
    assert not st.columns
    assert [v for v, s in by_via.items() if s.errors] == ["dim_contacts.sql:1"]


@pytest.mark.parametrize("sql", [
    "GRANT SELECT ON TABLE core.orders TO `owner@example.com`",
    "OPTIMIZE core.orders",
    "SELECT 1",
    "USE SCHEMA core",
    "CREATE SCHEMA IF NOT EXISTS scratch",
])
def test_a_statement_that_writes_no_object_is_neither_lineage_nor_a_gap(model, sql):
    assert sl.analyse("t.sql:1", sql, model, demo.CATALOG) is None


def test_a_name_in_this_catalog_is_the_models_and_another_catalogs_stays_outside(model):
    st = analyse(model, "INSERT INTO other_cat.core.orders SELECT * FROM atlas_demo.core.orders")
    assert st.reads == {"core.orders"}
    assert st.target == "other_cat.core.orders"
    # `*` into a table the model doesn't have: a column each, named after qualifying
    assert ("core.orders.channel", "other_cat.core.orders.channel") in edges(st)
    assert not any(e.target.endswith(".*") for e in st.columns)


def test_use_schema_resolves_bare_names_to_the_end_of_the_file(model):
    # core and raw_pos both have `refunds`: only a USE says which
    files = [("a.sql", "USE SCHEMA raw_pos;\nINSERT INTO core.refunds SELECT * FROM refunds;"),
             ("b.sql", "INSERT INTO core.refunds SELECT * FROM refunds;")]
    st = {s.via: s for s in sl.statements(model, demo.CATALOG, files)}
    assert (st["a.sql:2"].target, st["a.sql:2"].reads) == ("core.refunds", {"raw_pos.refunds"})
    assert st["b.sql:1"].reads == {"refunds"}  # the control: the USE doesn't carry into b.sql


def test_a_bare_name_resolves_only_when_one_schema_has_it(model):
    st = analyse(model, "CREATE TABLE core.x AS SELECT s.session_id FROM sessions AS s")
    assert st.reads == {"raw_web.sessions"}  # only raw_web has `sessions`
    assert edges(st) == [("raw_web.sessions.session_id", "core.x.session_id")]
    # the control: two schemas have `orders`, so it isn't guessed
    st = analyse(model, "CREATE TABLE core.x AS SELECT order_key FROM orders")
    assert st.reads == {"orders"}
    assert st.errors and "Ambiguous" in st.errors[0]


def test_a_view_the_catalog_defines_wins_over_a_files_create_view(model):
    files = [("v.sql", "CREATE OR REPLACE VIEW sales.top_products AS SELECT 1 AS x;")]
    views = [s for s in sl.statements(model, demo.CATALOG, files)
             if s.target == "sales.top_products"]
    assert [s.via for s in views] == ["sales.top_products"]


def test_a_file_that_cant_be_split_is_a_gap_and_the_rest_are_read(model, monkeypatch):
    import ucmodel.sqltext

    real = ucmodel.sqltext.split_statements

    def split(text):
        if "broken" in text:
            raise ValueError("unterminated string")
        return real(text)
    monkeypatch.setattr(ucmodel.sqltext, "split_statements", split)
    st = sl.statements(model, demo.CATALOG, [("x.sql", "-- broken"), *demo.sql_files()[:1]])
    unread = [s for s in st if s.kind == "unread"]
    assert [(s.via, s.errors) for s in unread] == [
        ("x.sql", ["couldn't split it into statements: unterminated string"])]
    assert any(s.via.startswith("build_core.sql:") for s in st)
