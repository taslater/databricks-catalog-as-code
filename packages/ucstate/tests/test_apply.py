"""apply.py and state.py, offline: a fake workspace and a fake `_ucstate` schema.

The live tests of the same paths are in test_apply_live.py.
"""

import copy
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_plan import ORDERS, sources
from ucmodel import samples
from ucmodel.workspace import SqlError, Workspace
from ucstate import apply as apply_module
from ucstate import shadow
from ucstate.apply import (
    ApplyError,
    StatementFailed,
    deploy,
    execute,
    left_to_do,
    needs_rehearsal,
    run_prechecks,
    touched_tables,
)
from ucstate.intent import History
from ucstate.plan import Change, PlanError, plan
from ucstate.project import Project
from ucstate.shadow import CompilerDisagrees
from ucstate.state import CHUNK, LockHeld, _chunks, _fit


def quiet(message: str) -> None:
    pass


class FakeWorkspace(Workspace):
    """Records statements instead of running them, and fails the one asked to."""

    def __init__(self, fail_on: str | None = None):
        me = SimpleNamespace(user_name="tester")
        super().__init__(SimpleNamespace(current_user=SimpleNamespace(me=lambda: me)))  # type: ignore[arg-type]
        self.ran: list[tuple[str, str | None, str | None]] = []
        self.fail_on = fail_on
        self.answers: dict[str, list[dict]] = {}  # rows for a statement holding the key

    def catalog(self, name):
        return None if name == "ucstate_missing" else {"name": name}

    def sql(self, statement, catalog=None, schema=None, params=None):
        if self.fail_on and self.fail_on in statement:
            raise SqlError("[TABLE_OR_VIEW_NOT_FOUND] it went wrong", statement)
        self.ran.append((statement, catalog, schema))
        return next((rows for k, rows in self.answers.items() if k in statement), [])


@pytest.fixture
def live() -> dict:
    return samples.model("roundtrip")


@pytest.fixture
def desired(live) -> dict:
    out = copy.deepcopy(live)
    out[ORDERS]["comment"] = "changed"
    return out


@pytest.fixture
def saved(live, desired):
    return plan(desired, live, sources(desired), "ucstate_test")


@pytest.fixture
def project(tmp_path) -> Project:
    return Project(root=tmp_path, schemas=["roundtrip"], shadow_catalog="ucstate_shadow",
                   targets={"test": "ucstate_test"}, warehouse_id="w")


@pytest.fixture
def events(monkeypatch) -> list:
    """What deploy asks of `_ucstate`, in order. Fakes Store, and leaves reads to `reads`."""
    log: list = []
    held = {"locked": False, "history": History()}

    class FakeStore:
        def __init__(self, ws, catalog):
            pass

        def ensure(self):
            pass

        @contextmanager
        def lock(self, run):
            if held["locked"]:
                raise LockHeld("ucstate_test is locked by another apply")
            log.append("lock")
            try:
                yield
            finally:
                log.append("unlock")

        def start(self, run, record):
            log.append(("start", sorted(record)))

        def finish(self, run, status, statements, versions, error=None):
            log.append(("finish", status, len(statements), error and error.split(": ")[0]))

        def write_state(self, run, desired, live):
            log.append(("state", len(desired)))

        def history(self):
            return held["history"]

        def record(self, run, refactors, scripts):
            log.append(("record", [r["id"] for r in refactors], [s["id"] for s in scripts]))

    monkeypatch.setattr(apply_module, "Store", FakeStore)
    monkeypatch.setattr(apply_module, "table_versions", lambda *a: {})
    log.append(held)  # the first item lets a test hold the lock; tests drop it
    return log


def reads(monkeypatch, *models: dict) -> None:
    """The models read_live returns, in order: before the run, then after it."""
    queue = list(models)
    monkeypatch.setattr(apply_module, "read_live", lambda *a: queue.pop(0))


def run(ws, project, saved, events, **kw) -> list:
    try:
        deploy(ws, project, "test", saved=saved, log=quiet, **kw)
    finally:
        events.pop(0)
    return events


RECORD = ["catalog", "changes", "fingerprint", "format", "notes", "refactors", "renamed",
          "scripts", "source", "unmanaged"]


def test_a_deploy_locks_runs_verifies_and_records(monkeypatch, project, saved, live, desired,
                                                  events):
    reads(monkeypatch, live, copy.deepcopy(desired))
    ws = FakeWorkspace()
    n = len([s for c in saved.changes for s in c.statements])
    assert run(ws, project, saved, events) == [
        "lock", ("start", RECORD), ("state", len(desired)), ("record", [], []),
        ("finish", "succeeded", n, None), "unlock"]
    assert ws.ran == [(s, "ucstate_test", "roundtrip") for c in saved.changes for s in c.statements]


def test_a_changed_target_refuses_the_plan_and_runs_nothing(monkeypatch, project, saved, live,
                                                             events):
    changed = copy.deepcopy(live)
    changed[ORDERS]["comment"] = "changed by someone else"
    reads(monkeypatch, changed)
    ws = FakeWorkspace()
    with pytest.raises(ApplyError, match=f"changed since the plan was made.*{ORDERS}"):
        run(ws, project, saved, events)
    assert events == ["lock", "unlock"] and ws.ran == []


def test_a_blocked_or_unapproved_plan_runs_nothing(monkeypatch, project, saved, events):
    reads(monkeypatch)
    ws = FakeWorkspace()
    with pytest.raises(ApplyError, match="not applied"):
        deploy(ws, project, "test", saved=saved, approve=lambda p: False, log=quiet)
    saved.changes.append(Change(ORDERS, "destructive", "drop column x", blocked="no allowance"))
    with pytest.raises(ApplyError, match="1 blocked changes"):
        run(ws, project, saved, events)
    assert events == ["lock", "unlock", "lock", "unlock"] and ws.ran == []


def test_a_plan_for_another_catalog_is_refused(monkeypatch, project, saved, events):
    saved.catalog = "ucstate_prod"
    with pytest.raises(ApplyError, match="the plan is for ucstate_prod, not ucstate_test"):
        run(FakeWorkspace(), project, saved, events)


def test_a_missing_target_catalog_is_an_error(project, saved):
    project.targets["test"] = "ucstate_missing"
    with pytest.raises(ApplyError, match="catalog ucstate_missing doesn't exist"):
        deploy(FakeWorkspace(), project, "test", saved=saved, log=quiet)


def test_a_held_lock_stops_the_apply(monkeypatch, project, saved, events):
    events[0]["locked"] = True
    with pytest.raises(LockHeld, match="locked by another apply"):
        run(FakeWorkspace(), project, saved, events)
    assert events == []


def test_a_failed_statement_is_recorded_and_no_state_is_written(monkeypatch, project, saved,
                                                                 live, events):
    reads(monkeypatch, live)
    ws = FakeWorkspace(fail_on="COMMENT ON TABLE")
    with pytest.raises(StatementFailed, match="it went wrong"):
        run(ws, project, saved, events)
    assert events == ["lock", ("start", RECORD), ("finish", "failed", 1, ORDERS), "unlock"]


def test_an_unfinished_apply_fails_verification(monkeypatch, project, saved, live, events):
    reads(monkeypatch, live, live)  # the statements "ran" but changed nothing
    with pytest.raises(ApplyError, match=r"(?s)verify: .*comment"):
        run(FakeWorkspace(), project, saved, events)
    assert events[-2:] == [("finish", "failed", 1, "verify"), "unlock"]
    assert not any(e[0] == "state" for e in events if isinstance(e, tuple))


def test_execute_records_each_statement_and_stops_at_a_failure(saved):
    ran: list[dict] = []
    saved.changes.append(Change(ORDERS, "auto", "never runs", ["SELECT 1"]))
    with pytest.raises(StatementFailed):
        execute(FakeWorkspace(fail_on="COMMENT ON TABLE"), saved, quiet, ran)
    assert [(r["key"], r.get("error")) for r in ran] == [
        (ORDERS, "[TABLE_OR_VIEW_NOT_FOUND] it went wrong")]
    assert isinstance(ran[0]["seconds"], float)


def test_left_to_do_ignores_only_what_the_plan_leaves_alone(saved, desired):
    live = copy.deepcopy(desired)
    live["view:roundtrip.extra"] = {**live["view:roundtrip.customer_orders"],
                                    "name": "roundtrip.extra"}
    live["table:roundtrip.odd"] = {"kind": "unsupported", "name": "roundtrip.odd",
                                   "type": "STREAMING_TABLE"}
    assert left_to_do(saved, live) == []
    saved.unmanaged = "drop"
    assert left_to_do(saved, live) == ["view:roundtrip.extra: not in the source"]
    del live[ORDERS]
    assert f"{ORDERS}: missing from the target" in left_to_do(saved, live)


def test_state_is_written_in_chunks_under_the_parameter_limit():
    rows = [{"key": f"table:s.t{i}", "hash": "h", "object": "x" * 300_000} for i in range(7)]
    chunks = list(_chunks(rows))
    assert [len(c) for c in chunks] == [2, 2, 2, 1]
    assert all(len(json.dumps(c)) < CHUNK for c in chunks)
    with pytest.raises(SqlError, match="too large"):
        list(_chunks([{"key": "table:s.big", "hash": "h", "object": "x" * CHUNK}]))


def test_long_records_are_cut_down_rather_than_refused():
    assert _fit("short") == "short"
    cut = json.loads(_fit("x" * 1_000_000))
    assert cut["truncated"] and cut["length"] == 1_000_000 and len(cut["start"]) < CHUNK // 2


# Phase 4: prechecks, and what a deploy records -------------------------------------------


def checked(saved):
    """The saved plan with its one change made a checked one, as NOT NULL would be."""
    saved.changes[0].cls = "checked"
    saved.changes[0].precheck = "SELECT count_if(x IS NULL) AS violations FROM roundtrip.orders"
    return saved


def test_a_precheck_that_finds_rows_blocks_its_change_and_a_clean_one_unblocks_it(saved):
    ws = FakeWorkspace()
    ws.answers["count_if(x IS NULL)"] = [{"violations": "3"}]
    run_prechecks(ws, checked(saved), quiet)
    c = saved.changes[0]
    assert (c.violations, c.blocked) == (3, "its precheck found 3 rows in the way")
    assert "precheck (3 rows in the way): SELECT count_if" in saved.text()
    ws.answers["count_if(x IS NULL)"] = [{"violations": "0"}]
    run_prechecks(ws, saved, quiet)
    assert (c.violations, c.blocked) == (0, None) and "precheck (passed)" in saved.text()


def test_execute_refuses_a_checked_change_whose_precheck_has_not_run(saved):
    with pytest.raises(PlanError, match="the prechecks haven't run"):
        execute(FakeWorkspace(), checked(saved), quiet)


def test_a_deploy_runs_the_prechecks_again_and_stops_on_rows_in_the_way(monkeypatch, project,
                                                                         saved, live, events):
    reads(monkeypatch, live)
    ws = FakeWorkspace()
    ws.answers["count_if(x IS NULL)"] = [{"violations": "1"}]
    saved = checked(saved)
    saved.changes[0].violations = 0  # it passed when the plan was made
    with pytest.raises(ApplyError, match="a precheck found rows in the way"):
        run(ws, project, saved, events)
    assert events == ["lock", "unlock"]
    assert [s for s, _, _ in ws.ran] == [saved.changes[0].precheck]


def test_a_deploy_records_the_renames_and_scripts_it_dealt_with(monkeypatch, project, saved,
                                                                 live, desired, events):
    reads(monkeypatch, live, copy.deepcopy(desired))
    saved.refactors = [{"id": "r1", "kind": "rename_table", "outcome": "already done"}]
    saved.scripts = [{"id": "s1", "kind": "once", "object": None, "checksum": "c",
                      "outcome": "ran"}]
    run(FakeWorkspace(), project, saved, events)
    assert ("record", ["r1"], ["s1"]) in events


def test_a_saved_plan_whose_script_has_run_since_is_refused(monkeypatch, project, saved, live,
                                                            events):
    reads(monkeypatch, live)
    events[0]["history"] = History(scripts={"s1": "c"})
    saved.scripts = [{"id": "s1", "kind": "once", "object": None, "checksum": "c",
                      "outcome": "ran"}]
    with pytest.raises(ApplyError, match=r"script s1 \(done since\)"):
        run(FakeWorkspace(), project, saved, events)


@pytest.mark.parametrize("script, rehearsals", [(None, []), ("2026-11-01-x", ["test"])])
def test_a_plan_that_depends_on_the_rows_is_rehearsed_first(monkeypatch, project, saved, live,
                                                            desired, events, script, rehearsals):
    calls: list = []
    monkeypatch.setattr(apply_module, "rehearse", lambda *a, **kw: calls.append(a[2]))
    reads(monkeypatch, live, copy.deepcopy(desired))
    project.rehearsal_catalog = "ucstate_rehearsal"
    saved.changes[0].script = script  # a transition script, when there is one
    run(FakeWorkspace(), project, saved, events)
    assert calls == rehearsals


def test_needs_rehearsal(saved):
    assert not needs_rehearsal(saved)
    saved.changes[0].cls = "checked"
    assert needs_rehearsal(saved)


def test_the_tables_a_plan_touches_are_named_before_any_rename(saved):
    saved.changes += [Change("table:roundtrip.clients", "auto", "rename"),
                      Change("script:x", "script", "run once",
                             ["DELETE FROM roundtrip.order_lines WHERE 1 = 0"], script="x")]
    saved.renamed = {"table:roundtrip.clients": "table:roundtrip.customers"}
    assert touched_tables(saved) == {"roundtrip.orders", "roundtrip.customers",
                                     "roundtrip.order_lines"}


# The desired model: compiler and shadow ------------------------------------------------------


@pytest.fixture
def source_project() -> Project:
    """The round-trip example, whose source the compiler can read."""
    return Project.load(Path(__file__).parent.parent / "examples" / "roundtrip")


def fresh(ws, project, events, **kw) -> list:
    try:
        deploy(ws, project, "test", log=quiet, **kw)
    finally:
        events.pop(0)
    return events


def test_a_fresh_deploy_stops_when_the_compiler_and_the_shadow_disagree(
        monkeypatch, source_project, live, events):
    """The known-bad control: the shadow's model differs, and nothing runs."""
    different = copy.deepcopy(live)
    different[ORDERS]["comment"] = "changed"
    monkeypatch.setattr(shadow, "build", lambda ws, project, log=print: different)
    reads(monkeypatch, live)
    ws = FakeWorkspace()
    with pytest.raises(CompilerDisagrees, match="nothing was run"):
        fresh(ws, source_project, events)
    assert ws.ran == []
    assert events == ["lock", "unlock"]


def test_a_fresh_deploy_goes_on_when_the_compiler_and_the_shadow_agree(
        monkeypatch, source_project, live, events):
    built: list = []
    monkeypatch.setattr(shadow, "build",
                        lambda ws, project, log=print: built.append(1) or copy.deepcopy(live))
    reads(monkeypatch, live, live, live)
    ws = FakeWorkspace()
    assert fresh(ws, source_project, events)[0] == "lock"
    assert built == [1]
    assert ws.ran == []  # the target already matches, so there is nothing to run


def test_shadow_only_skips_the_compiler(monkeypatch, source_project, live, events):
    monkeypatch.setattr(shadow, "compile_statements",
                        lambda statements: pytest.fail("the compiler ran"))
    monkeypatch.setattr(shadow, "build", lambda ws, project, log=print: copy.deepcopy(live))
    reads(monkeypatch, live, live, live)
    fresh(FakeWorkspace(), source_project, events, shadow_only=True)


def test_a_deployment_records_the_ci_run_that_applied_it(monkeypatch):
    for name in ("GITHUB_RUN_ID", "GITHUB_REPOSITORY", "GITHUB_SERVER_URL", "CI_JOB_URL"):
        monkeypatch.delenv(name, raising=False)
    assert apply_module._run_url() is None
    monkeypatch.setenv("CI_JOB_URL", "https://gitlab.example.com/group/repo/-/jobs/7")
    assert apply_module._run_url() == "https://gitlab.example.com/group/repo/-/jobs/7"
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    assert apply_module._run_url() == "https://github.com/owner/repo/actions/runs/42"
