"""project.yml and the source tree (docs/design.md, section 1)."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlglot import TokenType

from ucmodel.sqltext import split_statements, tokens

from . import intent
from .intent import Intent, IntentError

KIND_DIRS = {"tables": "table", "views": "view", "functions": "function", "volumes": "volume"}
UNMANAGED = ("report", "drop")
STATE_SCHEMA = "_ucstate"  # in every target catalog, outside the managed scope


class ProjectError(Exception):
    pass


REBUILDABLE = re.compile(r"^--\s*ucstate:\s*rebuildable\b", re.MULTILINE)


@dataclass
class SourceFile:
    path: Path
    key: str  # the model key, such as `table:sales.orders`
    schema: str
    statements: list[str]
    rebuildable: bool = False  # `-- ucstate: rebuildable`: a CREATE TABLE ... AS SELECT


@dataclass
class Project:
    root: Path
    schemas: list[str]
    shadow_catalog: str
    targets: dict[str, str]
    warehouse_id: str | None
    unmanaged: str = "report"  # policy.unmanaged_objects
    allow: list = field(default_factory=list)  # project.yml's allow: entries
    rehearsal_catalog: str | None = None  # where apply rehearses a plan on clones

    @classmethod
    def load(cls, root: Path, shadow_catalog: str | None = None) -> Project:
        """`shadow_catalog` replaces project.yml's, so CI runs can each build their own."""
        path = root / "project.yml"
        if not path.exists():
            raise ProjectError(f"{path} not found")
        cfg = yaml.safe_load(path.read_text()) or {}
        project = cls(
            root=root,
            schemas=list(cfg.get("schemas", [])),
            shadow_catalog=shadow_catalog or cfg.get("shadow", {}).get("catalog", "ucstate_shadow"),
            targets={name: t["catalog"] for name, t in cfg.get("targets", {}).items()},
            warehouse_id=cfg.get("warehouse_id"),
            unmanaged=(cfg.get("policy") or {}).get("unmanaged_objects", "report"),
            allow=list(cfg.get("allow") or []),
            rehearsal_catalog=(cfg.get("rehearsal") or {}).get("catalog"),
        )
        if project.unmanaged not in UNMANAGED:
            raise ProjectError(f"policy.unmanaged_objects must be one of {', '.join(UNMANAGED)}")
        if STATE_SCHEMA in project.schemas:
            raise ProjectError(f"{STATE_SCHEMA} holds ucstate's own records, so it can't be "
                               "managed")
        # The shadow build drops its schemas, so it must never point at a target.
        if project.shadow_catalog in project.targets.values():
            raise ProjectError(
                f"the shadow catalog {project.shadow_catalog!r} is also a target catalog")
        # Rehearsal drops its schemas too.
        if project.rehearsal_catalog in [*project.targets.values(), project.shadow_catalog]:
            raise ProjectError(f"the rehearsal catalog {project.rehearsal_catalog!r} is also "
                               "a target or the shadow")
        return project

    def catalog(self, target: str) -> str:
        if target not in self.targets:
            raise ProjectError(f"no target {target!r} in project.yml: {sorted(self.targets)}")
        return self.targets[target]

    def revision(self) -> str | None:
        """The git commit the source comes from, with `+dirty` for uncommitted changes."""
        if sha := os.environ.get("GITHUB_SHA"):
            return sha
        try:
            git = ["git", "-C", str(self.root)]
            sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True,
                                 check=True).stdout.strip()
            dirty = subprocess.run([*git, "status", "--porcelain", "--", "."],
                                   capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None
        return sha + ("+dirty" if dirty else "")

    def intent(self) -> Intent:
        """Allowances, refactor.yml and the scripts in migrations/."""
        try:
            return Intent(intent.allowances(self.allow),
                          intent.refactors(self.root / "refactor.yml"),
                          intent.scripts(self.root))
        except IntentError as e:
            raise ProjectError(str(e)) from None

    def sources(self) -> list[SourceFile]:
        files, problems = [], []
        for schema in self.schemas:
            folder = self.root / "src" / schema
            schema_file = folder / "schema.sql"
            if not schema_file.exists():
                problems.append(f"{schema_file}: missing")
            else:
                files.append(self._load(schema_file, f"schema:{schema}", schema, problems))
            for sub, kind in KIND_DIRS.items():
                for path in sorted((folder / sub).glob("*.sql")):
                    key = f"{kind}:{schema}.{path.stem.lower()}"
                    files.append(self._load(path, key, schema, problems))
        if problems:
            raise ProjectError("\n".join(problems))
        return files

    def _load(self, path: Path, key: str, schema: str, problems: list[str]) -> SourceFile:
        statements = split_statements(path.read_text())
        rel = path.relative_to(self.root)
        if not statements:
            problems.append(f"{rel}: no statements")
        else:
            head = tokens(statements[0])[:12]
            words = " ".join(t.text.upper() for t in head)
            if not words.startswith("CREATE "):
                problems.append(f"{rel}: the first statement must be a CREATE")
            elif words.startswith("CREATE OR REPLACE ") or " IF NOT EXISTS " in f"{words} ":
                problems.append(f"{rel}: use a plain CREATE, without OR REPLACE or IF NOT EXISTS")
            elif not _names(head, key.split(":", 1)[1]):
                problems.append(f"{rel}: the CREATE must name {key.split(':', 1)[1]}, "
                                "as a two-part name for anything but a schema")
        rebuildable = bool(REBUILDABLE.search(path.read_text()))
        if rebuildable and not (key.startswith("table:") and _selects(statements[0])):
            problems.append(f"{rel}: only a CREATE TABLE ... AS SELECT can be rebuildable")
        return SourceFile(path=rel, key=key, schema=schema, statements=statements,
                          rebuildable=rebuildable)


def _selects(statement: str) -> bool:
    """Whether a CREATE has `AS` followed by a query, outside brackets."""
    depth, prev = 0, None
    for t in tokens(statement):
        if t.token_type == TokenType.L_PAREN:
            if depth == 0 and prev == "AS":
                return True
            depth += 1
        elif t.token_type == TokenType.R_PAREN:
            depth -= 1
        elif depth == 0 and prev == "AS" and t.text.upper() in ("SELECT", "WITH", "FROM"):
            return True
        prev = t.text.upper() if depth == 0 else None
    return False


def _names(head: list, name: str) -> bool:
    """Whether the tokens hold `name` (`schema` or `schema.object`) as a dotted sequence."""
    want = name.lower().split(".")
    texts = ["'" if t.token_type == TokenType.STRING else t.text.lower() for t in head]
    span = 2 * len(want) - 1
    for i in range(len(texts) - span + 1):
        window = texts[i:i + span]
        if window[::2] == want and all(x == "." for x in window[1::2]):
            before = texts[i - 1] if i else ""
            after = texts[i + span] if i + span < len(texts) else ""
            if before != "." and after != ".":
                return True
    return False
