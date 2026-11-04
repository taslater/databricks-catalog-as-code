"""Four schemas' raw REST state, saved so that tests need no workspace.

- `roundtrip` is `ucstate_test.roundtrip`, as `roundtrip.sql` builds it. Each
  statement there is one feature the model has to carry.
- `probe_lineage` is `workspace.probe_lineage`, from the repo's
  `probes/lineage_workload.sql`. A test turns one of its tables into an
  object the model doesn't support.
- `shop` is `ucstate_prod.shop`, as the repo's `catalog/` deploys it, and
  `shop.sql` is that source. It has a view whose column types the platform
  derived, so it tests the compiler.
- `kinds` is `ucstate_test.kinds`, as `kinds.sql` builds it: a materialized
  view, a streaming table and a metric view over one table, kinds the model
  keeps outside what ucstate manages, and a view over the materialized view.

`raw` loads a sample and `model` normalises it. `build` runs `roundtrip.sql`
on the warehouse, as the live round trip does.

`scrub` is what made the samples safe to save: storage paths, metastore and
access-configuration ids are dropped, a cloud storage URL anywhere becomes
`s3://storage` (or its own scheme's), and email addresses become
owner@example.com. So is a function's `properties`, which is the session's
SQL configuration as a JSON string, about 15 KB. The normaliser ignores all
of them. `anonymise` is `scrub` and then every UUID (object ids, the ids of
principals, the metastore's name) replaced by a placeholder, the same one
wherever it recurs in what it is given. Save a read with `anonymise`. To save
a sample again, after its workload changes:

    uv run python -m ucmodel.samples --profile my-profile --catalog ucstate_test \
        --schema roundtrip > packages/ucmodel/src/ucmodel/samples/roundtrip.raw.json
"""

import json
import re
from pathlib import Path
from typing import Any

from ..normalise import normalise
from ..sqltext import split_statements
from ..workspace import Workspace

HERE = Path(__file__).parent
DROP = {
    "browse_only", "cache_version_info", "catalog_id", "data_access_configuration_id",
    "delta_runtime_properties_kvpairs", "effective_auto_maintenance_flag",
    "effective_predictive_optimization_flag", "enable_auto_maintenance",
    "enable_predictive_optimization", "encryption_details", "etag", "metastore_id",
    "metastore_version", "properties_pairs", "related_entity_hints", "securable_kind",
    "securable_type", "storage_location", "storage_mode", "storage_root", "storage_routing",
    "generation", "data_source_format_version", "metastore_name", "workspace_id",
    "workspace_url",
}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# a cloud storage path, as a pipeline's properties name its backing table's
STORAGE = re.compile(r"\b(s3a?|s3n|abfss?|wasbs?|gs|r2)://[^\s'\"`]+")
# a UUID, with dashes or, as in a pipeline's backing table names, underscores
UUID = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}([-_])[0-9a-f]{4}(?:\1[0-9a-f]{4}){2}\1[0-9a-f]{12}"
                  r"(?![0-9a-f])", re.I)


def raw(name: str) -> dict:
    return json.loads((HERE / f"{name}.raw.json").read_text())


def model(name: str) -> dict[str, dict]:
    return normalise(raw(name))


def build(ws: Workspace, name: str = "roundtrip") -> None:
    """Run `<name>.sql` on the warehouse, one statement at a time."""
    for statement in split_statements((HERE / f"{name}.sql").read_text()):
        ws.sql(statement)


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()
                if k not in DROP and not (k == "properties" and isinstance(v, str))}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        value = STORAGE.sub(lambda m: f"{m[1]}://storage", value)
        return value if value.endswith("@example.com") else EMAIL.sub("owner@example.com", value)
    return value


def anonymise(data: Any) -> Any:
    """`scrub`, then each UUID becomes `00000000-0000-0000-0000-000000000001`, `...2` and
    so on, in the order they first appear. The same UUID gets the same placeholder
    throughout `data`, so what refers to what is kept."""
    seen: dict[str, str] = {}

    def swap(match: re.Match) -> str:
        placeholder = seen.setdefault(match[0].lower().replace("_", "-"),
                                      f"00000000-0000-0000-0000-{len(seen) + 1:012d}")
        return placeholder.replace("-", match[1])

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {UUID.sub(swap, k): walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, str):
            return UUID.sub(swap, value)
        return value

    return walk(scrub(data))
