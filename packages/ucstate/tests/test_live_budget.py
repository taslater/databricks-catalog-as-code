"""What a live run may create: which scenarios run, what it costs, and when it is refused.

All offline. The quota numbers are made up, and the scenarios are copies in
a temporary folder, so no recording is touched.
"""

import json
import shutil
from types import SimpleNamespace
from typing import Any

import pytest

import conftest
from budget import DEFAULTS, judge, ledger_runs, needed, pending, runnable, setting
from scenarios import ROOT, Scenario, all_scenarios, load


def copy_of(name: str, tmp_path) -> Scenario:
    """A scenario copied to tmp_path, without its recording."""
    shutil.copytree(ROOT / name, tmp_path / name)
    scenario = Scenario(name, tmp_path / name, load(name).expect)
    scenario.recording_path.unlink(missing_ok=True)
    return scenario


def item(scenario: Scenario | None) -> SimpleNamespace:
    """A stand-in for a collected test: a scenario's, or another live test's."""
    return SimpleNamespace(callspec=SimpleNamespace(params={"scenario": scenario})
                           if scenario else None)


def record(scenario: Scenario, inputs: str) -> None:
    scenario.recording_path.write_text(json.dumps({"inputs": inputs}))


# Which scenarios run -------------------------------------------------------------------


def test_a_scenario_with_a_current_recording_needs_no_live_run(tmp_path, monkeypatch):
    monkeypatch.delenv("UCSTATE_RECORD", raising=False)
    scenario = copy_of("add_check", tmp_path)
    assert scenario.needs_live_run()  # no recording
    record(scenario, scenario.inputs())
    assert not scenario.needs_live_run()


def test_a_stale_recording_or_an_ask_to_record_again_needs_a_live_run(tmp_path, monkeypatch):
    monkeypatch.delenv("UCSTATE_RECORD", raising=False)
    scenario = copy_of("add_check", tmp_path)
    record(scenario, "inputs from before an edit")
    assert scenario.needs_live_run()
    record(scenario, scenario.inputs())
    monkeypatch.setenv("UCSTATE_RECORD", "1")
    assert scenario.needs_live_run()


def test_the_estimate_counts_only_the_scenarios_that_will_run(tmp_path, monkeypatch):
    monkeypatch.delenv("UCSTATE_RECORD", raising=False)
    current, stale = copy_of("add_check", tmp_path), copy_of("drop_table", tmp_path)
    record(current, current.inputs())
    record(stale, "old")
    items = [item(current), item(stale), item(None)]
    assert runnable(items) == [items[1], items[2]]
    other = {"table": 10, "volume": 0}
    assert needed(items) == {k: stale.creates()[k] + other[k] for k in other}
    monkeypatch.setenv("UCSTATE_RECORD", "1")
    assert needed(items)["table"] == current.creates()["table"] + stale.creates()["table"] + 10


# What a scenario costs ------------------------------------------------------------------


def test_the_full_suite_costs_less_than_when_after_was_built_twice():
    total = {"table": 0, "volume": 0}
    for scenario in all_scenarios():
        for kind, n in scenario.creates().items():
            total[kind] += n
    assert total == {"table": 105, "volume": 3}


# The reserve and the budget -----------------------------------------------------------


NOW = 1_793_469_000_000  # 2026-10-31 17:50:00 UTC
MINUTE, DAY = 60_000, 86_400_000


def verdict(n: int, used: int | None, limit: int | None = 500, reserve: int = 250,
            budget: int = 60, **more: Any):
    return judge("table", n, used, limit, reserve, budget, "UCSTATE_BUDGET", **more)


def test_the_estimate_line_says_what_is_created_used_and_kept():
    assert verdict(40, 100).line == (
        "this live run creates about 40 tables; the metastore has used 100 of 500, "
        "and runs since then created about 0 more; 250 are kept free for the workspace's owner")
    assert verdict(40, 100).refusal is None
    counted = verdict(40, 100, later=15, refreshed=NOW)  # 2026-10-31 17:50 UTC
    assert counted.line == (
        "this live run creates about 40 tables; the metastore has used 100 of 500 "
        "(counted at 17:50 UTC), and runs since then created about 15 more; "
        "250 are kept free for the workspace's owner")


def test_the_runs_since_the_count_are_added_to_it():
    assert verdict(40, 100, later=110).refusal is None
    refusal = verdict(40, 100, later=111).refusal
    assert refusal and "would leave 249 free" in refusal


def test_a_run_that_fits_the_cap_but_not_the_reserve_is_refused():
    """The known-bad control: 300 + 40 is under 500, and over 500 - 250."""
    assert 300 + 40 <= 500
    refusal = verdict(40, 300).refusal
    assert refusal and "would leave 160 free" in refusal and "250 are kept" in refusal
    assert verdict(40, 210).refusal is None  # lands exactly on the reserve


def test_a_run_over_its_budget_is_refused_and_told_how_to_select_fewer():
    refusal = verdict(61, 0).refusal
    assert refusal and "budget of 60" in refusal and "-k" in refusal
    assert "UCSTATE_BUDGET" in refusal
    assert verdict(60, 0).refusal is None
    assert verdict(61, 0, budget=100).refusal is None


def test_a_run_that_creates_nothing_is_never_refused():
    assert verdict(0, 499, budget=0).refusal is None
    assert verdict(0, None, limit=None).refusal is None


def test_an_unread_quota_refuses_a_run_except_in_a_dry_run():
    assert "wasn't read" in verdict(10, None, limit=None).line
    assert verdict(10, None, limit=None).refusal
    assert verdict(10, None, limit=None, unread_ok=True).refusal is None
    assert verdict(61, None, limit=None, unread_ok=True).refusal  # the budget needs no quota


def test_volumes_have_their_own_cap_reserve_and_budget():
    assert DEFAULTS["volume"] == {"reserve": 25, "budget": 5}
    over = judge("volume", 6, 0, 50, 25, 5, "UCSTATE_BUDGET_VOLUMES")
    assert over.refusal and "UCSTATE_BUDGET_VOLUMES" in over.refusal
    short = judge("volume", 5, 21, 50, 25, 5, "UCSTATE_BUDGET_VOLUMES")
    assert short.refusal and "would leave 24 free" in short.refusal


def test_the_reserve_and_budget_come_from_the_environment():
    assert setting("table", "reserve", {}) == 250 and setting("table", "budget", {}) == 60
    env = {"UCSTATE_RESERVE": "100", "UCSTATE_BUDGET": "200", "UCSTATE_RESERVE_VOLUMES": "10",
           "UCSTATE_BUDGET_VOLUMES": "7"}
    assert [setting(k, n, env) for k in ("table", "volume") for n in ("reserve", "budget")] == [
        100, 200, 10, 7]
    with pytest.raises(ValueError, match="UCSTATE_BUDGET"):
        setting("table", "budget", {"UCSTATE_BUDGET": "lots"})


# The ledger of live runs ----------------------------------------------------------------


def ledger(*rows: dict) -> list[dict]:
    return ledger_runs("\n".join(json.dumps(row) for row in rows))


def start(run: str, ago: int, tables: int = 60, metastore: str = "m1") -> dict:
    return {"run": run, "host": "h", "metastore": metastore, "started": NOW - ago,
            "tables": tables, "volumes": 1}


def pend(runs: list[dict], metastore: str = "m1", kind: str = "table") -> int:
    return pending(runs, metastore, kind, NOW - 10 * MINUTE, NOW)


def test_a_run_counts_as_pending_only_if_it_had_not_ended_by_the_refresh():
    runs = ledger(start("a", 60 * MINUTE), {"run": "a", "ended": NOW - 20 * MINUTE},  # seen
                  start("b", 30 * MINUTE), {"run": "b", "ended": NOW - 5 * MINUTE},  # after
                  start("c", 5 * MINUTE))  # still running
    assert pend(runs) == 120
    assert pend(runs, kind="volume") == 2


def test_pending_ignores_other_metastores_and_runs_older_than_eight_days():
    runs = ledger(start("a", 5 * MINUTE, metastore="m2"), start("b", 8 * DAY + MINUTE),
                  start("c", 8 * DAY - MINUTE, tables=7))
    assert pend(runs) == 7
    assert pend(runs, metastore="m2") == 60
    assert pend(ledger()) == 0


def test_a_count_with_no_refresh_time_counts_every_recent_run():
    runs = ledger(start("a", 60 * MINUTE), {"run": "a", "ended": NOW - 59 * MINUTE})
    assert pending(runs, "m1", "table", None, NOW) == 60


def test_the_ledger_skips_lines_that_are_not_runs():
    text = "\n".join(["", "not json", "[1]", json.dumps({"run": "x", "ended": 1}),
                      json.dumps(start("a", MINUTE))])
    assert [run["run"] for run in ledger_runs(text)] == ["a"]


def test_a_stale_count_is_not_enough_when_the_ledger_shows_runs_since():
    """The known-bad control: the count reads 0 while two runs of 100 are unseen.

    0 + 100 + 100 + 60 is over 500 - 250, and 60 alone is not.
    """
    runs = ledger(start("a", 40 * MINUTE, tables=100), {"run": "a", "ended": NOW - 5 * MINUTE},
                  start("b", 20 * MINUTE, tables=100), {"run": "b", "ended": NOW - 2 * MINUTE})
    later = pend(runs)
    assert later == 200
    assert verdict(60, 0).refusal is None  # the count alone lets it run
    refusal = verdict(60, 0, later=later, refreshed=NOW - 10 * MINUTE).refusal
    assert refusal and "would leave 240 free" in refusal


def session(*tables_of_other_tests: int) -> SimpleNamespace:
    """A stand-in session of live tests that aren't scenarios, 10 tables each."""
    live = SimpleNamespace(callspec=None, get_closest_marker=lambda name: True)
    return SimpleNamespace(items=[live] * len(tables_of_other_tests), config=SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugin=lambda name: None)))


def workspace(monkeypatch, tmp_path, used: int):
    """A fake workspace whose count is `used`, and a ledger in tmp_path."""
    info = SimpleNamespace(quota_count=used, quota_limit=500, last_refreshed_at=NOW)
    client = SimpleNamespace(
        config=SimpleNamespace(host="https://h"),
        metastores=SimpleNamespace(current=lambda: SimpleNamespace(metastore_id="m1")),
        resource_quotas=SimpleNamespace(get_quota=lambda *args: SimpleNamespace(quota_info=info)))
    connect = lambda profile: SimpleNamespace(client=client)  # noqa: E731
    monkeypatch.setattr(conftest.Workspace, "connect", connect)
    monkeypatch.setattr(conftest, "LEDGER", tmp_path / ".ucstate" / "live-runs.jsonl")
    monkeypatch.setattr(conftest, "ENABLED", True)
    monkeypatch.setattr(conftest, "started", False)
    monkeypatch.delenv("UCSTATE_DRY_RUN", raising=False)


def test_a_run_that_starts_is_written_to_the_ledger_and_ended_at_the_finish(
        monkeypatch, tmp_path):
    workspace(monkeypatch, tmp_path, used=0)
    conftest.pytest_collection_finish(session(0))  # type: ignore[arg-type]
    start_line = json.loads(conftest.LEDGER.read_text())
    assert start_line | {"started": 0} == {
        "run": conftest.RUN, "host": "https://h", "metastore": "m1", "started": 0,
        "tables": 10, "volumes": 0}
    conftest.pytest_sessionfinish()
    [run] = ledger_runs(conftest.LEDGER.read_text())
    assert run["ended"] >= run["started"]


def test_a_dry_run_and_a_refused_run_write_nothing(monkeypatch, tmp_path):
    workspace(monkeypatch, tmp_path, used=0)
    monkeypatch.setenv("UCSTATE_DRY_RUN", "1")
    with pytest.raises(pytest.exit.Exception):
        conftest.pytest_collection_finish(session(0))  # type: ignore[arg-type]
    conftest.pytest_sessionfinish()
    workspace(monkeypatch, tmp_path, used=300)  # 300 + 10 is over 500 - 250
    with pytest.raises(pytest.exit.Exception, match="refused"):
        conftest.pytest_collection_finish(session(0))  # type: ignore[arg-type]
    conftest.pytest_sessionfinish()
    assert not conftest.LEDGER.exists()
