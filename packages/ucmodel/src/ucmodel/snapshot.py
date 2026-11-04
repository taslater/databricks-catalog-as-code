"""Snapshots: a catalog's model as read at one time, kept in a file.

A snapshot is written once, by `ucmodel snapshot`, and after that only read.
Reports, drawings and plan previews are made from snapshots, so a catalog
is read once however many times it is looked at. Reading costs no warehouse,
but it takes several REST calls per table and, with lineage, one per column.

The file holds a hash of everything else in it, and `load` refuses a file
whose contents don't match. That catches an edit. It doesn't stop someone
who also recomputes the hash: where that matters, keep snapshots where they
can't be changed, such as a CI job's artifacts. The file is named for its
catalog, when it was taken and the start of its hash, and `write` never
replaces a file.

A snapshot holds what the API returns, including owners' names.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import lineage, reader
from .lineage import Lineage
from .normalise import normalise
from .progress import Progress
from .workspace import Workspace

FORMAT = "ucmodel.snapshot/1"
STAMP = "%Y-%m-%dT%H:%M:%SZ"


class SnapshotError(Exception):
    pass


@dataclass(frozen=True)
class Snapshot:
    catalog: str
    schemas: list[str]  # the schemas that were read
    taken_at: datetime
    model: dict[str, dict]
    lineage: Lineage | None  # None when lineage wasn't read
    hash: str

    @property
    def file_name(self) -> str:
        return f"{self.catalog}-{self.taken_at:%Y%m%dT%H%M%SZ}-{self.hash[:12]}.json"

    def describe(self) -> str:
        """Such as `ucstate_prod at 2026-11-01 12:00 UTC (3f2a9c01b7de)`."""
        return f"{self.catalog} at {self.taken_at:%Y-%m-%d %H:%M} UTC ({self.hash[:12]})"


def take(ws: Workspace, catalog: str, schemas: list[str] | None = None, *,
         with_lineage: bool = True, now: datetime | None = None,
         progress: Progress | None = None) -> Snapshot:
    """Read the schemas, or every schema but `information_schema`, over REST."""
    if ws.catalog(catalog) is None:
        raise SnapshotError(f"catalog {catalog} not found")
    names = sorted(schemas or reader.schemas(ws, catalog))
    raw = reader.read(ws, catalog, names, progress)
    if missing := [name for name, schema in raw["schemas"].items() if schema is None]:
        raise SnapshotError(f"schema {', '.join(missing)} not found in {catalog}")
    model = normalise(raw)
    found = lineage.read(ws, catalog, model, progress) if with_lineage else None
    return make(catalog, names, now or datetime.now(UTC), model, found)


def make(catalog: str, schemas: list[str], taken_at: datetime, model: dict[str, dict],
         found: Lineage | None = None) -> Snapshot:
    taken_at = taken_at.astimezone(UTC).replace(microsecond=0)
    body = _body(catalog, sorted(schemas), taken_at, model, found)
    return Snapshot(catalog, sorted(schemas), taken_at, model, found, _digest(body))


def _body(catalog: str, schemas: list[str], taken_at: datetime, model: dict[str, dict],
          found: Lineage | None) -> dict:
    return {"format": FORMAT, "catalog": catalog, "schemas": schemas,
            "taken_at": taken_at.strftime(STAMP), "model": model,
            "lineage": found.to_json() if found else None}


def _digest(body: dict) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def dumps(s: Snapshot) -> str:
    body = _body(s.catalog, s.schemas, s.taken_at, s.model, s.lineage)
    return json.dumps({**body, "hash": s.hash}, indent=1, sort_keys=True) + "\n"


def write(s: Snapshot, out: str | Path) -> Path:
    """Write to `out`, or into it when it is a folder. Returns the path written.

    A folder that exists, or a name ending in `/` (as typed: a Path drops
    it), gets the snapshot's own file name. An existing file is never
    replaced.
    """
    folder = isinstance(out, str) and out.endswith(("/", os.sep))
    path = Path(out) / s.file_name if folder or Path(out).is_dir() else Path(out)
    if path.exists():
        raise SnapshotError(f"{path} exists, and a snapshot is never replaced")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(s))
    return path


def load(path: Path) -> Snapshot:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SnapshotError(f"{path}: not a snapshot: {e}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        found = data.get("format") if isinstance(data, dict) else None
        raise SnapshotError(f"{path}: format {found!r}; this ucmodel reads {FORMAT}")
    stored = data.pop("hash", None)
    if _digest(data) != stored:
        raise SnapshotError(f"{path}: its contents don't match its hash, so it was changed "
                            "after it was taken. Take a new snapshot")
    found = Lineage.from_json(data["lineage"]) if data["lineage"] else None
    taken_at = datetime.strptime(data["taken_at"], STAMP).replace(tzinfo=UTC)
    return Snapshot(data["catalog"], data["schemas"], taken_at, data["model"], found, stored)


def within(model: dict[str, dict], schemas: list[str]) -> dict[str, dict]:
    """The objects of a model that are in these schemas."""
    keep = set(schemas)
    return {k: o for k, o in model.items() if k.split(":", 1)[1].split(".")[0] in keep}
