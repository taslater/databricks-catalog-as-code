"""The `ucstate` command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
from collections import Counter
from collections.abc import Callable
from datetime import date
from pathlib import Path

from ucmodel.compiler import Unsupported, compile_statements
from ucmodel.model import compare
from ucmodel.snapshot import SnapshotError, load, within
from ucmodel.workspace import NoCredentials, SqlError, Workspace

from . import shadow
from .apply import ApplyError, deploy, read_live, rehearse, run_prechecks, touched_tables
from .choices import add_allowance, add_rename, column_name, table_name
from .intent import ALLOWANCES, History
from .plan import DROPS_FIRST, FK_DROPS, OBJECTS, Plan, PlanError, plan, renames, scaffold
from .project import Project, ProjectError
from .render import render
from .state import LockHeld, Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ucstate", description="Desired-state Unity Catalog.")
    parser.add_argument("--project", type=Path, default=Path("."),
                        help="folder holding project.yml (default: the current one)")
    parser.add_argument("--profile",
                        help="Databricks CLI profile. Without one, DATABRICKS_* variables are used")
    parser.add_argument("--shadow-catalog",
                        help="build the shadow here instead of project.yml's shadow catalog")
    parser.add_argument("--warehouse",
                        help="run statements on this SQL warehouse. Without it, the "
                             "DATABRICKS_WAREHOUSE_ID variable, then project.yml's warehouse_id")
    parser.add_argument("--shadow", action="store_true",
                        help="build the desired model in the shadow catalog, without compiling "
                             "the source first")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="write source files from a target's live state")
    p.add_argument("--target", required=True)
    p.add_argument("--force", action="store_true",
                   help="replace the managed schemas' folders under src/ if they exist")

    p = sub.add_parser("model", help="print a target's live model as JSON")
    p.add_argument("--target", required=True)
    p.add_argument("-o", "--out", type=Path)

    p = sub.add_parser("shadow", help="build the shadow catalog and print the desired model")
    p.add_argument("-o", "--out", type=Path)

    p = sub.add_parser("compare", help="compare the source with a target")
    _target_options(p)

    p = sub.add_parser("plan", help="plan the changes a target needs")
    _target_options(p)
    p.add_argument("-o", "--out", type=Path, help="also write the plan, for `apply --plan`")

    p = sub.add_parser("apply", help="plan, apply, verify and record, under the target's lock")
    p.add_argument("--target", required=True)
    p.add_argument("--plan", type=Path, help="apply this saved plan rather than a fresh one")
    p.add_argument("--yes", action="store_true", help="apply a fresh plan without asking")

    p = sub.add_parser("rehearse", help="run a target's plan on clones of its tables")
    p.add_argument("--target", required=True)

    p = sub.add_parser("transition", help="write a transition script for one table's change")
    _target_options(p, "the target whose table the script starts from")
    p.add_argument("--object", required=True, help="the table, such as sales.orders")
    p.add_argument("--id", help="the script's id and file name (default: today's date and "
                                "the table's name)")

    p = sub.add_parser("history", help="list a target's recent deployments")
    p.add_argument("--target", required=True)
    p.add_argument("-n", type=int, default=10)

    p = sub.add_parser("unlock", help="release a target's lock, left by an apply that died")
    p.add_argument("--target", required=True)

    p = sub.add_parser("rename-table", help="add a table rename to refactor.yml")
    p.add_argument("old", help="the table's name now, such as sales.orders")
    p.add_argument("new", help="its new name, such as sales.order_headers")
    p.add_argument("--id", help="the entry's id (default: today's date and the new name)")

    p = sub.add_parser("rename-column", help="add a column rename to refactor.yml")
    p.add_argument("table", help="the table, such as sales.orders")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--id", help="the entry's id (default: today's date, the table and the new "
                                "name)")

    p = sub.add_parser("allow", help="add an allowance for a destructive change to project.yml")
    p.add_argument("kind", choices=ALLOWANCES)
    p.add_argument("name", help="the object, such as sales.orders, or sales.orders.code for "
                                "a column")

    args = parser.parse_args(argv)
    # A cancelled CI job gets SIGTERM. As SystemExit it still records the
    # deployment as failed and releases the lock.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        project = Project.load(args.project, args.shadow_catalog)
        against = getattr(args, "against", None)
        if hasattr(args, "against") and not (against or args.target):
            raise ProjectError("pass --target, or --against with a snapshot")
        if against and args.shadow:
            raise ProjectError("--shadow builds in a workspace, and --against uses none")
        if against or args.command in OFFLINE:
            return COMMANDS[args.command](project, None, args)
        ws = Workspace.connect(args.profile, warehouse(args.warehouse, project))
        return COMMANDS[args.command](project, ws, args)
    except (ApplyError, LockHeld, SqlError, shadow.CompilerDisagrees) as e:
        print(f"ucstate: {e}", file=sys.stderr)
        return 1
    except (ProjectError, shadow.ShadowError, PlanError, NoCredentials, SnapshotError) as e:
        print(f"ucstate: {e}", file=sys.stderr)
        return 2


def _target_options(p: argparse.ArgumentParser, help: str | None = None) -> None:
    p.add_argument("--target", help=help)
    p.add_argument("--against", type=Path,
                   help="use this snapshot, from `ucmodel snapshot`, as the target, with no "
                        "workspace. It needs the compiler to accept the whole source")


def warehouse(option: str | None, project: Project) -> str | None:
    """The `--warehouse` option, else DATABRICKS_WAREHOUSE_ID, else project.yml's."""
    return option or os.environ.get("DATABRICKS_WAREHOUSE_ID") or project.warehouse_id


def _live(project: Project, ws: Workspace, target: str) -> dict:
    return read_live(ws, project.catalog(target), project.schemas)


def _target(project: Project, ws: Workspace | None, args: argparse.Namespace
            ) -> tuple[str, dict[str, dict], str | None]:
    """The target's catalog and live model, and the snapshot's hash when one stands in."""
    if not args.against:
        assert ws is not None
        return project.catalog(args.target), _live(project, ws, args.target), None
    s = load(args.against)
    if args.target and project.catalog(args.target) != s.catalog:
        raise ProjectError(f"{args.against} is of {s.catalog}, but target {args.target} is "
                           f"{project.catalog(args.target)}")
    if missing := [name for name in project.schemas if name not in s.schemas]:
        raise ProjectError(f"{args.against} didn't read schema {', '.join(missing)}. Take a "
                           "snapshot with it, or with every schema")
    return s.catalog, within(s.model, project.schemas), s.hash


def _history(ws: Workspace | None, catalog: str) -> History:
    """What the target's _ucstate has recorded. A snapshot has none."""
    return Store(ws, catalog).history() if ws else History()


def _desired(project: Project, ws: Workspace | None, args: argparse.Namespace) -> dict:
    if ws is not None:
        return shadow.desired(ws, project, mode="shadow" if args.shadow else "auto")
    try:
        return compile_statements((f.schema, s) for f in project.sources() for s in f.statements)
    except Unsupported as e:
        raise ProjectError(f"the compiler declined: {str(e).splitlines()[0]}. With --against "
                           "there is no workspace to build a shadow in, so plan with --target "
                           "instead") from None


def _dump(model: dict, out: Path | None) -> None:
    text = json.dumps(model, indent=2) + "\n"
    if out:
        out.write_text(text)
    else:
        sys.stdout.write(text)


def cmd_import(project: Project, ws: Workspace, args: argparse.Namespace) -> int:
    model = _live(project, ws, args.target)
    for schema in project.schemas:
        folder = project.root / "src" / schema
        if f"schema:{schema}" not in model:
            raise ProjectError(f"schema {schema} not found in {project.catalog(args.target)}")
        if folder.exists():
            if not args.force:
                raise ProjectError(f"{folder} exists; pass --force to replace it")
            shutil.rmtree(folder)
    files = render(model)
    for rel, text in files.items():
        path = project.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    kinds = Counter(key.split(":")[0] for key, o in model.items() if o["kind"] != "unsupported")
    print(f"wrote {len(files)} files: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
    for obj in model.values():
        if obj["kind"] == "unsupported":
            print(f"skipped {obj['name']}: {obj['type']} is outside the model")
    return 0


def cmd_model(project: Project, ws: Workspace, args: argparse.Namespace) -> int:
    _dump(_live(project, ws, args.target), args.out)
    return 0


def cmd_shadow(project: Project, ws: Workspace, args: argparse.Namespace) -> int:
    _dump(shadow.build(ws, project, log=lambda m: print(m, file=sys.stderr)), args.out)
    return 0


def cmd_compare(project: Project, ws: Workspace | None, args: argparse.Namespace) -> int:
    desired = _desired(project, ws, args)
    catalog, live, _ = _target(project, ws, args)
    diffs = compare(desired, live)
    for d in diffs:
        print(d)
    if diffs:
        print(f"{len(diffs)} difference{'s' if len(diffs) != 1 else ''} between the source "
              f"and {catalog}")
        return 1
    print(f"the source and {catalog} agree on {len(desired)} objects")
    return 0


def cmd_plan(project: Project, ws: Workspace | None, args: argparse.Namespace) -> int:
    """Exit 0 when the plan can run (or is empty), 1 when any change is blocked."""
    desired = _desired(project, ws, args)
    catalog, live, against = _target(project, ws, args)
    result = plan(desired, live, project.sources(), catalog, project.unmanaged,
                  intent=project.intent(), history=_history(ws, catalog))
    result.source, result.against = project.revision(), against
    for option in (o for c in result.changes for o in c.options):
        if option["command"][0] == "transition":
            option["command"] += (["--against", str(args.against)] if against
                                  else ["--target", args.target])
    if ws is not None:
        run_prechecks(ws, result, log=lambda m: print(m, file=sys.stderr))
    print(result.text())
    if args.out:
        args.out.write_text(json.dumps(result.to_json(), indent=2) + "\n")
    return 1 if result.blocked else 0


def cmd_apply(project: Project, ws: Workspace, args: argparse.Namespace) -> int:
    """Exit 0 once the target matches the source, 1 when the apply stopped."""
    saved = Plan.from_json(json.loads(args.plan.read_text())) if args.plan else None
    if saved and saved.against:
        raise ApplyError(f"{args.plan} was planned against a snapshot, as a preview. Plan "
                         "against the target to apply")
    approve = None
    if not (saved or args.yes):
        if not sys.stdin.isatty():
            raise ApplyError("pass --yes to apply a fresh plan without a prompt")
        approve = _ask
    deploy(ws, project, args.target, saved=saved, approve=approve,
           shadow_only=args.shadow)
    return 0


def _ask(p: Plan) -> bool:
    n = len(p.changes)
    answer = input(f"\nApply {n} change{'s' if n != 1 else ''} to {p.catalog}? Type yes: ")
    return answer.strip() == "yes"


def cmd_rehearse(project: Project, ws: Workspace, args: argparse.Namespace) -> int:
    """Exit 0 when the plan ran and verified on the clones, 1 when it didn't."""
    if not project.rehearsal_catalog:
        raise ProjectError("project.yml names no rehearsal catalog, as in "
                           "rehearsal: {catalog: ucstate_rehearsal}")
    desired = _desired(project, ws, args)
    catalog = project.catalog(args.target)
    history = Store(ws, catalog).history()
    planned = plan(desired, _live(project, ws, args.target), project.sources(), catalog,
                   project.unmanaged, intent=project.intent(), history=history)
    print(rehearse(ws, project, args.target, desired, history,
                   touched=touched_tables(planned)).text())
    return 0


def cmd_transition(project: Project, ws: Workspace | None, args: argparse.Namespace) -> int:
    name = args.object.lower().removeprefix("table:")
    key = f"table:{name}"
    sid = args.id or f"{date.today().isoformat()}-{name.split('.')[-1].replace('_', '-')}"
    path = project.root / "migrations" / f"{sid}.sql"
    if path.exists():
        raise ProjectError(f"{path} exists")
    intent = project.intent()
    if pending := [s for s in intent.scripts if s.object == key]:
        raise ProjectError(f"{key} already has a transition script: {pending[0].path}. Delete "
                           "it first, or edit it")
    desired = _desired(project, ws, args)
    catalog, target, _ = _target(project, ws, args)
    history = _history(ws, catalog)
    live = renames(desired, target, intent, history)[0]
    if key not in desired or key not in live:
        where = "the source" if key not in desired else catalog
        raise ProjectError(f"{key} isn't in {where}, so there is nothing to transition. A "
                           "target without the table creates it as declared")
    result = plan(desired, target, project.sources(), catalog, project.unmanaged,
                  intent=intent, history=history)
    generated = [c for c in result.changes
                 if c.key == key and c.phase in (FK_DROPS, DROPS_FIRST, OBJECTS)]
    if not generated:
        raise ProjectError(f"{key} in {catalog} already matches the source")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scaffold(sid, key, desired, live, generated, catalog))
    print(f"wrote {path.relative_to(project.root)}, for: "
          + "; ".join(c.what for c in generated))
    return 0


def cmd_history(project: Project, ws: Workspace, args: argparse.Namespace) -> int:
    store = Store(ws, project.catalog(args.target))
    rows = store.deployments(args.n) if store.exists() else []
    if not rows:
        print(f"no deployments to {store.catalog}")
    for r in rows:
        print(f"{r['id']}  {r['status']:<9}  {r['statements'] or 0:>3} statements  "
              f"{r['source'] or '-'}  {r['ran_by']}")
        if r["error"]:
            print(f"    {r['error'].splitlines()[0][:150]}")
    print(store.describe_lock())
    return 0


def cmd_unlock(project: Project, ws: Workspace, args: argparse.Namespace) -> int:
    store = Store(ws, project.catalog(args.target))
    if store.holder() is None:
        print(f"{store.catalog} is not locked")
        return 0
    print(store.describe_lock())
    store.unlock()
    print(f"{store.catalog} unlocked")
    return 0


def cmd_rename_table(project: Project, ws: None, args: argparse.Namespace) -> int:
    spec = {"from": table_name(args.old), "to": table_name(args.new)}
    _added("refactor.yml", add_rename(project.root, "rename_table", spec, args.id))
    return 0


def cmd_rename_column(project: Project, ws: None, args: argparse.Namespace) -> int:
    spec = {"table": table_name(args.table), "from": column_name(args.old),
            "to": column_name(args.new)}
    _added("refactor.yml", add_rename(project.root, "rename_column", spec, args.id))
    return 0


def cmd_allow(project: Project, ws: None, args: argparse.Namespace) -> int:
    _added("project.yml", add_allowance(project.root, args.kind, args.name))
    return 0


def _added(file: str, text: str) -> None:
    print(f"added to {file}:\n" + "".join(f"  {line}\n" for line in text.splitlines()), end="")
    print("The next plan acts on it. Review it in git like the rest of the source.")


OFFLINE = {"rename-table", "rename-column", "allow"}  # commands that need no workspace

COMMANDS: dict[str, Callable[..., int]] = {
    "import": cmd_import,
    "model": cmd_model,
    "shadow": cmd_shadow,
    "compare": cmd_compare,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "rehearse": cmd_rehearse,
    "transition": cmd_transition,
    "history": cmd_history,
    "unlock": cmd_unlock,
    "rename-table": cmd_rename_table,
    "rename-column": cmd_rename_column,
    "allow": cmd_allow,
}
