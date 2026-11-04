"""reader.py, offline: which schemas a catalog's read covers, and what a read returns."""

from types import SimpleNamespace

import pytest

from saved import SavedWorkspace
from ucmodel import reader, samples
from ucmodel.progress import Progress
from ucmodel.workspace import UC, Workspace


class FakeWorkspace(Workspace):
    def __init__(self, schemas: list[str]):
        super().__init__(SimpleNamespace())  # type: ignore[arg-type]
        self.schemas = schemas
        self.calls: list[tuple[str, dict]] = []

    def list_all(self, path, key, **query):
        self.calls.append((path, query))
        return [{"name": s} for s in self.schemas]


def test_a_catalogs_schemas_are_all_but_information_schema_in_order():
    ws = FakeWorkspace(["shop", "information_schema", "_ucstate", "analytics"])
    assert reader.schemas(ws, "ucstate_prod") == ["_ucstate", "analytics", "shop"]
    assert ws.calls == [(f"{UC}/schemas", {"catalog_name": "ucstate_prod"})]


@pytest.mark.parametrize("name", ["shop", "roundtrip", "probe_lineage"])
def test_a_read_returns_what_the_workspace_answered(name):
    raw = samples.raw(name)
    ws = SavedWorkspace(raw)
    assert reader.read(ws, raw["catalog"], list(raw["schemas"])) == raw


def test_a_read_of_two_schemas_keeps_each_ones_objects_apart():
    shop, roundtrip = samples.raw("shop"), samples.raw("roundtrip")
    both = {"catalog": "c", "schemas": {**shop["schemas"], **roundtrip["schemas"]}}
    assert reader.read(SavedWorkspace(both), "c", ["roundtrip", "shop"]) == both


def test_a_schema_that_isnt_there_reads_as_none():
    raw = samples.raw("shop")
    assert reader.read(SavedWorkspace(raw), raw["catalog"], ["shop", "nope"])["schemas"] == {
        "shop": raw["schemas"]["shop"], "nope": None}


def test_a_read_counts_each_object_as_it_finishes(tmp_path):
    raw = samples.raw("roundtrip")
    log = tmp_path / "log"
    with log.open("w") as stream:
        reader.read(SavedWorkspace(raw), raw["catalog"], ["roundtrip"],
                    progress=Progress(stream, live=False))
    s = raw["schemas"]["roundtrip"]
    n = len(s["tables"]) + len(s["functions"]) + len(s["volumes"])
    lines = log.read_text().splitlines()
    assert lines[0] == "listing schemas: 1 schema"
    assert lines[1].startswith("listing schemas: 1 done in ")
    assert lines[2] == f"reading tables, views, functions and volumes: {n} objects"
    assert lines[3].startswith(f"reading tables, views, functions and volumes: {n} done in ")
    assert len(lines) == 4
