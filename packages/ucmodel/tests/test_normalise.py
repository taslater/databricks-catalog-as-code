"""The normaliser, on the REST payloads in `ucmodel.samples`.

The round-trip sample is built by `samples/roundtrip.sql`, so every
assertion here traces to a statement in that file.
"""

import json

import pytest

from ucmodel import samples
from ucmodel.normalise import normalise


@pytest.fixture(scope="module")
def roundtrip() -> dict:
    return samples.model("roundtrip")


def test_catalog_is_stripped_everywhere(roundtrip):
    text = json.dumps(roundtrip)
    assert "ucstate_test" not in text
    orders = roundtrip["table:roundtrip.orders"]
    assert orders["constraints"]["orders_customers_fk"]["references"] == "roundtrip.customers"
    assert "FROM roundtrip.orders" in roundtrip["function:roundtrip.big_orders"]["body"]
    view = roundtrip["view:roundtrip.customer_orders"]
    assert "FROM roundtrip.customers AS c" in view["definition"]


def test_column_features(roundtrip):
    customers = {c["name"]: c for c in roundtrip["table:roundtrip.customers"]["columns"]}
    assert customers["customer_id"]["identity"] == {"start": 1, "step": 1, "always": True}
    assert customers["region"]["default"] == "'EU'"
    assert customers["email"]["mask"] == {"function": "roundtrip.mask_email", "using": []}
    assert customers["email"]["tags"] == {"pii": "email"}
    assert "name" in customers and "full_name" not in customers  # renamed under column mapping
    orders = {c["name"]: c for c in roundtrip["table:roundtrip.orders"]["columns"]}
    assert orders["amount_doubled"]["generated"] == "amount * 2"
    assert orders["quantity"]["type"] == "bigint"  # widened from int
    assert orders["status"]["default"] == "'new'"
    assert list(orders)[:2] == ["order_id", "order_date"]  # moved AFTER order_id


def test_platform_metadata_is_dropped(roundtrip):
    text = json.dumps(roundtrip)
    for noise in ("delta.columnMapping.physicalName", "delta.typeChanges",
                  "delta.identity.highWaterMark", "delta.constraints.", "spark.sql.statistics",
                  "view.sqlConfig", "clusterByAuto", "delta.lastCommitTimestamp"):
        assert noise not in text, noise


def test_table_features(roundtrip):
    customers = roundtrip["table:roundtrip.customers"]
    assert customers["row_filter"] == {"function": "roundtrip.visible_region",
                                       "columns": ["region"]}
    assert customers["tags"] == {"domain": "sales", "reviewed": ""}  # a key-only tag reads as ''
    assert customers["grants"] == {"account users": ["SELECT"]}
    orders = roundtrip["table:roundtrip.orders"]
    assert orders["constraints"]["amount_positive"] == {"type": "check", "expression": "amount > 0"}
    assert orders["clustering"] == {"columns": ["order_date", "customer_id"]}
    lines = roundtrip["table:roundtrip.order_lines"]
    assert lines["partitioning"] == ["event_date"]
    assert lines["columns"][0]["comment"] == "O'Brien's order, with a \\ backslash"


def test_view_function_volume_and_schema(roundtrip):
    view = roundtrip["view:roundtrip.customer_orders"]
    assert view["schema_mode"] == "BINDING"
    assert view["columns"][0] == {"name": "customer_id", "type": "bigint",
                                  "comment": "Customer key", "tags": {}}
    tax = roundtrip["function:roundtrip.add_tax"]
    assert tax["parameters"][1] == {"name": "rate", "type": "double", "default": "0.2",
                                    "comment": "Tax rate as a fraction"}
    assert roundtrip["function:roundtrip.slugify"]["language"] == "PYTHON"
    assert roundtrip["function:roundtrip.big_orders"]["returns"] == (
        "TABLE(order_id bigint, amount decimal(10,2))")
    assert roundtrip["volume:roundtrip.landing"]["grants"] == {"account users": ["READ_VOLUME"]}
    assert roundtrip["schema:roundtrip"]["tags"] == {"purpose": "test"}


def test_unsupported_objects_are_kept_but_marked():
    raw = samples.raw("probe_lineage")
    t = raw["schemas"]["probe_lineage"]["tables"]["customers"]
    t["info"] = {**t["info"], "table_type": "MATERIALIZED_VIEW"}
    obj = normalise(raw)["unsupported:probe_lineage.customers"]
    assert obj["type"].startswith("MATERIALIZED_VIEW")
