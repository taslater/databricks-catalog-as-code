"""The `live` marker, and before a live run creates anything, a check that the
metastore has room for it and that the run stays within its budget.

Free Edition caps a metastore at 500 tables and 50 volumes, and a dropped
one counts until its UNDROP window ends, about 7 days (AGENTS.md). A full
live run creates a couple of hundred tables, and the quota may be shared
with the workspace's owner. So before any test starts, the run prints what
it will create, and stops when that is over its budget or would leave less
than the reserve free. UCSTATE_DRY_RUN=1 prints it and stops either way.

The metastore's count lags, so a run that starts is written to a ledger,
`.ucstate/live-runs.jsonl`, and the runs since the count's refresh are added
to it. The ledger is local: a run elsewhere isn't in it.
"""

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from budget import VARIABLES, judge, ledger_runs, needed, pending, runnable, scenario_of, setting
from livegate import ENABLED, PROFILE
from ucmodel.workspace import Workspace


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: runs against the Databricks workspace; needs UCSTATE_PROFILE")


LEDGER = Path(__file__).resolve().parents[3] / ".ucstate" / "live-runs.jsonl"
RUN = uuid.uuid4().hex[:8]
started = False


def quota(client, metastore: str, kind: str) -> tuple[int | None, int | None, int | None]:
    info = client.resource_quotas.get_quota("METASTORE", metastore, f"{kind}-quota").quota_info
    return (info.quota_count, info.quota_limit, info.last_refreshed_at) if info else (
        None, None, None)


def write(row: dict) -> None:
    LEDGER.parent.mkdir(exist_ok=True)
    with LEDGER.open("a") as file:
        file.write(json.dumps(row) + "\n")


def pytest_collection_finish(session: pytest.Session) -> None:
    dry = os.environ.get("UCSTATE_DRY_RUN") == "1"
    live = [item for item in session.items if item.get_closest_marker("live")]
    if not (ENABLED or dry) or not live:
        return
    report = session.config.pluginmanager.get_plugin("terminalreporter")
    say = report.write_line if report else print
    client, metastore = None, ""
    if ENABLED:
        client = Workspace.connect(PROFILE or None).client
        metastore = client.metastores.current().metastore_id or ""
    refused = []
    now = int(time.time() * 1000)
    runs = ledger_runs(LEDGER.read_text()) if LEDGER.exists() else []
    for kind, n in needed(live).items():
        used, limit, refreshed = quota(client, metastore, kind) if client else (None,) * 3
        verdict = judge(kind, n, used, limit, setting(kind, "reserve", os.environ),
                        setting(kind, "budget", os.environ), VARIABLES[kind]["budget"],
                        unread_ok=client is None,
                        later=pending(runs, metastore, kind, refreshed, now),
                        refreshed=refreshed)
        say(verdict.line)
        if verdict.refusal:
            refused.append(f"{kind}s: {verdict.refusal}")
    if dry:
        if refused:
            say("a real run would be refused. " + "; ".join(refused))
        say("these scenarios would run live: "
            + (", ".join(sorted(s.name for i in runnable(live) if (s := scenario_of(i))))
               or "none"))
        pytest.exit("a dry run: nothing was created", returncode=0)
    if refused:
        pytest.exit("this live run is refused. " + "; ".join(refused), returncode=1)
    assert client  # not a dry run, so ENABLED
    global started
    started = True
    write({"run": RUN, "host": client.config.host, "metastore": metastore, "started": now}
          | {f"{kind}s": n for kind, n in needed(live).items()})


def pytest_sessionfinish() -> None:
    if started:
        write({"run": RUN, "ended": int(time.time() * 1000)})
