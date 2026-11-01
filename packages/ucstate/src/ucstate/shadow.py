"""Build the desired model by applying the source to the shadow catalog.

Databricks parses the source, not a local parser (docs/design.md, section 4).
The shadow is empty, so it is rebuilt from scratch on every run: each
managed schema is dropped and recreated. Statements run with the shadow
catalog and the file's schema set on the request, which is what lets the
source use two-part names.

Order comes from retrying. Schema files run first. Then every other
file's statements run in file order, and a statement that fails waits for
the next pass. That covers FKs whose parent doesn't exist yet, views over
missing tables and masks over missing functions, which all fail with a
not-found error and create nothing (Phase 0). The build stops when a pass
makes no progress.

`desired` is what the commands call. It compiles the source locally when it
can, and builds the shadow for what the compiler declines (docs/offline.md).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from ucmodel import reader
from ucmodel.compiler import Unsupported, compile_statements
from ucmodel.model import compare
from ucmodel.normalise import normalise
from ucmodel.sqltext import declared_properties, declares_clustering, ident
from ucmodel.workspace import SqlError, Workspace

from .project import Project, SourceFile

# Errors that no amount of reordering will fix.
FATAL = {
    "PARSE_SYNTAX_ERROR",
    "TABLE_OR_VIEW_ALREADY_EXISTS",
    "SCHEMA_ALREADY_EXISTS",
    "ROUTINE_ALREADY_EXISTS",
}


class ShadowError(Exception):
    def __init__(self, failures: list[tuple[SourceFile, str, SqlError]]):
        self.failures = failures
        lines = [
            f"{f.path}: {e.summary}\n    in: {statement.splitlines()[0][:100]}"
            for f, statement, e in failures
        ]
        super().__init__("the shadow build failed:\n" + "\n".join(lines))


class CompilerDisagrees(Exception):
    def __init__(self, diffs: list):
        self.diffs = diffs
        super().__init__(
            f"the compiler and the shadow disagree on {len(diffs)} "
            f"difference{'s' if len(diffs) != 1 else ''}, so nothing was run "
            "(desired is the compiled model, live the shadow):\n"
            + "\n".join(f"  {d}" for d in diffs))


def desired(ws: Workspace, project: Project, *, mode: str = "auto",
            log: Callable[[str], None] = print) -> dict[str, dict]:
    """The desired model, in the shape `build` returns.

    - `auto`: the compiler's, or the shadow's when the compiler declines.
    - `shadow`: the shadow's.
    - `checked`: the shadow's, once the compiler's agrees with it under
      `compare`. A compiler that declines is skipped.
    """
    if mode == "shadow":
        log("desired: building the shadow, as asked")
        return build(ws, project, log)
    try:
        compiled = compile_statements((f.schema, s) for f in project.sources()
                                      for s in f.statements)
    except Unsupported as e:
        use = "building the shadow" if mode == "auto" else "using the shadow alone"
        log(f"desired: the compiler declined: {str(e).splitlines()[0]}; {use}")
        return build(ws, project, log)
    if mode == "auto":
        log(f"desired: compiled {len(compiled)} objects; no shadow built")
        return compiled
    built = build(ws, project, log)
    if diffs := compare(compiled, built):
        raise CompilerDisagrees(diffs)
    log(f"desired: the compiler and the shadow agree on {len(built)} objects")
    return built


def build(ws: Workspace, project: Project, log: Callable[[str], None] = print) -> dict:
    files = project.sources()
    catalog = project.shadow_catalog
    # Even IF NOT EXISTS needs CREATE CATALOG, which CI's principal doesn't
    # have (probes/phase3.py, q6). So create the catalog only when it's missing.
    if ws.catalog(catalog) is None:
        ws.sql(f"CREATE CATALOG {ident(catalog)} "
               "COMMENT 'ucstate shadow: empty desired-state builds, rebuilt on every plan'")
    for schema in project.schemas:
        ws.sql(f"DROP SCHEMA IF EXISTS {ident(catalog)}.{ident(schema)} CASCADE")
    apply(ws, catalog, files, log)
    model = normalise(reader.read(ws, catalog, project.schemas))
    return declared(model, files)


def apply(ws: Workspace, catalog: str, files: list[SourceFile],
          log: Callable[[str], None] = print) -> None:
    schema_files = [f for f in files if f.key.startswith("schema:")]
    for f in schema_files:
        for statement in f.statements:
            try:
                ws.sql(statement, catalog=catalog)
            except SqlError as e:
                raise ShadowError([(f, statement, e)]) from e
    queues = [(f, deque(f.statements)) for f in files if f not in schema_files]
    total = sum(len(q) for _, q in queues)
    passes = 0
    while queues:
        passes += 1
        progress = False
        failures: list[tuple[SourceFile, str, SqlError]] = []
        for f, queue in queues:
            while queue:
                try:
                    ws.sql(queue[0], catalog=catalog, schema=f.schema)
                except SqlError as e:
                    if e.error_class in FATAL:
                        raise ShadowError([(f, queue[0], e)]) from e
                    failures.append((f, queue[0], e))
                    break
                queue.popleft()
                progress = True
        queues = [(f, q) for f, q in queues if q]
        if queues and not progress:
            raise ShadowError(failures)
    log(f"shadow: {len(schema_files)} schemas and {total} statements applied "
        f"in {passes} pass{'es' if passes != 1 else ''}")


def declared(model: dict[str, dict], files: list[SourceFile]) -> dict[str, dict]:
    """Keep only what the source declares where the platform fills in the rest.

    Properties are declared-only, and clustering counts only when the
    source has a CLUSTER BY. `meta` describes the shadow's own objects,
    so it is emptied.
    """
    by_key = {f.key: f for f in files}
    for key, obj in model.items():
        f = by_key.get(key)
        obj["meta"] = {}
        if f is None:
            continue
        if "properties" in obj:
            keys = set().union(*(declared_properties(s) for s in f.statements))
            obj["properties"] = {k: v for k, v in obj["properties"].items() if k in keys}
        if obj["kind"] == "table" and not any(declares_clustering(s) for s in f.statements):
            obj["clustering"] = None
    return model
