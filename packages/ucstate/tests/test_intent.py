"""intent.py: allowances, refactor.yml and the scripts in migrations/."""

from pathlib import Path

import pytest

from ucstate.intent import (
    History,
    Intent,
    IntentError,
    Script,
    allowances,
    checksum,
    header,
    refactors,
    scripts,
)
from ucstate.project import Project, ProjectError

TRANSITION = """-- ucstate: transition
-- id: 2026-11-02-narrow
-- object: sales.orders
-- from: 3f9a1c0000000000   as it is now
-- to:   b71e040000000000   as declared
--
-- What the plan found:
--   rewrite: column quantity changes from bigint to int

CREATE OR REPLACE TABLE sales.orders AS SELECT CAST(quantity AS INT) AS quantity FROM sales.orders;
ALTER TABLE sales.orders ALTER COLUMN quantity SET NOT NULL;
"""
ONCE = """-- ucstate: once
-- id: 2026-11-03-dedupe
-- phase: post
DELETE FROM sales.orders WHERE order_id < 0;
"""


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_allowances_name_one_object_each():
    assert allowances([{"drop_table": "Sales.Old"}, {"drop_column": "sales.c.legacy"}]) == {
        ("drop_table", "sales.old"), ("drop_column", "sales.c.legacy")}
    assert allowances(None) == set()
    for bad, message in [([{"drop_schema": "sales"}], "isn't one of"),
                         ([{"drop_column": "sales.c"}], "takes a 3-part name"),
                         ([{"drop_table": "old"}], "takes a 2-part name"),
                         (["drop_table: sales.old"], "each entry is one `kind: name`")]:
        with pytest.raises(IntentError, match=message):
            allowances(bad)


def test_refactor_yml_entries(tmp_path):
    path = write(tmp_path, "refactor.yml", """
- id: 2026-11-02-customer-full-name
  rename_column: {table: sales.customers, from: name, to: full_name}
- id: 2026-11-05-order-headers
  rename_table: {from: sales.orders, to: sales.order_headers}
""")
    column, table = refactors(path)
    assert (column.kind, column.table, column.old, column.new) == (
        "rename_column", "sales.customers", "name", "full_name")
    assert column.old_key == column.new_key == "table:sales.customers"
    assert (table.old_key, table.new_key) == ("table:sales.orders", "table:sales.order_headers")
    assert table.describe() == "rename table sales.orders to sales.order_headers"
    assert refactors(tmp_path / "missing.yml") == []


@pytest.mark.parametrize("text, message", [
    ("{id: x}", "a list of entries"),
    ("- {rename_table: {from: s.a, to: s.b}}", "each entry has an id"),
    ("- {id: x, rename_table: {from: s.a, to: s.b}, rename_column: {}}", "each entry has an id"),
    ("- {id: x, move_table: {from: s.a, to: s.b}}", "isn't one of"),
    ("- {id: x, rename_table: {from: s.a}}", "needs from and to"),
    ("- {id: x, rename_column: {table: s.a, from: b}}", "needs table, from and to"),
    ("- {id: x, rename_table: {from: a, to: s.b}}", "two-part names"),
    ("- {id: x, rename_column: {table: s.a, from: b.c, to: d}}", "column names"),
    ("- {id: x, rename_table: {from: s.a, to: s.a}}", "the same"),
    ("- {id: x, rename_table: {from: s.a, to: s.b}}\n- {id: x, rename_table: {from: s.c, to: s.d}}",
     "used twice"),
])
def test_refactor_yml_mistakes_are_refused(tmp_path, text, message):
    with pytest.raises(IntentError, match=message):
        refactors(write(tmp_path, "refactor.yml", text))


def test_scripts_are_read_with_their_headers(tmp_path):
    write(tmp_path, "migrations/2026-11-02-narrow.sql", TRANSITION)
    write(tmp_path, "migrations/once/2026-11-03-dedupe.sql", ONCE)
    transition, once = scripts(tmp_path)
    assert (transition.kind, transition.object, transition.before, transition.after) == (
        "transition", "table:sales.orders", "3f9a1c0000000000", "b71e040000000000")
    assert len(transition.statements) == 2 and transition.statements[0].startswith("CREATE OR")
    assert transition.checksum == checksum(TRANSITION) == checksum(TRANSITION.replace("\n", "\r\n"))
    assert (once.kind, once.phase, once.path) == (
        "once", "post", Path("migrations/once/2026-11-03-dedupe.sql"))
    assert scripts(tmp_path / "nowhere") == []


@pytest.mark.parametrize("rel, text, message", [
    ("migrations/x.sql", ONCE.replace("2026-11-03-dedupe", "x"),
     "must be `-- ucstate: transition`"),
    ("migrations/once/y.sql", ONCE, "must be the file's name, y"),
    ("migrations/2026-11-02-narrow.sql", TRANSITION.replace("-- from: 3f9a1c0000000000", ""),
     "`-- from:` and `-- to:`"),
    ("migrations/2026-11-02-narrow.sql", TRANSITION.replace("sales.orders\n", "orders\n", 1),
     "names a table"),
    ("migrations/once/2026-11-03-dedupe.sql", ONCE.replace("post", "during"), "pre or post"),
    ("migrations/once/2026-11-03-dedupe.sql", ONCE.split("DELETE")[0], "no statements"),
])
def test_script_mistakes_are_refused(tmp_path, rel, text, message):
    write(tmp_path, rel, text)
    with pytest.raises(IntentError, match=message):
        scripts(tmp_path)


def test_a_script_id_is_used_once_across_both_folders(tmp_path):
    write(tmp_path, "migrations/once/2026-11-03-dedupe.sql", ONCE)
    write(tmp_path, "migrations/2026-11-03-dedupe.sql", TRANSITION.replace(
        "2026-11-02-narrow", "2026-11-03-dedupe"))
    with pytest.raises(IntentError, match="id 2026-11-03-dedupe is also"):
        scripts(tmp_path)


def test_the_header_ignores_comment_lines_that_merely_look_like_fields():
    fields, body = header(TRANSITION)
    assert fields == {"ucstate": "transition", "id": "2026-11-02-narrow", "object": "sales.orders",
                      "from": "3f9a1c0000000000", "to": "b71e040000000000"}
    assert body.startswith("CREATE OR REPLACE")


def test_pending_scripts_are_those_the_target_has_not_run():
    a = Script("a", "once", Path("a.sql"), ["SELECT 1"], "1")
    b = Script("b", "once", Path("b.sql"), ["SELECT 1"], "2")
    assert Intent(scripts=[b, a]).pending(History()) == [a, b]
    assert Intent(scripts=[b, a]).pending(History(scripts={"a": "1"})) == [b]


def test_a_project_gathers_its_intent(tmp_path):
    write(tmp_path, "project.yml", "schemas: [sales]\nallow:\n  - drop_table: sales.old\n")
    write(tmp_path, "refactor.yml", "- {id: r1, rename_table: {from: sales.a, to: sales.b}}\n")
    write(tmp_path, "migrations/once/2026-11-03-dedupe.sql", ONCE)
    got = Project.load(tmp_path).intent()
    assert got.allow == {("drop_table", "sales.old")}
    assert [r.id for r in got.refactors] == ["r1"] and [s.id for s in got.scripts] == [
        "2026-11-03-dedupe"]
    write(tmp_path, "project.yml", "schemas: [sales]\nallow:\n  - drop_everything: sales\n")
    with pytest.raises(ProjectError, match="isn't one of"):
        Project.load(tmp_path).intent()


def test_a_rebuildable_file_must_be_a_create_table_as_select(tmp_path):
    write(tmp_path, "project.yml", "schemas: [sales]\n")
    write(tmp_path, "src/sales/schema.sql", "CREATE SCHEMA sales;\n")
    write(tmp_path, "src/sales/tables/totals.sql",
          "-- ucstate: rebuildable\nCREATE TABLE sales.totals COMMENT 'Per customer'\n"
          "AS SELECT customer_id, sum(amount) AS amount FROM sales.orders GROUP BY 1;\n")
    totals = next(f for f in Project.load(tmp_path).sources() if f.key == "table:sales.totals")
    assert totals.rebuildable
    write(tmp_path, "src/sales/tables/totals.sql",
          "-- ucstate: rebuildable\nCREATE TABLE sales.totals (customer_id BIGINT);\n")
    with pytest.raises(ProjectError, match="only a CREATE TABLE ... AS SELECT can be rebuildable"):
        Project.load(tmp_path).sources()
