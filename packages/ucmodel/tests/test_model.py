import copy

from ucmodel import samples
from ucmodel.model import compare, depends_on

TABLE: dict = {
    "kind": "table",
    "name": "s.t",
    "comment": "c",
    "columns": [
        {"name": "a", "type": "int", "nullable": True, "comment": None, "default": None,
         "generated": None, "identity": None, "mask": None, "tags": {}},
        {"name": "b", "type": "string", "nullable": True, "comment": None, "default": None,
         "generated": None, "identity": None, "mask": None, "tags": {}},
    ],
    "constraints": {},
    "partitioning": [],
    "clustering": None,
    "row_filter": None,
    "properties": {"delta.appendOnly": "true"},
    "tags": {"k": "v"},
    "grants": {},
    "meta": {"table_id": "1"},
}


def paths(desired, live):
    return [(d.key, d.path) for d in compare(desired, live)]


def test_equal_models_have_no_differences():
    assert compare({"table:s.t": TABLE}, {"table:s.t": copy.deepcopy(TABLE)}) == []


def test_meta_is_ignored():
    live = copy.deepcopy(TABLE)
    live["meta"] = {"table_id": "2", "owner": "someone"}
    assert compare({"table:s.t": TABLE}, {"table:s.t": live}) == []


def test_properties_are_declared_only():
    live = copy.deepcopy(TABLE)
    live["properties"]["delta.enableDeletionVectors"] = "true"  # undeclared: ignored
    assert compare({"table:s.t": TABLE}, {"table:s.t": live}) == []
    live["properties"]["delta.appendOnly"] = "false"
    assert paths({"table:s.t": TABLE}, {"table:s.t": live}) == [
        ("table:s.t", "properties.delta.appendOnly")]


def test_clustering_counts_only_when_declared():
    live = copy.deepcopy(TABLE)
    live["clustering"] = {"auto": True}
    assert compare({"table:s.t": TABLE}, {"table:s.t": live}) == []
    desired = copy.deepcopy(TABLE)
    desired["clustering"] = {"columns": ["a"]}
    assert paths({"table:s.t": desired}, {"table:s.t": live}) == [("table:s.t", "clustering")]


def test_columns_tags_and_presence():
    live = copy.deepcopy(TABLE)
    live["columns"].reverse()
    live["columns"][0]["comment"] = "new"
    live["tags"] = {}
    assert paths({"table:s.t": TABLE}, {"table:s.t": live}) == [
        ("table:s.t", "columns"), ("table:s.t", "columns.b.comment"), ("table:s.t", "tags.k")]
    diffs = compare({"table:s.t": TABLE}, {"table:s.u": TABLE})
    assert [str(d) for d in diffs] == ["table:s.t: missing from the target",
                                      "table:s.u: not in the source"]


def test_view_text_is_compared_as_tokens():
    view = {"kind": "view", "definition": "SELECT a FROM s.t", "meta": {}}
    reformatted = {**view, "definition": "select a\n  from S.T"}
    changed = {**view, "definition": "SELECT a FROM s.t WHERE a > 0"}
    assert compare({"view:s.v": view}, {"view:s.v": reformatted}) == []
    assert paths({"view:s.v": view}, {"view:s.v": changed}) == [("view:s.v", "definition")]


def test_python_bodies_are_compared_exactly():
    fn = {"kind": "function", "language": "PYTHON", "body": "return x", "meta": {}}
    assert paths({"function:s.f": fn}, {"function:s.f": {**fn, "body": "return  x"}}) == [
        ("function:s.f", "body")]


def test_depends_on_reads_bodies_and_definitions():
    live = samples.model("roundtrip")
    orders, customers = "table:roundtrip.orders", "table:roundtrip.customers"
    assert depends_on("view:roundtrip.customer_orders",
                      live["view:roundtrip.customer_orders"]) >= {customers, orders}
    assert depends_on("function:roundtrip.big_orders", live["function:roundtrip.big_orders"]) \
        >= {orders}
    assert depends_on(customers, live[customers]) == {
        "function:roundtrip.mask_email", "function:roundtrip.visible_region"}
