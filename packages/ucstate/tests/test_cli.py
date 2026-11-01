"""cli.py, offline: what `ucstate` says when it can't connect."""

import json
import os
from pathlib import Path

import pytest

from ucmodel import samples
from ucmodel.model import Difference
from ucmodel.snapshot import load
from ucmodel.workspace import Workspace
from ucstate import cli, shadow
from ucstate.cli import main

PROJECT = str(Path(__file__).parent.parent / "examples" / "roundtrip")


@pytest.fixture
def no_config(monkeypatch, tmp_path):
    for name in [n for n in os.environ if n.startswith("DATABRICKS_")]:
        monkeypatch.delenv(name)
    config = tmp_path / "databrickscfg"
    config.write_text("[someone]\nhost = https://example.cloud.databricks.com\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))


def test_no_credentials_is_a_short_error_not_a_traceback(no_config, capsys):
    assert main(["--project", PROJECT, "history", "--target", "test"]) == 2
    assert capsys.readouterr().err.startswith("ucstate: no Databricks credentials: pass --profile")


def test_an_unknown_profile_is_named(no_config, capsys):
    assert main(["--profile", "nope", "--project", PROJECT, "history", "--target", "test"]) == 2
    assert capsys.readouterr().err.startswith("ucstate: profile nope: ")


# The warehouse ------------------------------------------------------------------------------


@pytest.mark.parametrize("option, variable, project_yml, expected", [
    ("w-option", "w-env", "w-project", "w-option"),
    (None, "w-env", "w-project", "w-env"),
    (None, None, "w-project", "w-project"),
    (None, None, None, None),
])
def test_the_warehouse_is_the_option_then_the_variable_then_project_yml(
        monkeypatch, tmp_path, option, variable, project_yml, expected):
    seen = []
    monkeypatch.setattr(Workspace, "connect",
                        classmethod(lambda cls, profile, warehouse=None: seen.append(warehouse)))
    monkeypatch.setitem(cli.COMMANDS, "history", lambda *a: 0)
    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID", raising=False)
    if variable:
        monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", variable)
    (tmp_path / "project.yml").write_text(
        (f"warehouse_id: {project_yml}\n" if project_yml else "")
        + "schemas: [s]\ntargets:\n  dev: {catalog: ucstate_dev}\n")
    argv = ["--project", str(tmp_path), "history", "--target", "dev"]
    assert main(["--warehouse", option, *argv] if option else argv) == 0
    assert seen == [expected]


# The desired model's source ----------------------------------------------------------------


@pytest.fixture
def connected(monkeypatch):
    """A workspace that fails on any SQL, a live model equal to the sample, and the shadow
    builds and deploys asked for."""
    class NoSql(Workspace):
        def __init__(self):
            super().__init__(None)  # type: ignore[arg-type]

        def sql(self, *a, **kw):
            raise AssertionError("ran SQL")

    seen: dict = {"built": 0, "deploy": None}

    def build(ws, project, log=print):
        seen["built"] += 1
        return samples.model("roundtrip")

    def deploy(ws, project, target, **kw):
        seen["deploy"] = kw

    monkeypatch.setattr(Workspace, "connect", classmethod(lambda cls, *a: NoSql()))
    monkeypatch.setattr(cli, "read_live", lambda *a: samples.model("roundtrip"))
    monkeypatch.setattr(cli, "deploy", deploy)
    monkeypatch.setattr(shadow, "build", build)
    return seen


def test_compare_compiles_the_source_without_building_a_shadow(connected, capsys):
    assert main(["--project", PROJECT, "compare", "--target", "test"]) == 0
    out = capsys.readouterr().out
    assert "desired: compiled 11 objects; no shadow built" in out
    assert "the source and ucstate_test agree on 11 objects" in out
    assert connected["built"] == 0


def test_the_shadow_flag_builds_the_shadow_instead(connected, capsys):
    assert main(["--project", PROJECT, "--shadow", "compare", "--target", "test"]) == 0
    assert "desired: building the shadow, as asked" in capsys.readouterr().out
    assert connected["built"] == 1


def test_apply_passes_the_shadow_flag_to_deploy(connected):
    assert main(["--project", PROJECT, "apply", "--target", "test", "--yes"]) == 0
    assert connected["deploy"]["shadow_only"] is False
    assert main(["--project", PROJECT, "--shadow", "apply", "--target", "test", "--yes"]) == 0
    assert connected["deploy"]["shadow_only"] is True


def test_a_compiler_that_disagrees_with_the_shadow_is_an_error_not_a_traceback(
        connected, monkeypatch, capsys):
    def deploy(ws, project, target, **kw):
        raise shadow.CompilerDisagrees([Difference("table:s.t", "comment", "a", "b")])

    monkeypatch.setattr(cli, "deploy", deploy)
    assert main(["--project", PROJECT, "apply", "--target", "test", "--yes"]) == 1
    assert capsys.readouterr().err.startswith("ucstate: the compiler and the shadow disagree on 1 ")


# Against a snapshot, with no workspace ------------------------------------------------------


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """A copy of the example project, and a snapshot of the sample it was imported from.
    Connecting fails the test."""
    import shutil
    from datetime import UTC, datetime

    from ucmodel.snapshot import make, write

    def connect(cls, *a):
        raise AssertionError("connected to a workspace")

    monkeypatch.setattr(Workspace, "connect", classmethod(connect))
    project = tmp_path / "project"
    shutil.copytree(PROJECT, project)
    snap = write(make("ucstate_test", ["roundtrip"], datetime(2026, 11, 1, tzinfo=UTC),
                      samples.model("roundtrip")), tmp_path)
    return project, snap


def test_a_plan_against_a_snapshot_needs_no_workspace(offline, capsys, tmp_path):
    project, snap = offline
    assert main(["--project", str(project), "plan", "--against", str(snap)]) == 0
    assert capsys.readouterr().out.startswith("No changes: ucstate_test matches the source.")
    assert main(["--project", str(project), "compare", "--target", "test",
                 "--against", str(snap)]) == 0


def test_a_blocked_change_lists_its_options_and_a_choice_unblocks_it(offline, capsys, tmp_path):
    project, snap = offline
    orders = project / "src" / "roundtrip" / "tables" / "orders.sql"
    orders.write_text(orders.read_text().replace("status STRING", "state STRING"))
    argv = ["--project", str(project), "plan", "--against", str(snap)]
    assert main([*argv, "-o", str(tmp_path / "plan.json")]) == 1
    out = capsys.readouterr().out
    assert "Planned against the snapshot" in out
    assert "ucstate rename-column roundtrip.orders status state  (it's a rename)" in out
    assert "ucstate allow drop_column roundtrip.orders.status" in out
    saved = json.loads((tmp_path / "plan.json").read_text())
    assert saved["against"] == load(snap).hash
    assert [o["command"][0] for o in saved["changes"][0]["options"]] == [
        "rename-column", "allow"]

    assert main(["--project", str(project), "rename-column", "roundtrip.orders", "status",
                 "state"]) == 0
    assert "added to refactor.yml:" in capsys.readouterr().out
    main(argv)
    out = capsys.readouterr().out
    assert "rename column status to state" in out and "ambiguous" not in out


def test_apply_refuses_a_plan_made_against_a_snapshot(offline, monkeypatch, capsys, tmp_path):
    project, snap = offline
    orders = project / "src" / "roundtrip" / "tables" / "orders.sql"
    orders.write_text(orders.read_text().replace("COMMENT 'One row per order'", ""))
    main(["--project", str(project), "plan", "--against", str(snap), "-o",
          str(tmp_path / "plan.json")])
    monkeypatch.setattr(Workspace, "connect", classmethod(lambda cls, *a: None))
    assert main(["--project", str(project), "apply", "--target", "test",
                 "--plan", str(tmp_path / "plan.json")]) == 1
    assert "was planned against a snapshot, as a preview" in capsys.readouterr().err


def test_a_transition_drafts_from_a_snapshot(offline, capsys):
    project, snap = offline
    orders = project / "src" / "roundtrip" / "tables" / "orders.sql"
    text = orders.read_text()
    orders.write_text(text.replace("order_id BIGINT", "order_id INT"))
    assert main(["--project", str(project), "plan", "--against", str(snap)]) == 1
    assert (f"ucstate transition --object table:roundtrip.orders --against {snap}"
            in capsys.readouterr().out)
    assert main(["--project", str(project), "transition", "--object", "roundtrip.orders",
                 "--against", str(snap), "--id", "narrow"]) == 0
    assert (project / "migrations" / "narrow.sql").read_text().startswith(
        "-- ucstate: transition\n-- id: narrow\n")


@pytest.mark.parametrize("extra, message", [
    (["--target", "nope"], "no target 'nope'"),
    (["--target", "test", "--against", "OTHER"], "is of ucstate_other, but target test is "
                                                 "ucstate_test"),
    ([], "pass --target, or --against with a snapshot"),
])
def test_a_snapshot_that_doesnt_fit_is_named(offline, capsys, tmp_path, extra, message):
    from datetime import UTC, datetime

    from ucmodel.snapshot import make, write
    project, _ = offline
    other = write(make("ucstate_other", ["roundtrip"], datetime(2026, 11, 1, tzinfo=UTC),
                       samples.model("roundtrip")), tmp_path / "other.json")
    extra = [str(other) if x == "OTHER" else x for x in extra]
    if extra and extra[0] == "--target" and "--against" not in extra:
        extra += ["--against", str(other)]
    assert main(["--project", str(project), "plan", *extra]) == 2
    assert message in capsys.readouterr().err


def test_a_snapshot_without_the_projects_schemas_is_refused(offline, capsys, tmp_path):
    from datetime import UTC, datetime

    from ucmodel.snapshot import make, write
    project, _ = offline
    shop = write(make("ucstate_test", ["shop"], datetime(2026, 11, 1, tzinfo=UTC),
                      samples.model("shop")), tmp_path / "shop.json")
    assert main(["--project", str(project), "plan", "--against", str(shop)]) == 2
    assert "didn't read schema roundtrip" in capsys.readouterr().err


def test_the_shadow_flag_needs_a_workspace(offline, capsys):
    project, snap = offline
    assert main(["--project", str(project), "--shadow", "plan", "--against", str(snap)]) == 2
    assert "--shadow builds in a workspace" in capsys.readouterr().err


def test_the_choice_commands_need_no_workspace(offline, capsys):
    project, _ = offline
    assert main(["--project", str(project), "allow", "drop_table", "roundtrip.old"]) == 0
    assert main(["--project", str(project), "rename-table", "roundtrip.a", "roundtrip.b",
                 "--id", "a-to-b"]) == 0
    assert main(["--project", str(project), "rename-table", "a", "roundtrip.b"]) == 2
    assert "a table is a two-part name" in capsys.readouterr().err
    assert "drop_table: roundtrip.old" in (project / "project.yml").read_text()
    assert "a-to-b" in (project / "refactor.yml").read_text()
