"""atlas_demo/: a synthetic catalog of nine schemas, the SQL files that load it, and
lineage as the platform might have recorded it. The explorer's tests use it, and
`python demo.py OUT` writes it as a snapshot, to try the explorer on.

`src/` is the catalog's DDL, a file per object as `ucstate` lays out a project.
`jobs/` are the statements that write its tables; `dim_contacts.sql` is one sqlglot
can't parse. `lineage.json` holds edges as `ucmodel.lineage` keeps them, including
some no SQL here explains (a notebook's).
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from ucmodel import snapshot
from ucmodel.compiler import compile_statements
from ucmodel.lineage import Lineage
from ucmodel.sqltext import split_statements

HERE = Path(__file__).parent / "atlas_demo"
CATALOG = "atlas_demo"
TAKEN = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
ORDER = {"schema.sql": 0, "tables": 1, "views": 2}  # schemas, then tables, then views


def model() -> dict[str, dict]:
    files = sorted((HERE / "src").rglob("*.sql"),
                   key=lambda p: (ORDER.get(p.name, ORDER.get(p.parent.name, 3)), str(p)))
    return compile_statements((p.relative_to(HERE / "src").parts[0], s)
                              for p in files for s in split_statements(p.read_text()))


def lineage() -> Lineage:
    return Lineage.from_json(json.loads((HERE / "lineage.json").read_text()))


def sql_files() -> list[tuple[str, str]]:
    return [(p.name, p.read_text()) for p in sorted((HERE / "jobs").glob("*.sql"))]


def take() -> snapshot.Snapshot:
    m = model()
    schemas = sorted(o["name"] for o in m.values() if o["kind"] == "schema")
    return snapshot.make(CATALOG, schemas, TAKEN, m, lineage())


if __name__ == "__main__":
    print(snapshot.write(take(), sys.argv[1] if len(sys.argv) > 1 else "./"))
