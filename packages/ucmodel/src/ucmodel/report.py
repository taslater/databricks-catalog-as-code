"""Reports: what changed between two snapshots.

A report is made from two files and nothing else, so it costs no workspace
calls, and the same two snapshots always give the same report. It compares
with `compare(..., exact=True)`, so every property counts, and `meta`
(owners, ids and audit times) never does. Lineage isn't compared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .model import Difference, compare
from .snapshot import Snapshot

LONGEST = 160  # characters of a value the text and markdown show; the JSON has it all


@dataclass
class Report:
    old: Snapshot
    new: Snapshot
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: dict[str, list[Difference]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff(old: Snapshot, new: Snapshot) -> Report:
    report = Report(old, new)
    for d in compare(new.model, old.model, exact=True):
        if d.path == "":
            (report.added if d.live is None else report.removed).append(d.key)
        else:
            report.changed.setdefault(d.key, []).append(d)
    return report


def _summary(r: Report) -> str:
    if r.empty:
        return "No differences."
    parts = [f"{len(r.added)} added", f"{len(r.removed)} removed", f"{len(r.changed)} changed"]
    n = len(r.added) + len(r.removed) + len(r.changed)
    return f"{n} object{'s differ' if n != 1 else ' differs'}: " + ", ".join(parts) + "."


def _value(v: Any) -> str:
    text = json.dumps(v)
    return text if len(text) <= LONGEST else text[:LONGEST - 1] + "…"


def _scope(r: Report) -> list[str]:
    if r.old.schemas == r.new.schemas:
        return []
    return [f"The snapshots read different schemas: {', '.join(r.old.schemas)}, then "
            f"{', '.join(r.new.schemas)}. An object in only one of them shows as added or "
            "removed."]


def text(r: Report) -> str:
    lines = [f"From {r.old.describe()}", f"  to {r.new.describe()}", "", _summary(r),
             *_scope(r)]
    for key in r.added:
        lines.append(f"  added    {key}")
    for key in r.removed:
        lines.append(f"  removed  {key}")
    for key, diffs in r.changed.items():
        lines.append(f"  changed  {key}")
        lines += [f"             {d.path}: {_value(d.live)} -> {_value(d.desired)}"
                  for d in diffs]
    return "\n".join(lines)


def markdown(r: Report) -> str:
    """For a merge request comment or a job summary."""
    lines = [f"**From** {r.old.describe()}  ", f"**to** {r.new.describe()}", "", _summary(r),
             *_scope(r)]
    if not r.empty:
        lines.append("")
    lines += [f"- added `{key}`" for key in r.added]
    lines += [f"- removed `{key}`" for key in r.removed]
    for key, diffs in r.changed.items():
        lines.append(f"- changed `{key}`")
        lines += [f"  - `{d.path}`: `{_value(d.live)}` → `{_value(d.desired)}`" for d in diffs]
    return "\n".join(lines)


def to_json(r: Report) -> dict:
    def side(s: Snapshot) -> dict:
        return {"catalog": s.catalog, "schemas": s.schemas,
                "taken_at": f"{s.taken_at:%Y-%m-%dT%H:%M:%SZ}", "hash": s.hash}
    return {"old": side(r.old), "new": side(r.new), "added": r.added, "removed": r.removed,
            "changed": {key: [{"path": d.path, "old": d.live, "new": d.desired} for d in diffs]
                        for key, diffs in r.changed.items()}}
