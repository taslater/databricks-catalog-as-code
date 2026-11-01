"""Applying a plan to its target: `ucstate apply` (docs/design.md, section 6).

`deploy` does, in order:

1. takes the target's lock, `_ucstate.lock` (see state.py);
2. plans from the desired model, which the compiler gives and a shadow
   build must agree with (`shadow.desired`), with the target's history of
   renames and scripts, or takes a saved plan from `ucstate plan -o`;
3. refuses a plan with any blocked change, then asks, unless told not to;
4. reads the target again, and refuses the plan if an object it touches has
   changed since it was made, or a rename or script it runs has been done
   since. That is the fingerprint check;
5. rehearses, when the plan has a change that depends on the rows (a
   `checked` change, a transition or a one-time script) and project.yml names
   a rehearsal catalog: the same migration, planned and run first on shallow
   clones of the target's tables (`rehearse`);
6. runs the prechecks again, since the rows can have changed, and refuses
   the plan if one finds rows in the way;
7. records the deployment as running, with each touched table's Delta
   version;
8. runs the statements one at a time, in plan order, stopping at the first
   failure;
9. verifies: reads the target and compares it with the desired model.
   Anything left to do fails the deployment. A transition script that
   didn't land on the declared state fails here;
10. records the desired model as the target's state, the renames and
    scripts it dealt with, and the deployment as succeeded.

The lock is released however the apply ends. There is no transaction
across DDL statements, so a failure partway leaves the target partly
changed. The deployment row says how far it got, and planning again gives
what is left.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from ucmodel import reader
from ucmodel.model import compare, object_hash
from ucmodel.normalise import normalise
from ucmodel.sqltext import ident, qualified, two_part_names
from ucmodel.workspace import SqlError, Workspace

from . import shadow
from .intent import History, Intent
from .plan import Plan, PlanError, plan, renames
from .project import Project
from .state import Run, Store


class ApplyError(Exception):
    """The apply stopped. When it got as far as running, its deployment row says why."""


class StatementFailed(ApplyError):
    pass


@dataclass
class Outcome:
    run: Run
    plan: Plan
    statements: list[dict]


def execute(ws: Workspace, plan: Plan, log: Callable[[str], None] = print,
            ran: list[dict] | None = None) -> list[dict]:
    """Run the plan's statements in order. Each one is appended to `ran` as it finishes."""
    if plan.blocked:
        raise PlanError(f"{len(plan.blocked)} blocked changes; nothing was run")
    if unchecked := [c.key for c in plan.changes if c.precheck and c.violations is None]:
        raise PlanError("the prechecks haven't run, so nothing was run: " + ", ".join(unchecked))
    ran = [] if ran is None else ran
    for change in plan.changes:
        for statement in change.statements:
            start = time.monotonic()
            record = {"key": change.key, "statement": statement}
            try:
                ws.sql(statement, catalog=plan.catalog, schema=change.schema)
            except SqlError as e:
                ran.append({**record, "seconds": _since(start), "error": e.summary})
                raise StatementFailed(f"{change.key}: {e.summary}\n    in: "
                                      f"{statement.splitlines()[0][:100]}") from e
            ran.append({**record, "seconds": _since(start)})
    log(f"apply: {_n(len(ran), 'statement')} for {_n(len(plan.changes), 'change')} "
        f"to {plan.catalog}")
    return ran


def run_prechecks(ws: Workspace, plan: Plan, log: Callable[[str], None] = print) -> None:
    """Run each `checked` change's query. One that finds rows blocks its change."""
    for c in plan.changes:
        if not c.precheck:
            continue
        rows = ws.sql(c.precheck, catalog=plan.catalog, schema=c.schema)
        c.violations = int(next(iter(rows[0].values())) or 0) if rows else 0
        if c.violations:
            n = c.violations
            c.blocked = f"its precheck found {n} row{'s' if n != 1 else ''} in the way"
        elif c.blocked and c.blocked.startswith("its precheck found"):
            c.blocked = None
    if checked := [c for c in plan.changes if c.precheck]:
        log(f"prechecks: {sum(1 for c in checked if not c.violations)} of {len(checked)} passed")


def read_live(ws: Workspace, catalog: str, schemas: list[str]) -> dict[str, dict]:
    return normalise(reader.read(ws, catalog, schemas))


def left_to_do(plan: Plan, live: dict[str, dict]) -> list[str]:
    """How the live model still differs from the plan's desired one. Empty once it matches.

    It compares rather than plans again, so it needs no source files. The
    planner finds its changes with the same comparison, so an empty result
    here means an empty plan. Objects the plan leaves alone don't count.
    """
    out = []
    for d in compare(plan.desired, live):
        left_alone = d.path == "" and d.desired is None and (
            plan.unmanaged == "report" or live[d.key]["kind"] == "unsupported")
        if not left_alone:
            out.append(str(d))
    return out


def table_versions(ws: Workspace, catalog: str, keys: list[str],
                   live: dict[str, dict]) -> dict[str, int]:
    """Each existing table's current Delta version, from DESCRIBE HISTORY.

    REST's `delta.lastUpdateVersion` misses inserts (probes/phase3.py, q3).
    """
    out = {}
    for key in keys:
        if key.startswith("table:") and live.get(key, {}).get("kind") == "table":
            name = key.split(":", 1)[1]
            rows = ws.sql(f"DESCRIBE HISTORY {qualified(name)} LIMIT 1", catalog=catalog)
            out[name] = int(rows[0]["version"])
    return out


def deploy(ws: Workspace, project: Project, target: str, *, saved: Plan | None = None,
           approve: Callable[[Plan], bool] | None = None,
           shadow_only: bool = False, log: Callable[[str], None] = print) -> Outcome:
    """`shadow_only` skips the compiler's cross-check, and builds the shadow alone."""
    catalog = project.catalog(target)
    if ws.catalog(catalog) is None:
        raise ApplyError(f"catalog {catalog} doesn't exist. Target catalogs are created once, "
                         "outside ucstate: see scripts/setup_workspace.py")
    store = Store(ws, catalog)
    store.ensure()
    source = saved.source if saved else project.revision()
    run = Run.new(source, ws.client.current_user.me().user_name or "unknown", _run_url())
    with store.lock(run):
        history = store.history()
        if saved is None:
            desired = shadow.desired(ws, project, log=log,
                                     mode="shadow" if shadow_only else "checked")
            the_plan = plan(desired, read_live(ws, catalog, project.schemas), project.sources(),
                            catalog, project.unmanaged, intent=project.intent(), history=history)
            the_plan.source = source
        elif saved.catalog != catalog:
            raise ApplyError(f"the plan is for {saved.catalog}, not {catalog}")
        else:
            the_plan = saved
        log(the_plan.text())
        if the_plan.blocked:
            raise ApplyError(f"{len(the_plan.blocked)} blocked changes; nothing was run")
        if the_plan.changes and approve and not approve(the_plan):
            raise ApplyError("not applied")

        if project.rehearsal_catalog and needs_rehearsal(the_plan):
            rehearse(ws, project, target, the_plan.desired, history, log,
                     touched_tables(the_plan))
        elif needs_rehearsal(the_plan):
            log("apply: not rehearsed, since project.yml names no rehearsal catalog")

        live = read_live(ws, catalog, project.schemas)
        stale = the_plan.stale(live) + _done_since(the_plan, history)
        if stale:
            raise ApplyError("changed since the plan was made, so nothing was run: "
                             + ", ".join(stale) + ". Plan again")
        run_prechecks(ws, the_plan, log)
        if the_plan.blocked:
            raise ApplyError("a precheck found rows in the way, so nothing was run:\n"
                             + "\n".join(f"  {c.key}: {c.what}: {c.precheck}"
                                          for c in the_plan.blocked))
        touched = sorted({c.key for c in the_plan.changes if not c.key.startswith("script:")})
        versions = {"before": table_versions(ws, catalog,
                                             [the_plan.renamed.get(k, k) for k in touched], live),
                    "after": {}}
        record = {k: v for k, v in the_plan.to_json().items() if k != "desired"}
        store.start(run, record)
        ran: list[dict] = []
        try:
            execute(ws, the_plan, log, ran)
            after = read_live(ws, catalog, project.schemas)
            versions["after"] = table_versions(ws, catalog, touched, after)
            if left := left_to_do(the_plan, after):
                raise ApplyError("verify: the target doesn't match the source after the apply:\n"
                                 + "\n".join(f"  {line}" for line in left))
            store.write_state(run, the_plan.desired, after)
            store.record(run, the_plan.refactors, the_plan.scripts)
        except BaseException as e:
            error = str(e) or type(e).__name__
            try:
                store.finish(run, "failed", ran, versions, error)
            except Exception as also:  # noqa: BLE001 - keep the first error
                log(f"apply: and recording the failure failed too: {also}")
            raise
        store.finish(run, "succeeded", ran, versions)
    log(f"apply: deployment {run.id} succeeded")
    return Outcome(run, the_plan, ran)


# Rehearsal ---------------------------------------------------------------------------------

REHEARSED = {"checked", "rewrite", "script"}


def needs_rehearsal(plan: Plan) -> bool:
    """Whether the plan has a change whose outcome depends on the rows."""
    return any(c.cls in REHEARSED or c.script for c in plan.changes)


def touched_tables(plan: Plan) -> set[str]:
    """The tables a plan changes, by their names before any rename, and those its scripts name."""
    out = {plan.renamed.get(c.key, c.key).split(":", 1)[1] for c in plan.changes
           if c.key.startswith("table:")}
    return out | {n for c in plan.changes if c.script for st in c.statements
                  for n in two_part_names(st)}


def rehearse(ws: Workspace, project: Project, target: str, desired: dict[str, dict],
             history: History, log: Callable[[str], None] = print,
             touched: set[str] | None = None) -> Plan:
    """Run the migration on clones of the target's tables first (section 8).

    The empty shadow proves the DDL, not the migration: a precheck, a CAST in
    a transition script or a backfill can still fail on real rows. So:

    1. under the rehearsal catalog's own lock, its copies of the managed
       schemas are dropped, and every table of the target is cloned into
       them. Writes to a clone leave its source alone (Phase 0). The tables
       the plan changes, or that a script names, are deep clones: CREATE OR
       REPLACE ... AS SELECT keeps a shallow clone's PK, where it drops a
       table's or a deep clone's (probes/phase4.py, p7). The rest are
       shallow, which copies no data;
    2. the clones are given the target tables' comments, tags, grants and
       keys, which a clone doesn't copy, so each starts as its table is;
    3. the source is planned against the clones, with the target's history.
       A transition script written for a target table runs from that table's
       clone;
    4. the plan's prechecks run, then its statements, then verify.

    Views, functions and volumes aren't cloned: the plan creates them from
    the source. The schemas are left for a look afterwards, and dropped by
    the next rehearsal. `touched` names the tables to deep-clone; None
    deep-clones every one.
    """
    catalog, rehearsal = project.catalog(target), project.rehearsal_catalog
    if not rehearsal or ws.catalog(rehearsal) is None:
        raise ApplyError(f"rehearsal: catalog {rehearsal} doesn't exist. It is created once, "
                         "outside ucstate: see scripts/setup_workspace.py")
    store = Store(ws, rehearsal)
    store.ensure()
    run = Run.new(project.revision(), ws.client.current_user.me().user_name or "unknown",
                  _run_url())
    with store.lock(run):
        live = read_live(ws, catalog, project.schemas)
        for schema in project.schemas:
            ws.sql(f"DROP SCHEMA IF EXISTS {ident(rehearsal)}.{ident(schema)} CASCADE")
            ws.sql(f"CREATE SCHEMA {ident(rehearsal)}.{ident(schema)}")
        tables = sorted(o["name"] for o in live.values() if o["kind"] == "table")
        deep = [n for n in tables if touched is None or n in touched]
        for name in tables:
            ws.sql(f"CREATE TABLE {ident(rehearsal)}.{qualified(name)} "
                   f"{'DEEP' if name in deep else 'SHALLOW'} CLONE "
                   f"{ident(catalog)}.{qualified(name)}")
        log(f"rehearsal: {_n(len(tables), 'table')} of {catalog} cloned into {rehearsal}, "
            f"{len(deep)} deep")
        _as_the_target(ws, live, read_live(ws, rehearsal, project.schemas), rehearsal, log)
        clones = read_live(ws, rehearsal, project.schemas)
        intent = _rebased(project.intent(), desired, live, clones, history)
        result = plan(desired, clones, project.sources(), rehearsal, project.unmanaged,
                      intent=intent, history=history)
        run_prechecks(ws, result, log)
        if result.blocked:
            raise ApplyError(f"rehearsal in {rehearsal}: {len(result.blocked)} blocked, so "
                             f"nothing was run in {catalog}:\n"
                             + "\n".join(f"  {c.key}: {c.what}: {c.blocked}"
                                          for c in result.blocked))
        try:
            execute(ws, result, log)
        except StatementFailed as e:
            raise ApplyError(f"rehearsal in {rehearsal} failed, so nothing was run in "
                             f"{catalog}: {e}") from e
        if left := left_to_do(result, read_live(ws, rehearsal, project.schemas)):
            raise ApplyError(f"rehearsal in {rehearsal}: verify found the clones don't match "
                             f"the source, so nothing was run in {catalog}:\n"
                             + "\n".join(f"  {line}" for line in left))
    log(f"rehearsal: {_n(len(result.changes), 'change')} ran and verified in {rehearsal}")
    return result


def _as_the_target(ws: Workspace, live: dict, clones: dict, rehearsal: str,
                   log: Callable[[str], None]) -> None:
    """Give the clones their tables' comments, tags, grants and keys.

    It plans from the target's model to the clones, leaving out what the
    clones would need other objects for (masks and row filters, which need
    their functions) and properties, which a clone has from its table.
    """
    want = {}
    for key, c in clones.items():
        if key not in live or c["kind"] not in ("schema", "table"):
            continue
        t = copy.deepcopy(live[key])
        t["properties"] = {}
        if c["kind"] == "table":
            t["row_filter"], t["clustering"] = c["row_filter"], None
            masks = {col["name"]: col["mask"] for col in c["columns"]}
            for col in t["columns"]:
                col["mask"] = masks.get(col["name"])
        want[key] = t
    prep = plan(want, clones, [], rehearsal)
    run_prechecks(ws, prep, log)
    if prep.blocked:
        raise ApplyError("rehearsal: the clones can't be made like their tables:\n"
                         + "\n".join(f"  {c.key}: {c.what}" for c in prep.blocked))
    execute(ws, prep, log)


def _rebased(intent: Intent, desired: dict, live: dict, clones: dict,
             history: History) -> Intent:
    """Each transition script that fits its target table, made to start from the clone."""
    renamed_live = renames(desired, live, intent, history)[0]
    renamed_clones = renames(desired, clones, intent, history)[0]
    scripts = []
    for s in intent.scripts:
        key = s.object or ""
        if (s.kind == "transition" and key in renamed_clones
                and object_hash(renamed_live.get(key)) == s.before):
            s = dataclasses.replace(s, before=object_hash(renamed_clones[key]))
        scripts.append(s)
    return Intent(intent.allow, intent.refactors, scripts)


def _done_since(saved: Plan, history: History) -> list[str]:
    """The renames and scripts a plan would deal with that the target has dealt with since."""
    return ([f"refactor {r['id']} (done since)" for r in saved.refactors
             if r["id"] in history.refactors]
            + [f"script {s['id']} (done since)" for s in saved.scripts
               if s["id"] in history.scripts])


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _since(start: float) -> float:
    return round(time.monotonic() - start, 2)


def _run_url() -> str | None:
    """The CI run that applied, for the deployment's record: GitHub Actions or GitLab CI."""
    env = os.environ
    if env.get("GITHUB_RUN_ID") and env.get("GITHUB_REPOSITORY"):
        return (f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}")
    return env.get("CI_JOB_URL") or None
