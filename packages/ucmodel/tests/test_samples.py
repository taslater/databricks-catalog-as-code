"""`ucmodel.samples.anonymise`: what a saved read keeps and what it loses."""

from ucmodel.samples import anonymise, scrub

A = "4529DD90-b1ef-4438-bc48-921e76385472"
B = "5b8e1c2a-7d3f-4a6b-9e0c-1f2a3b4c5d6e"


def placeholder(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def test_uuids_become_placeholders_in_the_order_they_appear():
    data = {"a": {"table_id": A, "owner": B}, "b": [f"/metastores/{B}/volumes/{A}", B]}
    assert anonymise(data) == {
        "a": {"table_id": placeholder(1), "owner": placeholder(2)},
        "b": [f"/metastores/{placeholder(2)}/volumes/{placeholder(1)}", placeholder(2)]}


def test_a_uuid_in_a_key_or_in_other_text_is_replaced_too():
    out = anonymise({B: "thriftserver-session-01f1d495-0e5a-18b0-a31b-afd4439d511c"})
    assert out == {placeholder(1): f"thriftserver-session-{placeholder(2)}"}


def test_it_is_scrub_as_well_and_leaves_other_values_alone():
    data = {"metastore_id": A, "metastore_name": B, "owner": "someone@example.org", "n": 3,
            "id": "0123456789abcdef", "ok": True, "none": None}
    assert anonymise(data) == {"owner": "owner@example.com", "n": 3, "id": "0123456789abcdef",
                               "ok": True, "none": None}


def test_anonymising_twice_changes_nothing():
    once = anonymise({"x": [A, B, A]})
    assert anonymise(once) == once


def test_a_uuid_with_underscores_keeps_them_and_gets_the_same_placeholder():
    under = A.replace("-", "_")
    out = anonymise({"id": A, "backing": f"__materialization_mat_{under}_orders_1"})
    assert out == {"id": placeholder(1),
                   "backing": f"__materialization_mat_{placeholder(1).replace('-', '_')}_orders_1"}


def test_a_storage_url_anywhere_loses_its_bucket_and_path():
    props = {"spark.internal.pipelines.backing_table_path": "s3://dbstorage-prod-abc123/uc/x/y",
             "query": "SELECT * FROM delta.`abfss://c@acct.dfs.core.windows.net/t` LIMIT 1",
             "plain": "s3 is a service"}
    assert scrub(props) == {"spark.internal.pipelines.backing_table_path": "s3://storage",
                            "query": "SELECT * FROM delta.`abfss://storage` LIMIT 1",
                            "plain": "s3 is a service"}
