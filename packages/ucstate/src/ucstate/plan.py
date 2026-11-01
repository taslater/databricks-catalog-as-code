"""Planning: the changes that take a target from its live model to the desired one.

docs/design.md, sections 3 and 5. The comparison is two-way: the desired model
(the shadow) against the live one. Apply records each deployed model in
`_ucstate.state` (Phase 3), and drift detection compares against it (Phase 6).

Every change has a class, and a change that can't run says why in
`blocked`. What lets each class run (Phase 4):

- `auto` always runs.
- `checked` runs once its precheck, a query on the existing rows, finds no
  violations. The precheck runs at plan time and again at apply time
  (apply.run_prechecks), since the data can change in between.
- `one-way` runs when the source declares what it needs: the table
  property that upgrades the protocol. The change is then a reviewed line in
  git. Type widening needs `delta.enableTypeWidening`; renaming or dropping
  a column needs `delta.columnMapping.mode`.
- `destructive` runs when project.yml allows it by name.
- `rewrite` runs through a transition script, which replaces every
  generated statement for its table. `ucstate transition` writes one.
- `ambiguous` never runs. refactor.yml or an allowance says which it was,
  and the plan then has a rename, or a drop and an add, instead.
- `script` is a one-time script from migrations/once/. Review is its gate.

Statements use two-part names. They run with the target catalog, and the
object's schema, set on the request, as the shadow build's do. So a plan
reads the same for every target.

- A new object is created by its source file's own statements.
- A changed view or function, or a changed `rebuildable` table, is replaced
  by its source file, with OR REPLACE. A replace drops a view's or a
  function's grants and tags (probes/phase2.py, r1 and r2), and a table's
  custom properties (probes/phase4.py, p1). So the file's other statements
  run again after it.
- Everything else is a generated ALTER, COMMENT, GRANT or REVOKE.

Changes run in eight phases, after "The order of a deploy" in section 5:

0. one-time scripts that declare `pre`;
1. schemas;
2. renames from refactor.yml, so every later statement sees the new names;
3. FK drops. That includes FKs whose parent's PK is about to change,
   because a PK with incoming FKs can't be dropped (r3). They are added
   back in phase 5;
4. PK and CHECK drops, and a view or table that one of the other kind and
   the same name replaces;
5. every other create, replace and alter, in dependency order. An object's
   changes stay together, and a transition script takes its table's place;
6. drops of objects the source no longer has;
7. one-time scripts that declare `post`.
"""

from __future__ import annotations

import copy
import heapq
import re
import shlex
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from ucmodel.model import compare_object, depends_on, object_hash
from ucmodel.sqltext import ident, or_replace, principal, qualified, string, two_part_names

from .choices import entry, rename_id
from .intent import History, Intent, Refactor, Script, checksum
from .project import SourceFile
from .render import column_list, constraint, privilege, sql_type, tag_list

CLASSES = ("auto", "checked", "one-way", "destructive", "rewrite", "ambiguous", "script")

PRE, SCHEMAS, RENAMES, FK_DROPS, DROPS_FIRST, OBJECTS, DROPS, POST = range(8)

# Properties that add a Delta table feature when switched on.
FEATURES = {
    "delta.appendOnly": "appendOnly",
    "delta.columnMapping.mode": "columnMapping",
    "delta.enableChangeDataFeed": "changeDataFeed",
    "delta.enableDeletionVectors": "deletionVectors",
    "delta.enableIcebergCompatV2": "icebergCompatV2",
    "delta.enableRowTracking": "rowTracking",
    "delta.enableTypeWidening": "typeWidening",
    "delta.universalFormat.enabledFormats": "icebergCompatV2",
}
MAPPING = "delta.columnMapping.mode"
WIDENING = "delta.enableTypeWidening"
REBUILD = "ucstate.rebuild"  # a rebuildable table's statement checksum, set by the plan
CONSTRAINT_NAMES = {"primary_key": "primary key", "foreign_key": "foreign key", "check": "CHECK"}
PLAN_FORMAT = 2  # the version of the plan file that `ucstate plan -o` writes


class PlanError(Exception):
    pass


@dataclass
class Change:
    key: str
    cls: str
    what: str
    statements: list[str] = field(default_factory=list)
    phase: int = OBJECTS
    blocked: str | None = None  # why it can't run; None when it can
    precheck: str | None = None  # a query whose one value counts the rows in the way
    violations: int | None = None  # what the precheck found, once it has run
    script: str | None = None  # the hand-written script it runs
    # What would unblock it: each a label, the ucstate command that writes the
    # choice into the project, and the file and text that command adds.
    options: list[dict] = field(default_factory=list)

    @property
    def schema(self) -> str | None:
        """The schema the statements run in. A schema's and a script's run in none."""
        kind, name = self.key.split(":", 1)
        return None if kind in ("schema", "script") else name.split(".")[0]


@dataclass
class Plan:
    """A plan, and what applying it needs later.

    `fingerprint` holds the hash of the live object behind every change, None
    for one that doesn't exist yet. Apply reads the target again and refuses
    the plan if any of them has changed (docs/design.md, section 6). Renamed
    tables are fingerprinted under their old key, which `renamed` maps from
    the new one. `desired` is the model the plan leads to, which apply
    verifies against and records. `source` is the git revision the plan was
    made from. `refactors` and `scripts` are what apply records in
    `_ucstate.refactor_log` and `_ucstate.migration_history` once it succeeds.
    `against` is the hash of the snapshot a preview was planned against, with
    no workspace. Apply refuses such a plan.
    """

    catalog: str
    changes: list[Change]
    notes: list[str]
    fingerprint: dict[str, str | None] = field(default_factory=dict)
    desired: dict[str, dict] = field(default_factory=dict)
    unmanaged: str = "report"
    source: str | None = None
    renamed: dict[str, str] = field(default_factory=dict)
    refactors: list[dict] = field(default_factory=list)
    scripts: list[dict] = field(default_factory=list)
    against: str | None = None

    @property
    def blocked(self) -> list[Change]:
        return [c for c in self.changes if c.blocked]

    def stale(self, live: dict[str, dict]) -> list[str]:
        """The keys whose live object has changed since the plan was made."""
        return [k for k, h in self.fingerprint.items() if object_hash(live.get(k)) != h]

    def to_json(self) -> dict:
        out = {"format": PLAN_FORMAT, "catalog": self.catalog, "source": self.source,
               "unmanaged": self.unmanaged, "changes": [asdict(c) for c in self.changes],
               "notes": self.notes, "fingerprint": self.fingerprint, "renamed": self.renamed,
               "refactors": self.refactors, "scripts": self.scripts, "desired": self.desired}
        return {**out, "against": self.against} if self.against else out

    @classmethod
    def from_json(cls, data: dict) -> Plan:
        if data.get("format") != PLAN_FORMAT:
            raise PlanError(f"plan file format {data.get('format')!r}; this ucstate reads "
                            f"{PLAN_FORMAT}. Plan again")
        return cls(data["catalog"], [Change(**c) for c in data["changes"]], data["notes"],
                   data["fingerprint"], data["desired"], data["unmanaged"], data["source"],
                   data["renamed"], data["refactors"], data["scripts"], data.get("against"))

    def text(self) -> str:
        n, b = len(self.changes), len(self.blocked)
        if not n:
            lines = [f"No changes: {self.catalog} matches the source."]
        else:
            lines = [f"{n} change{'s' if n != 1 else ''} to {self.catalog}"
                     + (f", {b} blocked" if b else "") + ":"]
            width = max(len(c.cls) for c in self.changes)
            for i, c in enumerate(self.changes, 1):
                lines += ["", f"{i:>3}. {c.cls:<{width}}  {c.key}: {c.what}"]
                if c.precheck:
                    found = ("not run yet" if c.violations is None else
                             "passed" if c.violations == 0 else
                             f"{c.violations} row{'s' if c.violations != 1 else ''} in the way")
                    lines.append(f"       precheck ({found}): {c.precheck}")
                for s in c.statements:
                    lines += [f"       {line}" for line in (s + ";").splitlines()]
                if c.blocked:
                    first, *rest = c.blocked.splitlines()
                    lines += [f"       blocked: {first}"] + [f"         {r}" for r in rest]
                if c.options:
                    shown = [(shlex.join(o["command"]), o["label"]) for o in c.options]
                    longest = max(len(command) for command, _ in shown)
                    lines.append("       options:")
                    lines += [f"         ucstate {command:<{longest}}  ({label})"
                              for command, label in shown]
        if self.against:
            lines[1:1] = ["", f"Planned against the snapshot {self.against[:12]}, with no "
                          "workspace: prechecks haven't run, and every refactor.yml entry and "
                          "script counts as not run yet. apply refuses this plan."]
        recorded = [f"  {r['id']}: {r['outcome']}" for r in self.refactors + self.scripts
                    if r["outcome"] not in ("ran", "renamed")]
        if recorded:
            lines += ["", "Recorded as done, with nothing to run:"] + recorded
        if self.notes:
            lines += ["", "Left alone:"] + [f"  {note}" for note in self.notes]
        return "\n".join(lines)


@dataclass
class Rules:
    """What lets a change run, and which allowances a plan used."""

    intent: Intent
    used: set[tuple[str, str]] = field(default_factory=set)

    def allowed(self, kind: str, name: str) -> bool:
        if self.intent.allows(kind, name):
            self.used.add((kind, name.lower()))
            return True
        return False


def plan(desired: dict[str, dict], live: dict[str, dict], sources: list[SourceFile],
         catalog: str, unmanaged: str = "report", *, intent: Intent | None = None,
         history: History | None = None) -> Plan:
    intent, history = intent or Intent(), history or History()
    _check_history(intent, history)
    files = {f.key: f for f in sources}
    desired = _with_rebuild_marks(desired, files)
    rules = Rules(intent)
    original = live
    live, changes, refactors, renamed = renames(desired, live, intent, history)
    notes: list[str] = []
    handled: set[str] = set()  # live-only keys a create already accounts for

    for key in sorted(desired.keys() - live.keys()):
        if desired[key]["kind"] != "unsupported":
            changes += _create(key, desired[key], files, live, desired, handled, rules)
    for key in sorted(live.keys() - desired.keys() - handled):
        a = live[key]
        if a["kind"] == "unsupported":
            notes.append(f"{key}: {a['type']} is outside the model")
        elif unmanaged == "report" and not rules.allowed(f"drop_{a['kind']}", a["name"]):
            notes.append(f"{key}: not in the source (policy unmanaged_objects: report)")
        else:
            changes.append(_drop(key, a, rules))
    for key in sorted(desired.keys() & live.keys()):
        d, a = desired[key], live[key]
        if d["kind"] != "unsupported":
            changes += ALTERS[d["kind"]](key, d, a, files.get(key), rules)
    changes += _readded_foreign_keys(desired, live)
    changes, scripts = _scripts(changes, desired, live, intent, history)
    notes += [f"allow {kind}: {name} matches no change. Delete it once every target has it"
              for kind, name in sorted(intent.allow - rules.used)]
    # Apply reads the target as it is, before any rename, so hash it that way.
    keys = sorted({renamed.get(c.key, c.key) for c in changes if not c.key.startswith("script:")})
    fingerprint = {k: object_hash(original.get(k)) for k in keys}
    # A rebuildable table's query reads tables the model doesn't link it to.
    reads = {k: {f"{kind}:{n}" for n in two_part_names(f.statements[0])
                 for kind in ("table", "view")}
             for k, f in files.items() if f.rebuildable}
    return Plan(catalog, _order(changes, desired, live, reads), notes, fingerprint, desired,
                unmanaged, renamed=renamed, refactors=refactors, scripts=scripts)


def _check_history(intent: Intent, history: History) -> None:
    for s in intent.scripts:
        ran = history.scripts.get(s.id)
        if ran and ran != s.checksum:
            raise PlanError(f"{s.path} changed after it ran in this target (checksum {ran}, "
                            f"now {s.checksum}). Put a new script beside it instead")


def _with_rebuild_marks(desired: dict, files: dict[str, SourceFile]) -> dict:
    """Mark each rebuildable table with its statement's checksum, so a new query shows."""
    marked = {k for k, f in files.items() if f.rebuildable and k in desired}
    if not marked:
        return desired
    out = dict(desired)
    for key in marked:
        out[key] = {**desired[key], "properties": {
            **desired[key]["properties"], REBUILD: checksum(files[key].statements[0])}}
    return out


# Renames ------------------------------------------------------------------------------------


def renames(desired: dict, live: dict, intent: Intent, history: History
            ) -> tuple[dict, list[Change], list[dict], dict[str, str]]:
    """Apply refactor.yml's pending entries to a copy of the live model.

    Returns the renamed model, the rename changes, a record per entry, and
    the new key of each renamed table mapped to its old one. Each entry is
    checked against the model as the earlier entries left it:

    - the old name exists and the new one doesn't: rename;
    - the new name exists: already done, so it is only recorded;
    - neither exists, as in a fresh target: skipped, and recorded.

    The live model passed in is left as it is.
    """
    out = dict(live)
    changes: list[Change] = []
    records: list[dict] = []
    renamed: dict[str, str] = {}
    for r in intent.refactors:
        if r.id in history.refactors:
            continue
        if r.kind == "rename_table":
            outcome, change = _rename_table(r, out, renamed)
        else:
            outcome, change = _rename_column(r, desired, out)
        records.append({"id": r.id, "kind": r.kind, "outcome": outcome})
        if change:
            changes.append(change)
    return out, changes, records, renamed


def _rename_table(r: Refactor, live: dict, renamed: dict[str, str]) -> tuple[str, Change | None]:
    if r.new_key in live:
        return "already done", None
    if r.old_key not in live or live[r.old_key]["kind"] != "table":
        return "skipped: neither table exists", None
    live[r.new_key] = {**live.pop(r.old_key), "name": r.new}
    renamed[r.new_key] = renamed.pop(r.old_key, r.old_key)
    for key, obj in list(live.items()):  # the children's FKs follow the rename (Phase 0)
        if obj["kind"] == "table" and any(c.get("references") == r.old
                                          for c in obj["constraints"].values()):
            moved = copy.deepcopy(obj)
            for c in moved["constraints"].values():
                if c.get("references") == r.old:
                    c["references"] = r.new
            live[key] = moved
    return "renamed", Change(r.new_key, "auto", f"{r.describe()} (refactor.yml {r.id})",
                             [f"ALTER TABLE {qualified(r.old)} RENAME TO {qualified(r.new)}"],
                             RENAMES)


def _rename_column(r: Refactor, desired: dict, live: dict) -> tuple[str, Change | None]:
    table = live.get(r.new_key)
    if table is None or table["kind"] != "table":
        return "skipped: the table doesn't exist", None
    names = [c["name"] for c in table["columns"]]
    if r.new in names:
        return "already done", None
    if r.old not in names:
        return "skipped: neither column exists", None
    name = qualified(table["name"])
    statements, cls, blocked = [], "auto", None
    if not _has_mapping(table):
        cls = "one-way"
        if _has_mapping(desired.get(r.new_key)):
            mode = desired[r.new_key]["properties"][MAPPING]
            statements.append(f"ALTER TABLE {name} SET TBLPROPERTIES "
                              f"({string(MAPPING)} = {string(mode)})")
        else:
            blocked = _needs_mapping(table["name"])
    statements.append(f"ALTER TABLE {name} RENAME COLUMN {ident(r.old)} TO {ident(r.new)}")
    after = copy.deepcopy(table)
    for c in after["columns"]:
        if c["name"] == r.old:
            c["name"] = r.new
    for c in after["constraints"].values():
        if "columns" in c:
            c["columns"] = [r.new if n == r.old else n for n in c["columns"]]
    if after.get("clustering") and after["clustering"].get("columns"):
        after["clustering"]["columns"] = [r.new if n == r.old else n
                                          for n in after["clustering"]["columns"]]
    if statements[:-1]:  # column mapping comes on with the rename
        after["properties"] = {**after["properties"],
                               MAPPING: desired[r.new_key]["properties"][MAPPING]}
    live[r.new_key] = after
    for key, obj in list(live.items()):  # FKs that point at the renamed column
        if obj["kind"] == "table" and any(c.get("references") == r.table and
                                          r.old in c.get("parent_columns", [])
                                          for c in obj["constraints"].values()):
            moved = copy.deepcopy(obj)
            for c in moved["constraints"].values():
                if c.get("references") == r.table:
                    c["parent_columns"] = [r.new if n == r.old else n
                                           for n in c["parent_columns"]]
            live[key] = moved
    return "renamed", Change(r.new_key, cls, f"{r.describe()} (refactor.yml {r.id})",
                             statements if not blocked else [], RENAMES, blocked=blocked)


def _has_mapping(table: dict | None) -> bool:
    return bool(table) and table["properties"].get(MAPPING) in ("name", "id")  # type: ignore[index]


def _needs_mapping(name: str) -> str:
    return (f"needs column mapping: declare '{MAPPING}' = 'name' in {name}'s TBLPROPERTIES. "
            "It upgrades the table protocol, which can't be undone")


# Creates and drops ---------------------------------------------------------------------


def _create(key: str, d: dict, files: dict[str, SourceFile], live: dict, desired: dict,
            handled: set[str], rules: Rules) -> list[Change]:
    kind, out = d["kind"], []
    if kind in ("table", "view"):
        other = f"{'view' if kind == 'table' else 'table'}:{d['name']}"
        if other in live:
            handled.add(other)
            if kind == "table":
                out.append(Change(other, "auto",
                                  "drop view, which a table of the same name replaces",
                                  [f"DROP VIEW {qualified(d['name'])}"], DROPS_FIRST))
            else:
                drop = _drop(other, live[other], rules)
                out.append(Change(other, "destructive",
                                  "drop table, which a view of the same name replaces",
                                  drop.statements, DROPS_FIRST, blocked=drop.blocked))
    if kind == "table":
        shape = [(c["name"], c["type"]) for c in d["columns"]]
        for k, a in sorted(live.items()):
            if (k not in desired and k not in handled and a["kind"] == "table"
                    and [(c["name"], c["type"]) for c in a["columns"]] == shape
                    and not rules.allowed("drop_table", a["name"])):
                handled.add(k)
                return [*out, Change(key, "ambiguous",
                                     f"new table with the same columns as {a['name']}, which "
                                     "is gone: a rename, or a drop and a create?",
                                     **_which(rename_table={"from": a["name"],
                                                            "to": d["name"]},
                                              drop=("drop_table", a["name"])))]
    f = files.get(key)
    if f is None:
        raise PlanError(f"{key}: in the desired model but no source file declares it")
    statements = list(f.statements)
    if f.rebuildable:
        statements.append(_rebuild_mark(d))
    out.append(Change(key, "auto", f"create {kind}", statements,
                      SCHEMAS if kind == "schema" else OBJECTS))
    return out


def _which(drop: tuple[str, str], **refactor: dict) -> dict:
    """An ambiguous change's blocked message, and its options: the two entries that would
    settle it."""
    ((kind, spec),) = refactor.items()
    added = entry(rename_id(kind, spec), kind, spec)
    args = [spec["from"], spec["to"]] if kind == "rename_table" else [
        spec["table"], spec["from"], spec["to"]]
    rename = {"label": "it's a rename", "command": [kind.replace("_", "-"), *args],
              "file": "refactor.yml", "adds": added}
    gone = "a drop and a create" if kind == "rename_table" else "a drop and an add"
    return {"blocked": ("if it's a rename, add to refactor.yml:\n"
                        + "".join(f"  {line}\n" for line in added.splitlines())
                        + "if it's a drop, add to project.yml:\n"
                        f"  allow:\n    - {drop[0]}: {drop[1]}"),
            "options": [rename, _allowance(*drop, f"it's {gone}")["options"][0]]}


def _allowance(kind: str, name: str, label: str | None = None) -> dict:
    """A destructive change's blocked message, and the allowance that lets it run."""
    option = {"label": label or "allow the drop, which loses its data",
              "command": ["allow", kind, name], "file": "project.yml",
              "adds": f"allow:\n  - {kind}: {name}\n"}
    return {"blocked": f"needs an allowance in project.yml: allow: [{kind}: {name}]",
            "options": [option]}


def _drop(key: str, a: dict, rules: Rules) -> Change:
    """An object the source no longer has. Views and functions hold no data."""
    kind, name = a["kind"], qualified(a["name"])
    statement = f"DROP {kind.upper()} {name}"
    if kind in ("view", "function"):
        return Change(key, "auto", f"drop {kind}", [statement], DROPS)
    what = f"drop {kind}"
    if kind == "table" and a.get("meta", {}).get("table_id"):
        what += (f"; UNDROP TABLE WITH ID '{a['meta']['table_id']}' brings it back within the "
                 "retention window")
    if rules.allowed(f"drop_{kind}", a["name"]):
        return Change(key, "destructive", what, [statement], DROPS)
    return Change(key, "destructive", what, phase=DROPS, **_allowance(f"drop_{kind}", a["name"]))


def _rebuild_mark(d: dict) -> str:
    return (f"ALTER TABLE {qualified(d['name'])} SET TBLPROPERTIES "
            f"({string(REBUILD)} = {string(d['properties'][REBUILD])})")


# Alters ----------------------------------------------------------------------------------


def _check_handled(key: str, d: dict, a: dict, attrs: set[str]) -> None:
    """Fail loudly on a difference no rule below plans for."""
    for diff in compare_object(key, d, a):
        if diff.path.split(".")[0] not in attrs:
            raise PlanError(f"{key}: no rule plans a change to {diff.path}")


def _schema(key: str, d: dict, a: dict, f: SourceFile | None, rules: Rules) -> list[Change]:
    _check_handled(key, d, a, {"comment", "properties", "tags", "grants"})
    name = ident(d["name"])
    out = []
    if d["comment"] != a["comment"]:
        out.append(_comment(key, "SCHEMA", name, d["comment"]))
    if changed := _changed_properties(d, a):
        out.append(Change(key, "auto", "set properties " + ", ".join(changed),
                          [f"ALTER SCHEMA {name} SET DBPROPERTIES ({_pairs(changed)})"]))
    out += _tags(key, f"ALTER SCHEMA {name}", "", d["tags"], a["tags"])
    out += _grants(key, "SCHEMA", name, d["grants"], a["grants"])
    for c in out:
        c.phase = SCHEMAS
    return out


TABLE_ATTRS = {"columns", "constraints", "partitioning", "clustering", "row_filter",
               "properties", "comment", "tags", "grants"}


def _table(key: str, d: dict, a: dict, f: SourceFile | None, rules: Rules) -> list[Change]:
    _check_handled(key, d, a, TABLE_ATTRS)
    if f is not None and f.rebuildable:
        paths = [x.path for x in compare_object(key, d, a) if not _tag_or_grant(x.path)]
        if paths:
            what = ", ".join(sorted({"its statement" if p == f"properties.{REBUILD}"
                                     else p.split(".")[0] for p in paths}))
            return [Change(key, "auto", f"rebuild ({what} changed)",
                           [or_replace(f.statements[0]), *f.statements[1:], _rebuild_mark(d)])]
    name = qualified(d["name"])
    out = _table_properties(key, name, d, a)
    out += _columns(key, name, d, a, rules)
    out += _constraints(key, name, d, a)
    if d["partitioning"] != a["partitioning"]:
        out.append(Change(key, "rewrite",
                          f"partitioning changes from {a['partitioning']} to {d['partitioning']}",
                          **_transition(key)))
    if d["clustering"] is not None and d["clustering"] != a["clustering"]:
        how = ("AUTO" if d["clustering"].get("auto")
               else f"({', '.join(map(qualified, d['clustering']['columns']))})")
        out.append(Change(key, "auto", f"cluster by {how.strip('()')}",
                          [f"ALTER TABLE {name} CLUSTER BY {how}"]))
    if d["row_filter"] != a["row_filter"]:
        rf = d["row_filter"]
        if rf is None:
            out.append(Change(key, "auto", "drop the row filter",
                              [f"ALTER TABLE {name} DROP ROW FILTER"]))
        else:
            out.append(Change(key, "auto", f"set the row filter {rf['function']}", [
                f"ALTER TABLE {name} SET ROW FILTER {qualified(rf['function'])} "
                f"ON ({column_list(rf['columns'])})"]))
    if d["comment"] != a["comment"]:
        out.append(_comment(key, "TABLE", name, d["comment"]))
    out += _tags(key, f"ALTER TABLE {name}", "", d["tags"], a["tags"])
    out += _grants(key, "TABLE", name, d["grants"], a["grants"])
    return out


def _transition(key: str) -> dict:
    """A rewrite's blocked message, and the command that drafts its transition script. The
    command line adds where the table is read from: `--target` or `--against`."""
    return {"blocked": "needs a transition script: "
                       f"ucstate transition --target <target> --object {key}",
            "options": [{"label": "draft a transition script to edit",
                         "command": ["transition", "--object", key],
                         "file": "migrations/", "adds": "a script for this table's change"}]}


def _view(key: str, d: dict, a: dict, f: SourceFile | None, rules: Rules) -> list[Change]:
    """Anything but tags and grants replaces the view."""
    paths = [x.path for x in compare_object(key, d, a)]
    replaced = sorted({p.split(".")[0] for p in paths if not _tag_or_grant(p)})
    if replaced:
        return [_replace(key, f, f"replace view ({', '.join(replaced)} changed)")]
    name = qualified(d["name"])
    out = _tags(key, f"ALTER VIEW {name}", "", d["tags"], a["tags"])
    live_columns = {c["name"]: c for c in a["columns"]}
    for c in d["columns"]:
        # ALTER VIEW has no ALTER COLUMN; ALTER TABLE tags a view's columns (r8).
        out += _tags(key, f"ALTER TABLE {name} ALTER COLUMN {ident(c['name'])}",
                     f" on column {c['name']}", c["tags"], live_columns[c["name"]]["tags"])
    out += _grants(key, "TABLE", name, d["grants"], a["grants"])
    return out


def _function(key: str, d: dict, a: dict, f: SourceFile | None, rules: Rules) -> list[Change]:
    """Anything but grants replaces the function."""
    paths = [x.path for x in compare_object(key, d, a)]
    replaced = sorted({p.split(".")[0] for p in paths if not p.startswith("grants.")})
    if replaced:
        return [_replace(key, f, f"replace function ({', '.join(replaced)} changed)")]
    return _grants(key, "FUNCTION", qualified(d["name"]), d["grants"], a["grants"])


def _volume(key: str, d: dict, a: dict, f: SourceFile | None, rules: Rules) -> list[Change]:
    _check_handled(key, d, a, {"comment", "tags", "grants"})
    name = qualified(d["name"])
    out = [_comment(key, "VOLUME", name, d["comment"])] if d["comment"] != a["comment"] else []
    out += _tags(key, f"ALTER VOLUME {name}", "", d["tags"], a["tags"])
    out += _grants(key, "VOLUME", name, d["grants"], a["grants"])
    return out


ALTERS = {"schema": _schema, "table": _table, "view": _view, "function": _function,
          "volume": _volume}


def _tag_or_grant(path: str) -> bool:
    return path.startswith(("tags.", "grants.")) or (
        path.startswith("columns.") and path.endswith(".tags"))


def _replace(key: str, f: SourceFile | None, what: str) -> Change:
    if f is None:
        raise PlanError(f"{key}: in the desired model but no source file declares it")
    return Change(key, "auto", what, [or_replace(f.statements[0]), *f.statements[1:]])


# Pieces of a table -------------------------------------------------------------------------


def _changed_properties(d: dict, a: dict) -> dict[str, str]:
    """Declared properties whose value differs, by name. Undeclared ones never count.

    Sorted, so that a plan doesn't depend on whether the model came from the
    compiler, which keeps the source's order, or from a read, which doesn't.
    """
    return {k: v for k, v in sorted(d["properties"].items()) if a["properties"].get(k) != v}


def _upgrades(key: str, value: str, live: dict[str, str]) -> bool:
    """Whether setting the property adds a table feature the table doesn't have."""
    if key in ("delta.minReaderVersion", "delta.minWriterVersion"):
        return int(value) > int(live.get(key, "0"))
    if key.startswith("delta.feature."):
        feature = key.removeprefix("delta.feature.")
    elif key in FEATURES and value.lower() not in ("false", "none", ""):
        feature = FEATURES[key]
    else:
        return False
    return live.get(f"delta.feature.{feature}") != "supported"


def _table_properties(key: str, name: str, d: dict, a: dict) -> list[Change]:
    """Declared properties. One that upgrades the protocol is one-way, and declaring it is
    what lets it run."""
    changed = _changed_properties(d, a)
    upgrades = {k: v for k, v in changed.items() if _upgrades(k, v, a["properties"])}
    plain = {k: v for k, v in changed.items() if k not in upgrades}
    out = [Change(key, "one-way", f"set {k} = {v}, which upgrades the table protocol",
                  [f"ALTER TABLE {name} SET TBLPROPERTIES ({_pairs({k: v})})"])
           for k, v in upgrades.items()]
    if plain:
        out.append(Change(key, "auto", "set properties " + ", ".join(plain),
                          [f"ALTER TABLE {name} SET TBLPROPERTIES ({_pairs(plain)})"]))
    return out


def _columns(key: str, name: str, d: dict, a: dict, rules: Rules) -> list[Change]:
    want = {c["name"]: c for c in d["columns"]}
    have = {c["name"]: c for c in a["columns"]}
    added = [n for n in want if n not in have]
    out = []
    for gone in [n for n in have if n not in want]:
        twin = next((n for n in added if want[n]["type"] == have[gone]["type"]), None)
        if twin and not rules.allowed("drop_column", f"{d['name']}.{gone}"):
            added.remove(twin)
            out.append(Change(key, "ambiguous", f"column {gone} is gone and {twin} is new, with "
                                                "the same type: a rename, or a drop and an add?",
                              **_which(rename_column={"table": d["name"], "from": gone,
                                                            "to": twin},
                                             drop=("drop_column", f"{d['name']}.{gone}"))))
        else:
            out.append(_drop_column(key, name, d, a, gone, rules))
    order = [n for n in want if n in have or n in added]
    for column, position in _moves(order, [n for n in have if n in want]):
        if column in have:
            out.append(Change(key, "auto", f"move column {column} {_says(position)}",
                              [f"ALTER TABLE {name} ALTER COLUMN {ident(column)} {position}"]))
        else:
            out.append(_add_column(key, name, want[column], position))
    for n, c in want.items():
        if n in have:
            out += _column(key, name, c, have[n], d, a)
    return out


def _drop_column(key: str, name: str, d: dict, a: dict, column: str, rules: Rules) -> Change:
    what = f"drop column {column}"
    allowance = f"{d['name']}.{column}"
    if not rules.allowed("drop_column", allowance):
        return Change(key, "destructive", what, **_allowance("drop_column", allowance))
    if not (_has_mapping(a) or _has_mapping(d)):
        return Change(key, "destructive", what, blocked=_needs_mapping(d["name"]))
    return Change(key, "destructive", what,
                  [f"ALTER TABLE {name} DROP COLUMN {ident(column)}"])


def _says(position: str) -> str:
    """`AFTER x` as `after x`, for the plan's description."""
    if not position:
        return "at the end"
    word, _, rest = position.partition(" ")
    return f"{word.lower()} {rest}".rstrip()


def _moves(want: list[str], have: list[str]) -> list[tuple[str, str]]:
    """The adds and moves that turn `have` into `want`, as (column, position).

    Position is `FIRST`, `AFTER x`, or empty for the end. The longest run of
    columns already in order stays put, so a plan moves as few as it can.
    Every name in `have` must be in `want`; the rest of `want` are new.
    """
    have = list(have)
    present = set(have)
    rank = {n: i for i, n in enumerate(have)}
    keep = _longest_run([n for n in want if n in present], rank)
    moves = []
    for i, n in enumerate(want):
        if n in keep:
            continue
        if n in present:
            have.remove(n)
        at = have.index(want[i - 1]) + 1 if i else 0
        if not i:
            position = "FIRST"
        elif n not in present and at == len(have):
            position = ""
        else:
            position = f"AFTER {ident(want[i - 1])}"
        have.insert(at, n)
        moves.append((n, position))
    if have != want:
        raise PlanError(f"column moves came out as {have}, not {want}")
    return moves


def _longest_run(items: list[str], rank: dict[str, int]) -> set[str]:
    """The longest subsequence of items whose ranks increase."""
    if not items:
        return set()
    best, prev = [1] * len(items), [-1] * len(items)
    for i in range(len(items)):
        for j in range(i):
            if rank[items[j]] < rank[items[i]] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    i = max(range(len(items)), key=lambda k: best[k])
    run = set()
    while i != -1:
        run.add(items[i])
        i = prev[i]
    return run


def _add_column(key: str, name: str, c: dict, position: str) -> Change:
    column = c["name"]
    if c["generated"] or c["identity"]:
        kind = "generated" if c["generated"] else "identity"
        return Change(key, "rewrite", f"add {kind} column {column}",
                      **_transition(key))
    add = f"ALTER TABLE {name} ADD COLUMN {ident(column)} {sql_type(c['type'])}"
    if c["comment"]:
        add += f" COMMENT {string(c['comment'])}"
    statements = [add + (f" {position}" if position else "")]
    alter = f"ALTER TABLE {name} ALTER COLUMN {ident(column)}"
    what = f"add column {column} {_says(position)}"
    cls, precheck = "auto", None
    if c["default"] is not None:
        # Delta refuses ADD COLUMN ... DEFAULT on an existing table (Phase 0).
        statements.append(f"{alter} SET DEFAULT {c['default']}")
        if c["nullable"]:
            what += ", with a default; existing rows read NULL"
    if not c["nullable"]:
        cls = "checked"
        if c["default"] is not None:
            # Delta refuses NOT NULL in ADD COLUMN too, so fill the rows, then set it.
            statements += [f"UPDATE {name} SET {ident(column)} = DEFAULT "
                           f"WHERE {ident(column)} IS NULL", f"{alter} SET NOT NULL"]
            what = f"add NOT NULL column {column} {_says(position)}, filling the rows from " \
                   "its default"
        else:
            statements.append(f"{alter} SET NOT NULL")
            precheck = f"SELECT count(*) AS violations FROM {name}"
            what = f"add NOT NULL column {column} {_says(position)}; the table must be empty"
    if c["mask"]:
        statements.append(_set_mask(alter, c["mask"]))
    if c["tags"]:
        statements.append(f"{alter} SET TAGS ({tag_list(c['tags'])})")
    return Change(key, cls, what, statements, precheck=precheck)


def _column(key: str, name: str, dc: dict, ac: dict, d: dict, a: dict) -> list[Change]:
    column = dc["name"]
    alter = f"ALTER TABLE {name} ALTER COLUMN {ident(column)}"
    out = []
    if dc["type"] != ac["type"]:
        what = f"column {column} changes from {ac['type']} to {dc['type']}"
        if not widens(ac["type"], dc["type"]):
            out.append(Change(key, "rewrite", what, **_transition(key)))
        elif "true" not in (d["properties"].get(WIDENING), a["properties"].get(WIDENING)):
            out.append(Change(key, "one-way", what, blocked=(
                f"needs type widening: declare '{WIDENING}' = 'true' in {d['name']}'s "
                "TBLPROPERTIES. It upgrades the table protocol, which can't be undone")))
        else:
            out.append(Change(key, "one-way", what, [f"{alter} TYPE {sql_type(dc['type'])}"]))
    if dc["generated"] != ac["generated"] or dc["identity"] != ac["identity"]:
        out.append(Change(key, "rewrite", f"column {column} changes how it is generated",
                          **_transition(key)))
    if dc["nullable"] and not ac["nullable"]:
        out.append(Change(key, "auto", f"drop NOT NULL on {column}", [f"{alter} DROP NOT NULL"]))
    elif ac["nullable"] and not dc["nullable"]:
        out.append(Change(key, "checked", f"set NOT NULL on {column}", [f"{alter} SET NOT NULL"],
                          precheck=f"SELECT count_if({ident(column)} IS NULL) AS violations "
                                   f"FROM {name}"))
    if dc["default"] != ac["default"]:
        if dc["default"] is None:
            out.append(Change(key, "auto", f"drop the default on {column}",
                              [f"{alter} DROP DEFAULT"]))
        else:
            out.append(Change(key, "auto", f"set the default on {column}",
                              [f"{alter} SET DEFAULT {dc['default']}"]))
    if dc["comment"] != ac["comment"]:
        out.append(Change(key, "auto", f"set the comment on {column}",
                          [f"{alter} COMMENT {string(dc['comment'] or '')}"]))
    if dc["mask"] != ac["mask"]:
        if dc["mask"] is None:
            out.append(Change(key, "auto", f"drop the mask on {column}", [f"{alter} DROP MASK"]))
        else:
            out.append(Change(key, "auto", f"set the mask on {column}",
                              [_set_mask(alter, dc["mask"])]))
    out += _tags(key, alter, f" on column {column}", dc["tags"], ac["tags"])
    return out


def _set_mask(alter: str, mask: dict) -> str:
    using = f" USING COLUMNS ({column_list(mask['using'])})" if mask["using"] else ""
    return f"{alter} SET MASK {qualified(mask['function'])}{using}"


INTEGRAL = ["tinyint", "smallint", "int", "bigint"]
DIGITS = {"tinyint": 3, "smallint": 5, "int": 10, "bigint": 20}


def widens(old: str, new: str) -> bool:
    """Whether Delta type widening covers old -> new."""
    def decimal(t: str) -> tuple[int, int] | None:
        m = re.fullmatch(r"decimal\((\d+),\s*(\d+)\)", t)
        return (int(m[1]), int(m[2])) if m else None

    if old in INTEGRAL and new in INTEGRAL:
        return INTEGRAL.index(new) > INTEGRAL.index(old)
    if (old, new) in (("float", "double"), ("date", "timestamp_ntz")):
        return True
    if old in INTEGRAL[:3] and new == "double":
        return True
    to = decimal(new)
    if to and old in INTEGRAL:
        return to[0] - to[1] >= DIGITS[old]
    if to and (frm := decimal(old)):
        return to[1] >= frm[1] and to[0] - to[1] >= frm[0] - frm[1] and to != frm
    return False


def _constraints(key: str, name: str, d: dict, a: dict) -> list[Change]:
    out = []
    want, have = d["constraints"], a["constraints"]
    for n in sorted(want.keys() | have.keys()):
        w, h = want.get(n), have.get(n)
        if w == h:
            continue
        if w and w["type"] == "check":
            statements = [f"ALTER TABLE {name} DROP CONSTRAINT {ident(n)}"] if h else []
            out.append(Change(key, "checked", f"{'replace' if h else 'add'} CHECK {n}",
                              [*statements, _add_constraint(name, n, w)],
                              # A NULL violates a CHECK too (probes/phase4.py, p5).
                              precheck=f"SELECT count_if(NOT coalesce(({w['expression']}), "
                                       f"false)) AS violations FROM {name}"))
            continue
        if h:
            phase = FK_DROPS if h["type"] == "foreign_key" else DROPS_FIRST
            out.append(Change(key, "auto", f"drop {CONSTRAINT_NAMES[h['type']]} {n}"
                              + (", to add it back changed" if w else ""),
                              [f"ALTER TABLE {name} DROP CONSTRAINT {ident(n)}"], phase))
        if w:
            out.append(Change(key, "auto", f"add {CONSTRAINT_NAMES[w['type']]} {n}",
                              [_add_constraint(name, n, w)]))
    return out


def _add_constraint(name: str, n: str, c: dict) -> str:
    return f"ALTER TABLE {name} ADD CONSTRAINT {ident(n)} {constraint(c)}".replace("\n    ", " ")


def _primary_key(table: dict) -> tuple[str, dict] | None:
    return next(((n, c) for n, c in table["constraints"].items()
                 if c["type"] == "primary_key"), None)


def _readded_foreign_keys(desired: dict, live: dict) -> list[Change]:
    """Drop, and add back, FKs whose parent's PK is dropped or changed (r3)."""
    changing = {
        a["name"] for key, a in live.items()
        if a["kind"] == "table" and key in desired and (pk := _primary_key(a))
        and pk != _primary_key(desired[key])
    }
    out = []
    for key, a in sorted(live.items()):
        if a["kind"] != "table":
            continue
        for n, c in sorted(a["constraints"].items()):
            if c["type"] != "foreign_key" or c["references"] not in changing:
                continue
            if key in desired and desired[key]["constraints"].get(n) != c:
                continue  # the table's own changes already drop it
            name = qualified(a["name"])
            drop = f"ALTER TABLE {name} DROP CONSTRAINT {ident(n)}"
            out += [
                Change(key, "auto",
                       f"drop foreign key {n} while {c['references']}'s primary key changes",
                       [drop], FK_DROPS),
                Change(key, "auto", f"add foreign key {n} back",
                       [_add_constraint(name, n, c)]),
            ]
    return out


# Hand-written scripts ------------------------------------------------------------------------


def _scripts(changes: list[Change], desired: dict, live: dict, intent: Intent,
             history: History) -> tuple[list[Change], list[dict]]:
    """Put the pending scripts into the plan, and say what to record for each.

    A transition script takes the place of every generated change for its
    table, but only while the live table is in the state it was written for.
    """
    records: list[dict] = []
    pending = intent.pending(history)
    by_object: dict[str, list[Script]] = defaultdict(list)
    for s in pending:
        if s.kind == "transition" and s.object:
            by_object[s.object].append(s)
    for key, found in sorted(by_object.items()):
        if len(found) > 1:
            changes.append(Change(key, "rewrite", "more than one transition script",
                                  blocked="only one can be pending per table: "
                                          + ", ".join(str(s.path) for s in found)))
            continue
        s = found[0]
        record = {"id": s.id, "kind": s.kind, "object": key, "checksum": s.checksum}
        if key not in live:
            records.append({**record, "outcome": "skipped: the table is created as declared"})
            continue
        if key not in desired:
            changes.append(Change(key, "rewrite", f"transition {s.id}",
                                  blocked=f"{s.path} transitions {key}, which the source "
                                          "no longer has"))
            continue
        generated = [c for c in changes
                     if c.key == key and c.phase in (FK_DROPS, DROPS_FIRST, OBJECTS)]
        if not generated:
            records.append({**record, "outcome": "already done: the table matches the source"})
            continue
        now, want = object_hash(live[key]), object_hash(desired[key])
        if s.after != want:
            blocked = (f"{s.path} lands on {s.after}, but the source now declares {want}. "
                       f"Run ucstate transition again, or delete the script")
        elif s.before != now:
            blocked = (f"{s.path} runs from {s.before}, but {key} is {now}, which is neither "
                       "the script's from nor its to. Look at what changed")
        else:
            blocked = None
        changes = [c for c in changes if c not in generated]
        changes.append(Change(key, "rewrite", f"transition {s.id}, in place of: "
                              + "; ".join(c.what for c in generated),
                              list(s.statements) if not blocked else [], script=s.id,
                              blocked=blocked))
        records.append({**record, "outcome": "ran"})
    for s in pending:
        if s.kind == "once":
            changes.append(Change(f"script:{s.id}", "script", f"run {s.path} once",
                                  list(s.statements), PRE if s.phase == "pre" else POST,
                                  script=s.id))
            records.append({"id": s.id, "kind": s.kind, "object": None,
                            "checksum": s.checksum, "outcome": "ran"})
    return changes, records


def scaffold(sid: str, key: str, desired: dict, live: dict, generated: list[Change],
             catalog: str) -> str:
    """A transition script for one table, to edit before it runs.

    The body is a starting point that is right for a type change: replace the
    table from its own rows, casting the columns that change, then set back
    what CREATE OR REPLACE ... AS SELECT can't carry (probes/phase4.py, p1
    and p2): NOT NULL, defaults, comments, constraints and properties.
    """
    d, a = desired[key], live[key]
    name = qualified(d["name"])
    have = {c["name"]: c for c in a["columns"]}
    lines = [
        "-- ucstate: transition",
        f"-- id: {sid}",
        f"-- object: {d['name']}",
        f"-- from: {object_hash(a)}   {d['name']} as it is in {catalog} now",
        f"-- to:   {object_hash(d)}   {d['name']} as the source declares it",
        "--",
        f"-- This script replaces every generated statement for {d['name']}. It runs",
        "-- only while the table is in the `from` state, and the deploy fails unless",
        "-- the table then matches the source. A target without the table never runs",
        "-- it: the table is created as declared.",
        "--",
        "-- What the plan found:",
        *[f"--   {c.cls}: {c.what}" for c in generated],
        "--",
        "-- The statements below are a starting point. CREATE OR REPLACE ... AS SELECT",
        "-- keeps the table's id, grants and tags, but takes no column list or",
        "-- constraints. Check each CAST: it is where rows can fail or change.",
    ]
    if any(c["generated"] or c["identity"] for c in d["columns"]):
        lines += [
            "--",
            "-- An identity or generated column can't come from AS SELECT. Create the",
            "-- table under another name with its column list, INSERT the rows, then",
            "-- swap the two with ALTER TABLE ... RENAME TO.",
        ]
    select = []
    for c in d["columns"]:
        old = have.get(c["name"])
        if old is None:
            expr = f"CAST(NULL AS {sql_type(c['type'])})"
        elif old["type"] != c["type"]:
            expr = f"CAST({ident(c['name'])} AS {sql_type(c['type'])})"
        else:
            expr = ident(c["name"])
        select.append(f"  {expr} AS {ident(c['name'])}")
    head = f"CREATE OR REPLACE TABLE {name}"
    if d["partitioning"]:
        head += f"\nPARTITIONED BY ({column_list(d['partitioning'])})"
    if d["comment"]:
        head += f"\nCOMMENT {string(d['comment'])}"
    body = [head + "\nAS SELECT\n" + ",\n".join(select) + f"\nFROM {name};"]
    props = {k: v for k, v in d["properties"].items() if k != REBUILD}
    if props:
        body.append(f"ALTER TABLE {name} SET TBLPROPERTIES ({_pairs(props)});")
    for c in d["columns"]:
        alter = f"ALTER TABLE {name} ALTER COLUMN {ident(c['name'])}"
        if not c["nullable"]:
            body.append(f"{alter} SET NOT NULL;")
        if c["default"] is not None:
            body.append(f"{alter} SET DEFAULT {c['default']};")
        if c["comment"]:
            body.append(f"{alter} COMMENT {string(c['comment'])};")
    order = {"primary_key": 0, "foreign_key": 1, "check": 2}
    for n, c in sorted(d["constraints"].items(), key=lambda kv: (order[kv[1]["type"]], kv[0])):
        body.append(_add_constraint(name, n, c) + ";")
    if d["clustering"] and d["clustering"].get("columns"):
        body.append(f"ALTER TABLE {name} CLUSTER BY "
                    f"({', '.join(map(qualified, d['clustering']['columns']))});")
    incoming = [(o["name"], n, c) for _, o in sorted(desired.items()) if o["kind"] == "table"
                for n, c in sorted(o["constraints"].items())
                if c["type"] == "foreign_key" and c["references"] == d["name"]]
    if incoming:
        body.append("-- The replace deletes the foreign keys that point here (Phase 0).")
        body += [_add_constraint(qualified(t), n, c) + ";" for t, n, c in incoming]
    return "\n".join(lines) + "\n\n" + "\n".join(body) + "\n"


# Shared pieces -------------------------------------------------------------------------------


def _pairs(props: dict[str, str]) -> str:
    return ", ".join(f"{string(k)} = {string(v)}" for k, v in props.items())


def _comment(key: str, securable: str, name: str, value: str | None) -> Change:
    return Change(key, "auto", "set the comment" if value else "remove the comment",
                  [f"COMMENT ON {securable} {name} IS {string(value) if value else 'NULL'}"])


def _tags(key: str, alter: str, where: str, want: dict[str, str],
          have: dict[str, str]) -> list[Change]:
    changed = {k: v for k, v in want.items() if have.get(k) != v}
    gone = sorted(have.keys() - want.keys())
    out = []
    if changed:
        out.append(Change(key, "auto", f"set tags {', '.join(changed)}{where}",
                          [f"{alter} SET TAGS ({tag_list(changed)})"]))
    if gone:
        out.append(Change(key, "auto", f"unset tags {', '.join(gone)}{where}",
                          [f"{alter} UNSET TAGS ({', '.join(map(string, gone))})"]))
    return out


def _grants(key: str, securable: str, name: str, want: dict[str, list[str]],
            have: dict[str, list[str]]) -> list[Change]:
    out = []
    for who in sorted(want.keys() | have.keys()):
        w, h = set(want.get(who, [])), set(have.get(who, []))
        if gone := sorted(h - w):
            out.append(Change(key, "auto", f"revoke {', '.join(gone)} from {who}", [
                f"REVOKE {', '.join(map(privilege, gone))} ON {securable} {name} "
                f"FROM {principal(who)}"]))
        if new := sorted(w - h):
            out.append(Change(key, "auto", f"grant {', '.join(new)} to {who}", [
                f"GRANT {', '.join(map(privilege, new))} ON {securable} {name} "
                f"TO {principal(who)}"]))
    return out


# Order -------------------------------------------------------------------------------------

KIND_ORDER = {"function": 0, "table": 1, "volume": 2, "view": 3}
DROP_ORDER = {"view": 0, "function": 1, "table": 2, "volume": 3, "schema": 4}


def _order(changes: list[Change], desired: dict, live: dict,
           reads: dict[str, set[str]] | None = None) -> list[Change]:
    rank = _dependency_order({c.key for c in changes if c.phase == OBJECTS}, desired, live,
                             reads or {})

    def position(item: tuple[int, Change]) -> tuple[int, int, int]:
        seq, c = item
        if c.phase == OBJECTS:
            return (c.phase, rank[c.key], seq)
        if c.phase == DROPS:
            return (c.phase, DROP_ORDER[c.key.split(":")[0]], seq)
        return (c.phase, 0, seq)

    return [c for _, c in sorted(enumerate(changes), key=position)]


def _dependency_order(keys: set[str], desired: dict, live: dict,
                      reads: dict[str, set[str]]) -> dict[str, int]:
    """Rank keys so each comes after the keys it depends on (Kahn's algorithm)."""
    waiting = {k: (depends_on(k, desired.get(k) or live[k]) | reads.get(k, set())) & keys - {k}
               for k in keys}
    needed_by = defaultdict(set)
    for k, needs in waiting.items():
        for n in needs:
            needed_by[n].add(k)
    ready = [(KIND_ORDER.get(k.split(":")[0], 9), k) for k in keys if not waiting[k]]
    heapq.heapify(ready)
    order = []
    while ready:
        _, k = heapq.heappop(ready)
        order.append(k)
        for user in needed_by[k]:
            waiting[user].discard(k)
            if not waiting[user]:
                heapq.heappush(ready, (KIND_ORDER.get(user.split(":")[0], 9), user))
    if len(order) < len(keys):
        raise PlanError("a dependency cycle among " + ", ".join(sorted(keys - set(order))))
    return {k: i for i, k in enumerate(order)}
