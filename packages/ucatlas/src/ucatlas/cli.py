"""The `ucatlas` command line."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from ucmodel import lineage, reader
from ucmodel.normalise import normalise
from ucmodel.snapshot import SnapshotError, load, within
from ucmodel.workspace import NoCredentials, Workspace

from .diagram import DiagramError, write_site


class NotThere(Exception):
    """A catalog or schema that the workspace, or the snapshot, doesn't have."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ucatlas", description="Diagrams of a Unity Catalog.")
    parser.add_argument("--profile",
                        help="Databricks CLI profile. Without one, DATABRICKS_* variables are used")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("draw", help="draw a catalog's schemas and their lineage as a static site")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--catalog", help="read this catalog over REST")
    source.add_argument("--snapshot", type=Path,
                        help="draw a snapshot that `ucmodel snapshot` wrote, with no workspace")
    p.add_argument("--schema", action="append",
                   help="a schema to draw. Give it once for each (default: every schema "
                        "but information_schema, or every schema in the snapshot)")
    p.add_argument("-o", "--out", type=Path, default=Path("site"),
                   help="the folder to write the site to (default: site)")
    p.add_argument("--no-lineage", action="store_true",
                   help="skip lineage, which takes one API call per column")

    p = sub.add_parser("serve", help="a local UI for the snapshot, diff, draw and plan commands")
    p.add_argument("--snapshots", type=Path, default=Path("snapshots"),
                   help="the folder of snapshots (default: snapshots)")
    p.add_argument("--project", type=Path,
                   help="the ucstate project, for plan previews (default: the current folder, "
                        "if it has a project.yml)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--work", type=Path, default=Path(".ucatlas"),
                   help="where drawings and previews are kept (default: .ucatlas)")

    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .serve import serve
            return serve(args.snapshots, args.project, args.port, args.work)
        if args.snapshot:
            return draw_snapshot(args)
        return cmd_draw(Workspace.connect(args.profile), args)
    except (NoCredentials, NotThere, DiagramError, SnapshotError) as e:
        print(f"ucatlas: {e}", file=sys.stderr)
        return 2


def cmd_draw(ws: Workspace, args: argparse.Namespace) -> int:
    if ws.catalog(args.catalog) is None:
        raise NotThere(f"catalog {args.catalog} not found")
    raw = reader.read(ws, args.catalog, args.schema or reader.schemas(ws, args.catalog))
    if missing := [name for name, schema in raw["schemas"].items() if schema is None]:
        raise NotThere(f"schema {', '.join(missing)} not found in {args.catalog}")
    model = normalise(raw)
    found = None if args.no_lineage else lineage.read(ws, args.catalog, model)
    return _write(model, args.catalog, args.out, datetime.now(UTC), found)


def draw_snapshot(args: argparse.Namespace) -> int:
    s = load(args.snapshot)
    model = s.model
    if args.schema:
        if missing := [name for name in args.schema if name not in s.schemas]:
            raise NotThere(f"schema {', '.join(missing)} not in the snapshot {args.snapshot}")
        model = within(model, args.schema)
    found = None if args.no_lineage else s.lineage
    return _write(model, s.catalog, args.out, s.taken_at, found)


def _write(model: dict, catalog: str, out: Path, when: datetime,
           found: lineage.Lineage | None) -> int:
    names = write_site(model, catalog, out, when, found)
    print(f"wrote {len(names)} schema page{'s' if len(names) != 1 else ''} to {out}. "
          "To look at the site:\n"
          f"  python3 -m http.server --bind 127.0.0.1 --directory {out} 8000")
    return 0
