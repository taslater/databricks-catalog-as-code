"""Phase 3 probes: what apply's lock, state and history rest on.

    uv run python -m probes.phase3 --profile my-profile [q1 q2 ...]

Runs in a throwaway catalog, cac_probe_p3, which it drops first and last.

- q1: a schema named `_ucstate`, unquoted.
- q2: CREATE TABLE as a lock. Two runs create the same table at once,
  several times over. Exactly one should succeed each time.
- q3: whether REST's `delta.lastUpdateVersion` keeps up with DESCRIBE
  HISTORY, straight after an ALTER and an INSERT.
- q4: a whole model as one statement parameter: INSERT OVERWRITE from
  `explode(from_json(:rows, ...))`, with quotes, backslashes, newlines and
  non-ASCII text, then values of 0.9 MB and 1.1 MB (the API's limit is 1 MiB
  of parameters per statement), and a NULL.
- q5: whether the model reads the same twice, so a fingerprint is stable.
- q6: a service principal with ALL PRIVILEGES and MANAGE on the catalog, as
  CI would have. Its secret lasts an hour, is never printed, and is deleted
  with the principal however the probe ends.
- q7: a lock the principal left, cleared locally. The user running the probe owns the
  catalog and has USE SCHEMA and SELECT on it, as on ucstate_prod; the
  principal owns `_ucstate` and the lock table.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from databricks.sdk import WorkspaceClient

from ucmodel.normalise import normalise
from ucmodel.reader import read_schema
from ucmodel.workspace import SqlError, Workspace
from ucstate.state import Run, Store

CATALOG = "cac_probe_p3"


def run(ws: Workspace, *statements: str, **kw) -> list[dict] | None:
    rows = None
    for s in statements:
        try:
            rows = ws.sql(s, **kw)
            print(f"  ok    {s.splitlines()[0][:110]}")
        except Exception as e:  # noqa: BLE001 - the API refuses some requests before SQL runs
            summary = e.summary if isinstance(e, SqlError) else f"{type(e).__name__}: {e}"
            print(f"  FAIL  {s.splitlines()[0][:110]}\n        {summary[:200]}")
            rows = None
    return rows


def model(ws: Workspace, schema: str) -> dict:
    return normalise({"catalog": CATALOG, "schemas": {schema: read_schema(ws, CATALOG, schema)}})


def q1(ws: Workspace) -> None:
    print("q1: an unquoted _ucstate schema")
    run(ws, f"CREATE SCHEMA {CATALOG}._ucstate COMMENT 'cac: state, history and lock'",
        f"CREATE TABLE {CATALOG}._ucstate.t (x INT)",
        f"SELECT count(*) AS n FROM {CATALOG}._ucstate.t")
    info = ws.schema(f"{CATALOG}._ucstate")
    print(f"  REST schema name: {info and info['name']}")


def q2(ws: Workspace) -> None:
    print("q2: two runs CREATE the same lock table at once")
    run(ws, f"CREATE SCHEMA IF NOT EXISTS {CATALOG}._ucstate")
    lock = f"{CATALOG}._ucstate.lock"
    for attempt in range(4):
        results: dict[str, str] = {}

        def take(who: str, results: dict[str, str] = results) -> None:
            try:
                ws.sql(f"CREATE TABLE {lock} (holder STRING) COMMENT '{who}'")
                results[who] = "won"
            except SqlError as e:
                results[who] = e.error_class or e.summary[:80]

        threads = [threading.Thread(target=take, args=(w,)) for w in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        info = ws.table(lock)
        print(f"  attempt {attempt}: {results}, REST comment {info and info.get('comment')!r}")
        ws.sql(f"DROP TABLE {lock}")
    run(ws, f"CREATE TABLE {lock} (holder STRING)", f"DROP TABLE {lock}",
        f"CREATE TABLE {lock} (holder STRING)", f"DROP TABLE {lock}")


def q3(ws: Workspace) -> None:
    print("q3: REST delta.lastUpdateVersion against DESCRIBE HISTORY")
    run(ws, f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.p")
    t = f"{CATALOG}.p.versions"

    def both(label: str) -> None:
        rest = (ws.table(t) or {}).get("properties", {}).get("delta.lastUpdateVersion")
        hist = ws.sql(f"DESCRIBE HISTORY {t} LIMIT 1")[0]["version"]
        print(f"  {label}: REST {rest}, history {hist}")

    run(ws, f"CREATE TABLE {t} (a INT)")
    both("after CREATE")
    run(ws, f"ALTER TABLE {t} ADD COLUMN b STRING")
    both("straight after ALTER")
    run(ws, f"INSERT INTO {t} VALUES (1, 'x')")
    both("straight after INSERT")
    run(ws, f"COMMENT ON TABLE {t} IS 'c'")
    both("straight after COMMENT")


def q4(ws: Workspace) -> None:
    print("q4: a model as one statement parameter")
    run(ws, f"CREATE SCHEMA IF NOT EXISTS {CATALOG}._ucstate",
        f"CREATE TABLE {CATALOG}._ucstate.state (key STRING, hash STRING, object STRING)")
    tricky = {"comment": "it's a \\ back\\slash, a \"quote\", a\nnewline, 'é' and '✓'"}
    rows = [{"key": f"table:s.t{i}", "hash": str(i), "object": json.dumps(tricky)}
            for i in range(3)]
    under = [{"key": "table:s.big", "hash": "b", "object": "x" * 900_000}]
    over = [{"key": "table:s.big", "hash": "b", "object": "x" * 1_100_000}]
    insert = (f"INSERT OVERWRITE {CATALOG}._ucstate.state SELECT s.key, s.hash, s.object FROM "
              "(SELECT explode(from_json(:rows, "
              "'ARRAY<STRUCT<key: STRING, hash: STRING, object: STRING>>')) AS s)")
    for label, value in (("tricky", rows), ("0.9 MB", under + rows), ("1.1 MB", over)):
        if run(ws, insert, params={"rows": json.dumps(value)}) is None:
            continue
        back = ws.sql(f"SELECT key, length(object) AS n, object FROM {CATALOG}._ucstate.state "
                      "ORDER BY key")
        same = {r["key"]: r["object"] for r in back} == {r["key"]: r["object"] for r in value}
        print(f"  {label}: {len(back)} rows back, identical: {same}")
    print(f"  NULL parameter: {run(ws, 'SELECT :a AS a, :b AS b', params={'a': 'x', 'b': None})}")


def q5(ws: Workspace) -> None:
    print("q5: the same model read twice")
    run(ws, f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.p",
        f"CREATE TABLE IF NOT EXISTS {CATALOG}.p.parent (id INT NOT NULL, CONSTRAINT parent_pk "
        "PRIMARY KEY (id)) COMMENT 'p'",
        f"CREATE TABLE IF NOT EXISTS {CATALOG}.p.child (id INT, parent_id INT, CONSTRAINT "
        f"child_fk FOREIGN KEY (parent_id) REFERENCES {CATALOG}.p.parent (id))",
        f"CREATE VIEW IF NOT EXISTS {CATALOG}.p.v AS SELECT id FROM {CATALOG}.p.child",
        f"GRANT SELECT ON TABLE {CATALOG}.p.parent TO `account users`",
        f"ALTER TABLE {CATALOG}.p.parent SET TAGS ('domain' = 'x')",
        f"INSERT INTO {CATALOG}.p.parent VALUES (1), (2)")

    def hashes() -> dict[str, str]:
        return {k: hashlib.sha256(json.dumps({a: v for a, v in o.items() if a != "meta"},
                                             sort_keys=True).encode()).hexdigest()[:12]
                for k, o in model(ws, "p").items()}

    first = hashes()
    time.sleep(20)
    second = hashes()
    print(f"  {len(first)} objects, changed between reads: "
          f"{[k for k in first if first[k] != second.get(k)]}")


@contextmanager
def principal(ws: Workspace) -> Iterator[tuple[Workspace, str]]:
    """A throwaway principal with CI's grants on CATALOG, signed in.

    Its secret lasts an hour, exists only inside this function, and is never
    printed. The principal and its secret are deleted however the probe ends.
    """
    from databricks.sdk.service.iam import ComplexValue

    w = ws.client
    sp = sec = None
    try:
        sp = w.service_principals.create(
            display_name="cac-probe-p3", active=True,
            entitlements=[ComplexValue(value="workspace-access"),
                          ComplexValue(value="databricks-sql-access")])
        assert sp.id and sp.application_id
        sec = w.service_principal_secrets_proxy.create(sp.id, lifetime="3600s")
        who = f"`{sp.application_id}`"
        run(ws, f"GRANT ALL PRIVILEGES ON CATALOG {CATALOG} TO {who}",
            f"GRANT MANAGE ON CATALOG {CATALOG} TO {who}")
        client = WorkspaceClient(host=w.config.host, client_id=sp.application_id,
                                 client_secret=sec.secret, auth_type="oauth-m2m")
        spw = Workspace(client, ws.warehouse_id)
        print(f"  signed in as the principal: "
              f"{client.current_user.me().user_name == sp.application_id}")
        try:
            yield spw, who
        except Exception as e:  # noqa: BLE001
            text = str(e).replace(sec.secret, "<redacted>") if sec.secret else str(e)
            print(f"  stopped: {type(e).__name__}: {text[:300]}")
    finally:
        if sp and sp.id and sec and sec.id:
            w.service_principal_secrets_proxy.delete(sp.id, sec.id)
        if sp and sp.id:
            w.service_principals.delete(sp.id)
        print(f"  principal and secret deleted: {bool(sp)}")


def q6(ws: Workspace) -> None:
    print("q6: a service principal with ALL PRIVILEGES and MANAGE on the catalog")
    run(ws, f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.p",
        f"CREATE TABLE IF NOT EXISTS {CATALOG}.p.mine (a INT) COMMENT 'made by a person'")
    with principal(ws) as (spw, _):
        print("  as the principal:")
        run(spw,
            f"CREATE CATALOG IF NOT EXISTS {CATALOG}",
            f"CREATE SCHEMA {CATALOG}.sp_s COMMENT 'made by the principal'",
            f"CREATE TABLE {CATALOG}.sp_s.t (a INT, b STRING) COMMENT 't'",
            f"COMMENT ON TABLE {CATALOG}.sp_s.t IS 't2'",
            f"ALTER TABLE {CATALOG}.sp_s.t SET TAGS ('domain' = 'x')",
            f"ALTER TABLE {CATALOG}.sp_s.t ALTER COLUMN b SET TAGS ('pii' = 'no')",
            f"GRANT SELECT ON TABLE {CATALOG}.sp_s.t TO `account users`",
            f"ALTER SCHEMA {CATALOG}.sp_s SET TAGS ('purpose' = 'probe')",
            f"GRANT USE SCHEMA ON SCHEMA {CATALOG}.sp_s TO `account users`",
            f"CREATE FUNCTION {CATALOG}.sp_s.f(x STRING) RETURNS STRING RETURN upper(x)",
            f"GRANT EXECUTE ON FUNCTION {CATALOG}.sp_s.f TO `account users`",
            f"ALTER TABLE {CATALOG}.sp_s.t ALTER COLUMN b SET MASK {CATALOG}.sp_s.f",
            f"CREATE VIEW {CATALOG}.sp_s.v AS SELECT a FROM {CATALOG}.sp_s.t",
            f"CREATE VOLUME {CATALOG}.sp_s.vol",
            f"ALTER TABLE {CATALOG}.p.mine ADD COLUMN b STRING",
            f"COMMENT ON TABLE {CATALOG}.p.mine IS 'changed by the principal'",
            f"GRANT SELECT ON TABLE {CATALOG}.p.mine TO `account users`",
            f"CREATE TABLE {CATALOG}._cac_lock_probe (x INT)",
            f"DROP SCHEMA {CATALOG}.sp_s CASCADE")
        m = model(spw, "p")
        print(f"  REST as the principal: {sorted(m)}; mine grants "
              f"{m.get('table:p.mine', {}).get('grants')}")


def q7(ws: Workspace) -> None:
    print("q7: a lock the principal left, cleared locally")
    me = ws.client.current_user.me().user_name
    assert me
    for privilege in ("USE SCHEMA", "SELECT"):  # not through run(), which echoes the name
        ws.sql(f"GRANT {privilege} ON CATALOG {CATALOG} TO `{me}`")
    print("  granted you USE SCHEMA and SELECT on the catalog, as on ucstate_prod")
    with principal(ws) as (spw, _):
        theirs = Store(spw, CATALOG)
        theirs.ensure()
        stuck = Run.new("abc123", "the principal")
        theirs.start(stuck, {"probe": True})
        # Held, not exited: a runner that died. Keep the reference, or the
        # garbage collector closes the generator and its finally unlocks.
        held = theirs.lock(stuck)
        held.__enter__()
        mine = Store(ws, CATALOG)
        print(f"  locked, as the principal sees it: {theirs.holder() is not None}")
        print(f"  as you: {mine.describe_lock()}")
        print(f"  as you: latest deployment {mine.deployments(1)[0]['id'] == stuck.id}")
        try:
            mine.unlock()
            print(f"  as you: unlocked. Still locked, as you see it: {mine.holder() is not None}"
                  f"; as the principal sees it: {theirs.holder() is not None}")
        except SqlError as e:
            print(f"  as you: unlock refused: {e.summary[:200]}")
        held.__exit__(None, None, None)


PROBES = [q1, q2, q3, q4, q5, q6, q7]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--warehouse", default=os.environ.get("DATABRICKS_WAREHOUSE_ID"),
                        required="DATABRICKS_WAREHOUSE_ID" not in os.environ,
                        help="a SQL warehouse (default: DATABRICKS_WAREHOUSE_ID)")
    parser.add_argument("only", nargs="*", help="probe names, such as q1 q3")
    args = parser.parse_args()
    ws = Workspace.connect(args.profile, args.warehouse)
    ws.sql(f"DROP CATALOG IF EXISTS {CATALOG} CASCADE")
    ws.sql(f"CREATE CATALOG {CATALOG} COMMENT 'Phase 3 probes; dropped when they finish'")
    try:
        for probe in PROBES:
            if not args.only or probe.__name__ in args.only:
                probe(ws)
    finally:
        ws.sql(f"DROP CATALOG IF EXISTS {CATALOG} CASCADE")


if __name__ == "__main__":
    main()
