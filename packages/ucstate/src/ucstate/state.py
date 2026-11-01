"""The `_ucstate` schema in each target catalog: the lock, the deployments and the state.

docs/design.md, sections 6 and 7. The schema is outside the managed scope,
and apply creates it on first use.

- **`_ucstate.lock`** exists while an apply runs, and its comment says whose
  it is. Creating a table is atomic: of two runs that create it at once,
  exactly one succeeds (probes/phase3.py, q2). So holding the lock means
  having created the table, and releasing it means dropping it.
- **`_ucstate.deployments`** has one row per apply. It is inserted as `running`
  before the first statement and finished as `succeeded` or `failed`, with
  the statements and their timings, and each touched table's Delta version
  before and after.
- **`_ucstate.state`** holds the model each successful apply deployed, one row
  per object. The current state is the rows of the latest succeeded
  deployment. Statement parameters are capped at 1 MiB (q4), so a model is
  written in chunks. Those rows count only once their deployment is marked
  succeeded, which is a single statement. So a failure partway through
  leaves the previous state current.
- **`_ucstate.refactor_log`** has a row per refactor.yml entry once a
  deployment has dealt with it, and **`_ucstate.migration_history`** one per
  transition or one-time script, with the file's checksum (docs/design.md,
  sections 3 and 5). They are written only when the deployment succeeds.
  A rename or script that ran in a deployment that then failed is found
  done by the next plan, which records it then.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from ucmodel.model import object_hash
from ucmodel.sqltext import ident, string
from ucmodel.workspace import SqlError, Workspace

from .intent import History
from .project import STATE_SCHEMA

CHUNK = 900_000  # characters of parameters per statement, under the API's 1 MiB

TABLES = {
    "deployments": """
        id STRING NOT NULL COMMENT 'When the apply started, then a random suffix',
        status STRING COMMENT 'running, succeeded or failed',
        started_at TIMESTAMP,
        finished_at TIMESTAMP,
        source STRING COMMENT 'The git commit, with +dirty for uncommitted changes',
        ran_by STRING COMMENT 'The user or service principal that applied',
        run_url STRING COMMENT 'The GitHub Actions run, when there is one',
        plan STRING COMMENT 'The plan as JSON: changes, notes and fingerprint',
        statements STRING COMMENT 'JSON: each statement run, with its key, seconds and error',
        versions STRING COMMENT 'JSON: the Delta version of each touched table, before and after',
        error STRING""",
    "state": """
        deployment_id STRING NOT NULL,
        key STRING NOT NULL COMMENT 'The model key, such as table:sales.orders',
        hash STRING COMMENT 'object_hash of the desired object',
        object STRING COMMENT 'The desired object as JSON, with meta from the live object'""",
    "refactor_log": """
        id STRING NOT NULL COMMENT 'The refactor.yml entry',
        kind STRING COMMENT 'rename_table or rename_column',
        outcome STRING COMMENT 'renamed, already done, or skipped and why',
        deployment_id STRING NOT NULL,
        recorded_at TIMESTAMP""",
    "migration_history": """
        id STRING NOT NULL COMMENT 'The script, by its file name',
        kind STRING COMMENT 'transition or once',
        object STRING COMMENT 'The table a transition script is for',
        checksum STRING COMMENT 'sha256 of the file, first 16 hex characters',
        outcome STRING COMMENT 'ran, already done, or skipped and why',
        deployment_id STRING NOT NULL,
        recorded_at TIMESTAMP""",
}
COMMENTS = {
    "deployments": "ucstate: one row per apply",
    "state": "ucstate: the model each successful apply deployed. The current state is the rows "
             "of the latest succeeded deployment",
    "refactor_log": "ucstate: the refactor.yml entries each deployment dealt with",
    "migration_history": "ucstate: the transition and one-time scripts each deployment dealt with",
}


class LockHeld(Exception):
    pass


@dataclass
class Run:
    """One apply, as the deployments table records it."""

    id: str
    source: str | None
    ran_by: str
    run_url: str | None = None

    @classmethod
    def new(cls, source: str | None, ran_by: str, run_url: str | None = None) -> Run:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return cls(f"{stamp}-{secrets.token_hex(3)}", source, ran_by, run_url)


class Store:
    """The `_ucstate` schema of one target catalog."""

    def __init__(self, ws: Workspace, catalog: str):
        self.ws = ws
        self.catalog = catalog
        self.schema = f"{ident(catalog)}.{STATE_SCHEMA}"

    def table(self, name: str) -> str:
        return f"{self.schema}.{name}"

    def exists(self) -> bool:
        return self.ws.table(f"{self.catalog}.{STATE_SCHEMA}.deployments") is not None

    def ensure(self) -> None:
        """Create the schema and its tables if they are missing."""
        if self.ws.schema(f"{self.catalog}.{STATE_SCHEMA}") is None:
            self.ws.sql(f"CREATE SCHEMA IF NOT EXISTS {self.schema} "
                        "COMMENT 'ucstate: the lock, deployments and state for this catalog'")
        for name, columns in TABLES.items():
            if self.ws.table(f"{self.catalog}.{STATE_SCHEMA}.{name}") is None:
                self.ws.sql(f"CREATE TABLE IF NOT EXISTS {self.table(name)} ({columns}\n) "
                            f"COMMENT {string(COMMENTS[name])}")

    # The lock ----------------------------------------------------------------------------

    @contextmanager
    def lock(self, run: Run) -> Iterator[None]:
        holder = {"deployment": run.id, "ran_by": run.ran_by, "source": run.source,
                  "run_url": run.run_url, "since": datetime.now(UTC).isoformat()}
        try:
            self.ws.sql(f"CREATE TABLE {self.table('lock')} (deployment STRING) "
                        f"COMMENT {string(json.dumps(holder))}")
        except SqlError as e:
            if e.error_class == "TABLE_OR_VIEW_ALREADY_EXISTS":
                raise LockHeld(self.describe_lock()) from None
            raise
        try:
            yield
        finally:
            self.unlock()

    def holder(self) -> dict | None:
        info = self.ws.table(f"{self.catalog}.{STATE_SCHEMA}.lock")
        if info is None:
            return None
        try:
            return json.loads(info.get("comment") or "{}")
        except json.JSONDecodeError:
            return {"comment": info.get("comment")}

    def describe_lock(self) -> str:
        h = self.holder()
        if h is None:
            return f"{self.catalog} is not locked"
        who = ", ".join(f"{k} {v}" for k, v in h.items() if v)
        return f"{self.catalog} is locked by another apply ({who})"

    def unlock(self) -> None:
        self.ws.sql(f"DROP TABLE IF EXISTS {self.table('lock')}")

    # Deployments -------------------------------------------------------------------------

    def start(self, run: Run, plan_json: dict) -> None:
        self.ws.sql(
            f"INSERT INTO {self.table('deployments')} (id, status, started_at, source, ran_by, "
            "run_url, plan) VALUES (:id, 'running', current_timestamp(), :source, :ran_by, "
            ":run_url, :plan)",
            params={"id": run.id, "source": run.source, "ran_by": run.ran_by,
                    "run_url": run.run_url, "plan": _fit(json.dumps(plan_json))})

    def finish(self, run: Run, status: str, statements: list[dict], versions: dict,
               error: str | None = None) -> None:
        self.ws.sql(
            f"UPDATE {self.table('deployments')} SET status = :status, "
            "finished_at = current_timestamp(), statements = :statements, "
            "versions = :versions, error = :error WHERE id = :id",
            params={"id": run.id, "status": status, "statements": _fit(json.dumps(statements)),
                    "versions": json.dumps(versions), "error": error and error[:20_000]})

    def deployments(self, limit: int = 10) -> list[dict]:
        return self.ws.sql(
            "SELECT id, status, started_at, finished_at, source, ran_by, run_url, "
            "json_array_length(statements) AS statements, error "
            f"FROM {self.table('deployments')} ORDER BY started_at DESC LIMIT {int(limit)}")

    # Refactors and scripts ------------------------------------------------------------------

    def history(self) -> History:
        """The refactors and scripts this target has already dealt with."""
        refactors = self._read("refactor_log", "DISTINCT id")
        scripts = self._read("migration_history", "id, checksum")
        return History({r["id"] for r in refactors}, {r["id"]: r["checksum"] for r in scripts})

    def _read(self, name: str, columns: str) -> list[dict]:
        """A record table's rows, or none before apply has created it. Plans create nothing."""
        if self.ws.table(f"{self.catalog}.{STATE_SCHEMA}.{name}") is None:
            return []
        return self.ws.sql(f"SELECT {columns} FROM {self.table(name)}")

    def record(self, run: Run, refactors: list[dict], scripts: list[dict]) -> None:
        if refactors:
            self.ws.sql(
                f"INSERT INTO {self.table('refactor_log')} SELECT r.id, r.kind, r.outcome, :run, "
                "current_timestamp() FROM (SELECT explode(from_json(:rows, "
                "'ARRAY<STRUCT<id: STRING, kind: STRING, outcome: STRING>>')) AS r)",
                params={"run": run.id, "rows": json.dumps(refactors)})
        if scripts:
            self.ws.sql(
                f"INSERT INTO {self.table('migration_history')} SELECT s.id, s.kind, s.object, "
                "s.checksum, s.outcome, :run, current_timestamp() FROM (SELECT explode(from_json("
                ":rows, 'ARRAY<STRUCT<id: STRING, kind: STRING, object: STRING, "
                "checksum: STRING, outcome: STRING>>')) AS s)",
                params={"run": run.id, "rows": json.dumps(scripts)})

    # State --------------------------------------------------------------------------------

    def write_state(self, run: Run, desired: dict[str, dict], live: dict[str, dict]) -> None:
        """Write the desired model's rows under this run. They count once it succeeds."""
        rows = []
        for key, obj in desired.items():
            meta = (live.get(key) or {}).get("meta", {})
            rows.append({"key": key, "hash": object_hash(obj),
                         "object": json.dumps({**obj, "meta": meta}, sort_keys=True)})
        insert = (f"INSERT INTO {self.table('state')} SELECT :id, s.key, s.hash, s.object FROM "
                  "(SELECT explode(from_json(:rows, "
                  "'ARRAY<STRUCT<key: STRING, hash: STRING, object: STRING>>')) AS s)")
        for chunk in _chunks(rows):
            self.ws.sql(insert, params={"id": run.id, "rows": json.dumps(chunk)})

    def current_state(self) -> tuple[str | None, dict[str, dict]]:
        """The latest succeeded deployment's id and model. (None, {}) before the first."""
        latest = self.ws.sql(
            f"SELECT id FROM {self.table('deployments')} WHERE status = 'succeeded' "
            "ORDER BY finished_at DESC LIMIT 1")
        if not latest:
            return None, {}
        rows = self.ws.sql(f"SELECT key, object FROM {self.table('state')} "
                           "WHERE deployment_id = :id ORDER BY key", params={"id": latest[0]["id"]})
        return latest[0]["id"], {r["key"]: json.loads(r["object"]) for r in rows}


def _fit(text: str, limit: int = CHUNK // 2) -> str:
    """Text short enough to pass as a parameter beside the others."""
    if len(text) <= limit:
        return text
    return json.dumps({"truncated": True, "length": len(text), "start": text[:limit // 2]})


def _chunks(rows: list[dict]) -> Iterator[list[dict]]:
    chunk: list[dict] = []
    size = 0
    for row in rows:
        n = len(json.dumps(row))
        if n > CHUNK:
            raise SqlError(f"{row['key']}: {n} characters is too large to record as state")
        if chunk and size + n > CHUNK:
            yield chunk
            chunk, size = [], 0
        chunk.append(row)
        size += n + 2
    if chunk:
        yield chunk
