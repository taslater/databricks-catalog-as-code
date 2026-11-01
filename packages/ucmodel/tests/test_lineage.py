"""lineage.py, offline: responses in the shapes the lineage API gave on 2026-10-31."""

from types import SimpleNamespace

from ucmodel.lineage import API, Edge, Lineage, column_edges, read, table_edges
from ucmodel.workspace import Workspace

SEEN = "2026-10-31 04:19:00.0"


def info(name: str, schema: str = "shop", catalog: str = "ucstate_prod",
         kind: str = "TABLE", seen: str = SEEN) -> dict:
    return {"tableInfo": {"name": name, "catalog_name": catalog, "schema_name": schema,
                          "table_type": kind, "lineage_timestamp": seen}}


def col(table: str, name: str, kind: str = "TABLE", seen: str = SEEN) -> dict:
    return {"name": name, "catalog_name": "ucstate_prod", "schema_name": "shop",
            "table_name": table, "table_type": kind, "lineage_timestamp": seen}


class FakeWorkspace(Workspace):
    """Answers lineage calls from `answers`, keyed by table or by table and column."""

    def __init__(self, answers: dict[str, dict]):
        super().__init__(SimpleNamespace())  # type: ignore[arg-type]
        self.answers = answers
        self.calls: list[str] = []

    def get(self, path, **query):
        key = ".".join(v for k, v in sorted(query.items()))
        self.calls.append(f"{path.removeprefix(API)} {key}")
        return self.answers.get(key, {})


def test_a_table_response_gives_an_edge_for_each_upstream_and_downstream():
    response = {"upstreams": [info("orders")],
                "downstreams": [info("order_totals", kind="PERSISTED_VIEW")]}
    assert table_edges("ucstate_prod", "shop.order_lines", response) == [
        Edge("shop.orders", "shop.order_lines", SEEN),
        Edge("shop.order_lines", "shop.order_totals", SEEN)]


def test_a_name_in_another_catalog_keeps_its_catalog():
    response = {"downstreams": [info("t", "sc_transition", "ucstate_rehearsal")]}
    assert table_edges("ucstate_test", "sc_transition.t", response) == [
        Edge("sc_transition.t", "ucstate_rehearsal.sc_transition.t", SEEN)]


def test_an_entry_that_isnt_a_table_is_skipped():
    """Notebooks, jobs and paths aren't read yet (docs/diagrams.md, section 7)."""
    response = {"upstreams": [{"notebookInfos": [{"notebook_id": 1}]}, info("orders")]}
    assert table_edges("ucstate_prod", "shop.order_lines", response) == [
        Edge("shop.orders", "shop.order_lines", SEEN)]


def test_a_column_response_gives_an_edge_for_each_column():
    response = {"upstream_cols": [col("order_lines", "quantity"), col("order_lines", "unit_price")]}
    assert column_edges("ucstate_prod", "shop.order_totals", "total", response) == [
        Edge("shop.order_lines.quantity", "shop.order_totals.total", SEEN),
        Edge("shop.order_lines.unit_price", "shop.order_totals.total", SEEN)]


def test_read_keeps_one_edge_per_pair_the_latest_and_no_self_edges():
    model = {
        "table:shop.orders": {"kind": "table", "name": "shop.orders",
                              "columns": [{"name": "order_id"}]},
        "view:shop.order_totals": {"kind": "view", "name": "shop.order_totals",
                                   "columns": [{"name": "order_id"}]},
        "function:shop.f": {"kind": "function", "name": "shop.f"},
    }
    later = "2026-10-31 09:00:00.0"
    ws = FakeWorkspace({
        # The same edge from both ends, seen at different times, and a self-edge
        # such as CREATE OR REPLACE ... AS SELECT from the same table leaves.
        "ucstate_prod.shop.orders": {"upstreams": [info("orders")],
                                 "downstreams": [info("order_totals", seen=later)]},
        "ucstate_prod.shop.order_totals": {"upstreams": [info("orders")]},
        "order_id.ucstate_prod.shop.order_totals": {
            "upstream_cols": [col("orders", "order_id")]},
    })
    found = read(ws, "ucstate_prod", model)
    assert found.tables == [Edge("shop.orders", "shop.order_totals", later)]
    assert found.columns == [Edge("shop.orders.order_id", "shop.order_totals.order_id", SEEN)]
    assert sorted(ws.calls) == [
        "/column-lineage order_id.ucstate_prod.shop.order_totals",
        "/column-lineage order_id.ucstate_prod.shop.orders",
        "/table-lineage ucstate_prod.shop.order_totals",
        "/table-lineage ucstate_prod.shop.orders"]


def test_upstream_columns_are_the_edges_into_a_column():
    lineage = Lineage(columns=[Edge("s.a.x", "s.b.y", SEEN), Edge("s.b.y", "s.c.z", SEEN)])
    assert lineage.upstream_columns("s.b.y") == [Edge("s.a.x", "s.b.y", SEEN)]
