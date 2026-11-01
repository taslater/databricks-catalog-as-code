"""The scenario corpus. The live tests run against ucstate_test, the rest offline.

    UCSTATE_PROFILE=my-profile uv run pytest -m live packages/ucstate/tests/test_scenarios.py

Each live scenario drops and rebuilds its own schema, sc_<name>, in ucstate_test,
and in ucstate_shadow too when the compiler declines its after/. A failed scenario
leaves its schema as it was, to look at. A passing one saves what it read as
recorded.json, which the offline replay plans from. A scenario whose
recording is current is skipped, since the replay covers it: UCSTATE_RECORD=1
runs it anyway. UCSTATE_DRY_RUN=1 lists what a live run would create and
which scenarios it would run, and creates nothing.
"""

import shutil

import pytest

from livegate import ENABLED, PROFILE, REASON, WAREHOUSE
from scenarios import ROOT, Replay, Scenario, all_scenarios, check_plan, load, quiet
from ucmodel.normalise import normalise
from ucmodel.workspace import Workspace
from ucstate.apply import execute
from ucstate.plan import Change, Plan, PlanError

SCENARIOS = all_scenarios()
SKIPPED = "recorded and current; the replay test covers it; UCSTATE_RECORD=1 runs it again"
# A scenario whose recording is current is skipped, with its marks set here
# so that it isn't connected for.
LIVE = [pytest.param(s, id=s.name, marks=pytest.mark.skipif(not s.needs_live_run(), reason=SKIPPED))
        for s in SCENARIOS]


@pytest.fixture(scope="module")
def ws():
    return Workspace.connect(PROFILE or None, WAREHOUSE)


# Offline ---------------------------------------------------------------------------------


def test_a_scenario_counts_the_tables_and_volumes_a_live_run_creates():
    # after/ and before/ once each, when the compiler accepts after/.
    assert load("drop_table").creates() == {"table": 3, "volume": 0}
    assert load("metadata").creates() == {"table": 2, "volume": 2}


def test_a_scenario_the_compiler_declines_also_builds_after_in_the_shadow():
    """rebuildable is a CTAS, which the compiler declines (docs/offline.md, section 3)."""
    declined = load("rebuildable")
    assert declined.compiled() is None
    tables = len([f for f in declined.sources("after") if f.key.startswith("table:")])
    before = len([f for f in declined.sources("before") if f.key.startswith("table:")])
    assert declined.creates()["table"] == 2 * tables + before
    accepted = load("drop_table")
    assert accepted.compiled() is not None
    assert accepted.creates()["table"] == len(
        [f for f in accepted.sources("after") if f.key.startswith("table:")]) + len(
        [f for f in accepted.sources("before") if f.key.startswith("table:")])


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario_files_lint(scenario):
    for side in ("before", "after"):
        folders = [p.name for p in (scenario.path / side / "src").iterdir()]
        assert folders == [scenario.schema], f"{side}/src must hold only {scenario.schema}"
        assert scenario.sources(side)
    assert scenario.expect["description"]
    assert scenario.expect.get("classes") is not None


# The rows of the table "What each change generates" in docs/design.md, section 3.
ROWS = {
    "create": "New schema, table, view, function or volume",
    "add_nullable_column": "A nullable column added",
    "reorder_columns": "Column order changed",
    "metadata": "Comments, tags or declared properties changed",
    "replace_view_function": "View or function body changed",
    "keys": "PK or FK added, dropped or changed",
    "clustering": "Clustering keys changed",
    "governance": "Grants, masks or row filters changed",
    "relax": "NOT NULL, a default or a CHECK dropped",
    "add_not_null": "NOT NULL added",
    "add_check": "A CHECK constraint added",
    "add_not_null_default": "A NOT NULL column with a default added",
    "first_default": "A table's first column default",
    "widen_type": "A type widened",
    "rename_drop_column": "A column renamed or dropped",
    "rename_table": "A table renamed",
    "drop_table": "A table dropped",
    "rewrite": "A type narrowed or made incompatible, partitioning changed, or an identity or "
               "generated column added",
    "ambiguous": "A column or table disappears and one of the same shape appears",
    "rebuildable": "Any change to a rebuildable table",
}


def test_every_row_of_the_plans_table_has_a_scenario_that_runs():
    """Phase 4's done-when. A row whose change can run needs a scenario where it runs."""
    covered = {r for s in SCENARIOS for r in s.expect["covers"]}
    assert covered == set(ROWS), sorted(covered ^ set(ROWS))
    runs = {r for s in SCENARIOS if not s.expect.get("blocked") for r in s.expect["covers"]}
    assert set(ROWS) - runs == {"ambiguous"}


def test_every_class_that_can_block_has_a_blocked_control():
    blocked = {c for s in SCENARIOS if s.expect.get("blocked") for c in s.expect["classes"]}
    assert blocked >= {"checked", "one-way", "destructive", "rewrite", "ambiguous"}
    for s in SCENARIOS:
        assert s.name.startswith("control_") == bool(
            s.expect.get("blocked") or s.expect.get("lands") is False), s.name


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario_replayed(scenario):
    """test_scenario's plan checks, on what its last passing live run read."""
    recorded = scenario.recording()
    if recorded is None:
        pytest.skip("no recording yet: run it live once")
    assert recorded["inputs"] == scenario.inputs(), (
        "before/, after/ or seed.sql changed since the recording: run it live again")
    fresh, result = scenario.replay(recorded)
    assert not fresh.blocked and {c.cls for c in fresh.changes} <= {"auto", "script"}, (
        fresh.text())
    assert check_plan(result, scenario.expect) == [], result.text()


def copy_of(name: str, tmp_path) -> Scenario:
    """A scenario copied to tmp_path, without its recording."""
    shutil.copytree(ROOT / name, tmp_path / name)
    scenario = Scenario(name, tmp_path / name, load(name).expect)
    scenario.recording_path.unlink(missing_ok=True)
    return scenario


def test_a_recording_goes_stale_when_what_it_was_built_from_changes(tmp_path):
    """The replay's own control: an edited scenario must not replay an old recording."""
    scenario = copy_of("add_check", tmp_path)
    unchanged = scenario.inputs()
    assert unchanged == load("add_check").inputs()
    (scenario.path / "seed.sql").write_text("INSERT INTO t VALUES (-1);\n")
    seeded = scenario.inputs()
    table = next((scenario.path / "before" / "src").rglob("*.sql"))
    table.write_text(table.read_text() + "\nCOMMENT ON TABLE t IS 'x';\n")
    assert len({unchanged, seeded, scenario.inputs()}) == 3


def test_a_recording_must_replay_to_the_live_runs_plans(tmp_path):
    """The recording's own control: one that replays to another plan isn't saved."""
    scenario = copy_of("add_check", tmp_path)
    empty = {"catalog": "ucstate_test", "schemas": {scenario.schema: None}}
    nothing = Plan("ucstate_test", [], [])
    wrong = Plan("ucstate_test", [Change("table:sc_add_check.t", "auto", "add column x")], [])
    with pytest.raises(AssertionError, match="different plan"):
        scenario.record(empty, empty, nothing, wrong)
    assert scenario.recording() is None
    scenario.record(empty, empty, nothing, nothing)
    recorded = scenario.recording()
    assert recorded and recorded["inputs"] == scenario.inputs()
    assert [p.text() for p in scenario.replay(recorded)] == [nothing.text()] * 2


def test_a_replay_refuses_a_precheck_it_has_no_result_for():
    ws = Replay({"SELECT 1": 0})
    assert ws.sql("SELECT 1") == [{"violations": "0"}]
    with pytest.raises(AssertionError, match="Run the scenario live again"):
        ws.sql("SELECT 2")


def test_check_plan_catches_a_wrong_plan():
    """The harness's own control: a plan that differs from expect.yml must not pass."""
    auto = Change("table:s.t", "auto", "add column x", ["ALTER TABLE s.t ADD COLUMN x INT"])
    blocked = Change("table:s.t", "destructive", "drop column y", blocked="needs an allowance")
    expect = {"classes": ["auto"], "contains": ["ADD COLUMN x INT"], "says": ["add column x"]}
    assert check_plan(Plan("c", [auto], []), expect) == []
    assert check_plan(Plan("c", [auto, blocked], []), expect) == [
        "classes ['auto', 'destructive'], expected ['auto']",
        "blocked is True, expected False"]
    assert check_plan(Plan("c", [], []), expect) == [
        "classes [], expected ['auto']", "no statement contains 'ADD COLUMN x INT'",
        "the plan doesn't say 'add column x'"]
    assert check_plan(Plan("c", [auto], []), {"classes": ["destructive"], "blocked": True}) == [
        "classes ['auto'], expected ['destructive']", "blocked is False, expected True"]


# Live ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", LIVE)
@pytest.mark.live
@pytest.mark.skipif(not ENABLED, reason=REASON)
def test_scenario(ws, scenario):
    compiled = scenario.compiled()
    desired = scenario.desired(ws)

    # 1. after/ from an empty schema: creates, and any one-time scripts. This
    # is the only build of after/, so what the target reads now is the recording's.
    scenario.reset(ws)
    fresh = scenario.plan(ws, desired)
    assert not fresh.blocked and {c.cls for c in fresh.changes} <= {"auto", "script"}, (
        fresh.text())
    execute(ws, fresh, log=quiet)
    assert scenario.problems_after_apply(ws, desired, fresh) == [], fresh.text()
    after = scenario.read(ws)
    if compiled is not None:
        assert scenario.conformance(compiled, after) == [], (
            "the compiler's model of after/ differs from the platform's build of it "
            "(desired is the compiled model, live the build): the compiler is wrong, "
            "or a rule it applies needs a new case (docs/offline.md, section 3)")

    # 2. before/ to after/.
    scenario.build_before(ws)
    before = scenario.read(ws)
    result = scenario.plan(ws, desired, live=normalise(before))
    assert check_plan(result, scenario.expect) == [], result.text()
    if result.blocked:
        with pytest.raises(PlanError, match="blocked"):
            execute(ws, result, log=quiet)
    else:
        # 3. It converges, or for a control that must not land, it doesn't.
        execute(ws, result, log=quiet)
        problems = scenario.problems_after_apply(ws, desired, result)
        if scenario.expect.get("lands", True):
            assert problems == [], result.text()
        else:
            assert problems != [], result.text()
    # 4. The rows survive.
    assert scenario.check_rows(ws) == []
    scenario.record(after, before, fresh, result)


@pytest.mark.live
@pytest.mark.skipif(not ENABLED, reason=REASON)
def test_a_plan_missing_its_last_change_does_not_converge(ws):
    """The convergence check's own control: it must see an unfinished apply."""
    scenario = load("add_columns")
    desired = scenario.desired(ws)
    scenario.build_before(ws)
    result = scenario.plan(ws, desired)
    assert len(result.changes) > 1
    result.changes.pop()
    execute(ws, result, log=quiet)
    assert scenario.problems_after_apply(ws, desired) != []


def test_scenarios_live_in_the_scenarios_folder():
    assert ROOT.name == "scenarios" and SCENARIOS
