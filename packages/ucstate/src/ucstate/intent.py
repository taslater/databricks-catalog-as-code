"""What the source says beyond its DDL: allowances, renames and hand-written scripts.

docs/design.md, sections 3 and 5. Each is loaded from the project, checked
here, and given to the planner.

- **Allowances**, in project.yml's `allow:`, let a destructive change run.
  Each names one object, such as `drop_table: sales.old_orders` or
  `drop_column: sales.customers.legacy_code`. An allowance only permits the
  change; the plan still has to find it.
- **Renames**, in refactor.yml. Each entry is applied once per target and
  recorded in `_ucstate.refactor_log`.
- **Transition scripts**, `migrations/<id>.sql`, replace the generated
  statements for one object's change. The header names the object and its
  hash before and after, and the script runs only from the first.
- **One-time scripts**, `migrations/once/<id>.sql`, are data fixes that run
  once per target, before or after the rest of the plan.

Both kinds of script are recorded in `_ucstate.migration_history` with a
checksum of the file, so editing one that has already run is an error.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ucmodel.sqltext import split_statements

ALLOWANCES = ("drop_table", "drop_view", "drop_function", "drop_volume", "drop_column")
REFACTORS = ("rename_table", "rename_column")
PHASES = ("pre", "post")
NAME = r"[a-z_][a-z0-9_]*"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class IntentError(Exception):
    pass


@dataclass(frozen=True)
class Refactor:
    """A rename. For a table, `table` is the old name; for a column, the table's."""

    id: str
    kind: str
    table: str
    old: str
    new: str

    @property
    def old_key(self) -> str:
        return f"table:{self.old if self.kind == 'rename_table' else self.table}"

    @property
    def new_key(self) -> str:
        return f"table:{self.new if self.kind == 'rename_table' else self.table}"

    def describe(self) -> str:
        if self.kind == "rename_table":
            return f"rename table {self.old} to {self.new}"
        return f"rename column {self.old} to {self.new}"


@dataclass
class Script:
    id: str
    kind: str  # transition or once
    path: Path
    statements: list[str]
    checksum: str
    object: str | None = None  # a transition's model key
    before: str | None = None  # the object's hash it runs from
    after: str | None = None  # the hash it must land on
    phase: str = "post"  # when a one-time script runs


@dataclass
class Intent:
    allow: set[tuple[str, str]] = field(default_factory=set)
    refactors: list[Refactor] = field(default_factory=list)
    scripts: list[Script] = field(default_factory=list)

    def allows(self, kind: str, name: str) -> bool:
        return (kind, name.lower()) in self.allow

    def pending(self, history: History) -> list[Script]:
        """The scripts that haven't run in the target, in id order."""
        return sorted((s for s in self.scripts if s.id not in history.scripts),
                      key=lambda s: s.id)


@dataclass
class History:
    """What a target's `_ucstate` says has already happened there."""

    refactors: set[str] = field(default_factory=set)
    scripts: dict[str, str] = field(default_factory=dict)  # id -> checksum


def allowances(entries: list | None) -> set[tuple[str, str]]:
    out = set()
    for entry in entries or []:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise IntentError(f"allow: each entry is one `kind: name`, not {entry!r}")
        ((kind, name),) = entry.items()
        parts = 3 if kind == "drop_column" else 2
        if kind not in ALLOWANCES:
            raise IntentError(f"allow: {kind!r} isn't one of {', '.join(ALLOWANCES)}")
        if not isinstance(name, str) or not re.fullmatch(r"\.".join([NAME] * parts), name.lower()):
            raise IntentError(f"allow: {kind} takes a {parts}-part name, not {name!r}")
        out.add((kind, name.lower()))
    return out


def refactors(path: Path) -> list[Refactor]:
    if not path.exists():
        return []
    entries = yaml.safe_load(path.read_text()) or []
    if not isinstance(entries, list):
        raise IntentError(f"{path.name}: a list of entries, each with an id")
    out, ids = [], set()
    for e in entries:
        actions = [k for k in e if k != "id"] if isinstance(e, dict) else []
        if not isinstance(e, dict) or not ID.fullmatch(str(e.get("id", ""))) or len(actions) != 1:
            raise IntentError(f"{path.name}: each entry has an id and one of "
                              f"{', '.join(REFACTORS)}, not {e!r}")
        rid, kind, spec = str(e["id"]), actions[0], e[actions[0]]
        if rid in ids:
            raise IntentError(f"{path.name}: id {rid} is used twice")
        ids.add(rid)
        if kind not in REFACTORS or not isinstance(spec, dict):
            raise IntentError(f"{path.name}: {rid}: {kind!r} isn't one of {', '.join(REFACTORS)}")
        try:
            if kind == "rename_table":
                r = Refactor(rid, kind, spec["from"].lower(), spec["from"].lower(),
                             spec["to"].lower())
                names, pattern = [r.old, r.new], rf"{NAME}\.{NAME}"
            else:
                r = Refactor(rid, kind, spec["table"].lower(), spec["from"].lower(),
                             spec["to"].lower())
                names, pattern = [r.table], rf"{NAME}\.{NAME}"
                if not all(re.fullmatch(NAME, n) for n in (r.old, r.new)):
                    raise IntentError(f"{path.name}: {rid}: column names, not {spec!r}")
        except (KeyError, AttributeError):
            raise IntentError(f"{path.name}: {rid}: {kind} needs "
                              + ("from and to" if kind == "rename_table" else "table, from and to")
                              + f", not {spec!r}") from None
        if not all(re.fullmatch(pattern, n) for n in names):
            raise IntentError(f"{path.name}: {rid}: tables are two-part names, not {spec!r}")
        if r.old == r.new:
            raise IntentError(f"{path.name}: {rid}: from and to are the same")
        out.append(r)
    return out


def checksum(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()[:16]


HEADER = ("ucstate", "id", "object", "from", "to", "phase")


def header(text: str) -> tuple[dict[str, str], str]:
    """The `-- key: value` lines at the top of a script, and the text after them."""
    fields: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].startswith("--") or not lines[i].strip()):
        if (m := re.match(r"-- ?([a-z]+):\s+(\S+)", lines[i])) and m[1] in HEADER:
            fields.setdefault(m[1], m[2])
        i += 1
    return fields, "\n".join(lines[i:])


def scripts(root: Path) -> list[Script]:
    out: list[Script] = []
    folder = root / "migrations"
    for path, kind in [*((p, "transition") for p in sorted(folder.glob("*.sql"))),
                       *((p, "once") for p in sorted((folder / "once").glob("*.sql")))]:
        rel = path.relative_to(root)
        text = path.read_text()
        fields, body = header(text)
        if fields.get("ucstate") != kind:
            raise IntentError(f"{rel}: the first line must be `-- ucstate: {kind}`")
        if fields.get("id") != path.stem:
            raise IntentError(f"{rel}: `-- id:` must be the file's name, {path.stem}")
        statements = split_statements(body)
        if not statements:
            raise IntentError(f"{rel}: no statements")
        s = Script(path.stem, kind, rel, statements, checksum(text))
        if kind == "transition":
            obj = fields.get("object", "")
            if not re.fullmatch(rf"(table:)?{NAME}\.{NAME}", obj.lower()):
                raise IntentError(f"{rel}: `-- object:` names a table, such as sales.orders")
            s.object = obj.lower() if obj.startswith("table:") else f"table:{obj.lower()}"
            s.before, s.after = fields.get("from"), fields.get("to")
            if not (s.before and s.after):
                raise IntentError(f"{rel}: `-- from:` and `-- to:` give the object's hashes. "
                                  "`ucstate transition` writes them")
        else:
            s.phase = fields.get("phase", "")
            if s.phase not in PHASES:
                raise IntentError(f"{rel}: `-- phase:` is pre or post")
        out.append(s)
    seen: dict[str, Path] = {}
    for s in out:
        if s.id in seen:
            raise IntentError(f"{s.path}: id {s.id} is also {seen[s.id]}")
        seen[s.id] = s.path
    return out
