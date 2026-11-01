"""What a live run may create (AGENTS.md, Constraints and "Live runs on a budget").

The quota is shared with whoever else uses the workspace, and a dropped table
counts for about 7 days. So a run states what it will create before it starts,
and is refused when that is more than its budget or leaves less than the
reserve free.

The quota's count lags by 45 to 50 minutes, so runs since its last refresh
are added from a local ledger of live runs.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

# Tables are the scarce kind. A reserve of half the cap is what a borrowed
# workspace's owner keeps at least.
DEFAULTS = {"table": {"reserve": 250, "budget": 60}, "volume": {"reserve": 25, "budget": 5}}
VARIABLES = {
    "table": {"reserve": "UCSTATE_RESERVE", "budget": "UCSTATE_BUDGET"},
    "volume": {"reserve": "UCSTATE_RESERVE_VOLUMES", "budget": "UCSTATE_BUDGET_VOLUMES"},
}

# The most that a live test other than a scenario creates: a shadow build,
# the lock, a rebuilt round-trip schema or a rehearsal's clones.
OTHER = {"table": 10, "volume": 0}


# A run older than this has aged out of the quota anyway.
LEDGER_DAYS = 8


@dataclass
class Verdict:
    line: str
    refusal: str | None = None


def setting(kind: str, name: str, env: Mapping[str, str]) -> int:
    variable = VARIABLES[kind][name]
    value = env.get(variable, "")
    if not value:
        return DEFAULTS[kind][name]
    if not value.isdigit():
        raise ValueError(f"{variable} is {value!r}, and must be a whole number")
    return int(value)


def ledger_runs(text: str) -> list[dict]:
    """The runs in a ledger's JSON lines: a start line, and an `ended` line when it finished.

    A run with no `ended` is treated as still running. Lines that aren't a run's are skipped.
    """
    runs: dict[str, dict] = {}
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "run" in row:
            runs.setdefault(row["run"], {}).update(row)
    return [run for run in runs.values() if "started" in run]


def pending(runs: list[dict], metastore: str, kind: str, refreshed: int | None, now: int) -> int:
    """What recent runs on the metastore created that the quota's count may not include.

    Those are the runs that hadn't ended by the count's refresh (epoch ms), and that
    started within the last LEDGER_DAYS. A run the count partly saw is counted whole.
    """
    return sum(run.get(f"{kind}s", 0) for run in runs
               if run.get("metastore") == metastore
               and now - run["started"] <= LEDGER_DAYS * 86_400_000
               and (run.get("ended") is None or run["ended"] > (refreshed or 0)))


def judge(kind: str, n: int, used: int | None, limit: int | None, reserve: int,
          budget: int, variable: str, unread_ok: bool = False, later: int = 0,
          refreshed: int | None = None) -> Verdict:
    """The line to print for one kind, and why the run is refused, if it is.

    `used` and `limit` are None when the quota wasn't read, which refuses a
    run unless `unread_ok`, as in a dry run without credentials. `later` is
    what the ledger says was created since the count, refreshed at `refreshed`.
    """
    if used is None or limit is None:
        line = (f"this live run creates about {n} {kind}s; the metastore's {kind} quota "
                f"wasn't read; {reserve} are kept free for the workspace's owner")
    else:
        counted = (f" (counted at {datetime.fromtimestamp(refreshed / 1000, UTC):%H:%M} UTC)"
                   if refreshed else "")
        line = (f"this live run creates about {n} {kind}s; the metastore has used {used} of "
                f"{limit}{counted}, and runs since then created about {later} more; "
                f"{reserve} are kept free for the workspace's owner")
    if not n:
        return Verdict(line)
    if n > budget:
        return Verdict(line, f"that is over the budget of {budget} {kind}s. Select fewer tests "
                             f"with -k, or raise {variable} deliberately")
    if used is None or limit is None:
        return Verdict(line, None if unread_ok else
                       "the reserve can't be checked, since the quota wasn't read")
    if used + later + n > limit - reserve:
        return Verdict(line, f"that would leave {max(limit - used - later - n, 0)} free, and "
                             f"{reserve} are kept for the workspace's owner. Run fewer tests, "
                             f"or wait for dropped {kind}s to stop counting (about 7 days)")
    return Verdict(line)


def scenario_of(item):
    callspec = getattr(item, "callspec", None)
    return callspec.params.get("scenario") if callspec else None


def runnable(items: list) -> list:
    """The live tests that will create something: not a scenario whose recording is current."""
    return [i for i in items if not (s := scenario_of(i)) or s.needs_live_run()]


def needed(items: list) -> dict[str, int]:
    total = dict.fromkeys(OTHER, 0)
    for item in runnable(items):
        scenario = scenario_of(item)
        for kind, n in (scenario.creates() if scenario else OTHER).items():
            total[kind] += n
    return total
