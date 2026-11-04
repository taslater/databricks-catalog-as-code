"""Comparing two models, and what an object depends on.

`compare` gives every difference between two models. The planner uses
`compare_object` to find what changed on one object, so a plan and a
comparison never disagree about whether two objects are equal.

Comparison rules:

- `meta` is never compared;
- `properties` are declared-only: only the keys the desired side has count;
- `clustering` counts only when the desired side declares it (not None);
- view definitions and SQL function bodies are compared as token streams,
  so formatting and keyword case don't matter.

The first two rules are there because a source file declares only some
properties. Two models read from catalogs declare everything, so a report
between snapshots compares them with `exact=True`, which counts every
property and clustering on both sides.

`object_hash` is exact rather than by these rules. A plan's fingerprint
hashes live objects, to tell whether one has changed since the plan was
made.

`depends_on` gives the objects one needs to exist first, as far as the
model shows. The planner orders its changes by it, and the diagrams draw a
view's reads from it.

`tabular` says whether an object has columns to read: a table, a view, or an
object outside the model that the platform lists with them (a materialized
view, a streaming table, a metric view, an external or foreign table).
Lineage and the drawings cover all of them; only ucstate leaves the last
ones alone.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .sqltext import canonical, two_part_names


@dataclass(frozen=True)
class Difference:
    key: str
    path: str
    desired: Any
    live: Any

    def __str__(self) -> str:
        if self.path == "":
            what = "missing from the target" if self.live is None else "not in the source"
            return f"{self.key}: {what}"
        return f"{self.key} {self.path}: desired {self.desired!r}, live {self.live!r}"


def object_hash(obj: dict | None) -> str | None:
    """A short hash of everything but `meta`, or None for an absent object."""
    if obj is None:
        return None
    text = json.dumps({k: v for k, v in obj.items() if k != "meta"}, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def compare(desired: dict[str, dict], live: dict[str, dict], *,
            exact: bool = False) -> list[Difference]:
    diffs: list[Difference] = []
    for key in sorted(desired.keys() | live.keys()):
        d, a = desired.get(key), live.get(key)
        if d is None or a is None:
            diffs.append(Difference(key, "", d and "present", a and "present"))
        else:
            diffs.extend(compare_object(key, d, a, exact=exact))
    return diffs


def compare_object(key: str, d: dict, a: dict, *, exact: bool = False) -> list[Difference]:
    diffs = []
    for attr in d.keys() | a.keys():
        if attr == "meta":
            continue
        dv, lv = d.get(attr), a.get(attr)
        if attr == "properties":
            declared, actual = dv or {}, lv or {}
            for k in sorted(declared.keys() | actual.keys() if exact else declared):
                if declared.get(k) != actual.get(k):
                    diffs.append(Difference(key, f"properties.{k}", declared.get(k),
                                            actual.get(k)))
        elif attr == "clustering" and dv is None and not exact:
            continue
        elif attr in ("definition", "body") and d.get("language", "SQL") == "SQL":
            if canonical(dv) != canonical(lv):
                diffs.append(Difference(key, attr, dv, lv))
        elif attr == "columns":
            diffs.extend(_columns(key, dv or [], lv or []))
        elif attr in ("constraints", "tags", "grants"):
            for k in sorted((dv or {}).keys() | (lv or {}).keys()):
                if (dv or {}).get(k) != (lv or {}).get(k):
                    diffs.append(Difference(key, f"{attr}.{k}", (dv or {}).get(k),
                                            (lv or {}).get(k)))
        elif dv != lv:
            diffs.append(Difference(key, attr, dv, lv))
    return sorted(diffs, key=lambda x: x.path)


def _columns(key: str, d: list[dict], a: list[dict]) -> list[Difference]:
    d_names, l_names = [c["name"] for c in d], [c["name"] for c in a]
    diffs = []
    if d_names != l_names:
        diffs.append(Difference(key, "columns", d_names, l_names))
    live_by_name = {c["name"]: c for c in a}
    for dc in d:
        lc = live_by_name.get(dc["name"])
        if lc is None:
            continue
        for attr in dc.keys() | lc.keys():
            if dc.get(attr) != lc.get(attr):
                diffs.append(Difference(key, f"columns.{dc['name']}.{attr}", dc.get(attr),
                                        lc.get(attr)))
    return diffs


def tabular(obj: dict) -> bool:
    """A table, a view, or an object outside the model with columns."""
    return obj["kind"] in ("table", "view") or (obj["kind"] == "unsupported"
                                                and bool(obj.get("columns")))


def depends_on(key: str, obj: dict) -> set[str]:
    """The keys an object needs to exist first, as far as the model shows."""
    kind = key.split(":")[0]
    needs = set()
    if kind == "table":
        needs |= {f"table:{c['references']}" for c in obj["constraints"].values()
                  if c["type"] == "foreign_key"}
        needs |= {f"function:{c['mask']['function']}" for c in obj["columns"] if c["mask"]}
        if obj["row_filter"]:
            needs.add(f"function:{obj['row_filter']['function']}")
    elif kind == "view" or (kind == "function" and obj["language"] == "SQL"):
        text = obj["definition"] if kind == "view" else obj["body"]
        needs |= {f"{k}:{n}" for n in two_part_names(text) for k in ("table", "view", "function")}
    needs.discard(key)
    return needs
