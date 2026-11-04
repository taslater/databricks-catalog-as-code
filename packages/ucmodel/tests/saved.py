"""A workspace that answers from the saved samples, for tests of what reads it."""

from types import SimpleNamespace
from urllib.parse import unquote

from ucmodel.workspace import UC, Workspace


class SavedWorkspace(Workspace):
    """Answers each REST call the reader makes from saved raw payloads, as the
    workspace answered them when the samples were taken."""

    def __init__(self, raw: dict):
        super().__init__(SimpleNamespace())  # type: ignore[arg-type]
        self.catalog_name = raw["catalog"]
        self.raw = raw["schemas"]

    def _schema(self, full: str) -> dict | None:
        catalog, schema = full.split(".")
        return self.raw.get(schema) if catalog == self.catalog_name else None

    def _object(self, full: str, group: str) -> dict:
        catalog, schema, name = full.split(".", 2)
        return self.raw[schema][group][name]

    def schema(self, full_name):
        s = self._schema(full_name)
        return s and s["info"]

    def get_or_none(self, path, **query):
        assert path.startswith(f"{UC}/functions/"), path
        return self._object(unquote(path.rsplit("/", 1)[1]), "functions")["info"]

    def list_all(self, path, key, **query):
        s = self.raw[query["schema_name"]]
        if key in ("tables", "volumes"):  # a list holds each one's whole state
            return [o["info"] for o in s[key].values()]
        return [{"name": name} for name in s[key]]

    def tags(self, entity_type, full_name):
        if entity_type == "schemas":
            return self.raw[full_name.split(".")[1]]["tags"]
        if entity_type == "columns":
            table, column = full_name.rsplit(".", 1)
            return self._object(table, "tables")["column_tags"].get(column, {})
        return self._object(full_name, entity_type)["tags"]

    def grants(self, securable_type, full_name):
        if securable_type == "schema":
            return self.raw[full_name.split(".")[1]]["grants"]
        return self._object(full_name, f"{securable_type}s")["grants"]
