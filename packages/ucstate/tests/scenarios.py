"""The scenario harness (docs/design.md, section 7, and AGENTS.md).

A scenario is a folder under scenarios/:

    expect.yml        what the plan from before/ to after/ must hold
    before/src/...    a source tree for one schema, sc_<scenario name>
    after/src/...     the same schema, changed
    after/refactor.yml, after/migrations/...
                      optional renames and scripts, as in a project
    seed.sql          optional rows, inserted once before/ is built
    recorded.json     what the last passing live run read, for the replay

expect.yml keys:

    description   one line
    covers        the rows of docs/design.md's section 3 table it exercises
    policy        optional, as in project.yml: {unmanaged_objects: drop}
    allow         optional, as in project.yml: [{drop_table: sc_x.t}]
    classes       the set of classes the plan holds
    blocked       true when the plan must be blocked, so nothing runs
    contains      optional strings that some statement of the plan must contain
    says          optional strings that the plan's text must contain
    lands         false for a control whose runnable plan must not converge,
                  such as a transition script that lands on the wrong state
    rows          optional checks after the migration: {query, result}, where
                  result is the first value of the first row

Each scenario is checked for these, against ucstate_test:

1. after/ built from an empty schema converges;
2. the plan from before/ (with its seed rows) to after/ holds exactly the
   expected classes, and is blocked or not as expected;
3. that plan, when it is runnable, converges (or, with `lands: false`,
   doesn't);
4. the rows checks hold afterwards.

Converging means planning again gives no changes, and `compare` finds no
difference, apart from objects the report policy leaves alone. The second
check doesn't trust the planner, so a planner that missed a change can't
pass by also missing it on the second run.

A passing live run records what it read: the target's after/, built from
empty, its before/ with its seed rows, and what each precheck found. The replay makes
the plans of checks 1 and 2 from that recording, offline, and checks them:
all of check 2, and check 1's plan but not its convergence. So a planner
change is tested without creating a table (AGENTS.md, Constraints).
Convergence and the rows need the workspace. A recording is kept until
before/, after/ or seed.sql changes, since the state it holds comes from
them. A live run of a scenario whose recording is current is skipped, since
the replay covers it and each live run uses up tables (AGENTS.md, Constraints).
UCSTATE_RECORD=1 runs it and records again anyway.

after/ is built once, in the target. The desired model comes from the
compiler when it accepts after/, and from a shadow build when it declines.
The target's build of after/ then stands in for the shadow's in the
recording, and when the compiler made the desired model it is checked
against that build, which is the compiler's conformance check.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import yaml

from ucmodel import reader
from ucmodel.compiler import Unsupported, compile_statements
from ucmodel.model import Difference, compare
from ucmodel.normalise import normalise
from ucmodel.samples import anonymise
from ucmodel.sqltext import ident, split_statements
from ucmodel.workspace import Workspace
from ucstate import shadow
from ucstate.apply import left_to_do, run_prechecks
from ucstate.intent import History
from ucstate.plan import Plan, plan
from ucstate.project import Project, SourceFile

ROOT = Path(__file__).parent.parent / "scenarios"
CATALOG = "ucstate_test"
SHADOW = "ucstate_shadow"


def quiet(message: str) -> None:
    pass


@dataclass
class Scenario:
    name: str
    path: Path
    expect: dict

    @property
    def schema(self) -> str:
        return f"sc_{self.name}"

    @property
    def unmanaged(self) -> str:
        return (self.expect.get("policy") or {}).get("unmanaged_objects", "report")

    def project(self, side: str) -> Project:
        return Project(root=self.path / side, schemas=[self.schema], shadow_catalog=SHADOW,
                       targets={"test": CATALOG}, warehouse_id=None, unmanaged=self.unmanaged,
                       allow=self.expect.get("allow") or [])

    def sources(self, side: str) -> list[SourceFile]:
        return self.project(side).sources()

    def compiled(self) -> dict | None:
        """The compiler's model of after/, or None when it declines."""
        try:
            return compile_statements((f.schema, s) for f in self.sources("after")
                                      for s in f.statements)
        except Unsupported:
            return None

    def creates(self) -> dict[str, int]:
        """The tables and volumes a live run of this scenario creates, by kind.

        after/ is built once, in the target, and before/ once. When the
        compiler declines after/, the shadow builds it a second time.
        Views count as tables.
        """
        builds = 1 if self.compiled() is not None else 2

        def count(side: str, kinds: tuple[str, ...]) -> int:
            return sum(f.key.startswith(kinds) for f in self.sources(side))
        return {kind: builds * count("after", prefixes) + count("before", prefixes)
                for kind, prefixes in (("table", ("table:", "view:")), ("volume", ("volume:",)))}

    def needs_live_run(self) -> bool:
        """Whether a live run is worth its tables: it has no current recording, or
        UCSTATE_RECORD=1 asks for one again."""
        recorded = self.recording()
        return bool(os.environ.get("UCSTATE_RECORD")) or not (
            recorded and recorded["inputs"] == self.inputs())

    def desired(self, ws: Workspace) -> dict:
        """The desired model: the compiler's, or the shadow's when it declines."""
        compiled = self.compiled()
        return compiled if compiled is not None else shadow.build(
            ws, self.project("after"), log=quiet)

    def conformance(self, compiled: dict, after: dict) -> list[Difference]:
        """How the compiler's model of after/ differs from the platform's build of it.

        `after` is a raw read of that build. Empty when they agree.
        """
        return compare(compiled, shadow.declared(normalise(after), self.sources("after")))

    def plan(self, ws: Workspace, desired: dict, history: History | None = None,
             live: dict | None = None) -> Plan:
        """The plan to after/, with its prechecks run. The scenario's history starts empty.

        `live` is the target's model, read from `ws` when it isn't given.
        """
        live = self.live(ws) if live is None else live
        result = plan(desired, live, self.sources("after"), CATALOG, self.unmanaged,
                      intent=self.project("after").intent(), history=history)
        run_prechecks(ws, result, log=quiet)
        return result

    def read(self, ws: Workspace, catalog: str = CATALOG) -> dict:
        return reader.read(ws, catalog, [self.schema])

    def live(self, ws: Workspace) -> dict:
        return normalise(self.read(ws))

    def reset(self, ws: Workspace) -> None:
        ws.sql(f"DROP SCHEMA IF EXISTS {ident(CATALOG)}.{ident(self.schema)} CASCADE")

    def build_before(self, ws: Workspace) -> None:
        self.reset(ws)
        shadow.apply(ws, CATALOG, self.sources("before"), log=quiet)
        seed = self.path / "seed.sql"
        if seed.exists():
            for statement in split_statements(seed.read_text()):
                ws.sql(statement, catalog=CATALOG, schema=self.schema)

    def problems_after_apply(self, ws: Workspace, desired: dict,
                             applied: Plan | None = None) -> list[str]:
        """What is left to do after an apply. Empty when it converged.

        Both checks run, the planner's and apply's own verify, so neither has
        to be trusted. `applied` is the plan that ran: what it recorded is the
        history the second plan starts from, as apply would leave it.
        """
        history = History()
        if applied:
            history = History({r["id"] for r in applied.refactors},
                              {s["id"]: s["checksum"] for s in applied.scripts})
        again = self.plan(ws, desired, history)
        live = self.live(ws)
        problems = [f"planning again: {c.cls} {c.key}: {c.what}" for c in again.changes]
        return problems + [f"compare: {d}" for d in left_to_do(again, live)]

    def inputs(self) -> str:
        """A hash of what the recorded state was built from: the statements of
        before/ and after/, as ucstate splits them, and of seed.sql."""
        seed = self.path / "seed.sql"
        statements = {side: {f.key: f.statements for f in self.sources(side)}
                      for side in ("before", "after")}
        statements["seed"] = {"seed": split_statements(seed.read_text()) if seed.exists() else []}
        text = json.dumps(statements, sort_keys=True)
        return hashlib.sha256(text.encode()).hexdigest()

    @property
    def recording_path(self) -> Path:
        return self.path / "recorded.json"

    def recording(self) -> dict | None:
        path = self.recording_path
        return json.loads(path.read_text()) if path.exists() else None

    def record(self, after: dict, before: dict, fresh: Plan, result: Plan) -> None:
        """Save what a passing live run read, unless a recording of the same inputs exists.

        `after` and `before` are raw reads of the target, built from empty
        and from before/.
        `fresh` and `result` are the run's plans from empty and from before/,
        and the recording must replay to the same two.
        """
        if not self.needs_live_run():
            return
        prechecks = {c.precheck: c.violations for p in (fresh, result) for c in p.changes
                     if c.precheck}
        data = anonymise({"inputs": self.inputs(), "after": after, "before": before,
                          "prechecks": prechecks})
        replayed = [p.text() for p in self.replay(data)]
        if replayed != [fresh.text(), result.text()]:
            # Kept for a look, and ignored by git: a live run costs tables.
            rejected = self.path / "recorded.rejected.json"
            rejected.write_text(json.dumps({**data, "live_plans": [fresh.text(), result.text()],
                                            "replayed_plans": replayed}, indent=1) + "\n")
            raise AssertionError("the recording replays to a different plan from the live "
                                 f"run's; both are in {rejected}")
        self.recording_path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")

    def replay(self, recorded: dict) -> tuple[Plan, Plan]:
        """Checks 1 and 2's plans, from empty and from before/, made from a recording."""
        desired = shadow.declared(normalise(recorded["after"]), self.sources("after"))
        ws = Replay(recorded["prechecks"])
        return (self.plan(ws, desired, live={}),
                self.plan(ws, desired, live=normalise(recorded["before"])))

    def check_rows(self, ws: Workspace) -> list[str]:
        problems = []
        for check in self.expect.get("rows", []):
            rows = ws.sql(check["query"], catalog=CATALOG, schema=self.schema)
            value = next(iter(rows[0].values())) if rows else None
            if value != str(check["result"]):
                problems.append(f"{check['query']}: {value!r}, expected {check['result']!r}")
        return problems


class Replay(Workspace):
    """A workspace that answers a recording's prechecks, and refuses anything else."""

    def __init__(self, prechecks: dict[str, int]):
        super().__init__(SimpleNamespace())  # type: ignore[arg-type]
        self.prechecks = prechecks

    def sql(self, statement, catalog=None, schema=None, params=None):
        if statement not in self.prechecks:
            raise AssertionError(f"the recording has no result for {statement!r}. "
                                 "Run the scenario live again")
        return [{"violations": str(self.prechecks[statement])}]


def check_plan(result: Plan, expect: dict) -> list[str]:
    """How the plan differs from expect.yml. Empty when it matches."""
    problems = []
    classes, wanted = {c.cls for c in result.changes}, set(expect.get("classes") or [])
    if classes != wanted:
        problems.append(f"classes {sorted(classes)}, expected {sorted(wanted)}")
    blocked, want_blocked = bool(result.blocked), bool(expect.get("blocked"))
    if blocked != want_blocked:
        problems.append(f"blocked is {blocked}, expected {want_blocked}")
    text = "\n".join(s for c in result.changes for s in c.statements)
    problems += [f"no statement contains {s!r}" for s in expect.get("contains", [])
                 if s not in text]
    shown = result.text()
    problems += [f"the plan doesn't say {s!r}" for s in expect.get("says", []) if s not in shown]
    return problems


def load(name: str) -> Scenario:
    path = ROOT / name
    return Scenario(name, path, yaml.safe_load((path / "expect.yml").read_text()))


def all_scenarios() -> list[Scenario]:
    return [load(p.parent.name) for p in sorted(ROOT.glob("*/expect.yml"))]
