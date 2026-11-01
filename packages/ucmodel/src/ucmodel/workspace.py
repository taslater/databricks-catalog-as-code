"""One Databricks workspace: Unity Catalog REST reads and SQL statements."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar, cast
from urllib.parse import quote

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.errors.base import DatabricksError
from databricks.sdk.service.sql import (
    ExecuteStatementRequestOnWaitTimeout,
    StatementParameterListItem,
    StatementState,
)

T = TypeVar("T")
R = TypeVar("R")

UC = "/api/2.1/unity-catalog"


class SqlError(Exception):
    """A statement that did not succeed. `error_class` is Databricks' bracketed code."""

    def __init__(self, message: str, statement: str = ""):
        super().__init__(message)
        self.statement = statement
        match = re.match(r"\s*\[([A-Z0-9_.]+)\]", message)
        self.error_class = match.group(1) if match else None

    @property
    def summary(self) -> str:
        return str(self).strip().split("\n")[0]


class NoCredentials(Exception):
    pass


class Workspace:
    def __init__(self, client: WorkspaceClient, warehouse_id: str | None = None):
        self.client = client
        self.warehouse_id = warehouse_id

    @classmethod
    def connect(cls, profile: str | None, warehouse_id: str | None = None) -> Workspace:
        """Connect with a named profile, or with environment variables when there is none.

        Never falls back to a profile nobody named: with no profile and no
        DATABRICKS_* variables, the SDK fails rather than guessing.
        """
        try:
            client = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
        except ValueError as e:  # the SDK's error for configuration it can't use
            if not profile:
                raise NoCredentials("no Databricks credentials: pass --profile, or set "
                                    "DATABRICKS_HOST and its credentials") from e
            raise NoCredentials(f"profile {profile}: {str(e).splitlines()[0]}") from e
        return cls(client, warehouse_id)

    # REST ------------------------------------------------------------------

    def get(self, path: str, **query: Any) -> dict:
        return cast(dict, self.client.api_client.do("GET", path, query=query or None))

    def get_or_none(self, path: str, **query: Any) -> dict | None:
        try:
            return self.get(path, **query)
        except NotFound:
            return None

    def list_all(self, path: str, key: str, **query: Any) -> list[dict]:
        """Follow `next_page_token` until it is absent. A page may be empty."""
        items: list[dict] = []
        token = None
        while True:
            page = self.get(path, **query, **({"page_token": token} if token else {}))
            items.extend(page.get(key, []))
            token = page.get("next_page_token")
            if not token:
                return items

    def map(self, fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(fn, items))

    # Unity Catalog shorthands ------------------------------------------------

    def catalog(self, name: str) -> dict | None:
        return self.get_or_none(f"{UC}/catalogs/{quote(name)}")

    def schema(self, full_name: str) -> dict | None:
        return self.get_or_none(f"{UC}/schemas/{quote(full_name)}")

    def table(self, full_name: str) -> dict | None:
        return self.get_or_none(f"{UC}/tables/{quote(full_name)}")

    def tags(self, entity_type: str, full_name: str) -> dict[str, str]:
        assignments = self.list_all(
            f"{UC}/entity-tag-assignments/{entity_type}/{quote(full_name)}/tags",
            "tag_assignments",
        )
        return {a["tag_key"]: a.get("tag_value", "") for a in assignments}

    def grants(self, securable_type: str, full_name: str) -> dict[str, list[str]]:
        assignments = self.list_all(
            f"{UC}/permissions/{securable_type}/{quote(full_name)}", "privilege_assignments"
        )
        return {
            a["principal"]: sorted(a.get("privileges", []))
            for a in assignments
            if a.get("privileges")
        }

    # SQL ---------------------------------------------------------------------

    def sql(
        self, statement: str, catalog: str | None = None, schema: str | None = None,
        params: dict[str, str | None] | None = None,
    ) -> list[dict]:
        """Run one statement on the warehouse and return its rows as dicts.

        `params` fill named markers such as `:id`, as strings, so values
        never need quoting into the statement. Raises SqlError if the
        statement does not succeed, including when the API refuses it before
        it runs (parameters over 1 MiB, say).
        """
        if not self.warehouse_id:
            raise RuntimeError("no SQL warehouse: pass --warehouse, or set DATABRICKS_WAREHOUSE_ID")
        api = self.client.statement_execution
        try:
            r = api.execute_statement(
                statement=statement,
                warehouse_id=self.warehouse_id,
                catalog=catalog,
                schema=schema,
                parameters=[StatementParameterListItem(name=k, value=v)
                            for k, v in (params or {}).items()] or None,
                wait_timeout="50s",
                on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
            )
        except DatabricksError as e:
            raise SqlError(f"{type(e).__name__}: {e}", statement) from e
        while (state := r.status and r.status.state) in (StatementState.PENDING,
                                                         StatementState.RUNNING):
            time.sleep(1)
            r = api.get_statement(cast(str, r.statement_id))
        if state != StatementState.SUCCEEDED:
            error = r.status and r.status.error
            raise SqlError((error and error.message) or str(state), statement)
        if not (r.manifest and r.manifest.schema and r.manifest.schema.columns):
            return []
        columns = [c.name for c in r.manifest.schema.columns]
        rows = (r.result.data_array or []) if r.result else []
        return [dict(zip(columns, row, strict=True)) for row in rows]
