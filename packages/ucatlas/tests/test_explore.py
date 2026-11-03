"""explore.py and layout.py: the explorer's data, its layouts and its page, over the
synthetic atlas_demo. Tests that lay out run `dot`, and skip without Graphviz."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import demo
from ucatlas import explore, layout

needs_dot = pytest.mark.skipif(shutil.which("dot") is None, reason="needs Graphviz")
TESTS = Path(__file__).parent


@pytest.fixture(scope="module")
def snap():
    return demo.take()


@pytest.fixture(scope="module")
def g(snap):
    return explore.graph(snap.model, snap.catalog, snap.taken_at, snap.lineage, demo.sql_files(),
                         snapshot=snap.hash[:12])


def run(script: str, seed: str, *args: str) -> subprocess.CompletedProcess:
    """`script` in a new Python with this hash seed, and this folder on its path."""
    path = os.pathsep.join(p for p in (str(TESTS), os.environ.get("PYTHONPATH")) if p)
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": path}
    return subprocess.run([sys.executable, "-c", script, *args], capture_output=True, text=True,
                          check=True, env=env)


def node(g, nid):
    return next(n for n in g["nodes"] if n["id"] == nid)


# The data -----------------------------------------------------------------------------------


def test_every_object_is_a_node_and_what_they_read_from_outside_gets_one_too(g, snap):
    kinds = {n["id"]: n["kind"] for n in g["nodes"]}
    inside = {o["name"] for o in snap.model.values() if o["kind"] in ("table", "view")}
    assert inside <= set(kinds)
    assert sorted(k for k in kinds if kinds[k] == "files") == [
        f"files:/Volumes/landing/{p}/" for p in
        ("crm/contacts", "pos/lines", "pos/refunds", "pos/transactions", "web/accounts",
         "web/order_items", "web/orders", "web/sessions")]
    files = node(g, "files:/Volumes/landing/web/orders/")
    # its columns, as the statements that read it name them
    assert {c["name"] for c in files["columns"]} >= {"order_id", "placed_at"}


def test_schemas_the_snapshot_read_come_first_and_the_rest_are_marked_outside(g):
    schemas = [(s["name"], s["outside"]) for s in g["schemas"]]
    assert [name for name, outside in schemas if not outside] == [
        "core", "exports", "finance", "marketing", "raw_crm", "raw_pos", "raw_web", "ref", "sales"]
    assert ("(files)", True) in schemas


def test_an_edge_the_sql_makes_and_the_lineage_saw_is_marked_observed(g):
    by = {(e["s"], e["t"]): e for e in g["column_edges"]}
    seen = by["sales.order_facts.net_amount", "marketing.campaign_attribution.attributed_revenue"]
    assert (seen["static"], seen["observed"]) == (True, "2026-11-03T08:20:00Z")
    assert seen["steps"] == ["SUM(f.net_amount)"]
    # the control: an edge the SQL makes that the lineage didn't see
    assert by["raw_web.sessions.campaign_code",
              "marketing.campaign_attribution.campaign_code"]["observed"] is None


def test_an_edge_only_the_lineage_saw_is_kept_and_says_so(g):
    pair = ("core.customers.customer_id", "marketing.customer_list.customer_id")
    e = next(e for e in g["column_edges"] if (e["s"], e["t"]) == pair)
    assert (e["via"], e["static"], e["steps"]) == ("lineage", False, [])
    tables = {(t["s"], t["t"]): t for t in g["table_edges"]}
    assert tables["core.customers", "marketing.customer_list"]["static"] is False
    assert tables["raw_crm.contacts", "marketing.customer_list"]["static"] is True


def test_a_table_read_only_to_join_is_a_join_edge(g):
    tables = {(t["s"], t["t"]): t["role"] for t in g["table_edges"]}
    assert tables["core.orders", "sales.daily_sales"] == "data"  # its date and store
    assert tables["raw_web.accounts", "marketing.customer_list"] == "join"


def test_last_written_is_the_latest_write_the_lineage_saw(g, snap):
    seen = [e.seen for e in snap.lineage.tables if e.target == "sales.order_facts"]
    assert node(g, "sales.order_facts")["written"] == explore._seen(max(seen))
    assert node(g, "sales.top_products")["written"] is None  # a view is computed when read


def test_declared_keys_and_the_joins_the_sql_makes_are_both_joins(g):
    joins = {(j["a"], j["b"]): j for j in g["joins"]}
    j = joins["core.order_lines.order_key", "core.orders.order_key"]
    assert j["code"] and j["declared"]
    assert any(f["s"] == "core.order_lines.order_key" for f in g["fk_edges"])


def test_the_statement_sqlglot_cant_read_is_a_gap_and_its_target_is_marked(g):
    gaps = [(x["via"], x["target"]) for x in g["gaps"]]
    assert gaps == [("dim_contacts.sql:1", "raw_crm.contacts")]
    assert any(w["errors"] for w in node(g, "raw_crm.contacts")["writers"])


def test_without_lineage_every_edge_comes_from_sql(snap):
    g = explore.graph(snap.model, snap.catalog, snap.taken_at, None, demo.sql_files())
    assert g["meta"]["lineage"] is False
    assert all(e["static"] for e in g["column_edges"] + g["table_edges"])
    assert all(n["written"] is None for n in g["nodes"])


def test_the_same_snapshot_gives_the_same_data_whatever_the_hash_seed():
    # sqlglot orders some of what it finds by a set: the graph mustn't
    script = ("import json, demo; from ucatlas import explore; s = demo.take(); "
              "print(json.dumps(explore.graph(s.model, s.catalog, s.taken_at, s.lineage, "
              "demo.sql_files())))")
    out = [run(script, seed).stdout for seed in ("1", "2")]
    assert out[0] == out[1]


def test_sql_files_takes_files_and_folders(tmp_path):
    (tmp_path / "jobs" / "deep").mkdir(parents=True)
    (tmp_path / "jobs" / "b.sql").write_text("SELECT 2")
    (tmp_path / "jobs" / "deep" / "a.sql").write_text("SELECT 1")
    (tmp_path / "jobs" / "notes.md").write_text("not SQL")
    (tmp_path / "one.sql").write_text("SELECT 3")
    got = explore.sql_files([tmp_path / "jobs", tmp_path / "one.sql"])
    assert got == [("b.sql", "SELECT 2"), ("deep/a.sql", "SELECT 1"), ("one.sql", "SELECT 3")]


# The page -----------------------------------------------------------------------------------


def inlined(page: str) -> dict:
    start = page.index('{"meta":')
    return json.JSONDecoder().raw_decode(page[start:])[0]


def test_the_page_inlines_the_graph_and_text_cant_close_its_script(g, tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "build", lambda nodes, edges, tries=None: {})
    g = json.loads(json.dumps(g))
    node(g, "core.orders")["comment"] = "One row per order </script><b>bold</b>"
    page = explore.write(g, tmp_path).read_text()
    assert "/*GRAPH*/null" not in page
    assert "</script><b>" not in page
    template = (explore.ASSETS / explore.PAGE).read_text()
    assert page.count("</script>") == template.count("</script>")  # the data added none
    assert node(inlined(page), "core.orders")["comment"].endswith("</script><b>bold</b>")
    # the libraries and their licenses go beside it, so it works offline
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*explore.VENDOR, "index.html"])


def test_the_libraries_the_page_loads_ship_with_it_as_their_notice_says():
    page = (explore.ASSETS / explore.PAGE).read_text(encoding="utf-8")
    scripts = [name for name in explore.VENDOR if name.endswith(".js")]
    # loaded from beside the page; jsDelivr is only the fallback, written with <\/script>
    assert re.findall(r'<script src="([^"]+)"></script>', page) == scripts
    notice = (explore.ASSETS / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
    for name in scripts:
        data = (explore.ASSETS / name).read_bytes()
        entry = notice.split(f"\n{name}\n", 1)[1].split("\n\n", 1)[0]
        assert f"SHA-256: {hashlib.sha256(data).hexdigest()}" in entry, name
        # the control: a file changed by one byte doesn't match its entry
        assert f"SHA-256: {hashlib.sha256(data + b' ').hexdigest()}" not in entry


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_the_pages_script_parses(tmp_path):
    page = (explore.ASSETS / explore.PAGE).read_text()
    script = page[page.rindex("<script>") + len("<script>"):page.rindex("</script>")]

    def parses(js: str) -> bool:
        (tmp_path / "page.js").write_text(js)
        return subprocess.run(["node", "--check", str(tmp_path / "page.js")],
                              capture_output=True).returncode == 0
    assert not parses(script + "\nconst x = ;")  # the control: a syntax error is caught
    assert parses(script)


# The layouts --------------------------------------------------------------------------------


def test_crossings_count_pairs_that_cross_and_not_pairs_that_share_an_object():
    ab: layout.Route = ("a", "b", [(0.0, 0.0), (10.0, 10.0)])
    assert layout.crossings([ab, ("c", "d", [(0.0, 10.0), (10.0, 0.0)])]) == 1
    assert layout.crossings([ab, ("c", "d", [(0.0, 20.0), (10.0, 30.0)])]) == 0  # the control
    assert layout.crossings([ab, ("a", "d", [(0.0, 10.0), (10.0, 0.0)])]) == 0  # they meet at a
    assert layout.crossings([ab, ("b", "a", [(0.0, 10.0), (10.0, 0.0)])]) == 0  # the same pair


def test_through_counts_an_edge_drawn_across_an_object_it_doesnt_connect():
    pos: dict[str, layout.Point] = {"a": (0.0, 0.0), "b": (600.0, 0.0), "m": (300.0, 0.0),
                                    "far": (300.0, 400.0)}
    assert layout.through([("a", "b", [(0.0, 0.0), (600.0, 0.0)])], pos) == 1  # across m
    assert layout.through([("a", "m", [(0.0, 0.0), (300.0, 0.0)])], pos) == 0  # the control: to m


def test_fewer_tries_for_a_bigger_catalog():
    assert [layout.tries_for(n) for n in (10, 150, 151, layout.BIG, layout.BIG + 1)] == [
        24, 24, 4, 4, 1]


def test_without_graphviz_the_error_says_how_to_get_it(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(layout.LayoutError, match="Graphviz isn't installed"):
        layout.build([{"id": "s.a", "kind": "table", "schema": "s"}], [], tries=1)


@pytest.fixture(scope="module")
def built(g, tmp_path_factory):
    if shutil.which("dot") is None:
        pytest.skip("needs Graphviz")
    out = tmp_path_factory.mktemp("explorer")
    return inlined(explore.write(g, out, tries=2).read_text())


def test_every_arrangement_is_laid_out_at_build(built):
    L = built["layouts"]
    assert set(L) == {"right", "right/grouped", "down", "down/grouped", "free", "map", "schemas"}
    ids = {n["id"] for n in built["nodes"]}
    for name in ("right", "right/grouped", "down", "down/grouped", "free"):
        assert set(L[name]["pos"]) == ids, name
    assert all(L[name]["tries"] == 2 for name in L)


def test_the_map_draws_the_catalogs_objects_as_dot_routed_them(built):
    m = built["layouts"]["map"]
    inside = {n["id"] for n in built["nodes"] if n["kind"] in ("table", "view")}
    assert set(m["pos"]) == inside
    assert {g[0] for g in m["groups"]} == {n["schema"] for n in built["nodes"] if n["id"] in inside}
    assert m["routes"] and all(len(r[2]) >= 4 for r in m["routes"])  # Bezier control points


def test_the_overview_has_a_box_per_schema_and_an_edge_per_pair_of_schemas(built):
    o = built["layouts"]["schemas"]
    schema = {n["id"]: n["schema"] for n in built["nodes"] if n["kind"] in ("table", "view")}
    pairs = {(schema[e["s"]], schema[e["t"]]) for e in built["table_edges"]
             if e["s"] in schema and e["t"] in schema and schema[e["s"]] != schema[e["t"]]}
    assert set(o["pos"]) == set(schema.values())
    assert {(r[0], r[1]) for r in o["routes"]} == pairs
    assert o["card"] == list(layout.SCHEMA_CARD)


@needs_dot
def test_the_same_snapshot_builds_the_same_page(tmp_path):
    # the layouts must be stable: a rebuild mustn't move anything, whatever the hash seed
    script = ("import sys, demo; from pathlib import Path; from ucatlas import explore; "
              "s = demo.take(); "
              "g = explore.graph(s.model, s.catalog, s.taken_at, s.lineage, demo.sql_files()); "
              "explore.write(g, Path(sys.argv[1]), tries=2)")
    for seed in ("1", "2"):
        run(script, seed, str(tmp_path / seed))
    one, two = (tmp_path / seed / "index.html" for seed in ("1", "2"))
    assert one.read_bytes() == two.read_bytes()

