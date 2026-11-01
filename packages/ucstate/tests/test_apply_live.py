"""`deploy` against ucstate_test, through the add_columns scenario.

    UCSTATE_PROFILE=my-profile uv run pytest -m live packages/ucstate/tests/test_apply_live.py

Each test rebuilds sc_add_columns from its before/ side. The records go in
ucstate_test._ucstate, which the tests share and never drop. Three of the tests are
known-bad controls: a stale plan, a failing statement and an unfinished
apply must each stop, and leave the previous state current.

The rehearsal tests use the transition scenarios and ucstate_rehearsal. A
transition script that lands on the wrong state must stop in the rehearsal,
before the target changes.
"""

import dataclasses
import json
import threading
import time

import pytest

from livegate import ENABLED, PROFILE, REASON, WAREHOUSE
from scenarios import CATALOG, Scenario, load, quiet
from ucmodel.workspace import Workspace
from ucstate import shadow
from ucstate.apply import ApplyError, StatementFailed, deploy
from ucstate.plan import Change
from ucstate.project import Project
from ucstate.state import LockHeld, Run, Store

SCENARIO = load("add_columns")
ORDERS = "table:sc_add_columns.orders"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not ENABLED, reason=REASON),
]


@pytest.fixture(scope="module")
def ws():
    return Workspace.connect(PROFILE or None, WAREHOUSE)


@pytest.fixture(scope="module")
def project():
    return SCENARIO.project("after")


@pytest.fixture(scope="module")
def desired(ws, project):
    return shadow.build(ws, project, log=quiet)


@pytest.fixture
def store(ws):
    s = Store(ws, CATALOG)
    s.ensure()
    s.unlock()  # a lock left by an earlier, killed run
    return s


def latest(store: Store) -> dict:
    return store.deployments(1)[0]


def test_an_apply_migrates_verifies_and_records(ws, project, desired, store):
    SCENARIO.build_before(ws)
    out = deploy(ws, project, "test", log=quiet)
    assert out.plan.changes and out.statements
    assert SCENARIO.problems_after_apply(ws, desired) == []
    assert SCENARIO.check_rows(ws) == []

    row = latest(store)
    assert (row["id"], row["status"], int(row["statements"])) == (
        out.run.id, "succeeded", len(out.statements))
    assert row["source"] == project.revision() and store.holder() is None
    versions = json.loads(ws.sql(f"SELECT versions FROM {store.table('deployments')} "
                                 "WHERE id = :id", params={"id": out.run.id})[0]["versions"])
    name = ORDERS.split(":")[1]
    assert versions["after"][name] > versions["before"][name]

    current, state = store.current_state()
    assert current == out.run.id and sorted(state) == sorted(desired)
    assert state[ORDERS]["meta"]["table_id"]  # live meta, for drift attribution later
    assert {k: v for k, v in state[ORDERS].items() if k != "meta"} == {
        k: v for k, v in desired[ORDERS].items() if k != "meta"}

    again = deploy(ws, project, "test", log=quiet)
    assert again.plan.changes == [] and latest(store)["status"] == "succeeded"
    assert store.current_state()[0] == again.run.id


def test_a_held_lock_stops_an_apply_and_only_one_of_two_takes_it(ws, project, store):
    holder = Run.new("abc123", "someone else")
    with store.lock(holder):
        before = latest(store)["id"]
        with pytest.raises(LockHeld, match=holder.id):
            deploy(ws, project, "test", log=quiet)
        assert latest(store)["id"] == before
    assert store.holder() is None

    results: list[str] = []

    def take(name: str) -> None:
        try:
            with store.lock(Run.new(None, name)):
                time.sleep(5)
            results.append("held")
        except LockHeld:
            results.append("refused")

    threads = [threading.Thread(target=take, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["held", "refused"]
    assert store.holder() is None


def test_a_stale_saved_plan_is_refused(ws, project, desired, store):
    SCENARIO.build_before(ws)
    saved = SCENARIO.plan(ws, desired)
    ws.sql("COMMENT ON TABLE sc_add_columns.orders IS 'changed after the plan'",
           catalog=CATALOG)
    before = latest(store)["id"]
    with pytest.raises(ApplyError, match=f"changed since the plan was made.*{ORDERS}"):
        deploy(ws, project, "test", saved=saved, log=quiet)
    assert latest(store)["id"] == before and store.holder() is None
    names = [c["name"] for c in SCENARIO.live(ws)[ORDERS]["columns"]]
    assert names == ["order_id", "amount"]


def test_a_failed_statement_is_recorded_and_the_state_stays(ws, project, desired, store):
    SCENARIO.build_before(ws)
    saved = SCENARIO.plan(ws, desired)
    real = sum(len(c.statements) for c in saved.changes)
    saved.changes.append(Change(ORDERS, "auto", "a statement that fails",
                                ["ALTER TABLE sc_add_columns.no_such_table ADD COLUMN x INT"]))
    current = store.current_state()[0]
    with pytest.raises(StatementFailed, match="TABLE_OR_VIEW_NOT_FOUND"):
        deploy(ws, project, "test", saved=saved, log=quiet)
    row = latest(store)
    assert row["status"] == "failed" and int(row["statements"]) == real + 1
    assert "no_such_table" in row["error"]
    assert store.current_state()[0] == current and store.holder() is None
    # The statements before the failure landed, so nothing is left to do.
    assert SCENARIO.problems_after_apply(ws, desired) == []


def test_an_unfinished_apply_fails_verification(ws, project, desired, store):
    SCENARIO.build_before(ws)
    saved = SCENARIO.plan(ws, desired)
    assert len(saved.changes) > 1
    saved.changes.pop()
    current = store.current_state()[0]
    with pytest.raises(ApplyError, match="verify"):
        deploy(ws, project, "test", saved=saved, log=quiet)
    row = latest(store)
    assert row["status"] == "failed" and row["error"].startswith("verify")
    assert store.current_state()[0] == current and store.holder() is None


def rehearsed(name: str, ws, store: Store) -> tuple[Scenario, Project, dict]:
    """A transition scenario's project, rehearsing in ucstate_rehearsal, built from before/."""
    scenario = load(name)
    project = dataclasses.replace(scenario.project("after"), rehearsal_catalog="ucstate_rehearsal")
    script = project.intent().scripts[0]
    ws.sql(f"DELETE FROM {store.table('migration_history')} WHERE id = :id",
           params={"id": script.id})  # an earlier run of this test recorded it
    scenario.build_before(ws)
    return scenario, project, shadow.build(ws, project, log=quiet)


def test_a_transition_is_rehearsed_on_clones_then_runs(ws, store):
    scenario, project, desired = rehearsed("transition", ws, store)
    logged: list[str] = []
    out = deploy(ws, project, "test", log=logged.append)
    assert "rehearsal: 1 table of ucstate_test cloned into ucstate_rehearsal, 1 deep" in logged
    assert any(m.startswith("rehearsal: ") and "ran and verified" in m for m in logged), logged
    assert [c.script for c in out.plan.changes] == ["2026-11-01-narrow-qty"]
    assert scenario.problems_after_apply(ws, desired, out.plan) == []
    assert scenario.check_rows(ws) == []
    clone = Workspace.sql(ws, "SELECT sum(qty) AS n FROM ucstate_rehearsal.sc_transition.t")
    assert clone == [{"n": "12"}]
    assert store.history().scripts == {"2026-11-01-narrow-qty": out.plan.scripts[0]["checksum"]}


def test_a_transition_that_fails_its_rehearsal_leaves_the_target_alone(ws, store):
    scenario, project, desired = rehearsed("control_transition_wrong_state", ws, store)
    before = latest(store)["id"]
    with pytest.raises(ApplyError, match="rehearsal in ucstate_rehearsal: verify found"):
        deploy(ws, project, "test", log=quiet)
    assert latest(store)["id"] == before and store.holder() is None
    columns = scenario.live(ws)[f"table:{scenario.schema}.t"]["columns"]
    assert [(c["name"], c["type"]) for c in columns] == [("id", "bigint"), ("qty", "bigint")]
