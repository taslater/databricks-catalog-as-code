"""reader.py, offline: which schemas a catalog's read covers."""

from types import SimpleNamespace

from ucmodel import reader
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
