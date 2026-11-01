"""The renderer: what `ucstate import` writes, on the payloads in `ucmodel.samples`."""

import copy
from pathlib import Path

import pytest

from ucmodel import samples
from ucmodel.normalise import normalise
from ucstate.render import render

EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.fixture(scope="module")
def roundtrip() -> dict:
    return samples.model("roundtrip")


@pytest.mark.parametrize("name", ["probe_lineage", "roundtrip"])
def test_render_reproduces_the_committed_example(name):
    """examples/<name>/src is exactly what `ucstate import` writes for the sample."""
    files = render(samples.model(name))
    example = EXAMPLES / name
    committed = {str(p.relative_to(example)): p.read_text()
                 for p in sorted((example / "src").rglob("*.sql"))}
    assert files == committed


def test_render_leaves_out_unsupported_objects():
    raw = samples.raw("probe_lineage")
    t = raw["schemas"]["probe_lineage"]["tables"]["customers"]
    t["info"] = {**t["info"], "table_type": "MATERIALIZED_VIEW"}
    assert "probe_lineage/tables/customers.sql" not in " ".join(render(normalise(raw)))


def test_render_puts_self_references_and_view_column_tags_where_they_build(roundtrip):
    """An inline FK to the table itself fails, and ALTER VIEW has no ALTER COLUMN
    (probes/phase2.py, r4 and r8)."""
    model = copy.deepcopy(roundtrip)
    customers = model["table:roundtrip.customers"]
    customers["columns"].append({**customers["columns"][3], "name": "referrer_id",
                                 "type": "bigint"})
    customers["constraints"]["customers_referrer_fk"] = {
        "type": "foreign_key", "columns": ["referrer_id"], "references": "roundtrip.customers",
        "parent_columns": ["customer_id"]}
    model["view:roundtrip.customer_orders"]["columns"][0]["tags"] = {"pii": "no"}
    files = render(model)
    table = files["src/roundtrip/tables/customers.sql"]
    assert "CONSTRAINT customers_referrer_fk" not in table.split(";")[0]
    assert ("ALTER TABLE roundtrip.customers ADD CONSTRAINT customers_referrer_fk "
            "FOREIGN KEY (referrer_id)\n    REFERENCES roundtrip.customers (customer_id);") in table
    assert ("ALTER TABLE roundtrip.customer_orders ALTER COLUMN customer_id "
            "SET TAGS ('pii' = 'no');") in files["src/roundtrip/views/customer_orders.sql"]
