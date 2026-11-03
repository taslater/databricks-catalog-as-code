"""cli.py, offline: what `ucatlas` says when it can't connect or can't find what to draw."""

import os
import shutil
from types import SimpleNamespace

import pytest

import demo
from ucatlas import cli
from ucmodel.workspace import Workspace


@pytest.fixture
def no_config(monkeypatch, tmp_path):
    for name in [n for n in os.environ if n.startswith("DATABRICKS_")]:
        monkeypatch.delenv(name)
    config = tmp_path / "databrickscfg"
    config.write_text("[someone]\nhost = https://example.cloud.databricks.com\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))


class FakeWorkspace(Workspace):
    """Has the catalog `ucstate_prod`, with one schema, `shop`, and nothing in it."""

    def __init__(self):
        super().__init__(SimpleNamespace())  # type: ignore[arg-type]

    def catalog(self, name):
        return {"name": name} if name == "ucstate_prod" else None

    def schema(self, full_name):
        return None


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(Workspace, "connect", classmethod(lambda cls, profile: FakeWorkspace()))


def test_no_credentials_is_a_short_error_not_a_traceback(no_config, capsys):
    assert cli.main(["draw", "--catalog", "ucstate_prod"]) == 2
    assert capsys.readouterr().err.startswith("ucatlas: no Databricks credentials: pass --profile")


def test_a_catalog_that_isnt_there_is_named(fake, capsys, tmp_path):
    assert cli.main(["draw", "--catalog", "nope", "-o", str(tmp_path)]) == 2
    assert capsys.readouterr().err == "ucatlas: catalog nope not found\n"


def test_a_schema_that_isnt_there_is_named(fake, capsys, tmp_path):
    args = ["draw", "--catalog", "ucstate_prod", "--schema", "nope", "-o", str(tmp_path)]
    assert cli.main(args) == 2
    assert capsys.readouterr().err == "ucatlas: schema nope not found in ucstate_prod\n"
    assert not any(tmp_path.iterdir())


# From a snapshot ----------------------------------------------------------------------------


def snapshot_file(tmp_path):
    from datetime import UTC, datetime

    from ucmodel import samples
    from ucmodel.snapshot import make, write
    model = {**samples.model("shop"), **samples.model("roundtrip")}
    return write(make("ucstate_dev", ["roundtrip", "shop"], datetime(2026, 11, 1, tzinfo=UTC),
                      model), tmp_path)


def test_a_snapshot_draws_with_no_credentials(no_config, capsys, tmp_path):
    if shutil.which("dot") is None:
        pytest.skip("needs Graphviz")
    path = snapshot_file(tmp_path)
    out = tmp_path / "site"
    assert cli.main(["draw", "--snapshot", str(path), "--schema", "shop", "-o", str(out)]) == 0
    assert sorted(p.name for p in out.glob("*.html")) == ["index.html", "shop.html"]
    assert "Read from ucstate_dev at 2026-11-01 00:00 UTC." in (out / "index.html").read_text()


def test_a_schema_the_snapshot_didnt_read_is_named(no_config, capsys, tmp_path):
    path = snapshot_file(tmp_path)
    assert cli.main(["draw", "--snapshot", str(path), "--schema", "nope",
                     "-o", str(tmp_path / "site")]) == 2
    assert capsys.readouterr().err == f"ucatlas: schema nope not in the snapshot {path}\n"


def test_an_edited_snapshot_isnt_drawn(no_config, capsys, tmp_path):
    path = snapshot_file(tmp_path)
    path.write_text(path.read_text().replace('"shop"', '"shops"', 1))
    assert cli.main(["draw", "--snapshot", str(path), "-o", str(tmp_path / "site")]) == 2
    assert "changed after it was taken" in capsys.readouterr().err
    assert not (tmp_path / "site").exists()


def test_a_catalog_and_a_snapshot_together_are_refused(capsys, tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["draw", "--catalog", "c", "--snapshot", str(tmp_path / "s.json")])
    assert "not allowed with argument" in capsys.readouterr().err


# explore ------------------------------------------------------------------------------------


def demo_snapshot(tmp_path, with_lineage=True):
    from ucmodel.snapshot import make, write

    s = demo.take()
    return write(make(s.catalog, s.schemas, s.taken_at, s.model,
                      s.lineage if with_lineage else None), tmp_path)


JOBS = str(demo.HERE / "jobs")


def test_explore_writes_the_page_and_says_what_it_couldnt_read(no_config, capsys, tmp_path):
    if shutil.which("dot") is None:
        pytest.skip("needs Graphviz")
    out = tmp_path / "explorer"
    args = ["explore", "--snapshot", str(demo_snapshot(tmp_path)), "--sql", JOBS,
            "-o", str(out), "--tries", "2"]
    assert cli.main(args) == 0
    said = capsys.readouterr().out
    assert said.startswith(f"wrote {out / 'index.html'}: 22 tables, 4 views, ")
    assert "; 1 statement with lineage gaps." in said
    assert f"--directory {out} 8000" in said
    assert "has no lineage" not in said
    assert '"catalog":"atlas_demo"' in (out / "index.html").read_text()


def test_explore_without_lineage_says_edges_come_from_sql_only(no_config, capsys, tmp_path,
                                                              monkeypatch):
    from ucatlas import layout

    monkeypatch.setattr(layout, "build", lambda nodes, edges, tries=None: {})
    path = demo_snapshot(tmp_path, with_lineage=False)
    assert cli.main(["explore", "--snapshot", str(path), "-o", str(tmp_path / "x")]) == 0
    said = capsys.readouterr().out
    assert "The snapshot has no lineage, so edges come from SQL only" in said
    assert "lineage gaps" not in said  # no --sql: only the views' definitions are read


def test_explore_names_a_sql_path_that_isnt_there(no_config, capsys, tmp_path):
    path = demo_snapshot(tmp_path)
    args = ["explore", "--snapshot", str(path), "--sql", str(tmp_path / "nope"),
            "-o", str(tmp_path / "x")]
    assert cli.main(args) == 2
    assert capsys.readouterr().err == f"ucatlas: no such file or folder: {tmp_path / 'nope'}\n"
    assert not (tmp_path / "x").exists()


def test_explore_refuses_an_edited_snapshot(no_config, capsys, tmp_path):
    path = demo_snapshot(tmp_path)
    path.write_text(path.read_text().replace('"core"', '"kore"', 1))
    assert cli.main(["explore", "--snapshot", str(path), "-o", str(tmp_path / "x")]) == 2
    assert "changed after it was taken" in capsys.readouterr().err
    assert not (tmp_path / "x").exists()


def test_explore_without_graphviz_is_a_short_error(no_config, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    path = demo_snapshot(tmp_path)
    assert cli.main(["explore", "--snapshot", str(path), "-o", str(tmp_path / "x")]) == 2
    assert capsys.readouterr().err.startswith("ucatlas: Graphviz isn't installed")
