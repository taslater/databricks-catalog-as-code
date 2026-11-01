"""Run a .sql file on the warehouse, one statement at a time, stopping at the first failure.

    uv run python -m probes.run_sql FILE --profile my-profile
"""

import argparse
import os
import sys
from pathlib import Path

from ucmodel.sqltext import split_statements
from ucmodel.workspace import SqlError, Workspace


def run_file(ws: Workspace, path: Path, echo=print) -> None:
    statements = split_statements(path.read_text())
    for n, statement in enumerate(statements, 1):
        first = " ".join(statement.split())[:90]
        try:
            ws.sql(statement)
        except SqlError as e:
            raise SystemExit(f"[{n}/{len(statements)}] FAILED: {first}\n  {e.summary}") from e
        echo(f"[{n}/{len(statements)}] ok: {first}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--warehouse", default=os.environ.get("DATABRICKS_WAREHOUSE_ID"),
                        required="DATABRICKS_WAREHOUSE_ID" not in os.environ,
                        help="a SQL warehouse (default: DATABRICKS_WAREHOUSE_ID)")
    args = parser.parse_args()
    run_file(Workspace.connect(args.profile, args.warehouse), args.file)
    sys.exit(0)
