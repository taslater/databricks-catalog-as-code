"""serve.py, offline: the local UI's guards, the commands it builds, and its pages.

The commands are run by a fake runner, so ucstate needn't be installed. One
test runs `ucmodel diff` for real.
"""

import http.client
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest

from ucatlas import serve
from ucatlas.serve import App, Refused, Result, make_server, run_tool
from ucmodel import samples
from ucmodel.snapshot import make, write

TOKEN = "t0ken"
PLAN = {"catalog": "ucstate_dev", "notes": ["table:shop.extra: not in the source"], "changes": [
    {"key": "table:shop.products", "cls": "ambiguous", "statements": [], "precheck": None,
     "what": "column sku is gone and stock_code is new", "blocked": "if it's a rename, ...",
     "options": [
         {"label": "it's a rename", "command": ["rename-column", "shop.products", "sku",
                                                "stock_code"],
          "file": "refactor.yml", "adds": "- id: x\n  rename_column: {}\n"},
         {"label": "it's a drop and an add",
          "command": ["allow", "drop_column", "shop.products.sku"],
          "file": "project.yml", "adds": "allow:\n  - drop_column: shop.products.sku\n"}]},
    {"key": "table:shop.orders", "cls": "rewrite", "statements": [], "precheck": None,
     "what": "partitioning changes", "blocked": "needs a transition script: ...",
     "options": [{"label": "draft a transition script to edit", "file": "migrations/",
                  "adds": "a script", "command": ["transition", "--object", "table:shop.orders",
                                                  "--against", "elsewhere.json"]}]},
    {"key": "table:shop.customers", "cls": "auto", "what": "set the comment", "blocked": None,
     "statements": ["COMMENT ON TABLE shop.customers IS '<b>'"], "precheck": None}]}


class Fake:
    """Answers each command as the real one would, from canned output, and keeps them."""

    def __init__(self):
        self.ran: list[tuple[str, list[str]]] = []

    def __call__(self, tool, args):
        self.ran.append((tool, args))
        if args[2:4] == ["plan", "--against"]:
            Path(args[args.index("-o") + 1]).write_text(json.dumps(PLAN))
            return Result(f"{tool} ...", 1)
        return Result(f"{tool} ...", 0, "done\n")


@pytest.fixture
def where(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "project").mkdir()
    config = tmp_path / "databrickscfg"
    config.write_text("[DEFAULT]\nhost = https://a.example.com\n[dev]\nhost = https://b.example.com\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    for day in (1, 2):
        write(make("ucstate_dev", ["shop"], datetime(2026, 11, day, tzinfo=UTC),
                   samples.model("shop")), tmp_path / "snapshots")
    return tmp_path


@pytest.fixture
def app(where):
    return App(Path("snapshots"), Path("project"), Path(".ucatlas"), runner=Fake(), token=TOKEN,
               has_ucstate=True)


def names(where):
    return sorted(p.name for p in (where / "snapshots").iterdir())


# The commands it builds -------------------------------------------------------------------


def test_a_snapshot_needs_a_profile_someone_chose(app):
    form = {"catalog": "ucstate_dev", "schemas": "shop, sales", "lineage": "1"}
    with pytest.raises(Refused, match="never picks one"):
        app.command("snapshot", form)
    assert app.command("snapshot", {**form, "profile": "DEFAULT"})[:2] == ("ucmodel", [
        "--profile", "DEFAULT", "snapshot", "--catalog", "ucstate_dev", "--schema", "shop",
        "--schema", "sales", "-o", "snapshots/"])
    assert app.command("snapshot", {**form, "profile": "dev", "lineage": ""})[1][-3] == (
        "--no-lineage")


@pytest.mark.parametrize("action, args", [
    ("apply", "--target prod"),
    ("rename-table", "shop.a"),
    ("rename-table", "shop.a shop.b; rm"),
    ("rename-column", "shop.a b c d"),
    ("allow", "drop_schema shop"),
    ("allow", "drop_column shop.a"),
    ("transition", "shop.a"),
])
def test_anything_else_is_refused(app, action, args):
    with pytest.raises(Refused):
        app.command(action, {"args": args, "snapshot": names(app.snapshots.resolve().parent)[0]})


def test_the_choices_run_in_the_project(app, where):
    snap = names(where)[0]
    assert app.command("rename-column", {"args": "shop.a b c", "snapshot": snap})[1] == [
        "--project", "project", "rename-column", "shop.a", "b", "c"]
    assert app.command("transition", {"args": "table:shop.a", "snapshot": snap})[1] == [
        "--project", "project", "transition", "--object", "table:shop.a",
        "--against", f"snapshots/{snap}"]


def test_without_ucstate_there_are_no_plans(where):
    bare = App(Path("snapshots"), Path("project"), Path(".ucatlas"), runner=Fake(),
               has_ucstate=False)
    with pytest.raises(Refused, match="ucstate isn't installed"):
        bare.preview(names(where)[0])
    assert "no plan previews" in bare.home()


# Pages ------------------------------------------------------------------------------------


def test_the_home_page_lists_snapshots_newest_first_and_no_profile_is_chosen(app, where):
    page = app.home()
    first, second = names(where)[1], names(where)[0]
    assert page.index(first) < page.index(second)
    assert '<option value="" selected disabled>choose one</option>' in page
    assert page.count("selected") == 1
    assert "dev (https://b.example.com)" in page


def test_a_broken_snapshot_is_listed_with_why(app, where):
    (where / "snapshots" / "broken.json").write_text("{}")
    assert "format None" in app.home()


def test_the_preview_shows_each_option_with_the_command_it_runs(app, where):
    snap = names(where)[0]
    page = app.preview(snap)
    assert app.runner.ran[0] == ("ucstate", [  # type: ignore[attr-defined]
        "--project", "project", "plan", "--against", f"snapshots/{snap}",
        "-o", f".ucatlas/plans/{snap.split('-')[-1][:12]}.json"])
    assert "ucstate --project project rename-column shop.products sku stock_code" in page
    assert "ucstate --project project allow drop_column shop.products.sku" in page
    assert ("ucstate --project project transition --object table:shop.orders "
            f"--against snapshots/{snap}") in page
    assert "elsewhere.json" not in page  # the UI previews this snapshot, not the plan's
    assert page.count("<button>Run it</button>") == 3
    assert "&#x27;&lt;b&gt;&#x27;" in page  # statements are escaped
    assert "table:shop.extra: not in the source" in page


def test_a_drawing_is_made_once_per_snapshot(app, where):
    snap = names(where)[0]
    first = app.draw(snap)
    assert first.status == 303 and app.runner.ran[-1][1][:2] == ["draw", "--snapshot"]  # type: ignore[attr-defined]
    site = Path(app.runner.ran[-1][1][-1])  # type: ignore[attr-defined]
    site.mkdir(parents=True)
    (site / "index.html").write_text("<p>drawn</p>")
    again = app.draw(snap)
    assert again.headers == first.headers and len(app.runner.ran) == 1  # type: ignore[attr-defined]
    assert app.site_file(f"{site.name}/index.html").body == "<p>drawn</p>"


def test_files_outside_the_sites_are_not_served(app, where):
    (where / ".ucatlas" / "sites").mkdir(parents=True)
    assert app.site_file("../../databrickscfg").status == 404


def test_the_diff_page_runs_ucmodel_diff(where):
    app = App(Path("snapshots"), None, Path(".ucatlas"), runner=run_tool, token=TOKEN)
    old, new = names(where)
    page = app.diff(old, new)
    assert f"ucmodel diff --format json snapshots/{old} snapshots/{new}" in page
    assert "No differences." in page


# The guards, over HTTP --------------------------------------------------------------------


@pytest.fixture
def server(app):
    httpd = make_server(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app
    httpd.shutdown()
    httpd.server_close()


def request(app, method, path, host="127.0.0.1", cookie=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", app.port)
    headers = {"Host": f"{host}:{app.port}"}
    if cookie:
        headers["Cookie"] = f"ucatlas={cookie}"
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body and urlencode(body), headers=headers)
    r = conn.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


def test_without_the_token_nothing_is_served(server):
    assert request(server, "GET", "/")[0] == 403
    assert request(server, "GET", "/", cookie="wrong")[0] == 403


def test_the_token_becomes_a_strict_cookie(server):
    status, headers, _ = request(server, "GET", f"/?token={TOKEN}")
    assert status == 303 and headers["Location"] == "/"
    assert headers["Set-Cookie"] == f"ucatlas={TOKEN}; HttpOnly; SameSite=Strict; Path=/"
    assert request(server, "GET", "/", cookie=TOKEN)[0] == 200


def test_another_host_name_is_refused(server):
    """DNS rebinding: a page on another name that resolves here."""
    assert request(server, "GET", f"/?token={TOKEN}", host="attacker.example")[0] == 403


def test_a_form_needs_the_token_as_well_as_the_cookie(server, where):
    body = {"action": "allow", "args": "drop_table shop.orders", "snapshot": names(where)[0]}
    assert request(server, "POST", "/run", cookie=TOKEN, body=body)[0] == 403
    assert server.runner.ran == []
    status, headers, _ = request(server, "POST", "/run", cookie=TOKEN,
                                 body={**body, "token": TOKEN})
    assert status == 303 and headers["Location"].startswith("/jobs/")
    job = server.jobs[headers["Location"].removeprefix("/jobs/")]
    assert job.done.wait(5)
    assert server.runner.ran == [("ucstate", ["--project", "project", "allow", "drop_table",
                                              "shop.orders"])]
    assert "done" in request(server, "GET", headers["Location"], cookie=TOKEN)[2]


def test_a_refused_form_says_why(server):
    status, _, page = request(server, "POST", "/run", cookie=TOKEN,
                              body={"token": TOKEN, "action": "apply"})
    assert status == 400 and "the UI doesn&#x27;t run apply" in page


def test_the_profiles_come_from_the_config_file_default_included(where):
    assert serve.profiles() == {"DEFAULT": "https://a.example.com", "dev": "https://b.example.com"}
