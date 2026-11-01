"""snapshot.py and report.py, offline: snapshot files, and reports between two of them."""

import copy
import json
from datetime import UTC, datetime

import pytest

from ucmodel import report, samples
from ucmodel.lineage import Edge, Lineage
from ucmodel.snapshot import SnapshotError, load, make, within, write

THEN = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 1, 12, 0, 30, 123456, tzinfo=UTC)
LINEAGE = Lineage([Edge("shop.orders", "shop.order_totals", "2026-10-31 04:19:00.0")], [])


def shop(when=THEN, model=None, found=None):
    return make("ucstate_dev", ["shop"], when, model or samples.model("shop"), found)


def test_a_snapshot_round_trips_through_its_file(tmp_path):
    s = shop(NOW, found=LINEAGE)
    path = write(s, tmp_path)
    assert path.name == f"ucstate_dev-20261101T120030Z-{s.hash[:12]}.json"
    again = load(path)
    assert again == s
    assert again.taken_at == NOW.replace(microsecond=0)
    assert again.lineage == LINEAGE


def test_the_hash_is_of_the_contents(tmp_path):
    assert shop().hash == shop().hash
    assert shop().hash != shop(NOW).hash
    assert shop().hash != shop(found=LINEAGE).hash


def test_an_edited_snapshot_is_refused(tmp_path):
    """The control: a file changed after it was taken must not load."""
    path = write(shop(), tmp_path)
    data = json.loads(path.read_text())
    data["model"]["table:shop.orders"]["comment"] = "edited"
    path.write_text(json.dumps(data))
    with pytest.raises(SnapshotError, match="changed after it was taken"):
        load(path)


@pytest.mark.parametrize("text, message", [
    ("not json", "not a snapshot"),
    ('{"format": "something/9"}', "format 'something/9'"),
    ("[]", "format None"),
])
def test_a_file_that_isnt_a_snapshot_is_refused(tmp_path, text, message):
    path = tmp_path / "x.json"
    path.write_text(text)
    with pytest.raises(SnapshotError, match=message):
        load(path)


def test_a_snapshot_is_never_replaced(tmp_path):
    s = shop()
    write(s, tmp_path / "named.json")
    with pytest.raises(SnapshotError, match="never replaced"):
        write(shop(NOW), tmp_path / "named.json")
    write(s, f"{tmp_path / 'new'}/")  # a folder that doesn't exist yet, by the trailing slash
    assert [p.name for p in (tmp_path / "new").iterdir()] == [s.file_name]


def test_within_keeps_the_schemas_asked_for():
    model = {**samples.model("shop"), **samples.model("roundtrip")}
    assert set(within(model, ["shop"])) == set(samples.model("shop"))


# Reports ------------------------------------------------------------------------------------


def changed_by_hand():
    model = copy.deepcopy(samples.model("shop"))
    model["table:shop.customers"]["comment"] = "Changed by hand"
    model["table:shop.customers"]["properties"]["team"] = "data"
    del model["view:shop.order_totals"]
    model["table:shop.returns"] = copy.deepcopy(model["table:shop.products"])
    model["table:shop.returns"]["name"] = "shop.returns"
    model["table:shop.orders"]["meta"]["owner"] = "someone@example.com"  # meta never counts
    return model


def test_a_report_lists_what_was_added_removed_and_changed():
    r = report.diff(shop(), shop(NOW, changed_by_hand()))
    assert (r.added, r.removed) == (["table:shop.returns"], ["view:shop.order_totals"])
    assert {k: [(d.path, d.live, d.desired) for d in v] for k, v in r.changed.items()} == {
        "table:shop.customers": [("comment", "One row per customer", "Changed by hand"),
                                 ("properties.team", None, "data")]}


def test_a_report_counts_a_property_that_went():
    """compare's declared-only rule would hide this; a report between snapshots is exact."""
    r = report.diff(shop(NOW, changed_by_hand()), shop())
    assert [(d.path, d.live, d.desired) for d in r.changed["table:shop.customers"]][-1] == (
        "properties.team", "data", None)


def test_the_same_model_reports_nothing():
    r = report.diff(shop(), shop(NOW))
    assert r.empty and report.text(r).splitlines()[3] == "No differences."


def test_the_report_formats():
    r = report.diff(shop(), shop(NOW, changed_by_hand()))
    text = report.text(r)
    assert text.splitlines()[:4] == [
        f"From ucstate_dev at 2026-10-30 09:00 UTC ({shop().hash[:12]})",
        f"  to ucstate_dev at 2026-11-01 12:00 UTC ({r.new.hash[:12]})", "",
        "3 objects differ: 1 added, 1 removed, 1 changed."]
    assert '             comment: "One row per customer" -> "Changed by hand"' in text
    assert "- changed `table:shop.customers`" in report.markdown(r)
    data = report.to_json(r)
    assert data["changed"]["table:shop.customers"][0] == {
        "path": "comment", "old": "One row per customer", "new": "Changed by hand"}
    assert data["old"]["hash"] == shop().hash


def test_a_long_value_is_cut_in_text_but_whole_in_json():
    model = copy.deepcopy(samples.model("shop"))
    model["table:shop.customers"]["comment"] = "x" * 500
    r = report.diff(shop(), shop(NOW, model))
    assert "…" in report.text(r)
    assert report.to_json(r)["changed"]["table:shop.customers"][0]["new"] == "x" * 500


def test_snapshots_of_different_schemas_say_so():
    other = make("ucstate_dev", ["roundtrip", "shop"], NOW,
                 {**samples.model("shop"), **samples.model("roundtrip")})
    assert "read different schemas: shop, then roundtrip, shop" in report.text(
        report.diff(shop(), other))
