"""The planner, on the captured round-trip model.

The live side is the fixture as read from ucstate_test. The desired side is a
copy with one thing changed, and its source files are what `ucstate import`
would write for it. So every test states a change and the plan it must give.
"""

import copy
import json
import random
from pathlib import Path

import pytest

from ucmodel import samples
from ucmodel.model import compare, object_hash
from ucmodel.sqltext import split_statements
from ucstate.intent import History, Intent, Refactor, Script, allowances, checksum, header
from ucstate.plan import RENAMES, Plan, PlanError, _moves, plan, scaffold, widens
from ucstate.project import SourceFile
from ucstate.render import path_for, render

CATALOG = "ucstate_test"


@pytest.fixture
def live() -> dict:
    return samples.model("roundtrip")


def sources(model: dict) -> list[SourceFile]:
    files = render(model)
    keys = {path_for(k): k for k, o in model.items() if o["kind"] != "unsupported"}
    return [SourceFile(Path(p), keys[p], keys[p].split(":")[1].split(".")[0],
                       split_statements(text)) for p, text in files.items()]


def plan_for(desired: dict, live: dict, unmanaged: str = "report"):
    return plan(desired, live, sources(desired), CATALOG, unmanaged)


def changes(result) -> list[tuple[str, str, str]]:
    return [(c.cls, c.key, c.what) for c in result.changes]


def statements(result) -> list[str]:
    return [s for c in result.changes for s in c.statements]


def column(model: dict, key: str, name: str) -> dict:
    return next(c for c in model[key]["columns"] if c["name"] == name)


ORDERS, CUSTOMERS, LINES = ("table:roundtrip.orders", "table:roundtrip.customers",
                            "table:roundtrip.order_lines")
VIEW, MASK = "view:roundtrip.customer_orders", "function:roundtrip.mask_email"


def test_an_unchanged_model_plans_nothing(live):
    result = plan_for(copy.deepcopy(live), live)
    assert result.changes == [] and result.notes == []
    assert result.text() == "No changes: ucstate_test matches the source."


def test_from_empty_everything_is_created_in_dependency_order(live):
    result = plan_for(live, {})
    assert {c.cls for c in result.changes} == {"auto"}
    order = [c.key for c in result.changes]
    assert order[0] == "schema:roundtrip"
    for later, earlier in [(CUSTOMERS, MASK), (CUSTOMERS, "function:roundtrip.visible_region"),
                           (ORDERS, CUSTOMERS), (VIEW, ORDERS), (VIEW, CUSTOMERS),
                           ("function:roundtrip.big_orders", ORDERS)]:
        assert order.index(earlier) < order.index(later), (earlier, later)
    # A create runs the source file's own statements.
    create = next(c for c in result.changes if c.key == CUSTOMERS)
    assert create.statements[0].startswith("CREATE TABLE roundtrip.customers (")
    assert "GRANT SELECT ON TABLE roundtrip.customers TO `account users`" in create.statements


def test_metadata_changes_are_auto(live):
    desired = copy.deepcopy(live)
    desired["schema:roundtrip"]["comment"] = "New schema comment"
    desired[ORDERS]["comment"] = None
    desired[ORDERS]["tags"] = {"domain": "sales"}
    desired[CUSTOMERS]["tags"] = {"domain": "marketing"}  # changed; 'reviewed' removed
    column(desired, CUSTOMERS, "email")["tags"] = {}
    column(desired, ORDERS, "status")["comment"] = "It's the status"
    desired[CUSTOMERS]["grants"] = {"account users": ["MODIFY", "SELECT"]}
    desired[VIEW]["grants"] = {}
    result = plan_for(desired, live)
    assert {c.cls for c in result.changes} == {"auto"}
    assert set(statements(result)) == {
        "COMMENT ON SCHEMA roundtrip IS 'New schema comment'",
        "COMMENT ON TABLE roundtrip.orders IS NULL",
        "ALTER TABLE roundtrip.orders SET TAGS ('domain' = 'sales')",
        "ALTER TABLE roundtrip.customers SET TAGS ('domain' = 'marketing')",
        "ALTER TABLE roundtrip.customers UNSET TAGS ('reviewed')",
        "ALTER TABLE roundtrip.customers ALTER COLUMN email UNSET TAGS ('pii')",
        "ALTER TABLE roundtrip.orders ALTER COLUMN status COMMENT 'It\\'s the status'",
        "GRANT MODIFY ON TABLE roundtrip.customers TO `account users`",
        "REVOKE SELECT ON TABLE roundtrip.customer_orders FROM `account users`",
    }
    assert result.changes[0].key == "schema:roundtrip"


def test_properties_are_declared_only_and_a_declared_upgrade_is_one_way(live):
    """Declaring the property is what lets the protocol upgrade run."""
    desired = copy.deepcopy(live)
    desired[ORDERS]["properties"] = {"delta.logRetentionDuration": "interval 60 days",
                                     "delta.enableTypeWidening": "true"}  # already on
    desired[LINES]["properties"] = {"delta.enableTypeWidening": "true"}  # not on yet
    result = plan_for(desired, live)
    assert changes(result) == [
        ("one-way", LINES, "set delta.enableTypeWidening = true, "
                           "which upgrades the table protocol"),
        ("auto", ORDERS, "set properties delta.logRetentionDuration"),
    ]
    assert statements(result) == [
        "ALTER TABLE roundtrip.order_lines SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')",
        "ALTER TABLE roundtrip.orders SET TBLPROPERTIES "
        "('delta.logRetentionDuration' = 'interval 60 days')"]
    assert result.blocked == []


def test_an_added_column_goes_where_the_source_puts_it(live):
    desired = copy.deepcopy(live)
    cols = desired[ORDERS]["columns"]
    note = {**cols[0], "name": "note", "type": "string", "nullable": True, "comment": "Free text",
            "default": "'none'", "tags": {"pii": "no"}}
    cols.insert(2, note)
    cols.append({**note, "name": "last", "comment": None, "default": None, "tags": {}})
    result = plan_for(desired, live)
    assert changes(result) == [
        ("auto", ORDERS, "add column note after order_date, with a default; "
                         "existing rows read NULL"),
        ("auto", ORDERS, "add column last at the end"),
    ]
    assert statements(result) == [
        "ALTER TABLE roundtrip.orders ADD COLUMN note STRING COMMENT 'Free text' "
        "AFTER order_date",
        "ALTER TABLE roundtrip.orders ALTER COLUMN note SET DEFAULT 'none'",
        "ALTER TABLE roundtrip.orders ALTER COLUMN note SET TAGS ('pii' = 'no')",
        "ALTER TABLE roundtrip.orders ADD COLUMN last STRING",
    ]


def test_a_reorder_moves_as_few_columns_as_it_can(live):
    desired = copy.deepcopy(live)
    cols = desired[ORDERS]["columns"]
    cols.insert(0, cols.pop(5))  # quantity first
    result = plan_for(desired, live)
    assert statements(result) == ["ALTER TABLE roundtrip.orders ALTER COLUMN quantity FIRST"]


@pytest.mark.parametrize("seed", range(200))
def test_moves_reach_any_order(seed):
    rng = random.Random(seed)
    have = [f"c{i}" for i in range(rng.randint(0, 9))]
    want = have + [f"new{i}" for i in range(rng.randint(0, 3))]
    rng.shuffle(want)
    moves = _moves(want, have)  # raises if the simulated result is wrong
    assert len(moves) <= len(want)
    if want[:len(have)] == have:
        assert [n for n, _ in moves] == want[len(have):]


def test_column_changes(live):
    desired = copy.deepcopy(live)
    column(desired, ORDERS, "amount")["nullable"] = True
    column(desired, ORDERS, "status")["default"] = None
    column(desired, CUSTOMERS, "region")["default"] = "'US'"
    column(desired, CUSTOMERS, "email")["mask"] = None
    column(desired, CUSTOMERS, "name")["mask"] = {"function": "roundtrip.mask_email",
                                                  "using": []}
    result = plan_for(desired, live)
    assert {c.cls for c in result.changes} == {"auto"}
    assert set(statements(result)) == {
        "ALTER TABLE roundtrip.orders ALTER COLUMN amount DROP NOT NULL",
        "ALTER TABLE roundtrip.orders ALTER COLUMN status DROP DEFAULT",
        "ALTER TABLE roundtrip.customers ALTER COLUMN region SET DEFAULT 'US'",
        "ALTER TABLE roundtrip.customers ALTER COLUMN email DROP MASK",
        "ALTER TABLE roundtrip.customers ALTER COLUMN name SET MASK roundtrip.mask_email",
    }


def test_table_level_changes(live):
    desired = copy.deepcopy(live)
    desired[ORDERS]["clustering"] = {"columns": ["customer_id"]}
    desired[CUSTOMERS]["clustering"] = None  # undeclared, so never compared
    desired[CUSTOMERS]["row_filter"] = None
    assert statements(plan_for(desired, live)) == [
        "ALTER TABLE roundtrip.customers DROP ROW FILTER",
        "ALTER TABLE roundtrip.orders CLUSTER BY (customer_id)",
    ]


def test_a_changed_primary_key_takes_its_foreign_keys_down_and_back(live):
    desired = copy.deepcopy(live)
    desired[CUSTOMERS]["constraints"]["customers_pk"]["rely"] = True
    result = plan_for(desired, live)
    assert [(c.key, s) for c in result.changes for s in c.statements] == [
        (ORDERS, "ALTER TABLE roundtrip.orders DROP CONSTRAINT orders_customers_fk"),
        (CUSTOMERS, "ALTER TABLE roundtrip.customers DROP CONSTRAINT customers_pk"),
        (CUSTOMERS, "ALTER TABLE roundtrip.customers ADD CONSTRAINT customers_pk "
                    "PRIMARY KEY (customer_id) RELY"),
        (ORDERS, "ALTER TABLE roundtrip.orders ADD CONSTRAINT orders_customers_fk "
                 "FOREIGN KEY (customer_id) REFERENCES roundtrip.customers (customer_id)"),
    ]


def test_dropping_a_check_is_auto_and_adding_one_is_checked(live):
    desired = copy.deepcopy(live)
    del desired[ORDERS]["constraints"]["amount_positive"]
    assert statements(plan_for(desired, live)) == [
        "ALTER TABLE roundtrip.orders DROP CONSTRAINT amount_positive"]
    assert changes(plan_for(live, desired)) == [("checked", ORDERS, "add CHECK amount_positive")]


def test_a_changed_view_is_replaced_from_its_source_and_its_grants_rerun(live):
    desired = copy.deepcopy(live)
    desired[VIEW]["definition"] += " HAVING count(o.order_id) > 0"
    desired[VIEW]["tags"] = {"domain": "other"}
    result = plan_for(desired, live)
    assert changes(result) == [("auto", VIEW, "replace view (definition changed)")]
    replace, *rest = result.changes[0].statements
    assert replace.startswith("CREATE OR REPLACE VIEW roundtrip.customer_orders (")
    assert rest == ["ALTER VIEW roundtrip.customer_orders SET TAGS ('domain' = 'other')",
                    "GRANT SELECT ON TABLE roundtrip.customer_orders TO `account users`"]
    # Formatting alone isn't a change.
    desired = copy.deepcopy(live)
    desired[VIEW]["definition"] = desired[VIEW]["definition"].upper().replace(" ", "\n  ")
    assert plan_for(desired, live).changes == []


def test_a_function_grant_alone_is_a_grant_but_a_body_change_replaces(live):
    desired = copy.deepcopy(live)
    desired[MASK]["grants"] = {"account users": ["EXECUTE"]}
    assert statements(plan_for(desired, live)) == [
        "GRANT EXECUTE ON FUNCTION roundtrip.mask_email TO `account users`"]
    desired[MASK]["body"] = "'***'"
    result = plan_for(desired, live)
    assert changes(result) == [("auto", MASK, "replace function (body changed)")]
    assert result.changes[0].statements == [
        "CREATE OR REPLACE FUNCTION roundtrip.mask_email(email STRING)\nRETURNS STRING\n"
        "DETERMINISTIC\nCOMMENT 'Keeps only the domain'\nRETURN '***'",
        "GRANT EXECUTE ON FUNCTION roundtrip.mask_email TO `account users`"]


def test_view_column_tags_use_alter_table(live):
    desired = copy.deepcopy(live)
    desired[VIEW]["columns"][0]["tags"] = {"pii": "no"}
    assert statements(plan_for(desired, live)) == [
        "ALTER TABLE roundtrip.customer_orders ALTER COLUMN customer_id SET TAGS ('pii' = 'no')"]


@pytest.mark.parametrize("change, expected, reason", [
    (lambda m: m[ORDERS]["columns"].pop(), [("destructive", ORDERS, "drop column status")],
     "needs an allowance in project.yml: allow: [drop_column: roundtrip.orders.status]"),
    (lambda m: column(m, ORDERS, "status").update(name="state"),
     [("ambiguous", ORDERS, "column status is gone and state is new, with the same type: "
                            "a rename, or a drop and an add?")],
     "rename_column: {table: roundtrip.orders, from: status, to: state}"),
    (lambda m: column(m, ORDERS, "quantity").update(type="int"),
     [("rewrite", ORDERS, "column quantity changes from bigint to int")],
     "needs a transition script: ucstate transition --target <target> --object " + ORDERS),
    (lambda m: column(m, LINES, "Line Number").update(type="bigint"),
     [("one-way", LINES, "column Line Number changes from int to bigint")],
     "needs type widening: declare 'delta.enableTypeWidening' = 'true'"),
    (lambda m: m[LINES].update(partitioning=[]),
     [("rewrite", LINES, "partitioning changes from ['event_date'] to []")],
     "needs a transition script"),
    (lambda m: column(m, ORDERS, "amount_doubled").update(generated="amount * 3"),
     [("rewrite", ORDERS, "column amount_doubled changes how it is generated")],
     "needs a transition script"),
])
def test_changes_that_stay_blocked_without_what_they_need(live, change, expected, reason):
    desired = copy.deepcopy(live)
    change(desired)
    result = plan_for(desired, live)
    assert changes(result) == expected
    assert result.blocked == result.changes
    assert all(c.statements == [] for c in result.blocked)
    why = result.changes[0].blocked or ""
    assert reason in why and f"blocked: {why.splitlines()[0]}" in result.text()


@pytest.mark.parametrize("change, commands", [
    (lambda m: m[ORDERS]["columns"].pop(),
     [["allow", "drop_column", "roundtrip.orders.status"]]),
    (lambda m: column(m, ORDERS, "status").update(name="state"),
     [["rename-column", "roundtrip.orders", "status", "state"],
      ["allow", "drop_column", "roundtrip.orders.status"]]),
    (lambda m: column(m, ORDERS, "quantity").update(type="int"),
     [["transition", "--object", ORDERS]]),
    (lambda m: column(m, LINES, "Line Number").update(type="bigint"), []),  # a SQL edit
])
def test_a_blocked_change_lists_the_commands_that_would_unblock_it(live, change, commands):
    desired = copy.deepcopy(live)
    change(desired)
    result = plan_for(desired, live)
    assert [o["command"] for o in result.changes[0].options] == commands
    text = result.text()
    assert all(f"ucstate {' '.join(c)}" in text for c in commands)
    assert ("options:" in text) == bool(commands)


def test_an_ambiguous_tables_options_are_a_rename_or_a_drop(live):
    desired = copy.deepcopy(live)
    desired["table:roundtrip.order_headers"] = {**desired.pop(ORDERS),
                                                "name": "roundtrip.order_headers"}
    result = plan_for(desired, live)
    ambiguous = next(c for c in result.changes if c.cls == "ambiguous")
    rename, drop = ambiguous.options
    assert rename["command"] == ["rename-table", "roundtrip.orders", "roundtrip.order_headers"]
    assert rename["file"] == "refactor.yml"
    assert rename["adds"] in ambiguous.blocked.replace("\n  ", "\n")  # type: ignore[union-attr]
    assert (drop["label"], drop["command"]) == (
        "it's a drop and a create", ["allow", "drop_table", "roundtrip.orders"])


def test_a_planned_rename_isnt_listed_as_done(live):
    desired = copy.deepcopy(live)
    column(desired, ORDERS, "status").update(name="state")
    desired[ORDERS]["properties"] = {"delta.columnMapping.mode": "name"}
    r = Refactor("s", "rename_column", "roundtrip.orders", "status", "state")
    text = plan_with(desired, live, Intent(refactors=[r])).text()
    assert "rename column status to state" in text and "Recorded as done" not in text


def test_widening_runs_on_a_table_with_type_widening(live):
    desired = copy.deepcopy(live)
    column(desired, ORDERS, "quantity").update(type="decimal(30,0)")
    result = plan_for(desired, live)
    assert changes(result) == [
        ("one-way", ORDERS, "column quantity changes from bigint to decimal(30,0)")]
    assert statements(result) == [
        "ALTER TABLE roundtrip.orders ALTER COLUMN quantity TYPE DECIMAL(30,0)"]
    assert result.blocked == []


def test_a_new_not_null_or_generated_column(live):
    desired = copy.deepcopy(live)
    cols = desired[ORDERS]["columns"]
    cols.append({**cols[0], "name": "required", "nullable": False})
    cols.append({**cols[4], "name": "tripled", "generated": "amount * 3"})
    cols.append({**cols[6], "name": "state", "nullable": False, "default": "'new'"})
    result = plan_for(desired, live)
    assert changes(result) == [
        ("checked", ORDERS, "add NOT NULL column required at the end; the table must be empty"),
        ("rewrite", ORDERS, "add generated column tripled"),
        ("checked", ORDERS, "add NOT NULL column state at the end, filling the rows from its "
                            "default"),
    ]
    required, tripled, state = result.changes
    assert required.precheck == "SELECT count(*) AS violations FROM roundtrip.orders"
    assert required.statements == [
        "ALTER TABLE roundtrip.orders ADD COLUMN required BIGINT",
        "ALTER TABLE roundtrip.orders ALTER COLUMN required SET NOT NULL"]
    assert tripled.blocked and not tripled.statements
    # Delta refuses DEFAULT and NOT NULL in ADD COLUMN, so it takes four statements (Phase 0).
    assert state.precheck is None and state.statements == [
        "ALTER TABLE roundtrip.orders ADD COLUMN state STRING COMMENT 'Order status'",
        "ALTER TABLE roundtrip.orders ALTER COLUMN state SET DEFAULT 'new'",
        "UPDATE roundtrip.orders SET state = DEFAULT WHERE state IS NULL",
        "ALTER TABLE roundtrip.orders ALTER COLUMN state SET NOT NULL"]


def test_checked_changes_carry_a_precheck(live):
    desired = copy.deepcopy(live)
    column(desired, ORDERS, "status").update(nullable=False)
    desired[ORDERS]["constraints"]["big"] = {"type": "check", "expression": "amount > 10"}
    result = plan_for(desired, live)
    assert [(c.cls, c.what, c.statements, c.precheck) for c in result.changes] == [
        ("checked", "set NOT NULL on status",
         ["ALTER TABLE roundtrip.orders ALTER COLUMN status SET NOT NULL"],
         "SELECT count_if(status IS NULL) AS violations FROM roundtrip.orders"),
        ("checked", "add CHECK big", ["ALTER TABLE roundtrip.orders ADD CONSTRAINT big "
                                      "CHECK (amount > 10)"],
         "SELECT count_if(NOT coalesce((amount > 10), false)) AS violations "
         "FROM roundtrip.orders"),
    ]
    # Not blocked: the prechecks run next, at plan time and again at apply time.
    assert result.blocked == []
    assert "precheck (not run yet): SELECT count_if(status IS NULL)" in result.text()


def test_objects_not_in_the_source_follow_the_policy(live):
    desired = copy.deepcopy(live)
    for key in (VIEW, "function:roundtrip.slugify", LINES):
        del desired[key]
    reported = plan_for(desired, live)
    assert reported.changes == []
    assert reported.notes == [f"{k}: not in the source (policy unmanaged_objects: report)"
                              for k in ("function:roundtrip.slugify", LINES, VIEW)]
    dropped = plan_for(desired, live, unmanaged="drop")
    undrop = f"UNDROP TABLE WITH ID '{live[LINES]['meta']['table_id']}'"
    assert changes(dropped) == [
        ("auto", VIEW, "drop view"),
        ("auto", "function:roundtrip.slugify", "drop function"),
        ("destructive", LINES, f"drop table; {undrop} brings it back within the retention "
                               "window"),
    ]
    assert dropped.blocked == [dropped.changes[2]]
    assert dropped.changes[2].blocked == ("needs an allowance in project.yml: "
                                          "allow: [drop_table: roundtrip.order_lines]")


def test_a_table_replacing_a_view_drops_the_view_first(live):
    desired = copy.deepcopy(live)
    view = desired.pop(VIEW)
    desired["table:roundtrip.customer_orders"] = {
        **copy.deepcopy(live[LINES]), "name": view["name"], "partitioning": []}
    result = plan_for(desired, live)
    assert [(c.cls, c.key, c.what) for c in result.changes] == [
        ("auto", VIEW, "drop view, which a table of the same name replaces"),
        ("auto", "table:roundtrip.customer_orders", "create table"),
    ]


def test_a_new_table_shaped_like_a_gone_one_is_ambiguous(live):
    desired = copy.deepcopy(live)
    desired["table:roundtrip.lines"] = {**desired.pop(LINES), "name": "roundtrip.lines"}
    result = plan_for(desired, live)
    assert changes(result) == [
        ("ambiguous", "table:roundtrip.lines", "new table with the same columns as "
         "roundtrip.order_lines, which is gone: a rename, or a drop and a create?")]
    assert result.notes == []


def test_a_difference_no_rule_covers_fails_loudly(live):
    desired = copy.deepcopy(live)
    desired[ORDERS]["something_new"] = 1
    with pytest.raises(PlanError, match="no rule plans a change to something_new"):
        plan_for(desired, live)


def test_every_difference_compare_finds_gets_a_change(live):
    """The planner covers compare: a mutation compare sees must reach the plan."""
    mutations = [
        lambda m: m[ORDERS].update(comment="x"),
        lambda m: m["volume:roundtrip.landing"].update(comment="x"),
        lambda m: m["volume:roundtrip.landing"]["tags"].update(new="x"),
        lambda m: m["schema:roundtrip"]["properties"].update(team="x"),
        lambda m: m[VIEW].update(schema_mode="EVOLUTION"),
        lambda m: m[VIEW]["columns"][1].update(comment="x"),
        lambda m: m["function:roundtrip.add_tax"]["parameters"][1].update(default="0.1"),
        lambda m: m["function:roundtrip.slugify"].update(deterministic=True),
    ]
    for mutate in mutations:
        desired = copy.deepcopy(live)
        mutate(desired)
        assert compare(desired, live)
        assert plan_for(desired, live).changes


@pytest.mark.parametrize("old, new, expected", [
    ("int", "bigint", True), ("bigint", "int", False), ("tinyint", "smallint", True),
    ("float", "double", True), ("int", "double", True), ("bigint", "double", False),
    ("decimal(10,2)", "decimal(12,2)", True), ("decimal(10,2)", "decimal(10,3)", False),
    ("decimal(10,2)", "decimal(12,4)", True), ("int", "decimal(10,0)", True),
    ("int", "decimal(9,0)", False), ("date", "timestamp_ntz", True),
    ("int", "string", False), ("string", "int", False),
])
def test_widens(old, new, expected):
    assert widens(old, new) is expected


def test_object_hash_ignores_meta_and_key_order(live):
    obj = live[ORDERS]
    same = {k: obj[k] for k in reversed(list(obj))}
    same["meta"] = {"table_id": "another"}
    assert object_hash(same) == object_hash(obj)
    assert object_hash({**obj, "comment": "x"}) != object_hash(obj)
    assert object_hash(None) is None


def test_a_plan_fingerprints_the_live_objects_it_touches(live):
    desired = copy.deepcopy(live)
    desired[ORDERS]["comment"] = "changed"
    before = {k: v for k, v in live.items() if k != LINES}
    result = plan_for(desired, before)
    assert result.fingerprint == {LINES: None, ORDERS: object_hash(live[ORDERS])}
    assert result.stale(before) == []

    later = copy.deepcopy(before)
    later[CUSTOMERS]["comment"] = "an untouched object changed"
    later[ORDERS]["meta"]["updated_at"] = 1
    assert result.stale(later) == []
    later[ORDERS]["comment"] = "changed by someone else"
    later[LINES] = live[LINES]
    assert result.stale(later) == [LINES, ORDERS]


def test_a_plan_file_round_trips(live):
    desired = copy.deepcopy(live)
    desired[ORDERS]["comment"] = "changed"
    result = plan_for(desired, live)
    result.source = "abc123"
    data = json.loads(json.dumps(result.to_json()))
    assert data["format"] == 2 and data["desired"] == desired
    assert Plan.from_json(data) == result
    with pytest.raises(PlanError, match="format 0"):
        Plan.from_json({**data, "format": 0})


# Phase 4: allowances, renames and scripts --------------------------------------------------


def plan_with(desired: dict, live: dict, intent: Intent, history: History | None = None):
    return plan(desired, live, sources(desired), CATALOG, intent=intent, history=history)


def test_an_allowance_lets_a_drop_run_and_an_unused_one_is_noted(live):
    desired = copy.deepcopy(live)
    del desired[LINES]
    desired[CUSTOMERS]["columns"] = [c for c in desired[CUSTOMERS]["columns"]
                                     if c["name"] != "region"]
    intent = Intent(allowances([{"drop_table": "roundtrip.order_lines"},
                                {"drop_column": "roundtrip.customers.region"},
                                {"drop_view": "roundtrip.gone"}]))
    result = plan_with(desired, live, intent)
    assert [(c.cls, c.key, c.statements) for c in result.changes] == [
        ("destructive", CUSTOMERS, ["ALTER TABLE roundtrip.customers DROP COLUMN region"]),
        ("destructive", LINES, ["DROP TABLE roundtrip.order_lines"]),
    ]
    assert result.blocked == []
    assert result.notes == ["allow drop_view: roundtrip.gone matches no change. Delete it once "
                            "every target has it"]


def test_dropping_a_column_needs_column_mapping_declared(live):
    desired = copy.deepcopy(live)
    desired[ORDERS]["columns"].pop()  # status
    intent = Intent(allowances([{"drop_column": "roundtrip.orders.status"}]))
    blocked = plan_with(desired, live, intent)
    assert "needs column mapping: declare 'delta.columnMapping.mode' = 'name'" in (
        blocked.changes[0].blocked or "")
    desired[ORDERS]["properties"] = {"delta.columnMapping.mode": "name"}
    result = plan_with(desired, live, intent)
    assert changes(result) == [
        ("one-way", ORDERS, "set delta.columnMapping.mode = name, which upgrades the table "
                            "protocol"),
        ("destructive", ORDERS, "drop column status")]
    assert statements(result) == [
        "ALTER TABLE roundtrip.orders SET TBLPROPERTIES ('delta.columnMapping.mode' = 'name')",
        "ALTER TABLE roundtrip.orders DROP COLUMN status"]
    assert result.blocked == []


def test_an_allowance_settles_an_ambiguous_column_as_a_drop_and_an_add(live):
    desired = copy.deepcopy(live)
    column(desired, CUSTOMERS, "region").update(name="area")
    intent = Intent(allowances([{"drop_column": "roundtrip.customers.region"}]))
    assert changes(plan_with(desired, live, intent)) == [
        ("destructive", CUSTOMERS, "drop column region"),
        ("auto", CUSTOMERS, "add column area after email, with a default; existing rows "
                            "read NULL")]


def test_an_ambiguous_change_prints_the_entries_that_would_settle_it(live):
    desired = copy.deepcopy(live)
    desired["table:roundtrip.lines"] = {**desired.pop(LINES), "name": "roundtrip.lines"}
    result = plan_for(desired, live)
    text = result.changes[0].blocked or ""
    assert "rename_table: {from: roundtrip.order_lines, to: roundtrip.lines}" in text
    assert "- drop_table: roundtrip.order_lines" in text
    allow = Intent(allowances([{"drop_table": "roundtrip.order_lines"}]))
    settled = plan_with(desired, live, allow)
    assert [c.cls for c in settled.changes] == ["auto", "destructive"]


def test_refactor_renames_a_column_in_place_of_an_ambiguous_change(live):
    desired = copy.deepcopy(live)
    column(desired, CUSTOMERS, "region").update(name="area")
    r = Refactor("2026-11-01-area", "rename_column", "roundtrip.customers", "region", "area")
    result = plan_with(desired, live, Intent(refactors=[r]))
    assert [(c.cls, c.key, c.what, c.statements, c.phase) for c in result.changes] == [
        ("auto", CUSTOMERS, "rename column region to area (refactor.yml 2026-11-01-area)",
         ["ALTER TABLE roundtrip.customers RENAME COLUMN region TO area"], RENAMES)]
    assert result.refactors == [{"id": r.id, "kind": "rename_column", "outcome": "renamed"}]
    assert result.fingerprint == {CUSTOMERS: object_hash(live[CUSTOMERS])}
    assert not result.stale(live)
    # Once the log has it, the entry is never looked at again.
    done = plan_with(desired, live, Intent(refactors=[r]), History(refactors={r.id}))
    assert done.changes[0].cls == "ambiguous" and done.refactors == []


def test_renaming_a_column_needs_column_mapping(live):
    desired = copy.deepcopy(live)
    column(desired, ORDERS, "status").update(name="state")
    r = Refactor("s", "rename_column", "roundtrip.orders", "status", "state")
    blocked = plan_with(desired, live, Intent(refactors=[r]))
    assert [(c.cls, c.statements) for c in blocked.blocked] == [("one-way", [])]
    assert "needs column mapping" in (blocked.blocked[0].blocked or "")
    desired[ORDERS]["properties"] = {"delta.columnMapping.mode": "name"}
    result = plan_with(desired, live, Intent(refactors=[r]))
    assert [(c.cls, c.statements) for c in result.changes] == [("one-way", [
        "ALTER TABLE roundtrip.orders SET TBLPROPERTIES ('delta.columnMapping.mode' = 'name')",
        "ALTER TABLE roundtrip.orders RENAME COLUMN status TO state"])]


def test_a_refactor_already_done_or_for_a_fresh_target_is_only_recorded(live):
    already = Refactor("x", "rename_column", "roundtrip.customers", "zone", "region")
    fresh = Refactor("y", "rename_table", "roundtrip.old", "roundtrip.old", "roundtrip.new")
    result = plan_with(copy.deepcopy(live), live, Intent(refactors=[already, fresh]))
    assert result.changes == []
    assert result.refactors == [
        {"id": "x", "kind": "rename_column", "outcome": "already done"},
        {"id": "y", "kind": "rename_table", "outcome": "skipped: neither table exists"}]
    assert "Recorded as done, with nothing to run:\n  x: already done" in result.text()


def test_refactor_renames_a_table_first_and_its_dependents_follow(live):
    desired = copy.deepcopy(live)
    desired["table:roundtrip.clients"] = {**desired.pop(CUSTOMERS), "name": "roundtrip.clients"}
    desired[ORDERS]["constraints"]["orders_customers_fk"]["references"] = "roundtrip.clients"
    desired[VIEW]["definition"] = desired[VIEW]["definition"].replace("roundtrip.customers",
                                                                      "roundtrip.clients")
    r = Refactor("c", "rename_table", "roundtrip.customers", "roundtrip.customers",
                 "roundtrip.clients")
    result = plan_with(desired, live, Intent(refactors=[r]))
    assert [(c.key, c.what) for c in result.changes] == [
        ("table:roundtrip.clients",
         "rename table roundtrip.customers to roundtrip.clients (refactor.yml c)"),
        (VIEW, "replace view (definition changed)")]
    assert result.changes[0].statements == [
        "ALTER TABLE roundtrip.customers RENAME TO roundtrip.clients"]
    # Apply reads the target before the rename, so that is what is fingerprinted.
    assert result.renamed == {"table:roundtrip.clients": CUSTOMERS}
    assert result.fingerprint == {CUSTOMERS: object_hash(live[CUSTOMERS]),
                                  VIEW: object_hash(live[VIEW])}
    assert not result.stale(live)


def transition_for(live: dict, desired: dict, **override) -> Script:
    s = Script("2026-11-01-narrow", "transition", Path("migrations/2026-11-01-narrow.sql"),
               ["CREATE OR REPLACE TABLE roundtrip.orders AS SELECT 1"], "c0ffee", object=ORDERS,
               before=object_hash(live[ORDERS]), after=object_hash(desired[ORDERS]))
    for k, v in override.items():
        setattr(s, k, v)
    return s


def narrowed(live: dict) -> dict:
    desired = copy.deepcopy(live)
    column(desired, ORDERS, "quantity").update(type="int")
    desired[ORDERS]["comment"] = "narrowed"
    return desired


def test_a_transition_script_takes_the_place_of_its_tables_changes(live):
    desired = narrowed(live)
    s = transition_for(live, desired)
    result = plan_with(desired, live, Intent(scripts=[s]))
    assert changes(result) == [
        ("rewrite", ORDERS, "transition 2026-11-01-narrow, in place of: column quantity changes "
                            "from bigint to int; set the comment")]
    assert statements(result) == s.statements and result.blocked == []
    assert result.scripts == [{"id": s.id, "kind": "transition", "object": ORDERS,
                               "checksum": "c0ffee", "outcome": "ran"}]


@pytest.mark.parametrize("override, reason", [
    ({"before": "0" * 16}, "which is neither the script's from nor its to"),
    ({"after": "0" * 16}, "but the source now declares"),
])
def test_a_transition_from_or_to_another_state_is_blocked(live, override, reason):
    desired = narrowed(live)
    result = plan_with(desired, live, Intent(scripts=[transition_for(live, desired, **override)]))
    assert [(c.cls, c.statements) for c in result.blocked] == [("rewrite", [])]
    assert reason in (result.blocked[0].blocked or "")


def test_a_transition_that_ran_or_has_no_table_yet_does_not_run(live):
    desired = narrowed(live)
    s = transition_for(live, desired)
    ran = plan_with(desired, live, Intent(scripts=[s]), History(scripts={s.id: "c0ffee"}))
    assert ran.scripts == [] and "needs a transition script" in (ran.blocked[0].blocked or "")
    fresh = {k: v for k, v in live.items() if k != ORDERS}
    created = plan_with(desired, fresh, Intent(scripts=[s]))
    assert ("auto", ORDERS, "create table") in changes(created)
    assert created.scripts[0]["outcome"] == "skipped: the table is created as declared"
    with pytest.raises(PlanError, match="changed after it ran"):
        plan_with(desired, live, Intent(scripts=[s]), History(scripts={s.id: "edited"}))


def test_one_time_scripts_run_first_or_last_and_only_once(live):
    desired = copy.deepcopy(live)
    desired[ORDERS]["comment"] = "changed"
    pre = Script("a-pre", "once", Path("migrations/once/a-pre.sql"),
                 ["DELETE FROM roundtrip.orders WHERE amount < 0"], "1", phase="pre")
    post = Script("b-post", "once", Path("migrations/once/b-post.sql"),
                  ["UPDATE roundtrip.orders SET status = 'x'"], "2", phase="post")
    result = plan_with(desired, live, Intent(scripts=[post, pre]))
    assert [(c.cls, c.key) for c in result.changes] == [
        ("script", "script:a-pre"), ("auto", ORDERS), ("script", "script:b-post")]
    assert result.changes[0].schema is None and list(result.fingerprint) == [ORDERS]
    again = plan_with(desired, live, Intent(scripts=[post, pre]),
                      History(scripts={"a-pre": "1", "b-post": "2"}))
    assert [c.key for c in again.changes] == [ORDERS]


def test_a_rebuildable_table_is_replaced_from_its_file(live):
    desired = copy.deepcopy(live)
    files = sources(desired)
    f = next(f for f in files if f.key == LINES)
    f.rebuildable = True
    f.statements = ["CREATE TABLE roundtrip.order_lines AS SELECT * FROM roundtrip.orders",
                    *f.statements[1:]]
    mark = checksum(f.statements[0])
    result = plan(desired, live, files, CATALOG)
    assert changes(result) == [("auto", LINES, "rebuild (its statement changed)")]
    first, *_, last = result.changes[0].statements
    assert first.startswith("CREATE OR REPLACE TABLE roundtrip.order_lines AS SELECT")
    assert last == ("ALTER TABLE roundtrip.order_lines SET TBLPROPERTIES "
                    f"('ucstate.rebuild' = '{mark}')")
    assert result.desired[LINES]["properties"]["ucstate.rebuild"] == mark
    rebuilt = copy.deepcopy(live)
    rebuilt[LINES]["properties"]["ucstate.rebuild"] = mark
    assert plan(desired, rebuilt, files, CATALOG).changes == []


def test_the_transition_scaffold_starts_from_the_live_table(live):
    desired = narrowed(live)
    result = plan_for(desired, live)
    text = scaffold("2026-11-01-narrow", ORDERS, desired, live, result.changes, "ucstate_test")
    fields, body = header(text)
    assert fields == {"ucstate": "transition", "id": "2026-11-01-narrow",
                      "object": "roundtrip.orders", "from": object_hash(live[ORDERS]),
                      "to": object_hash(desired[ORDERS])}
    assert "--   rewrite: column quantity changes from bigint to int" in text
    statements_ = split_statements(body)
    assert statements_[0].startswith("CREATE OR REPLACE TABLE roundtrip.orders\n"
                                     "COMMENT 'narrowed'\nAS SELECT")
    assert "  CAST(quantity AS INT) AS quantity" in statements_[0]
    assert "ALTER TABLE roundtrip.orders ALTER COLUMN order_id SET NOT NULL" in statements_
    assert ("ALTER TABLE roundtrip.orders ADD CONSTRAINT orders_pk PRIMARY KEY (order_id)"
            in statements_)


def test_a_rebuildable_table_is_created_after_the_tables_its_query_reads(live):
    desired = copy.deepcopy(live)
    desired["table:roundtrip.a_totals"] = {**copy.deepcopy(live[LINES]),
                                           "name": "roundtrip.a_totals", "partitioning": []}
    files = sources(desired)
    f = next(f for f in files if f.key == "table:roundtrip.a_totals")
    f.rebuildable = True
    f.statements = ["CREATE TABLE roundtrip.a_totals AS SELECT * FROM roundtrip.orders"]
    fresh = {k: v for k, v in live.items() if k != ORDERS}
    keys = [c.key for c in plan(desired, fresh, files, CATALOG).changes if c.what == "create table"]
    assert keys == [ORDERS, "table:roundtrip.a_totals"]


def test_objects_outside_the_model_are_never_dropped_even_when_unmanaged_ones_are():
    live = samples.model("kinds")
    desired = {k: o for k, o in live.items() if o["kind"] != "unsupported"}
    result = plan_for(desired, live, unmanaged="drop")
    assert result.changes == []
    assert result.notes == [
        "unsupported:kinds.order_metrics: METRIC_VIEW is outside the model",
        "unsupported:kinds.orders_feed: STREAMING_TABLE is outside the model",
        "unsupported:kinds.region_totals: MATERIALIZED_VIEW is outside the model"]
