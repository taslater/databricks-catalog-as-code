"""Objects outside the model in the explorer: materialized views, streaming tables and
metric views, from ucmodel's `kinds` sample (`samples/kinds.sql`), with lineage as the
platform recorded it for them."""

import shutil
from datetime import UTC, datetime

import pytest

from ucatlas import explore, layout
from ucatlas import sqllineage as sl
from ucmodel import samples
from ucmodel.lineage import Edge, Lineage

TAKEN = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
# what the platform recorded for kinds.sql: nothing for the metric view, and one column of
# the materialized view's backing table that the view doesn't have
SEEN = "2026-11-04 17:27:42.0"
LINEAGE = Lineage(
    [Edge("kinds.orders", "kinds.orders_feed", SEEN),
     Edge("kinds.orders", "kinds.region_totals", SEEN),
     Edge("kinds.region_totals", "kinds.top_regions", SEEN)],
    [Edge("kinds.orders.amount", "kinds.region_totals.total", SEEN),
     Edge("kinds.orders.amount", "kinds.region_totals.__DECOMP_GENERATED_count_0", SEEN),
     Edge("kinds.orders.region", "kinds.orders_feed.region", SEEN)])


@pytest.fixture(scope="module")
def model():
    return samples.model("kinds")


@pytest.fixture(scope="module")
def g(model):
    return explore.graph(model, "ucstate_test", TAKEN, LINEAGE)


def nodes(g) -> dict[str, dict]:
    return {n["id"]: n for n in g["nodes"]}


def column_edges(g, target: str) -> list[tuple[str, str]]:
    return sorted((e["s"], e["t"]) for e in g["column_edges"] if e["t"].startswith(target + "."))


def test_each_object_is_a_node_of_its_own_kind(g):
    assert {i: n["kind"] for i, n in nodes(g).items()} == {
        "kinds.orders": "table", "kinds.top_regions": "view",
        "kinds.region_totals": "materialized_view", "kinds.orders_feed": "streaming_table",
        "kinds.order_metrics": "metric_view"}
    assert [c["name"] for c in nodes(g)["kinds.region_totals"]["columns"]] == [
        "region", "total", "orders"]


@pytest.mark.parametrize(("type_", "kind"), [
    ("EXTERNAL DELTA", "external_table"), ("FOREIGN", "foreign_table"),
    ("MANAGED PARQUET", "table"),  # a managed table that isn't Delta is still a table
])
def test_other_types_outside_the_model_have_kinds_too(type_, kind):
    assert explore.node_kind({"kind": "unsupported", "type": type_}) == kind


def test_a_materialized_views_and_a_streaming_tables_definitions_give_column_lineage(g):
    assert column_edges(g, "kinds.region_totals") == [
        ("kinds.orders.amount", "kinds.region_totals.total"),
        ("kinds.orders.region", "kinds.region_totals.region")]
    # STREAM(kinds.orders) reads kinds.orders
    assert column_edges(g, "kinds.orders_feed") == [
        ("kinds.orders.amount", "kinds.orders_feed.amount"),
        ("kinds.orders.order_id", "kinds.orders_feed.order_id"),
        ("kinds.orders.region", "kinds.orders_feed.region")]


def test_a_metric_views_measures_come_from_its_sources_columns(g):
    by_target = {e["t"]: e for e in g["column_edges"] if e["t"].startswith("kinds.order_metrics.")}
    assert by_target["kinds.order_metrics.region"]["s"] == "kinds.orders.region"
    revenue = by_target["kinds.order_metrics.revenue"]
    assert (revenue["s"], revenue["agg"]) == ("kinds.orders.amount", True)
    writer = nodes(g)["kinds.order_metrics"]["writers"][0]
    assert writer["sql"].startswith("version: 1.1")  # the YAML, as the catalog holds it
    assert ("kinds.orders", "kinds.order_metrics") in {(e["s"], e["t"]) for e in g["table_edges"]}


def test_a_column_the_object_doesnt_have_is_left_out_of_the_lineage(g):
    assert not [e for e in g["column_edges"] if "__DECOMP" in e["t"]]
    seen = [e for e in g["column_edges"] if e["t"] == "kinds.region_totals.total"]
    assert seen and seen[0]["observed"]  # the control: the real column's edge is kept, as seen


def test_a_materialized_view_and_a_streaming_table_are_written_and_views_arent(g):
    n = nodes(g)
    assert n["kinds.region_totals"]["written"] == "2026-11-04T17:27:42Z"
    assert n["kinds.orders_feed"]["written"] == "2026-11-04T17:27:42Z"
    assert n["kinds.top_regions"]["written"] is None


def test_the_summary_names_every_kind(g):
    assert explore.objects_said(explore.counts(g)) == (
        "1 table, 1 view, 1 materialized view, 1 streaming table, 1 metric view")
    assert explore.objects_said({"table": 2}) == "2 tables, 0 views"


@pytest.mark.skipif(shutil.which("dot") is None, reason="needs Graphviz")
def test_the_map_has_every_object_in_the_catalog(g):
    L = layout.build(g["nodes"], explore.layout_edges(g), tries=1)
    assert set(L["map"]["pos"]) == set(nodes(g))


# metric_query ----------------------------------------------------------------------------

def test_a_metric_view_reads_as_a_select_in_its_columns_order():
    text = ("version: 1.1\nsource: s.orders\nfilter: amount > 0\n"
            "dimensions:\n  - name: Region\n    expr: region\n"
            "measures:\n  - name: revenue\n    expr: SUM(amount)\n")
    assert sl.metric_query(text, ["revenue", "Region", "extra"]) == (
        "SELECT SUM(amount) AS `revenue`, region AS `Region`, NULL AS `extra` "
        "FROM s.orders AS `source` WHERE amount > 0")


def test_a_metric_views_joins_nested_ones_too_and_a_query_source():
    text = ("source: SELECT * FROM s.orders WHERE ok\n"
            "joins:\n"
            "  - name: customers\n    source: s.customers\n"
            "    on: source.customer_id = customers.id\n"
            "    joins:\n      - name: regions\n        source: s.regions\n"
            "        using: [region_id]\n"
            "dimensions:\n  - name: region\n    expr: regions.name\n")
    assert sl.metric_query(text, ["region"]) == (
        "SELECT regions.name AS `region` FROM (SELECT * FROM s.orders WHERE ok) AS `source`"
        " LEFT JOIN s.customers AS `customers` ON source.customer_id = customers.id"
        " LEFT JOIN s.regions AS `regions` USING (region_id)")


def test_a_metric_view_through_a_join_gets_its_lineage(model):
    m = {**model, "table:kinds.regions": {
        "kind": "table", "name": "kinds.regions",
        "columns": [{"name": "region", "type": "string"}, {"name": "label", "type": "string"}]}}
    obj = {"kind": "unsupported", "type": "METRIC_VIEW", "name": "kinds.m",
           "columns": [{"name": "label"}, {"name": "revenue"}],
           "definition": ("source: kinds.orders\njoins:\n  - name: r\n    source: kinds.regions\n"
                          "    on: source.region = r.region\n"
                          "dimensions:\n  - name: label\n    expr: r.label\n"
                          "measures:\n  - name: revenue\n    expr: SUM(source.amount)\n")}
    st = sl._metric("kinds.m", obj, m, "ucstate_test", sl.glot_schema(m))
    assert st is not None and not st.errors
    assert sorted((e.source, e.target) for e in st.columns) == [
        ("kinds.orders.amount", "kinds.m.revenue"), ("kinds.regions.label", "kinds.m.label")]
    assert st.reads == {"kinds.orders", "kinds.regions"}


@pytest.mark.parametrize("text", ["dimensions: [", "just text", "source: [1, 2]"])
def test_yaml_that_isnt_a_metric_views_is_a_gap(model, text):
    obj = {"kind": "unsupported", "type": "METRIC_VIEW", "name": "kinds.m", "columns": [],
           "definition": text}
    st = sl._metric("kinds.m", obj, model, "ucstate_test", sl.glot_schema(model))
    assert st is not None and st.kind == "unread" and st.target == "kinds.m"
    assert st.errors[0].startswith("its YAML can't be read as a metric view's")


def test_a_stream_keeps_its_alias(model):
    st = sl.analyse("f.sql:1", "CREATE STREAMING TABLE kinds.f2 AS SELECT o.order_id "
                    "FROM STREAM(kinds.orders) AS o "
                    "JOIN kinds.region_totals r ON o.region = r.region", model, "ucstate_test")
    assert st is not None and not st.errors
    assert [(e.source, e.target) for e in st.columns] == [
        ("kinds.orders.order_id", "kinds.f2.order_id")]
    assert st.joins == [("kinds.orders.region", "kinds.region_totals.region")]


def test_a_files_create_of_an_object_the_catalog_defines_is_skipped(model):
    sql = ("CREATE OR REFRESH MATERIALIZED VIEW kinds.region_totals AS "
           "SELECT region FROM kinds.orders;")
    sts = sl.statements(model, "ucstate_test", [("mv.sql", sql)])
    assert [st.via for st in sts if st.target == "kinds.region_totals"] == ["kinds.region_totals"]
