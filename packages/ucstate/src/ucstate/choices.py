"""Writing a choice into the project: a refactor.yml rename, or an allowance in project.yml.

The planner stops at an ambiguous or destructive change and lists the
choices that would settle it, each as one of the commands below. A command
adds one entry to one file, as text at the end of the file or of its
`allow:` list, so comments and layout are kept. It then loads the file
again, and writes nothing unless the file reads as before plus the new
entry. Nothing here reads or changes a catalog: the entry is a reviewed
line in git, and the next plan acts on it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from datetime import date
from pathlib import Path

import yaml

from .intent import ALLOWANCES, NAME, IntentError, allowances, refactors
from .project import ProjectError


def rename_id(kind: str, spec: dict[str, str], taken: Collection[str] = ()) -> str:
    """Today's date, then the table and the new name, such as 2026-11-01-orders-total."""
    parts = [spec["table"].split(".")[-1]] if kind == "rename_column" else []
    rid = "-".join([date.today().isoformat(), *parts, spec["to"].split(".")[-1]]).replace("_", "-")
    n, out = 2, rid
    while out in taken:
        out, n = f"{rid}-{n}", n + 1
    return out


def entry(rid: str, kind: str, spec: dict[str, str]) -> str:
    fields = ", ".join(f"{k}: {v}" for k, v in spec.items())
    return f"- id: {rid}\n  {kind}: {{{fields}}}\n"


def add_rename(root: Path, kind: str, spec: dict[str, str], rid: str | None = None) -> str:
    """Add a rename_table or rename_column entry to refactor.yml. Returns what was added."""
    path = root / "refactor.yml"
    old = path.read_text() if path.exists() else ""
    try:
        before = refactors(path)
    except (IntentError, yaml.YAMLError) as e:
        raise ProjectError(str(e)) from None
    spec = {k: v.lower() for k, v in spec.items()}
    for r in before:
        if r.kind == kind and (r.old, r.new) == (spec["from"], spec["to"]) and (
                kind == "rename_table" or r.table == spec["table"]):
            raise ProjectError(f"refactor.yml already has this rename, as {r.id}")
    rid = rid or rename_id(kind, spec, {r.id for r in before})
    added = entry(rid, kind, spec)
    text = (old if not old or old.endswith("\n") else old + "\n") + added
    _write_checked(path, text, lambda: len(refactors(path)) == len(before) + 1, old)
    return added


def add_allowance(root: Path, kind: str, name: str) -> str:
    """Add `kind: name` to project.yml's allow: list. Returns what was added."""
    if kind not in ALLOWANCES:
        raise ProjectError(f"{kind!r} isn't one of {', '.join(ALLOWANCES)}")
    path = root / "project.yml"
    if not path.exists():
        raise ProjectError(f"{path} not found")
    old = path.read_text()
    cfg = yaml.safe_load(old) or {}
    try:
        allowances([{kind: name}])
        before = allowances(cfg.get("allow"))
    except IntentError as e:
        raise ProjectError(str(e)) from None
    if (kind, name.lower()) in before:
        raise ProjectError(f"project.yml already allows {kind}: {name}")
    item = f"- {kind}: {name.lower()}"
    lines = old.splitlines(keepends=True)
    at = next((i for i, line in enumerate(lines) if re.match(r"allow:\s*(#.*)?$", line)), None)
    if at is None:
        if "allow" in cfg:
            raise ProjectError("project.yml's allow: isn't a block list, so add this by hand: "
                               f"{item}")
        text = (old if old.endswith("\n") else old + "\n") + f"allow:\n  {item}\n"
        added = f"allow:\n  {item}\n"
    else:
        end, indent = at + 1, "  "
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t-"):
            if m := re.match(r"(\s*)- ", lines[end]):
                indent = m[1]
            end += 1
        while end > at + 1 and not lines[end - 1].strip():
            end -= 1
        if end > 0 and not lines[end - 1].endswith("\n"):
            lines[end - 1] += "\n"
        added = f"{indent}{item}\n"
        text = "".join([*lines[:end], added, *lines[end:]])

    def ok() -> bool:
        new = yaml.safe_load(path.read_text()) or {}
        rest = {k: v for k, v in new.items() if k != "allow"}
        return (rest == {k: v for k, v in cfg.items() if k != "allow"}
                and allowances(new.get("allow")) == before | {(kind, name.lower())})
    _write_checked(path, text, ok, old)
    return added


def _write_checked(path: Path, text: str, ok: Callable[[], bool], old: str) -> None:
    """Write `text`, and put the old contents back unless `ok()` holds afterwards."""
    existed = path.exists()
    path.write_text(text)
    try:
        good = ok()
    except (IntentError, yaml.YAMLError):
        good = False
    if not good:
        if existed:
            path.write_text(old)
        else:
            path.unlink()
        raise ProjectError(f"adding the entry would leave {path.name} reading wrongly, so it "
                           "is unchanged. Add the entry by hand")


def table_name(text: str) -> str:
    if not re.fullmatch(rf"{NAME}\.{NAME}", text.lower()):
        raise ProjectError(f"a table is a two-part name, such as sales.orders, not {text!r}")
    return text.lower()


def column_name(text: str) -> str:
    if not re.fullmatch(NAME, text.lower()):
        raise ProjectError(f"{text!r} isn't a column name")
    return text.lower()
