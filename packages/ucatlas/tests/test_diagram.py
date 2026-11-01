"""diagram.py, offline: the drawings' Graphviz source, and the site where Graphviz is installed."""

import copy
import re
import shutil
from collections import Counter
from datetime import UTC, datetime
from html import escape

import pytest

from ucatlas import diagram
from ucatlas.diagram import DiagramError, dot_source, edges, lineage_source, svg, write_site
from ucmodel import samples
from ucmodel.lineage import Edge, Lineage

SEEN = "2026-10-31 04:19:00.0"
# Lineage for the round trip: a view's reads, a load from outside the catalog,
# and column lineage into the view.
LINEAGE = Lineage(
    tables=[Edge("elsewhere.raw.orders", "roundtrip.orders", SEEN),
            Edge("roundtrip.customers", "roundtrip.customer_orders", SEEN),
            Edge("roundtrip.orders", "roundtrip.customer_orders", SEEN)],
    columns=[Edge("roundtrip.customers.customer_id", "roundtrip.customer_orders.customer_id",
                  SEEN),
             Edge("roundtrip.orders.order_id", "roundtrip.customer_orders.orders", SEEN)])
HAS_DOT = shutil.which("dot") is not None
EDGE = re.compile(r'^  "([^"]+)"(?::"[cr]\d+":[ew])? -> "([^"]+)"(?::"[cr]\d+":[ew])? \[(.*)\];$',
                  re.MULTILINE)


def load(name: str) -> dict:
    return samples.model(name)


@pytest.fixture
def model() -> dict:
    return load("roundtrip")


def box(source: str, name: str) -> str:
    return next(line for line in source.splitlines() if line.startswith(f'  "{name}" [label=<'))


def missing_keys(model: dict, schema: str, source: str) -> list[str]:
    """The foreign keys of the schema's tables that the drawing has no edge for."""
    drawn = Counter((a, b) for a, b, attrs in EDGE.findall(source) if "crow" in attrs)
    missing = []
    for obj in model.values():
        if obj["kind"] == "table" and obj["name"].startswith(f"{schema}."):
            for name, fk in diagram.foreign_keys(obj):
                pair = (fk["references"], obj["name"])
                if drawn[pair]:
                    drawn[pair] -= 1
                else:
                    missing.append(f"{obj['name']}.{name}")
    return missing


def two_schemas(model: dict) -> dict:
    """The round trip with customers moved to a crm schema."""
    m = copy.deepcopy(model)
    customers = m.pop("table:roundtrip.customers")
    m["table:crm.customers"] = {**customers, "name": "crm.customers"}
    m["schema:crm"] = {**m["schema:roundtrip"], "name": "crm", "comment": None}
    m["table:roundtrip.orders"]["constraints"]["orders_customers_fk"]["references"] = (
        "crm.customers")
    view = m["view:roundtrip.customer_orders"]
    view["definition"] = view["definition"].replace("roundtrip.customers", "crm.customers")
    return m


def missing_lineage(lineage: Lineage, names: set[str], source: str) -> list[str]:
    """The table edges that reach one of `names` and that the graph doesn't draw."""
    drawn = {(a, b) for a, b, _ in EDGE.findall(source)}
    return [f"{e.source} -> {e.target}" for e in lineage.tables
            if (e.source in names or e.target in names) and (e.source, e.target) not in drawn]


def test_every_table_and_view_is_a_box_with_a_row_per_column(model):
    source = dot_source(model, "roundtrip")
    drawn = [o for o in model.values() if o["kind"] in ("table", "view")]
    assert len(drawn) == 4
    for obj in drawn:
        assert box(source, obj["name"]).count(' port="c') == len(obj["columns"]), obj["name"]
    assert "Line Number" in box(source, "roundtrip.order_lines")


def test_every_foreign_key_is_an_edge_from_the_parents_column_to_the_childs(model):
    source = dot_source(model, "roundtrip")
    assert missing_keys(model, "roundtrip", source) == []
    # customer_id is customers' first column and orders' third. The edge leaves
    # the right of the parent's row and reaches the left of the child's.
    assert ('  "roundtrip.customers":"r0":e -> "roundtrip.orders":"c2":w [dir=both '
            'arrowtail=tee arrowhead=crow') in source


def test_the_key_check_catches_a_drawing_without_the_edge(model):
    """The control: a drawing that leaves out a foreign key must fail the check above."""
    source = dot_source(model, "roundtrip")
    without = "\n".join(line for line in source.splitlines() if "crow" not in line)
    assert missing_keys(model, "roundtrip", without) == ["roundtrip.orders.orders_customers_fk"]


def test_a_view_has_an_edge_from_each_table_or_view_it_reads(model):
    reads = [(a, b) for a, b, fk in edges(model, "roundtrip") if fk is None]
    assert reads == [("roundtrip.customers", "roundtrip.customer_orders"),
                     ("roundtrip.orders", "roundtrip.customer_orders")]
    lineage = load("probe_lineage")
    assert [(a, b) for a, b, fk in edges(lineage, "probe_lineage") if fk is None] == [
        ("probe_lineage.customer_totals", "probe_lineage.big_customers")]


def test_keys_and_masks_are_marked(model):
    source = dot_source(model, "roundtrip")
    assert '>customer_id</td><td align="left">bigint <font color="#777777">not null</font>' \
           '</td><td align="left" port="r0">PK</td>' in box(source, "roundtrip.customers")
    assert '<td align="left" port="r1">mask</td>' in box(source, "roundtrip.customers")
    assert '>customer_id</td><td align="left">bigint</td><td align="left" port="r2">FK</td>' in (
        box(source, "roundtrip.orders"))
    view = box(source, "roundtrip.customer_orders")
    assert "<b>roundtrip.customer_orders (view)</b>" in view and ' port="r' not in view


def test_names_types_and_comments_are_escaped(model):
    model["table:roundtrip.customers"]["comment"] = 'Say "hi" & <bye>'
    model["table:roundtrip.customers"]["columns"][1]["type"] = "map<string, array<int>>"
    source = dot_source(model, "roundtrip")
    assert 'tooltip="Say &quot;hi&quot; &amp; &lt;bye&gt;"' in source
    assert "map&lt;string, array&lt;int&gt;&gt;" in source
    if HAS_DOT:
        assert "<svg" in svg(source)


def test_a_long_type_is_cut_in_the_drawing():
    long = "struct<" + ", ".join(f"field_{i}: string" for i in range(10)) + ">"
    obj = {"kind": "table", "name": "s.t", "comment": None, "constraints": {},
           "columns": [{"name": "c", "type": long, "comment": None, "mask": None}]}
    model = {"schema:s": {"kind": "schema", "name": "s", "comment": None}, "table:s.t": obj}
    assert f"{escape(long[:diagram.WIDEST_TYPE - 1])}…</td>" in dot_source(model, "s")


def test_the_drawing_does_not_depend_on_the_models_order(model):
    assert dot_source(dict(reversed(model.items())), "roundtrip") == dot_source(model, "roundtrip")


def test_objects_in_another_schema_are_boxes_that_link_to_its_page(model):
    m = two_schemas(model)
    here = dot_source(m, "roundtrip")
    assert '  "crm.customers" [shape=box' in here and 'URL="crm.html#crm.customers"' in here
    assert '  "crm.customers" -> "roundtrip.orders":"c2":w [dir=both' in here
    assert missing_keys(m, "roundtrip", here) == []
    there = dot_source(m, "crm")
    assert '  "crm.customers":"r0":e -> "roundtrip.orders" [dir=both' in there
    assert 'URL="roundtrip.html#roundtrip.customer_orders"' in there


def test_a_parent_outside_the_model_is_a_box_with_no_link(model):
    fk = model["table:roundtrip.orders"]["constraints"]["orders_customers_fk"]
    fk["references"] = "elsewhere.sales.accounts"
    source = dot_source(model, "roundtrip")
    stub = next(line for line in source.splitlines() if line.startswith('  "elsewhere.'))
    assert 'tooltip="not in the model"' in stub and "URL" not in stub


def test_the_lineage_graph_draws_every_edge_that_reaches_the_schema(model):
    source = lineage_source(model, LINEAGE, "roundtrip")
    assert source is not None
    names = {o["name"] for o in model.values() if o["kind"] in ("table", "view")}
    assert missing_lineage(LINEAGE, names, source) == []
    stub = next(line for line in source.splitlines() if line.startswith('  "elsewhere.'))
    assert 'tooltip="not in the model"' in stub
    assert 'tooltip="roundtrip.orders from elsewhere.raw.orders, last 2026-10-31 04:19"' in source


def test_the_lineage_check_catches_a_graph_without_an_edge(model):
    """The control: a graph that leaves out an edge must fail the check above."""
    source = lineage_source(model, LINEAGE, "roundtrip") or ""
    edge = '"elsewhere.raw.orders" ->'
    without = "\n".join(line for line in source.splitlines() if edge not in line)
    names = {o["name"] for o in model.values() if o["kind"] in ("table", "view")}
    assert missing_lineage(LINEAGE, names, without) == [
        "elsewhere.raw.orders -> roundtrip.orders"]


def test_a_schema_no_lineage_reaches_has_no_graph(model):
    assert lineage_source(model, Lineage(), "roundtrip") is None
    elsewhere = Lineage(tables=[Edge("other.a", "other.b", SEEN)])
    assert lineage_source(model, elsewhere, "roundtrip") is None


@pytest.mark.skipif(not HAS_DOT, reason="needs Graphviz's dot")
def test_a_page_with_lineage_has_its_graph_and_where_columns_come_from(model, tmp_path):
    write_site(model, "ucstate_test", tmp_path, datetime(2026, 10, 31, tzinfo=UTC), LINEAGE)
    page = (tmp_path / "roundtrip.html").read_text()
    assert page.count("<svg") == 2 and "<h2>Lineage</h2>" in page
    assert (tmp_path / "roundtrip.lineage.dot").exists()
    view = page[page.index('<h3 id="roundtrip.customer_orders">'):]
    assert '<a href="#roundtrip.orders"><code>roundtrip.orders.order_id</code></a>' in view
    write_site(model, "ucstate_test", tmp_path, datetime(2026, 10, 31, tzinfo=UTC), Lineage())
    assert "No lineage recorded." in (tmp_path / "roundtrip.html").read_text()


@pytest.mark.skipif(not HAS_DOT, reason="needs Graphviz's dot")
def test_the_site_has_an_index_and_a_page_per_schema(model, tmp_path):
    m = two_schemas(model)
    when = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
    assert write_site(m, "ucstate_test", tmp_path, when) == ["crm", "roundtrip"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "crm.dot", "crm.html", "index.html", "roundtrip.dot", "roundtrip.html"]
    index = (tmp_path / "index.html").read_text()
    assert "Read from ucstate_test at 2026-10-31 08:00 UTC." in index
    assert '<a href="roundtrip.html">roundtrip</a>' in index
    assert "2 tables, 1 view, 5 functions, 1 volume" in index
    page = (tmp_path / "roundtrip.html").read_text()
    assert page.count("<svg") == 1 and "<?xml" not in page
    for name in ("roundtrip.orders", "roundtrip.order_lines", "roundtrip.customer_orders",
                 "roundtrip.add_tax", "roundtrip.landing"):
        assert f'<h3 id="{name}">' in page
    assert ('references <a href="crm.html#crm.customers">crm.customers</a>' in page)
    assert "<code>Line Number</code>" in page
    assert "Check <code>amount_positive</code>: <code>amount &gt; 0</code>" in page
    # Read without lineage, so the page has no lineage section and no From column.
    assert "Lineage" not in page and "<th>From</th>" not in page


@pytest.mark.skipif(not HAS_DOT, reason="needs Graphviz's dot")
def test_a_schema_with_nothing_to_draw_says_so(model, tmp_path):
    for key in [k for k, o in model.items() if o["kind"] in ("table", "view")]:
        del model[key]
    write_site(model, "ucstate_test", tmp_path, datetime(2026, 10, 31, tzinfo=UTC))
    page = (tmp_path / "roundtrip.html").read_text()
    assert "No tables or views." in page and "<svg" not in page
    assert not (tmp_path / "roundtrip.dot").exists()


@pytest.mark.skipif(not HAS_DOT, reason="needs Graphviz's dot")
def test_a_drawing_dot_cannot_read_is_an_error():
    with pytest.raises(DiagramError, match="dot: "):
        svg("digraph {")


def test_without_graphviz_the_error_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(DiagramError, match="brew install graphviz"):
        svg("digraph {}")
