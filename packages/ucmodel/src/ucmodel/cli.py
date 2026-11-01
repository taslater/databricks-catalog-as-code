"""The `ucmodel` command line: take a snapshot of a catalog, and compare two snapshots."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

from . import report
from .snapshot import SnapshotError, load, take, write
from .workspace import NoCredentials, Workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ucmodel",
                                     description="Snapshots of a Unity Catalog, and reports.")
    parser.add_argument("--profile",
                        help="Databricks CLI profile. Without one, DATABRICKS_* variables are used")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("snapshot", help="read a catalog over REST and write it to a file")
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", action="append",
                   help="a schema to read. Give it once for each (default: every schema but "
                        "information_schema)")
    p.add_argument("-o", "--out", default="snapshots/",
                   help="the file, or the folder to name one in (default: snapshots/)")
    p.add_argument("--no-lineage", action="store_true",
                   help="skip lineage, which takes one API call per table and one per column")

    p = sub.add_parser("diff", help="report what changed between two snapshots. Exits 1 "
                                    "when anything did")
    p.add_argument("old", type=Path)
    p.add_argument("new", type=Path)
    p.add_argument("--format", choices=("text", "markdown", "json"), default="text")

    args = parser.parse_args(argv)
    try:
        if args.command == "diff":
            return cmd_diff(args)
        return cmd_snapshot(Workspace.connect(args.profile), args)
    except (NoCredentials, SnapshotError) as e:
        print(f"ucmodel: {e}", file=sys.stderr)
        return 2


def cmd_snapshot(ws: Workspace, args: argparse.Namespace) -> int:
    """Prints the file's path, and what it holds on stderr."""
    start = time.monotonic()
    s = take(ws, args.catalog, args.schema, with_lineage=not args.no_lineage)
    path = write(s, args.out)
    kinds = Counter(key.split(":")[0] for key in s.model)
    held = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in sorted(kinds.items()))
    if s.lineage is None:
        held += "; no lineage"
    else:
        held += (f"; lineage: {len(s.lineage.tables)} table edges, "
                 f"{len(s.lineage.columns)} column edges")
    print(f"{s.describe()}: {held or 'nothing'}, read in {time.monotonic() - start:.1f} s",
          file=sys.stderr)
    print(path)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    r = report.diff(load(args.old), load(args.new))
    if args.format == "json":
        print(json.dumps(report.to_json(r), indent=2))
    else:
        print(report.markdown(r) if args.format == "markdown" else report.text(r))
    return 0 if r.empty else 1
