"""shadow.py, offline: what the desired model keeps of the shadow's."""

import copy
from pathlib import Path

import pytest

from ucmodel import samples
from ucmodel.compiler import Unsupported
from ucmodel.model import compare
from ucmodel.workspace import Workspace
from ucstate import shadow
from ucstate.project import Project, SourceFile
from ucstate.shadow import declared

TABLE: dict = {
    "kind": "table",
    "name": "s.t",
    "comment": "c",
    "columns": [
        {"name": "a", "type": "int", "nullable": True, "comment": None, "default": None,
         "generated": None, "identity": None, "mask": None, "tags": {}},
        {"name": "b", "type": "string", "nullable": True, "comment": None, "default": None,
         "generated": None, "identity": None, "mask": None, "tags": {}},
    ],
    "constraints": {},
    "partitioning": [],
    "clustering": None,
    "row_filter": None,
    "properties": {"delta.appendOnly": "true"},
    "tags": {"k": "v"},
    "grants": {},
    "meta": {"table_id": "1"},
}


def test_declared_keeps_only_what_the_source_names():
    shadow_model = {"table:s.t": {**copy.deepcopy(TABLE), "clustering": {"auto": True},
                                  "properties": {"delta.appendOnly": "true",
                                                 "delta.enableRowTracking": "true"}}}
    source = SourceFile(path=Path("src/s/tables/t.sql"), key="table:s.t", schema="s", statements=[
        "CREATE TABLE s.t (a INT, b STRING) TBLPROPERTIES ('delta.appendOnly' = 'true')"])
    obj = declared(shadow_model, [source])["table:s.t"]
    assert obj["properties"] == {"delta.appendOnly": "true"}
    assert obj["clustering"] is None
    assert obj["meta"] == {}


# desired -----------------------------------------------------------------------------------

EXAMPLES = Path(__file__).parent.parent / "examples"


class NoSql(Workspace):
    """A workspace that fails the test if anything runs on it."""

    def __init__(self):
        super().__init__(None)  # type: ignore[arg-type]

    def sql(self, statement, catalog=None, schema=None, params=None):
        raise AssertionError(f"ran SQL on {catalog}: {statement}")

    def catalog(self, name):
        raise AssertionError("looked for the shadow catalog")


@pytest.fixture
def project() -> Project:
    return Project.load(EXAMPLES / "roundtrip")


@pytest.fixture
def built(monkeypatch) -> list:
    """Replaces the shadow build with the sample, and records each call."""
    calls: list = []

    def build(ws, project, log=print):
        calls.append(project.shadow_catalog)
        return samples.model("roundtrip")

    monkeypatch.setattr(shadow, "build", build)
    return calls


def test_auto_compiles_and_builds_no_shadow(project, built):
    lines: list[str] = []
    model = shadow.desired(NoSql(), project, log=lines.append)
    assert compare(model, samples.model("roundtrip")) == []
    assert built == []
    assert lines == [f"desired: compiled {len(model)} objects; no shadow built"]


def test_auto_falls_back_to_the_shadow_when_the_compiler_declines(monkeypatch, project, built):
    def decline(statements):
        raise Unsupported("CREATE TABLE ... AS SELECT isn't modelled\n    in: CREATE TABLE s.t")

    monkeypatch.setattr(shadow, "compile_statements", decline)
    lines: list[str] = []
    model = shadow.desired(NoSql(), project, log=lines.append)
    assert model == samples.model("roundtrip")
    assert built == ["ucstate_shadow"]
    assert lines == ["desired: the compiler declined: CREATE TABLE ... AS SELECT isn't modelled; "
                     "building the shadow"]


def test_shadow_mode_never_compiles(monkeypatch, project, built):
    monkeypatch.setattr(shadow, "compile_statements",
                        lambda statements: pytest.fail("the compiler ran"))
    lines: list[str] = []
    assert shadow.desired(NoSql(), project, mode="shadow", log=lines.append)
    assert built == ["ucstate_shadow"]
    assert lines == ["desired: building the shadow, as asked"]


def test_checked_returns_the_shadow_when_the_compiler_agrees(project, built):
    lines: list[str] = []
    model = shadow.desired(NoSql(), project, mode="checked", log=lines.append)
    assert built == ["ucstate_shadow"]
    assert lines == [f"desired: the compiler and the shadow agree on {len(model)} objects"]


def test_checked_stops_with_the_differences_when_they_disagree(monkeypatch, project, built):
    """The known-bad control: the compiler is made to differ from the shadow."""
    compiled = samples.model("roundtrip")
    compiled["table:roundtrip.customers"]["comment"] = "not the shadow's"
    monkeypatch.setattr(shadow, "compile_statements", lambda statements: compiled)
    with pytest.raises(shadow.CompilerDisagrees) as e:
        shadow.desired(NoSql(), project, mode="checked", log=lambda m: None)
    assert [(d.key, d.path) for d in e.value.diffs] == [("table:roundtrip.customers", "comment")]
    assert "1 difference, so nothing was run" in str(e.value)
    assert "table:roundtrip.customers comment" in str(e.value)


def test_checked_uses_the_shadow_alone_when_the_compiler_declines(monkeypatch, project, built):
    def decline(statements):
        raise Unsupported("a thing\n    in: CREATE ...")

    monkeypatch.setattr(shadow, "compile_statements", decline)
    lines: list[str] = []
    assert shadow.desired(NoSql(), project, mode="checked", log=lines.append)
    assert lines == ["desired: the compiler declined: a thing; using the shadow alone"]
