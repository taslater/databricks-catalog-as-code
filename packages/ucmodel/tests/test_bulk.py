"""bulk.py, offline: reads over a warehouse give what the REST reads give, from rows as
information_schema and the lineage system tables return them."""

import io
from types import SimpleNamespace

import pytest

from saved import SavedWorkspace
from ucmodel import bulk, reader, samples
from ucmodel.progress import Progress
from ucmodel.snapshot import take
from ucmodel.workspace import SqlError, Workspace

SAMPLES = ["shop", "roundtrip", "probe_lineage", "kinds"]


def rows(raw: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """A sample's tags, direct grants and functions, as the three queries return them."""
    tags, grants, functions = [], [], []

    def tag(kind, s, o, col, found):
        tags.extend({"kind": kind, "s": s, "o": o, "col": col, "tag_name": k,
                     "tag_value": v or None} for k, v in found.items())

    def grant(kind, s, o, found):
        grants.extend({"kind": kind, "s": s, "o": o, "grantee": who, "privilege_type": p}
                      for who, ps in found.items() for p in ps)

    for s, schema in raw["schemas"].items():
        tag("schemas", s, None, None, schema["tags"])
        grant("schema", s, None, schema["grants"])
        for name, t in schema["tables"].items():
            tag("tables", s, name, None, t["tags"])
            for column, found in t["column_tags"].items():
                tag("columns", s, name, column, found)
            grant("table", s, name, t["grants"])
        for name, f in schema["functions"].items():
            grant("function", s, name, f["grants"])
            functions.append({"s": s, "o": name})
        for name, v in schema["volumes"].items():
            tag("volumes", s, name, None, v["tags"])
            grant("volume", s, name, v["grants"])
    return tags, grants, functions


class NoPerObjectCalls(SavedWorkspace):
    """The saved workspace, refusing the calls a bulk read stands in for."""

    def tags(self, entity_type, full_name):
        raise AssertionError(f"tags of {full_name} asked over REST")

    def grants(self, securable_type, full_name):
        raise AssertionError(f"grants of {full_name} asked over REST")

    def list_all(self, path, key, **query):
        assert key != "functions", "functions listed over REST"
        return super().list_all(path, key, **query)


@pytest.mark.parametrize("name", SAMPLES)
def test_a_read_from_the_queries_rows_is_the_read_over_rest(name):
    raw = samples.raw(name)
    found = bulk.Read.from_rows(raw["catalog"], *rows(raw))
    assert reader.read(NoPerObjectCalls(raw), raw["catalog"], list(raw["schemas"]),
                       found=found) == raw


def test_the_control_a_read_without_the_rows_asks_over_rest():
    raw = samples.raw("roundtrip")
    with pytest.raises(AssertionError, match="over REST"):
        reader.read(NoPerObjectCalls(raw), raw["catalog"], list(raw["schemas"]))


def test_tags_and_grants_are_keyed_by_full_name_and_sorted():
    found = bulk.Read.from_rows("c", [
        {"kind": "columns", "s": "s", "o": "t", "col": "id", "tag_name": "pii", "tag_value": None},
        {"kind": "tables", "s": "s", "o": "t", "col": None, "tag_name": "k", "tag_value": "v"},
    ], [
        {"kind": "table", "s": "s", "o": "t", "grantee": "g", "privilege_type": "SELECT"},
        {"kind": "table", "s": "s", "o": "t", "grantee": "g", "privilege_type": "MODIFY"},
        {"kind": "schema", "s": "s", "o": None, "grantee": "h", "privilege_type": "USE_SCHEMA"},
    ], [{"s": "s", "o": "f2"}, {"s": "s", "o": "f1"}])
    assert found.tags("columns", "c.s.t.id") == {"pii": ""}  # a tag with no value
    assert found.tags("tables", "c.s.t") == {"k": "v"}
    assert found.tags("tables", "c.s.other") == {}
    assert found.grants("table", "c.s.t") == {"g": ["MODIFY", "SELECT"]}
    assert found.grants("schema", "c.s") == {"h": ["USE_SCHEMA"]}
    assert found.functions == {"s": ["f1", "f2"]}


# lineage --------------------------------------------------------------------------------

def table_row(source: str, target: str, seen: str = "2026-11-04T17:28:20.668Z") -> dict:
    (sc, ss, st), (tc, ts, tt) = source.split("."), target.split(".")
    return {"source_table_catalog": sc, "source_table_schema": ss, "source_table_name": st,
            "target_table_catalog": tc, "target_table_schema": ts, "target_table_name": tt,
            "seen": seen}


def column_row(source: str, target: str, seen: str = "2026-11-04T17:28:20.668Z") -> dict:
    s, sc = source.rsplit(".", 1)
    t, tc = target.rsplit(".", 1)
    return {**table_row(s, t, seen), "source_column_name": sc, "target_column_name": tc}


@pytest.fixture(scope="module")
def kinds():
    return samples.model("kinds")


def test_lineage_keeps_edges_with_an_end_in_the_model_whose_other_end_is_there(kinds):
    elsewhere = {"other.s.wide": {"amount", "region"}}
    found = bulk.edges("ucstate_test", kinds, [
        table_row("ucstate_test.kinds.orders", "ucstate_test.kinds.region_totals"),
        table_row("ucstate_test.kinds.orders", "other.s.wide"),
        table_row("ucstate_test.kinds.orders", "ucstate_test.kinds.dropped"),  # gone
        table_row("ucstate_test.kinds.orders", "other.s.dropped"),  # gone too
        table_row("other.s.wide", "other.s.wider"),  # neither end in the model
        table_row("ucstate_test.kinds.orders", "ucstate_test.kinds.orders"),  # itself
    ], [
        column_row("ucstate_test.kinds.orders.amount", "ucstate_test.kinds.region_totals.total"),
        column_row("ucstate_test.kinds.orders.amount", "other.s.wide.amount"),
        column_row("ucstate_test.kinds.orders.amount", "other.s.wide.renamed"),  # column gone
        column_row("ucstate_test.kinds.orders.amount",
                   "ucstate_test.kinds.region_totals.__DECOMP_GENERATED_count_0"),
    ], elsewhere.get)
    assert [(e.source, e.target) for e in found.tables] == [
        ("kinds.orders", "kinds.region_totals"), ("kinds.orders", "other.s.wide")]
    assert [(e.source, e.target) for e in found.columns] == [
        ("kinds.orders.amount", "kinds.region_totals.total"),
        ("kinds.orders.amount", "other.s.wide.amount")]


def test_the_tables_to_look_up_are_those_outside_the_schemas_read(kinds):
    assert bulk.outside("ucstate_test", kinds, [
        table_row("ucstate_test.kinds.orders", "other.s.wide"),
        table_row("ucstate_test.elsewhere.t", "ucstate_test.kinds.orders"),
        table_row("ucstate_test.kinds.orders", "ucstate_test.kinds.dropped"),
    ]) == {"other.s.wide", "elsewhere.t"}


@pytest.mark.parametrize(("event_time", "seen"), [
    ("2026-11-04T17:28:20.668Z", "2026-11-04 17:28:21.0"),
    ("2026-11-04T17:28:20.4Z", "2026-11-04 17:28:20.0"),
    ("2026-11-04T17:28:20Z", "2026-11-04 17:28:20.0"),
    ("2026-12-31T23:59:59.5Z", "2027-01-01 00:00:00.0"),
])
def test_an_event_time_reads_as_the_lineage_api_writes_it(event_time, seen):
    assert bulk.seen(event_time) == seen


def test_an_edge_seen_twice_keeps_its_latest(kinds):
    found = bulk.edges("ucstate_test", kinds, [
        table_row("ucstate_test.kinds.orders", "ucstate_test.kinds.region_totals",
                  "2026-11-04T17:27:42.366Z"),
        table_row("ucstate_test.kinds.orders", "ucstate_test.kinds.region_totals",
                  "2026-11-04T17:27:45.733Z")], [])
    assert [e.seen for e in found.tables] == ["2026-11-04 17:27:46.0"]


# snapshots ------------------------------------------------------------------------------

class Kinds(SavedWorkspace):
    """`ucstate_test.kinds` with a warehouse that answers from the rows, or refuses."""

    def __init__(self, refuse: bool = False):
        super().__init__(samples.raw("kinds"))
        self.warehouse_id: str | None = "w"
        self.refuse = refuse
        self.statements: list[str] = []

    def catalog(self, name):
        return {"name": name}

    def get(self, path, **query):  # lineage over REST: none
        return {}

    def sql(self, statement, catalog=None, schema=None, params=None):
        self.statements.append(statement)
        if self.refuse:
            raise SqlError("[INSUFFICIENT_PERMISSIONS] not for you")
        tags, grants, functions = rows({"schemas": self.raw})
        if "_lineage" in statement:
            return []
        return {bulk.TAGS: tags, bulk.GRANTS: grants, bulk.FUNCTIONS: functions}[statement]


def test_a_snapshot_with_a_warehouse_is_the_snapshot_over_rest():
    rest_only, with_warehouse = Kinds(), Kinds()
    rest_only.warehouse_id = None
    assert (take(with_warehouse, "ucstate_test", ["kinds"]).model
            == take(rest_only, "ucstate_test", ["kinds"]).model == samples.model("kinds"))
    assert len(with_warehouse.statements) == 5


def test_a_query_that_fails_falls_back_to_rest_and_says_so():
    ws, out = Kinds(refuse=True), io.StringIO()
    s = take(ws, "ucstate_test", ["kinds"], progress=Progress(out, live=False))
    assert s.model == samples.model("kinds")
    notes = [line for line in out.getvalue().splitlines() if "over REST" in line]
    assert notes == [
        "reading tags, grants and functions over REST: information_schema can't be read: "
        "[INSUFFICIENT_PERMISSIONS] not for you",
        "reading lineage over REST: the system tables can't be read: "
        "[INSUFFICIENT_PERMISSIONS] not for you"]


def test_without_a_warehouse_no_statement_runs():
    ws = Kinds()
    ws.warehouse_id = None
    take(ws, "ucstate_test", ["kinds"])
    assert ws.statements == []


# Workspace.sql --------------------------------------------------------------------------

def test_a_result_in_chunks_is_read_to_its_end():
    from databricks.sdk.service.sql import StatementState

    def result(data, following):
        return SimpleNamespace(data_array=data, next_chunk_index=following)

    chunks = {1: result([["2"]], 2), 2: result([["3"]], None)}
    api = SimpleNamespace(
        execute_statement=lambda **kw: SimpleNamespace(
            statement_id="s", status=SimpleNamespace(state=StatementState.SUCCEEDED),
            manifest=SimpleNamespace(schema=SimpleNamespace(columns=[SimpleNamespace(name="n")])),
            result=result([["1"]], 1)),
        get_statement_result_chunk_n=lambda statement_id, chunk: chunks[chunk])
    ws = Workspace(SimpleNamespace(statement_execution=api), "w")  # type: ignore[arg-type]
    assert ws.sql("SELECT n") == [{"n": "1"}, {"n": "2"}, {"n": "3"}]
