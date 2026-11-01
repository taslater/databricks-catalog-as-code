"""The Phase 1 round trip, against the workspace, with known-bad controls.

Runs only when UCSTATE_PROFILE names a Databricks CLI profile, or
UCSTATE_LIVE=1 with the DATABRICKS_* variables, as in CI:

    UCSTATE_PROFILE=my-profile uv run pytest -m live

It rebuilds ucstate_test.roundtrip from ucmodel's samples/roundtrip.sql before
and after, so ucstate_test is left as the workload builds it. A full run takes
about four minutes of warehouse time.

Agreement alone would also pass if the normaliser threw everything away.
So after the round trip agrees, the source and then the target are changed
in known ways. `compare` must report exactly the changes it should, and
none of the ones the policies say to ignore.
"""

import shutil
from pathlib import Path

import pytest

from livegate import ENABLED, PROFILE, REASON, WAREHOUSE
from ucmodel import reader, samples
from ucmodel.model import compare
from ucmodel.normalise import normalise
from ucmodel.workspace import Workspace
from ucstate import shadow
from ucstate.cli import main
from ucstate.project import Project

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not ENABLED, reason=REASON),
]
EXAMPLE = Path(__file__).parent.parent / "examples" / "roundtrip"
QUIET = lambda message: None  # noqa: E731


def paths(diffs) -> set[tuple[str, str]]:
    return {(d.key, d.path) for d in diffs}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new))


@pytest.fixture(scope="module")
def ws():
    workspace = Workspace.connect(PROFILE or None, WAREHOUSE)
    samples.build(workspace)
    yield workspace
    samples.build(workspace)


@pytest.fixture(scope="module")
def imported(ws, tmp_path_factory) -> Project:
    root = tmp_path_factory.mktemp("roundtrip")
    shutil.copy(EXAMPLE / "project.yml", root / "project.yml")
    profile = ["--profile", PROFILE] if PROFILE else []
    assert main(["--project", str(root), *profile, "--warehouse", WAREHOUSE,
                 "import", "--target", "test"]) == 0
    return Project.load(root)


@pytest.fixture(scope="module")
def desired(ws, imported) -> dict:
    return shadow.build(ws, imported, log=QUIET)


def live(ws, project: Project) -> dict:
    return normalise(reader.read(ws, project.catalog("test"), project.schemas))


def test_import_matches_the_committed_example(imported):
    def files(root):
        return {str(p.relative_to(root)): p.read_text() for p in (root / "src").rglob("*.sql")}
    assert files(imported.root) == files(EXAMPLE)


def test_the_round_trip_agrees(ws, imported, desired):
    assert compare(desired, live(ws, imported)) == []


def test_source_changes_are_reported(ws, imported, tmp_path):
    shutil.copytree(imported.root, tmp_path, dirs_exist_ok=True)
    src = tmp_path / "src" / "roundtrip"
    edit(src / "tables/orders.sql", "COMMENT 'One row per order'", "COMMENT 'Changed'")
    edit(src / "tables/customers.sql", "('pii' = 'email')", "('pii' = 'contact')")
    edit(src / "views/customer_orders.sql", "GROUP BY c.customer_id, c.name;",
         "GROUP BY c.customer_id, c.name\nHAVING count(o.order_id) >= 0;")
    edit(src / "tables/order_lines.sql", "  labels array<string>\n)",
         "  labels array<string>,\n  note STRING\n)")
    edit(src / "volumes/landing.sql",
         "GRANT READ VOLUME ON VOLUME roundtrip.landing TO `account users`;\n", "")
    (src / "views/extra.sql").write_text("CREATE VIEW roundtrip.extra AS SELECT 1 AS one;\n")
    (src / "functions/add_tax.sql").unlink()
    # Ignored by policy: an undeclared property, and a reformatted SQL body.
    edit(src / "tables/orders.sql", "  'delta.enableTypeWidening' = 'true',\n", "")
    edit(src / "functions/mask_email.sql",
         "RETURN concat('***@', substring_index(email, '@', -1));",
         "return CONCAT(\n  '***@',\n  substring_index(email, '@', -1)\n);")

    changed = shadow.build(ws, Project.load(tmp_path), log=QUIET)
    assert paths(compare(changed, live(ws, imported))) == {
        ("table:roundtrip.orders", "comment"),
        ("table:roundtrip.customers", "columns.email.tags"),
        ("view:roundtrip.customer_orders", "definition"),
        ("table:roundtrip.order_lines", "columns"),
        ("volume:roundtrip.landing", "grants.account users"),
        ("view:roundtrip.extra", ""),
        ("function:roundtrip.add_tax", ""),
    }


def test_target_changes_are_reported(ws, imported, desired):
    for statement in [
        "COMMENT ON TABLE ucstate_test.roundtrip.customers IS 'drifted'",
        "ALTER TABLE ucstate_test.roundtrip.customers ALTER COLUMN region SET TAGS ('new' = 'tag')",
        "ALTER TABLE ucstate_test.roundtrip.orders SET TBLPROPERTIES "
        "('delta.enableTypeWidening' = 'false')",
        "ALTER TABLE ucstate_test.roundtrip.orders ALTER COLUMN status DROP DEFAULT",
        "ALTER TABLE ucstate_test.roundtrip.orders CLUSTER BY (customer_id)",
        "REVOKE SELECT ON TABLE ucstate_test.roundtrip.customer_orders FROM `account users`",
        "CREATE TABLE ucstate_test.roundtrip.stray (x INT)",
        # Ignored by policy: a property the source doesn't declare.
        "ALTER TABLE ucstate_test.roundtrip.orders SET TBLPROPERTIES ('my.undeclared' = 'x')",
    ]:
        ws.sql(statement)
    assert paths(compare(desired, live(ws, imported))) == {
        ("table:roundtrip.customers", "comment"),
        ("table:roundtrip.customers", "columns.region.tags"),
        ("table:roundtrip.orders", "properties.delta.enableTypeWidening"),
        ("table:roundtrip.orders", "columns.status.default"),
        ("table:roundtrip.orders", "clustering"),
        ("view:roundtrip.customer_orders", "grants.account users"),
        ("table:roundtrip.stray", ""),
    }
