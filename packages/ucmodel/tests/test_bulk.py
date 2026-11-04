"""bulk.py, offline: reads over a warehouse give what the REST reads give, from rows as
information_schema and the lineage system tables return them."""

import io
from types import SimpleNamespace

import pytest
from databricks.sdk.errors import NotFound

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
        self.history: dict[str, list[dict]] = {q: [] for q in (
            bulk.WRITES, bulk.USES, bulk.QUERIES_READ, bulk.SIZES, bulk.TASK_RUNS)}
        self.jobs: dict[str, dict] = {}  # what the jobs API has, by id

    def catalog(self, name):
        return {"name": name}

    def get(self, path, **query):  # lineage over REST: none
        if path == "/api/2.1/jobs/get":
            if query["job_id"] not in self.jobs:
                raise NotFound("no such job")
            return self.jobs[query["job_id"]]
        return {}

    def sql(self, statement, catalog=None, schema=None, params=None):
        self.statements.append(statement)
        if self.refuse:
            raise SqlError("[INSUFFICIENT_PERMISSIONS] not for you")
        tags, grants, functions = rows({"schemas": self.raw})
        if statement in self.history:
            return self.history[statement]
        if statement == bulk.NAMES:
            return [{"id": "pipeline:p", "name": "MV-kinds"}]
        if "_lineage" in statement:
            return []
        return {bulk.TAGS: tags, bulk.GRANTS: grants, bulk.FUNCTIONS: functions}[statement]


def test_a_snapshot_with_a_warehouse_is_the_snapshot_over_rest():
    rest_only, with_warehouse = Kinds(), Kinds()
    rest_only.warehouse_id = None
    assert (take(with_warehouse, "ucstate_test", ["kinds"]).model
            == take(rest_only, "ucstate_test", ["kinds"]).model == samples.model("kinds"))
    assert len(with_warehouse.statements) == 10  # with one naming the two pipelines


def test_a_query_that_fails_falls_back_to_rest_and_says_so():
    ws, out = Kinds(refuse=True), io.StringIO()
    s = take(ws, "ucstate_test", ["kinds"], progress=Progress(out, live=False))
    assert s.model == samples.model("kinds")
    notes = [line for line in out.getvalue().splitlines()
             if "over REST" in line or "no query history" in line]
    assert notes == [
        "reading tags, grants and functions over REST: information_schema can't be read: "
        "[INSUFFICIENT_PERMISSIONS] not for you",
        "reading lineage over REST: the system tables can't be read: "
        "[INSUFFICIENT_PERMISSIONS] not for you",
        "no query history: it can't be read: [INSUFFICIENT_PERMISSIONS] not for you"]


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


# query history --------------------------------------------------------------------------

def write_row(t: str, sql: str, last: str, runs: int = 1, of: int = 1, **more) -> dict:
    return {"s": "kinds", "t": t, "sql": sql, "type": more.get("type", "INSERT"),
            "runs": str(runs), "last": last, "by": "owner@example.com",
            "client": more.get("client", "Databricks CLI"),
            "source": more.get("source", '{"job_info":{},"pipeline_info":{}}'), "of": str(of),
            "reads": more.get("reads", "true")}


def test_history_rows_read_as_writes_of_the_models_objects_latest_first(kinds):
    writes, uses = bulk.ran(kinds, [
        write_row("orders", "INSERT INTO kinds.orders SELECT 1", "2026-11-04T17:00:00.5Z"),
        write_row("orders", "INSERT INTO kinds.orders SELECT 2", "2026-11-04T18:00:00Z", runs=3,
                  of=2, source='{"job_info":{"job_id":"11","job_run_id":"12"},"pipeline_info":{}}',
                  client="Jobs"),
        write_row("dropped", "INSERT INTO kinds.dropped SELECT 1", "2026-11-04T18:00:00Z"),
        write_row("orders_feed", "INSERT INTO kinds.orders_feed SELECT 1", "2026-11-01T00:00:00Z"),
    ], [{"s": "kinds", "t": "orders", "queries": "7", "people": "2",
         "last": "2026-11-04T19:00:00.1Z"},
        {"s": "kinds", "t": "dropped", "queries": "1", "people": "1",
         "last": "2026-11-04T19:00:00Z"}])
    assert [(w.target, w.sql[-1]) for w in writes] == [
        ("kinds.orders", "2"), ("kinds.orders", "1"), ("kinds.orders_feed", "1")]
    latest = writes[0]
    assert (latest.runs, latest.last, latest.of, latest.client) == (
        3, "2026-11-04T18:00:00Z", 2, "Jobs")
    assert latest.source == {"job_id": "11", "job_run_id": "12"}
    assert writes[1].source == {} and writes[1].reads
    assert [(u.target, u.queries, u.people, u.last) for u in uses] == [
        ("kinds.orders", 7, 2, "2026-11-04T19:00:00Z")]


def test_a_snapshot_keeps_query_history_in_its_lineage(tmp_path):
    from ucmodel.snapshot import load, write
    ws = Kinds()
    ws.history[bulk.WRITES] = [write_row("orders", "INSERT INTO kinds.orders SELECT 1",
                                         "2026-11-04T17:00:00Z")]
    s = take(ws, "ucstate_test", ["kinds"])
    assert s.lineage is not None and s.lineage.writes and s.lineage.uses == []
    assert load(write(s, tmp_path / "s.json")).lineage == s.lineage


def test_without_history_a_snapshots_lineage_is_as_before(tmp_path):
    from ucmodel.snapshot import load, write
    s = take(Kinds(), "ucstate_test", ["kinds"], with_history=False)
    assert s.lineage is not None and s.lineage.writes is None
    assert not {"writes", "uses", "queries", "jobs"} & set(s.lineage.to_json())
    assert load(write(s, tmp_path / "s.json")).lineage == s.lineage


class Names:
    """A warehouse that answers the names query, and keeps what it was asked."""

    def __init__(self, rows):
        self.rows, self.asked = rows, []

    def sql(self, statement, params=None):
        self.asked.append(params)
        return self.rows


def test_jobs_and_pipelines_are_named_from_the_writes_and_the_models_pipelines(kinds):
    from ucmodel.lineage import Write
    writes = [Write("kinds.orders", "INSERT ...", "INSERT", 1, "2026-11-04T17:00:00Z", None, None,
                    {"job_id": "11", "job_run_id": "12"}, 1, True)]
    ws = Names([{"id": "job:11", "name": "nightly"}, {"id": "pipeline:p1", "name": None}])
    found = bulk.names(ws, kinds, writes)  # type: ignore[arg-type]
    assert found == {"job:11": "nightly"}  # a name the table doesn't have is left out
    pipelines = sorted(o["meta"]["pipeline_id"] for o in kinds.values()
                       if (o.get("meta") or {}).get("pipeline_id"))
    assert ws.asked == [{"jobs": "11", "pipelines": ",".join(pipelines)}]
    assert len(pipelines) == 2  # the materialized view's and the streaming table's


def test_with_nothing_to_name_no_query_runs():
    ws = Names([])
    assert bulk.names(ws, {}, []) == {}  # type: ignore[arg-type]
    assert ws.asked == []


def test_one_query_is_counted_as_one_query(kinds):
    out = io.StringIO()
    bulk.names(Names([]), kinds, [], progress=Progress(out, live=False))  # type: ignore[arg-type]
    assert "naming jobs and pipelines: 1 query" in out.getvalue()
    assert "querie " not in out.getvalue() and "querie\n" not in out.getvalue()


def test_the_control_two_queries_are_counted_as_queries(kinds):
    out = io.StringIO()
    bulk.history(Kinds(), "ucstate_test", kinds,  # type: ignore[arg-type]
                 progress=Progress(out, live=False))
    assert "reading query history: 3 queries" in out.getvalue()



# queries that join, sizes and jobs ------------------------------------------------------

def test_queries_that_read_the_models_objects_together_are_kept_most_run_first(kinds):
    found = bulk.read_together("ucstate_test", kinds, [
        {"sql": "SELECT 1", "tables": '["kinds.orders","kinds.orders_feed"]', "runs": "2",
         "people": "1", "last": "2026-11-04T17:00:00.4Z"},
        {"sql": "SELECT 2", "tables": '["kinds.orders","sales.t@other"]', "runs": "5",
         "people": "2", "last": "2026-11-04T18:00:00Z"},
        {"sql": "SELECT 3", "tables": '["kinds.gone","kinds.also_gone"]', "runs": "9",
         "people": "1", "last": "2026-11-04T18:00:00Z"}])
    assert [(q.sql, q.tables, q.runs, q.people, q.last) for q in found] == [
        ("SELECT 2", ["kinds.orders", "other.sales.t"], 5, 2, "2026-11-04T18:00:00Z"),
        ("SELECT 1", ["kinds.orders", "kinds.orders_feed"], 2, 1, "2026-11-04T17:00:00Z")]


def size_row(t: str, day: str, size: int, files: int = 1) -> dict:
    return {"s": "kinds", "t": t, "day": day, "bytes": str(size), "files": str(files)}


def test_sizes_are_each_tables_latest_with_the_days_it_changed(kinds):
    found = bulk.sized(kinds, [size_row("orders", "2026-11-04", 300, 3),
                               size_row("orders", "2026-11-01", 100),
                               size_row("orders", "2026-11-03", 200, 2),
                               size_row("dropped", "2026-11-04", 5)])
    assert [(z.target, z.bytes, z.files, z.on, z.history) for z in found] == [
        ("kinds.orders", 300, 3, "2026-11-04",
         [("2026-11-01", 100), ("2026-11-03", 200), ("2026-11-04", 300)])]


@pytest.mark.parametrize(("definition", "runs", "path"), [
    ({"notebook_task": {"notebook_path": "/Shared/daily"}}, "notebook", "/Shared/daily"),
    ({"sql_task": {"file": {"path": "/Shared/refresh.sql"}, "warehouse_id": "w"}}, "sql_file",
     "/Shared/refresh.sql"),
    ({"sql_task": {"query": {"query_id": "q1"}}}, "sql_query", "q1"),
    ({"pipeline_task": {"pipeline_id": "p1"}}, "pipeline", "p1"),
    ({"run_job_task": {"job_id": 7}}, "job", "7"),
    ({"condition_task": {"op": "EQUAL_TO", "left": "a", "right": "b"}}, "condition", None),
    ({"something_new": {}}, "other", None),
])
def test_a_task_says_what_it_runs(definition, runs, path):
    t = bulk.task({"task_key": "k", "depends_on": [{"task_key": "b"}, {"task_key": "a"}],
                   **definition})
    assert (t.key, t.depends_on, t.runs, t.path) == ("k", ["a", "b"], runs, path)


def job_write(run: str | None, job: str = "11") -> object:
    from ucmodel.lineage import Write
    source = {"job_id": job, "job_run_id": "12", **({"job_task_run_id": run} if run else {})}
    return Write("kinds.orders", "INSERT ...", "INSERT", 1, "2026-11-04T17:00:00Z", None, None,
                 source, 1, True)


def test_writes_get_their_task_and_their_jobs_tasks_are_read():
    ws = Kinds()
    ws.history[bulk.TASK_RUNS] = [{"run_id": "21", "task_key": "daily"}]
    ws.jobs = {"11": {"settings": {"tasks": [
        {"task_key": "daily", "notebook_task": {"notebook_path": "/Shared/daily"}},
        {"task_key": "refresh", "depends_on": [{"task_key": "daily"}],
         "sql_task": {"file": {"path": "/Shared/refresh.sql"}}}]}}}
    writes, jobs = bulk.jobs(ws, [job_write("21"), job_write("22"), job_write(None, job="99")])  # type: ignore[arg-type,list-item]
    assert [w.source.get("task_key") for w in writes] == ["daily", None, None]
    assert [(j.id, [(t.key, t.depends_on, t.runs) for t in j.tasks]) for j in jobs] == [
        ("11", [("daily", [], "notebook"), ("refresh", ["daily"], "sql_file")])]  # 99 is gone


def test_with_no_job_no_query_runs():
    ws = Kinds()
    assert bulk.jobs(ws, []) == ([], [])  # type: ignore[arg-type]
    assert ws.statements == []


def test_a_snapshot_keeps_sizes_queries_and_jobs(tmp_path):
    from ucmodel.snapshot import load, write
    ws = Kinds()
    ws.history[bulk.WRITES] = [write_row(
        "orders", "INSERT INTO kinds.orders SELECT 1", "2026-11-04T17:00:00Z",
        source='{"job_info":{"job_id":"11","job_run_id":"12","job_task_run_id":"21"}}')]
    ws.history[bulk.TASK_RUNS] = [{"run_id": "21", "task_key": "daily"}]
    ws.history[bulk.SIZES] = [size_row("orders", "2026-11-04", 300)]
    ws.history[bulk.QUERIES_READ] = [
        {"sql": "SELECT 1", "tables": '["kinds.orders","kinds.orders_feed"]', "runs": "2",
         "people": "1", "last": "2026-11-04T17:00:00Z"}]
    ws.jobs = {"11": {"settings": {"tasks": [{"task_key": "daily", "notebook_task": {
        "notebook_path": "/Shared/daily"}}]}}}
    s = take(ws, "ucstate_test", ["kinds"])
    assert s.lineage is not None
    assert s.lineage.writes and s.lineage.writes[0].source["task_key"] == "daily"
    assert [z.target for z in s.lineage.sizes or []] == ["kinds.orders"]
    assert [q.sql for q in s.lineage.queries or []] == ["SELECT 1"]
    assert [j.id for j in s.lineage.jobs or []] == ["11"]
    assert load(write(s, tmp_path / "s.json")).lineage == s.lineage
