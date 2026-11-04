"""cli.py, offline: `ucmodel snapshot` and `ucmodel diff`."""

import copy
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from saved import SavedWorkspace
from ucmodel import cli, samples
from ucmodel.snapshot import load, make, write
from ucmodel.workspace import Workspace


@pytest.fixture
def no_config(monkeypatch, tmp_path):
    for name in [n for n in os.environ if n.startswith("DATABRICKS_")]:
        monkeypatch.delenv(name)
    config = tmp_path / "databrickscfg"
    config.write_text("[someone]\nhost = https://example.cloud.databricks.com\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))


class NoCatalogs(Workspace):
    def __init__(self):
        super().__init__(SimpleNamespace())  # type: ignore[arg-type]

    def catalog(self, name):
        return None


def test_no_credentials_is_a_short_error_not_a_traceback(no_config, capsys):
    assert cli.main(["snapshot", "--catalog", "ucstate_prod"]) == 2
    assert capsys.readouterr().err.startswith("ucmodel: no Databricks credentials: pass --profile")


def test_a_catalog_that_isnt_there_is_named(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(Workspace, "connect", classmethod(lambda cls, profile: NoCatalogs()))
    assert cli.main(["snapshot", "--catalog", "nope", "-o", str(tmp_path)]) == 2
    assert capsys.readouterr().err == "ucmodel: catalog nope not found\n"
    assert not any(tmp_path.iterdir())


def test_snapshot_prints_the_path_and_what_it_holds(monkeypatch, capsys, tmp_path):
    taken = make("ucstate_dev", ["shop"], datetime(2026, 11, 1, tzinfo=UTC), samples.model("shop"))
    monkeypatch.setattr(Workspace, "connect", classmethod(lambda cls, profile: NoCatalogs()))
    monkeypatch.setattr(cli, "take", lambda ws, catalog, schemas, with_lineage, progress: taken)
    assert cli.main(["--profile", "p", "snapshot", "--catalog", "ucstate_dev",
                     "-o", f"{tmp_path}/"]) == 0
    out, err = capsys.readouterr()
    assert load(tmp_path / taken.file_name) == taken
    assert out == f"{tmp_path / taken.file_name}\n"
    assert err.startswith(f"{taken.describe()}: 1 schema, 4 tables, 1 view; no lineage, read in ")


class Shop(SavedWorkspace):
    """`ucstate_dev`, with the schema `shop` as the saved sample has it, and no lineage."""

    def __init__(self):
        super().__init__({**samples.raw("shop"), "catalog": "ucstate_dev"})

    def catalog(self, name):
        return {"name": name}

    def get(self, path, **query):
        return {}


def test_snapshot_writes_each_step_to_stderr_and_only_the_path_to_stdout(monkeypatch, capsys,
                                                                        tmp_path):
    monkeypatch.setattr(Workspace, "connect", classmethod(lambda cls, profile: Shop()))
    assert cli.main(["--profile", "p", "snapshot", "--catalog", "ucstate_dev", "--schema", "shop",
                     "-o", f"{tmp_path}/"]) == 0
    out, err = capsys.readouterr()
    assert out.count("\n") == 1 and out.startswith(str(tmp_path))
    started = [line.split(":")[0] for line in err.splitlines()[:-1] if " done in " not in line]
    assert started == ["listing schemas", "reading tables, views, functions and volumes",
                       "reading table lineage", "reading column lineage"]
    assert err.splitlines()[-1].startswith("ucstate_dev at ")


def test_quiet_writes_only_what_the_snapshot_holds(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(Workspace, "connect", classmethod(lambda cls, profile: Shop()))
    assert cli.main(["--profile", "p", "--quiet", "snapshot", "--catalog", "ucstate_dev",
                     "--schema", "shop", "--no-lineage", "-o", f"{tmp_path}/"]) == 0
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and err.startswith("ucstate_dev at ")


def test_diff_exits_0_when_nothing_changed_1_when_something_did(tmp_path, capsys):
    model = samples.model("shop")
    edited = copy.deepcopy(model)
    edited["table:shop.orders"]["comment"] = "edited"
    a, b, c = (write(make("ucstate_dev", ["shop"], datetime(2026, 11, day, tzinfo=UTC), m),
                     tmp_path) for day, m in ((1, model), (2, model), (3, edited)))
    assert cli.main(["diff", str(a), str(b)]) == 0
    assert "No differences." in capsys.readouterr().out
    assert cli.main(["diff", str(a), str(c), "--format", "json"]) == 1
    assert list(json.loads(capsys.readouterr().out)["changed"]) == ["table:shop.orders"]
    assert cli.main(["diff", str(a), str(c), "--format", "markdown"]) == 1
    assert "- changed `table:shop.orders`" in capsys.readouterr().out


def test_diff_of_an_edited_snapshot_is_refused(tmp_path, capsys):
    path = write(make("ucstate_dev", ["shop"], datetime(2026, 11, 1, tzinfo=UTC),
                      samples.model("shop")), tmp_path)
    path.write_text(path.read_text().replace("One row per customer", "One row per client"))
    assert cli.main(["diff", str(path), str(path)]) == 2
    assert "changed after it was taken" in capsys.readouterr().err


def test_a_snapshot_names_what_is_outside_the_model_by_its_type(monkeypatch, capsys, tmp_path):
    taken = make("ucstate_test", ["kinds"], datetime(2026, 11, 4, tzinfo=UTC),
                 samples.model("kinds"))
    monkeypatch.setattr(Workspace, "connect", classmethod(lambda cls, profile: NoCatalogs()))
    monkeypatch.setattr(cli, "take", lambda ws, catalog, schemas, with_lineage, progress: taken)
    assert cli.main(["--profile", "p", "snapshot", "--catalog", "ucstate_test",
                     "-o", f"{tmp_path}/"]) == 0
    assert capsys.readouterr().err.startswith(
        f"{taken.describe()}: 1 materialized view, 1 metric view, 1 schema, 1 streaming table, "
        "1 table, 1 view; no lineage, read in ")


@pytest.mark.parametrize(("type_", "said"), [
    ("EXTERNAL DELTA", "external delta table"), ("FOREIGN", "foreign table"),
    ("EXTERNAL VOLUME", "external volume"), ("STREAMING_TABLE", "streaming table")])
def test_an_object_outside_the_model_is_said_by_its_type(type_, said):
    assert cli._said("unsupported:s.x", {"type": type_}) == said
    assert cli._said("table:s.x", {}) == "table"  # the control: a modelled kind is its kind
