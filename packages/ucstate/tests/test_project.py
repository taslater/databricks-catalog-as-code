import subprocess
from pathlib import Path

import pytest

from ucstate.project import Project, ProjectError


def make(tmp_path: Path, files: dict[str, str]) -> Project:
    (tmp_path / "project.yml").write_text(
        "warehouse_id: w\nschemas: [s]\ntargets:\n  dev: {catalog: ucstate_dev}\n")
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return Project.load(tmp_path)


def test_sources_have_keys_from_their_paths(tmp_path):
    project = make(tmp_path, {
        "src/s/schema.sql": "CREATE SCHEMA s;",
        "src/s/tables/Orders.sql":
            "CREATE TABLE s.orders (a INT);\nCOMMENT ON TABLE s.orders IS 'x';",
        "src/s/views/v.sql": "CREATE VIEW s.v AS SELECT 1;",
    })
    files = project.sources()
    assert [(f.key, len(f.statements)) for f in files] == [
        ("schema:s", 1), ("table:s.orders", 2), ("view:s.v", 1)]
    assert project.shadow_catalog == "ucstate_shadow"
    assert project.catalog("dev") == "ucstate_dev"
    with pytest.raises(ProjectError, match="no target"):
        project.catalog("prod")


@pytest.mark.parametrize("text, message", [
    ("CREATE OR REPLACE TABLE s.t (a INT);", "plain CREATE"),
    ("CREATE TABLE IF NOT EXISTS s.t (a INT);", "plain CREATE"),
    ("ALTER TABLE s.t ADD COLUMN b INT;", "first statement must be a CREATE"),
    ("-- nothing here", "no statements"),
    ("CREATE TABLE s.other (a INT);", "must name s.t"),
    ("CREATE TABLE ucstate_dev.s.t (a INT);", "must name s.t"),
    ("CREATE TABLE t (a INT);", "must name s.t"),
    ("CREATE TABLE s.u (a INT) COMMENT 't';", "must name s.t"),
])
def test_lint_rejects(tmp_path, text, message):
    project = make(tmp_path, {"src/s/schema.sql": "CREATE SCHEMA s;", "src/s/tables/t.sql": text})
    with pytest.raises(ProjectError, match=message):
        project.sources()


def test_a_missing_schema_file_is_an_error(tmp_path):
    project = make(tmp_path, {"src/s/tables/t.sql": "CREATE TABLE s.t (a INT);"})
    with pytest.raises(ProjectError, match="schema.sql: missing"):
        project.sources()


def test_a_function_called_replace_is_not_or_replace(tmp_path):
    project = make(tmp_path, {
        "src/s/schema.sql": "CREATE SCHEMA s;",
        "src/s/functions/replace.sql":
            "CREATE FUNCTION s.replace(x STRING) RETURNS STRING RETURN x;",
    })
    assert [f.key for f in project.sources()] == ["schema:s", "function:s.replace"]


def test_the_shadow_may_not_be_a_target(tmp_path):
    (tmp_path / "project.yml").write_text(
        "schemas: [s]\nshadow: {catalog: ucstate_prod}\n"
        "targets:\n  prod: {catalog: ucstate_prod}\n")
    with pytest.raises(ProjectError, match="also a target"):
        Project.load(tmp_path)


def test_the_shadow_can_be_overridden_but_still_not_to_a_target(tmp_path):
    (tmp_path / "project.yml").write_text(
        "schemas: [s]\ntargets:\n  prod: {catalog: ucstate_prod}\n")
    assert Project.load(tmp_path, "ucstate_shadow_pr").shadow_catalog == "ucstate_shadow_pr"
    with pytest.raises(ProjectError, match="also a target"):
        Project.load(tmp_path, "ucstate_prod")


def test_the_state_schema_cannot_be_managed(tmp_path):
    (tmp_path / "project.yml").write_text("schemas: [s, _ucstate]\n")
    with pytest.raises(ProjectError, match="_ucstate holds ucstate's own records"):
        Project.load(tmp_path)


def test_revision_is_the_commit_and_says_when_the_source_is_dirty(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    project = make(tmp_path, {"src/s/schema.sql": "CREATE SCHEMA s;"})
    assert project.revision() is None  # not a git repository
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert project.revision() == sha
    (tmp_path / "src/s/schema.sql").write_text("CREATE SCHEMA s COMMENT 'x';")
    assert project.revision() == f"{sha}+dirty"
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    assert project.revision() == "abc123"
