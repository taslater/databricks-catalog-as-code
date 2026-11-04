"""The explorer with query history: statements that ran give writers and lineage, as SQL
files do, and say how often they ran, by whom and from where; reads give usage."""

from datetime import UTC, datetime

import pytest

from ucatlas import explore
from ucmodel import samples
from ucmodel.lineage import Lineage, Use, Write

TAKEN = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)


def wrote(target: str, sql: str, last: str = "2026-11-04T17:50:00Z", **more) -> Write:
    return Write(target, sql, more.get("type", "INSERT"), more.get("runs", 1), last,
                 "owner@example.com", more.get("client", "Databricks CLI"),
                 more.get("source", {}), more.get("of", 1), more.get("reads", True))


@pytest.fixture(scope="module")
def model():
    return samples.model("kinds")


def graph(model, writes=None, uses=None):
    return explore.graph(model, "ucstate_test", TAKEN, Lineage([], [], writes, uses))


def nodes(g) -> dict[str, dict]:
    return {n["id"]: n for n in g["nodes"]}


def test_a_statement_that_ran_writes_its_object_with_column_lineage(model):
    g = graph(model, [wrote(
        "kinds.orders", "INSERT INTO ucstate_test.kinds.orders "
        "SELECT order_id, region, amount, placed_at FROM ucstate_test.kinds.orders_feed "
        "WHERE amount > 0", runs=24, client="Jobs", of=3,
        source={"job_id": "11", "job_run_id": "12"})])
    writer = next(w for w in nodes(g)["kinds.orders"]["writers"] if w["ran"])
    assert writer["via"] == "history:kinds.orders:1"
    assert writer["ran"] == {"type": "INSERT", "runs": 24, "last": "2026-11-04T17:50:00Z",
                             "by": "owner@example.com", "client": "Jobs",
                             "source": {"job_id": "11", "job_run_id": "12"}, "of": 3}
    assert writer["filters"] == ["amount > 0"]
    edges = {(e["s"], e["t"]) for e in g["column_edges"] if e["via"] == writer["via"]}
    assert ("kinds.orders_feed.amount", "kinds.orders.amount") in edges
    assert ("kinds.orders_feed", "kinds.orders") in {(e["s"], e["t"]) for e in g["table_edges"]}
    assert g["meta"]["history"] is True


def test_a_statement_that_ran_is_a_write_even_with_no_lineage_edge(model):
    g = graph(model, [wrote("kinds.orders", "INSERT INTO kinds.orders VALUES (9, 'x', 1, NULL)",
                            last="2026-11-04T17:55:00Z")])
    assert nodes(g)["kinds.orders"]["written"] == "2026-11-04T17:55:00Z"
    assert nodes(g)["kinds.top_regions"]["written"] is None  # a view isn't written


def test_a_view_the_catalog_defines_keeps_its_definition_over_its_create(model):
    g = graph(model, [wrote("kinds.top_regions", "CREATE VIEW kinds.top_regions AS "
                            "SELECT region FROM kinds.region_totals", type="CREATE")])
    assert [w["via"] for w in nodes(g)["kinds.top_regions"]["writers"]] == ["kinds.top_regions"]


def test_a_statement_that_reads_as_writing_another_object_is_a_gap(model):
    g = graph(model, [wrote("kinds.orders", "INSERT INTO kinds.orders_feed "
                            "SELECT order_id, region, amount FROM kinds.orders")])
    gap = next(gp for gp in g["gaps"] if gp["via"] == "history:kinds.orders:1")
    assert gap["errors"] == ["it reads as writing kinds.orders_feed, but the lineage says it "
                             "wrote kinds.orders"]
    assert not [e for e in g["column_edges"] if e["via"] == "history:kinds.orders:1"]


def test_each_objects_statements_are_numbered_in_their_order(model):
    g = graph(model, [wrote("kinds.orders", "INSERT INTO kinds.orders SELECT * FROM kinds.orders"),
                      wrote("kinds.orders", "DELETE FROM kinds.orders WHERE order_id = 1",
                            type="DELETE"),
                      wrote("kinds.orders_feed", "INSERT INTO kinds.orders_feed "
                            "SELECT order_id, region, amount FROM kinds.orders")])
    vias = sorted(w["via"] for n in g["nodes"] for w in n["writers"] if w["ran"])
    assert vias == ["history:kinds.orders:1", "history:kinds.orders:2",
                    "history:kinds.orders_feed:1"]


def test_a_statement_with_no_lineage_to_read_still_writes_its_object(model):
    g = graph(model, [wrote("kinds.orders", "DELETE FROM kinds.orders WHERE order_id = 1",
                            type="DELETE", runs=3)])
    writer = next(w for w in nodes(g)["kinds.orders"]["writers"] if w["ran"])
    assert (writer["sql"], writer["ran"]["runs"], writer["errors"]) == (
        "DELETE FROM kinds.orders WHERE order_id = 1", 3, [])
    assert not g["gaps"]


def test_reads_are_each_objects_usage(model):
    g = graph(model, [], [Use("kinds.orders", 7, 2, "2026-11-04T17:00:00Z")])
    assert nodes(g)["kinds.orders"]["uses"] == {"queries": 7, "people": 2,
                                                "last": "2026-11-04T17:00:00Z"}
    assert nodes(g)["kinds.region_totals"]["uses"] is None


def test_without_history_the_page_says_so(model):
    g = explore.graph(model, "ucstate_test", TAKEN, Lineage([], []))
    assert g["meta"]["history"] is False
    assert all(w["ran"] is None for n in g["nodes"] for w in n["writers"])


UNPARSED = "CREATE TABLE kinds.orders (email STRING MASK kinds.mask_a, region STRING)"


def test_sql_sqlglot_cant_parse_is_no_gap_when_the_statement_read_no_table(model):
    g = graph(model, [wrote("kinds.orders", UNPARSED, type="CREATE", reads=False)])
    assert not g["gaps"]
    writer = next(w for w in nodes(g)["kinds.orders"]["writers"] if w["ran"])
    assert (writer["sql"], writer["errors"]) == (UNPARSED, [])


def test_the_control_sql_sqlglot_cant_parse_is_a_gap_when_the_statement_read_a_table(model):
    g = graph(model, [wrote("kinds.orders", UNPARSED, type="CREATE", reads=True)])
    assert [gp["via"] for gp in g["gaps"]] == ["history:kinds.orders:1"]


def test_names_of_jobs_and_pipelines_go_to_the_page(model):
    g = explore.graph(model, "ucstate_test", TAKEN,
                      Lineage([], [], [], [], {"job:11": "nightly", "pipeline:p": "MV-x"}))
    assert g["names"] == {"job:11": "nightly", "pipeline:p": "MV-x"}
    assert nodes(g)["kinds.region_totals"]["pipeline"]  # its pipeline's id, from the model
    assert nodes(g)["kinds.orders"]["pipeline"] is None
