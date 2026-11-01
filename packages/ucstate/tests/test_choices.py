"""choices.py, offline: adding a rename to refactor.yml, and an allowance to project.yml."""

from datetime import date

import pytest
import yaml

from ucstate.choices import add_allowance, add_rename, rename_id
from ucstate.intent import allowances, refactors
from ucstate.project import ProjectError

TODAY = date.today().isoformat()
PROJECT = """\
# The sample project.
shadow: {catalog: ucstate_shadow}
schemas: [shop]
targets:
  dev: {catalog: ucstate_dev}
"""


def test_a_rename_is_appended_and_the_comments_kept(tmp_path):
    path = tmp_path / "refactor.yml"
    path.write_text("# Renames, applied once per target.\n- id: first\n"
                    "  rename_table: {from: shop.a, to: shop.b}")  # no newline at the end
    added = add_rename(tmp_path, "rename_column",
                       {"table": "shop.orders", "from": "Total", "to": "amount"})
    assert added == (f"- id: {TODAY}-orders-amount\n"
                     "  rename_column: {table: shop.orders, from: total, to: amount}\n")
    assert path.read_text().startswith("# Renames, applied once per target.\n")
    assert [r.id for r in refactors(path)] == ["first", f"{TODAY}-orders-amount"]


def test_the_first_rename_makes_the_file(tmp_path):
    add_rename(tmp_path, "rename_table", {"from": "shop.a", "to": "shop.b"}, rid="move-a")
    assert [(r.id, r.old, r.new) for r in refactors(tmp_path / "refactor.yml")] == [
        ("move-a", "shop.a", "shop.b")]


def test_the_same_rename_twice_is_refused(tmp_path):
    add_rename(tmp_path, "rename_table", {"from": "shop.a", "to": "shop.b"})
    with pytest.raises(ProjectError, match="already has this rename"):
        add_rename(tmp_path, "rename_table", {"from": "shop.a", "to": "shop.b"})


def test_an_id_in_use_gets_a_suffix():
    spec = {"from": "shop.a", "to": "shop.b"}
    assert rename_id("rename_table", spec, {f"{TODAY}-b"}) == f"{TODAY}-b-2"


def test_an_entry_that_would_break_the_file_leaves_it_as_it_was(tmp_path):
    """The control: a file this can't append to must come back unchanged."""
    path = tmp_path / "refactor.yml"
    path.write_text("[]\n")
    with pytest.raises(ProjectError, match="Add the entry by hand"):
        add_rename(tmp_path, "rename_table", {"from": "shop.a", "to": "shop.b"})
    assert path.read_text() == "[]\n"


def allowed(tmp_path):
    cfg = yaml.safe_load((tmp_path / "project.yml").read_text())
    return cfg, allowances(cfg.get("allow"))


def test_the_first_allowance_starts_the_list(tmp_path):
    (tmp_path / "project.yml").write_text(PROJECT)
    assert add_allowance(tmp_path, "drop_table", "shop.Old") == "allow:\n  - drop_table: shop.old\n"
    cfg, allow = allowed(tmp_path)
    assert allow == {("drop_table", "shop.old")} and cfg["schemas"] == ["shop"]
    assert (tmp_path / "project.yml").read_text().startswith(PROJECT)


def test_an_allowance_joins_the_list_where_it_is(tmp_path):
    (tmp_path / "project.yml").write_text(
        "allow:\n    # dropped on purpose\n    - drop_view: shop.v\n\n" + PROJECT)
    add_allowance(tmp_path, "drop_column", "shop.orders.code")
    text = (tmp_path / "project.yml").read_text()
    assert text.startswith("allow:\n    # dropped on purpose\n    - drop_view: shop.v\n"
                           "    - drop_column: shop.orders.code\n\n# The sample project.")
    assert allowed(tmp_path)[1] == {("drop_view", "shop.v"), ("drop_column", "shop.orders.code")}


@pytest.mark.parametrize("kind, name, message", [
    ("drop_schema", "shop", "isn't one of"),
    ("drop_column", "shop.orders", "3-part name"),
    ("drop_view", "shop.v", "already allows"),
])
def test_a_wrong_or_repeated_allowance_is_refused(tmp_path, kind, name, message):
    (tmp_path / "project.yml").write_text(PROJECT + "allow:\n  - drop_view: shop.v\n")
    with pytest.raises(ProjectError, match=message):
        add_allowance(tmp_path, kind, name)


def test_a_flow_list_is_left_for_a_person(tmp_path):
    text = PROJECT + "allow: [drop_view: shop.v]\n"
    (tmp_path / "project.yml").write_text(text)
    with pytest.raises(ProjectError, match="add this by hand"):
        add_allowance(tmp_path, "drop_table", "shop.t")
    assert (tmp_path / "project.yml").read_text() == text
