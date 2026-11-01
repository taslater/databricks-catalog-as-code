"""`ucatlas serve`: a local UI over the commands.

It is for the person who started it, on 127.0.0.1 only. Each page is made
from files that the commands write, and each button runs one command as a
subprocess. The page shows the command line, and its output when it ends.
So the UI does nothing a terminal can't, and anything it did can be done
again by hand. The commands it runs:

- `ucmodel snapshot`, the only one that reaches a workspace, and it only
  reads;
- `ucmodel diff` and `ucatlas draw --snapshot`, which read snapshot files;
- with `ucstate` installed and a project: `ucstate plan --against`, which
  reads files, and the commands that a plan lists as options (`rename-table`,
  `rename-column`, `allow` and `transition --against`). Each adds one entry
  or one file to the project, for review in git.

It never runs `apply`, or anything else that changes a catalog.

Every request needs the token in the address it prints, which it then keeps
in a cookie, and every form sends the token too. A request for any host name
but 127.0.0.1 or localhost is refused. So another web page can't drive it,
not even through DNS rebinding.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import mimetypes
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from ucmodel.snapshot import Snapshot, SnapshotError, load

from .diagram import STYLE

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CATALOG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.json")
ALLOWANCES = ("drop_table", "drop_view", "drop_function", "drop_volume", "drop_column")
CLASSES = {"auto": "#dcefd8", "checked": "#dbe8f7", "one-way": "#f7ecd0",
           "destructive": "#f6d5d2", "rewrite": "#e8dcf3", "ambiguous": "#fbe3c8",
           "script": "#ececec"}
PAGE_STYLE = STYLE + """
.cmd { background: #f6f6f6; border: 1px solid #ddd; padding: 0.4em 0.6em; margin: 0.4em 0;
  white-space: pre-wrap; }
.change { border: 1px solid #ddd; padding: 0.6em 1em; margin: 0.8em 0; }
.blocked { border-left: 4px solid #c0392b; }
.cls { padding: 0.1em 0.5em; border-radius: 3px; font: 12px Menlo, monospace; }
.option { border: 1px dashed #bbb; padding: 0.5em 0.8em; margin: 0.5em 0; }
.bad { color: #b03a2e; } .ok { color: #2e7d32; }
form.inline { display: inline; } label { margin-right: 1em; }
input[type=text] { width: 14em; }
"""


@dataclass
class Result:
    shown: str  # the command line, as typed in a terminal
    code: int | None = None  # the exit status, None while it runs
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0


Runner = Callable[[str, list[str]], Result]


def run_tool(tool: str, args: list[str]) -> Result:
    """Run `tool args` as `python -m tool args`, with the same Python as this server."""
    start = time.monotonic()
    done = subprocess.run([sys.executable, "-m", tool, *args], capture_output=True, text=True)
    return Result(shlex.join([tool, *args]), done.returncode, done.stdout, done.stderr,
                  time.monotonic() - start)


@dataclass
class Job:
    result: Result
    back: str  # where the page links when the command ends
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class Response:
    status: int
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class Refused(Exception):
    """A request the UI won't act on, with the reason shown to the person."""


class App:
    def __init__(self, snapshots: Path, project: Path | None, work: Path, *,
                 runner: Runner = run_tool, token: str | None = None,
                 has_ucstate: bool | None = None):
        self.snapshots = snapshots
        self.project = project
        self.work = work
        self.runner = runner
        self.token = token or secrets.token_urlsafe(18)
        self.has_ucstate = (importlib.util.find_spec("ucstate") is not None
                            if has_ucstate is None else has_ucstate)
        self.port = 0
        self.jobs: dict[str, Job] = {}
        self._loaded: dict[Path, tuple[float, Snapshot | str]] = {}

    # Inputs ---------------------------------------------------------------------------

    def listing(self) -> list[tuple[Path, Snapshot | str]]:
        """Each snapshot file, newest first, with its snapshot or why it won't load."""
        out = []
        for path in sorted(self.snapshots.glob("*.json"), key=lambda p: p.name):
            mtime = path.stat().st_mtime
            cached = self._loaded.get(path)
            if cached is None or cached[0] != mtime:
                try:
                    cached = (mtime, load(path))
                except SnapshotError as e:
                    cached = (mtime, str(e))
                self._loaded[path] = cached
            out.append((path, cached[1]))
        return sorted(out, key=lambda x: (not isinstance(x[1], str), _taken(x[1])), reverse=True)

    def snapshot(self, name: str) -> tuple[Path, Snapshot]:
        if not FILE.fullmatch(name or ""):
            raise Refused(f"{name!r} isn't a snapshot file name")
        for path, s in self.listing():
            if path.name == name:
                if isinstance(s, str):
                    raise Refused(s)
                return path, s
        raise Refused(f"no snapshot {name} in {self.snapshots}")

    def ready_project(self) -> Path:
        if not self.has_ucstate:
            raise Refused("ucstate isn't installed beside ucatlas, so there is no planner")
        if self.project is None:
            raise Refused("no project: start the UI in a folder with a project.yml, or pass "
                          "--project")
        return self.project

    # Commands -------------------------------------------------------------------------

    def ucstate(self, *args: str) -> list[str]:
        return ["--project", _rel(self.ready_project()), *args]

    def command(self, action: str, form: dict[str, str]) -> tuple[str, list[str], str]:
        """The tool, arguments and back link for a form, after checking every field."""
        if action == "snapshot":
            profile = form.get("profile", "")
            if profile not in profiles():
                raise Refused("choose a profile: the UI never picks one")
            catalog = _need(CATALOG, form.get("catalog", ""), "catalog")
            args = ["--profile", profile, "snapshot", "--catalog", catalog]
            for schema in re.split(r"[\s,]+", form.get("schemas", "").strip()):
                if schema:
                    args += ["--schema", _need(NAME, schema, "schema")]
            if not form.get("lineage"):
                args.append("--no-lineage")
            return "ucmodel", [*args, "-o", _rel(self.snapshots) + "/"], "/"
        back = "/preview?" + urlencode({"snapshot": form.get("snapshot", "")})
        values = form.get("args", "").split()
        if action == "rename-table" and len(values) == 2:
            return "ucstate", self.ucstate(action, *(_table(v) for v in values)), back
        if action == "rename-column" and len(values) == 3:
            names = [_table(values[0]), *(_need(NAME, v, "column") for v in values[1:])]
            return "ucstate", self.ucstate(action, *names), back
        if action == "allow" and len(values) == 2 and values[0] in ALLOWANCES:
            parts = 3 if values[0] == "drop_column" else 2
            name = _need(re.compile(r"\.".join([NAME.pattern] * parts)), values[1], "name")
            return "ucstate", self.ucstate(action, values[0], name), back
        if action == "transition" and len(values) == 1:
            path, _ = self.snapshot(form.get("snapshot", ""))
            key = _need(re.compile(rf"table:{NAME.pattern}\.{NAME.pattern}"), values[0], "table")
            return "ucstate", self.ucstate(action, "--object", key, "--against", _rel(path)), back
        raise Refused(f"the UI doesn't run {action} {' '.join(values)}".strip())

    def start(self, tool: str, args: list[str], back: str) -> str:
        job_id = secrets.token_hex(6)
        job = Job(Result(shlex.join([tool, *args])), back)
        self.jobs[job_id] = job

        def work() -> None:
            job.result = self.runner(tool, args)
            job.done.set()
        threading.Thread(target=work, daemon=True).start()
        return job_id

    # Pages ----------------------------------------------------------------------------

    def get(self, path: str, query: dict[str, str]) -> Response:
        if path == "/":
            return Response(200, self.home())
        if path == "/diff":
            return Response(200, self.diff(query.get("old", ""), query.get("new", "")))
        if path == "/preview":
            return Response(200, self.preview(query.get("snapshot", "")))
        if path == "/draw":
            return self.draw(query.get("snapshot", ""))
        if path.startswith("/jobs/"):
            job = self.jobs.get(path.removeprefix("/jobs/"))
            return Response(200, self.job(job)) if job else Response(404, _page("Not found", ""))
        if path.startswith("/sites/"):
            return self.site_file(path.removeprefix("/sites/"))
        return Response(404, _page("Not found", "<p>No such page.</p>"))

    def home(self) -> str:
        rows, choices = [], []
        for path, s in self.listing():
            if isinstance(s, str):
                rows.append(f"<tr><td>{escape(path.name)}</td>"
                            f'<td colspan="5" class="bad">{escape(s)}</td></tr>')
                continue
            name = quote(path.name)
            actions = [f'<a href="/draw?snapshot={name}">Draw</a>']
            if self.has_ucstate and self.project is not None:
                actions.append(f'<a href="/preview?snapshot={name}">Plan the spec against it</a>')
            rows.append(f"<tr><td>{escape(path.name)}</td><td>{escape(s.catalog)}</td>"
                        f"<td>{s.taken_at:%Y-%m-%d %H:%M} UTC</td>"
                        f"<td>{escape(', '.join(s.schemas))}</td><td>{len(s.model)}</td>"
                        f"<td>{' · '.join(actions)}</td></tr>")
            choices.append(f'<option value="{escape(path.name)}">{escape(s.describe())}</option>')
        table = (f'<table class="list"><tr><th>File</th><th>Catalog</th><th>Taken</th>'
                 f"<th>Schemas</th><th>Objects</th><th></th></tr>{''.join(rows)}</table>"
                 if rows else f"<p>No snapshots in {escape(_rel(self.snapshots))} yet.</p>")
        compare = ""
        if len(choices) > 1:
            older = "".join(choices[1:] + choices[:1])  # the second newest, then the rest
            compare = ("<h2>Compare two snapshots</h2>"
                       f'<form action="/diff">From <select name="old">{older}</select> '
                       f'to <select name="new">{"".join(choices)}</select> '
                       '<button>Compare</button></form>'
                       f"<p class=\"dim\">Runs {_code('ucmodel diff OLD NEW')}.</p>")
        options = "".join(f'<option value="{escape(n)}">{escape(n)} ({escape(host)})</option>'
                          for n, host in profiles().items())
        take = ("<h2>Take a snapshot</h2>"
                f'<form method="post" action="/run">{self._token_field()}'
                '<input type="hidden" name="action" value="snapshot">'
                '<label>Profile <select name="profile" required>'
                f'<option value="" selected disabled>choose one</option>{options}</select></label>'
                '<label>Catalog <input type="text" name="catalog" required></label>'
                '<label>Schemas <input type="text" name="schemas" '
                'placeholder="all but information_schema"></label>'
                '<label><input type="checkbox" name="lineage" value="1" checked> lineage</label>'
                "<button>Take it</button></form>"
                '<p class="dim">Runs <code>ucmodel snapshot</code>. It reads the catalog over '
                "REST, with no warehouse: about three calls per table, and with lineage one more "
                "per table and per column.</p>")
        if not self.has_ucstate:
            project = "<p class=\"dim\">ucstate isn't installed, so there are no plan " \
                      "previews.</p>"
        elif self.project is None:
            project = ("<p class=\"dim\">No project: start the UI in a folder with a "
                       "project.yml, or pass --project, to plan the spec against a snapshot.</p>")
        else:
            project = (f"<p>Project: <code>{escape(_rel(self.project))}</code>. A plan preview "
                       "compiles its spec and plans it against a snapshot, with no workspace."
                       "</p>")
        intro = ("<p>Each button runs one command and shows it, so you can run it yourself "
                 "instead. Only <code>ucmodel snapshot</code> reaches a workspace, and it only "
                 "reads. Nothing here changes a catalog.</p>")
        body = (f"<h1>Snapshots</h1>{intro}{project}{table}{compare}{take}")
        return _page("ucatlas", body)

    def diff(self, old: str, new: str) -> str:
        (a, _), (b, _) = self.snapshot(old), self.snapshot(new)
        result = self.runner("ucmodel", ["diff", "--format", "json", _rel(a), _rel(b)])
        ran = _ran(result, f"In a terminal: ucmodel diff {shlex.join([_rel(a), _rel(b)])}")
        if result.code not in (0, 1):
            return _page("Compare", ran + _output(result))
        report = json.loads(result.stdout)
        parts = [f"<h1>{escape(old)} to {escape(new)}</h1>", ran]
        if not (report["added"] or report["removed"] or report["changed"]):
            parts.append("<p>No differences.</p>")
        for word in ("added", "removed"):
            if report[word]:
                parts.append(f"<h2>{word.capitalize()}</h2><ul>"
                             + "".join(f"<li><code>{escape(k)}</code></li>" for k in report[word])
                             + "</ul>")
        if report["changed"]:
            parts.append("<h2>Changed</h2>")
            for key, diffs in report["changed"].items():
                rows = "".join(f"<tr><td><code>{escape(d['path'])}</code></td>"
                               f"<td>{_value(d['old'])}</td><td>{_value(d['new'])}</td></tr>"
                               for d in diffs)
                parts.append(f"<h3>{escape(key)}</h3><table class=\"list\"><tr><th>What</th>"
                             f"<th>Before</th><th>After</th></tr>{rows}</table>")
        return _page("Compare", "".join(parts))

    def draw(self, name: str) -> Response:
        path, s = self.snapshot(name)
        site = self.work / "sites" / s.hash[:12]
        target = f"/sites/{s.hash[:12]}/index.html"
        if (site / "index.html").exists():
            return Response(303, headers={"Location": target})
        result = self.runner("ucatlas", ["draw", "--snapshot", _rel(path), "-o", _rel(site)])
        if result.code != 0:
            return Response(200, _page("Draw", _ran(result) + _output(result)))
        return Response(303, headers={"Location": target})

    def site_file(self, rest: str) -> Response:
        root = (self.work / "sites").resolve()
        path = (root / rest).resolve()
        if root not in path.parents or not path.is_file():
            return Response(404, _page("Not found", ""))
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return Response(200, path.read_text(), {"Content-Type": f"{kind}; charset=utf-8"})

    def preview(self, name: str) -> str:
        path, s = self.snapshot(name)
        out = self.work / "plans" / f"{s.hash[:12]}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        result = self.runner("ucstate", self.ucstate("plan", "--against", _rel(path),
                                                     "-o", _rel(out)))
        ran = _ran(result)
        if result.code not in (0, 1) or not out.exists():
            return _page("Plan preview", ran + _output(result))
        plan = json.loads(out.read_text())
        changes, blocked = plan["changes"], [c for c in plan["changes"] if c["blocked"]]
        head = (f"<h1>The spec, planned against {escape(s.describe())}</h1>{ran}"
                f"<p>{len(changes)} change{'s' if len(changes) != 1 else ''}"
                + (f', <span class="bad">{len(blocked)} blocked</span>' if blocked else "")
                + ". This is a preview: the prechecks haven't run, and every refactor.yml entry "
                "and script counts as not run yet. To apply, plan against the target.</p>")
        parts = [head] + [self._change(i, c, name) for i, c in enumerate(changes, 1)]
        if plan["notes"]:
            parts.append("<h2>Left alone</h2><ul>"
                         + "".join(f"<li>{escape(n)}</li>" for n in plan["notes"]) + "</ul>")
        return _page("Plan preview", "".join(parts))

    def _change(self, i: int, c: dict, snapshot: str) -> str:
        colour = CLASSES.get(c["cls"], "#eee")
        out = [f'<div class="change{" blocked" if c["blocked"] else ""}">'
               f'<b>{i}.</b> <span class="cls" style="background:{colour}">{escape(c["cls"])}'
               f"</span> <code>{escape(c['key'])}</code>: {escape(c['what'])}"]
        if c["precheck"]:
            out.append(f"<p class=\"dim\">Precheck, not run in a preview: "
                       f"<code>{escape(c['precheck'])}</code></p>")
        if c["statements"]:
            out.append(f"<pre>{escape(chr(10).join(s + ';' for s in c['statements']))}</pre>")
        if c["blocked"] and c.get("options"):
            out.append('<p class="bad">Blocked. Each command below adds to the project what '
                       "would let a plan go on:</p>")
        elif c["blocked"]:
            out.append(f'<p class="bad">Blocked:</p><pre>{escape(c["blocked"])}</pre>')
        for o in c.get("options", []):
            action, *args = o["command"]
            if action == "transition":  # the UI adds the snapshot it previews
                args = [args[args.index("--object") + 1]]
            fields = {"args": " ".join(args), "snapshot": snapshot}
            try:
                tool, argv, _ = self.command(action, fields)
            except Refused as e:
                button = f'<p class="bad">{escape(str(e))}</p>'
            else:
                hidden = "".join(f'<input type="hidden" name="{k}" value="{escape(v)}">'
                                 for k, v in {"action": action, **fields}.items())
                button = (f'<form method="post" action="/run">{self._token_field()}{hidden}'
                          f"<code>{escape(shlex.join([tool, *argv]))}</code> "
                          "<button>Run it</button></form>")
            out.append(f'<div class="option"><b>{escape(o["label"].capitalize())}.</b> '
                       f"Adds to <code>{escape(o['file'])}</code>:<pre>{escape(o['adds'])}</pre>"
                       f"{button}</div>")
        out.append("</div>")
        return "".join(out)

    def job(self, job: Job) -> str:
        r, done = job.result, job.done.is_set()
        refresh = "" if done else '<meta http-equiv="refresh" content="1">'
        status = ("running" if not done else
                  '<span class="ok">done</span>' if r.code == 0 else
                  f'<span class="bad">exit {r.code}</span>')
        body = (f"{refresh}<h1>{escape(r.shown.split()[0])}</h1>"
                f'<div class="cmd">$ {escape(r.shown)}</div><p>{status}'
                + (f", {r.seconds:.1f} s" if done else "") + "</p>"
                + (_output(r) if done else "")
                + (f'<p><a href="{escape(job.back)}">Back</a></p>' if done else ""))
        return _page("Running", body)

    def post(self, path: str, form: dict[str, str]) -> Response:
        if path != "/run":
            return Response(404, _page("Not found", ""))
        if not secrets.compare_digest(form.get("token", ""), self.token):
            return Response(403, _page("Refused", "<p>The form's token is wrong.</p>"))
        tool, args, back = self.command(form.get("action", ""), form)
        return Response(303, headers={"Location": f"/jobs/{self.start(tool, args, back)}"})

    def _token_field(self) -> str:
        return f'<input type="hidden" name="token" value="{escape(self.token)}">'


def profiles() -> dict[str, str]:
    """The Databricks CLI profiles in the config file, each with its host."""
    path = Path(os.environ.get("DATABRICKS_CONFIG_FILE", "~/.databrickscfg")).expanduser()
    config = configparser.ConfigParser(default_section="\0", interpolation=None)
    try:
        config.read(path)
    except configparser.Error:
        return {}
    return {name: config[name].get("host", "") for name in config.sections()}


def _taken(s: Snapshot | str) -> str:
    return "" if isinstance(s, str) else s.taken_at.isoformat()


def _need(pattern: re.Pattern, value: str, what: str) -> str:
    if not pattern.fullmatch(value):
        raise Refused(f"{value!r} isn't a {what} name")
    return value


def _table(value: str) -> str:
    return _need(re.compile(rf"{NAME.pattern}\.{NAME.pattern}"), value, "table")


def _rel(path: Path) -> str:
    """The path from the current folder, as the commands take it."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _code(text: str) -> str:
    return f"<code>{escape(text)}</code>"


def _ran(r: Result, also: str = "") -> str:
    status = "" if r.code in (0, 1) else f' <span class="bad">exit {r.code}</span>'
    extra = f'<br><span class="dim">{escape(also)}</span>' if also else ""
    return f'<div class="cmd">$ {escape(r.shown)}{status}{extra}</div>'


def _output(r: Result) -> str:
    parts = [f"<pre>{escape(text)}</pre>" for text in (r.stdout, r.stderr) if text.strip()]
    return "".join(parts) or '<p class="dim">No output.</p>'


def _value(v: object) -> str:
    text = json.dumps(v, indent=1) if isinstance(v, dict | list) else json.dumps(v)
    if len(text) > 120 or "\n" in text:
        return f"<details><summary><code>{escape(text[:60])}…</code></summary>" \
               f"<pre>{escape(text)}</pre></details>"
    return f"<code>{escape(text)}</code>"


def _page(title: str, body: str) -> str:
    return (f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
            f"<title>{escape(title)}</title><style>{PAGE_STYLE}</style></head>\n"
            f'<body><nav><a href="/">Snapshots</a></nav>\n{body}\n</body></html>\n')


class Handler(BaseHTTPRequestHandler):
    app: App  # set on the subclass that `make_server` makes

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def _serve(self, method: str) -> None:
        app = self.app
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            return self._send(Response(403, "refused: the UI answers only on 127.0.0.1"))
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        known = "ucatlas" in jar and secrets.compare_digest(jar["ucatlas"].value, app.token)
        if method == "GET" and secrets.compare_digest(query.get("token", ""), app.token):
            rest = {k: v for k, v in query.items() if k != "token"}
            return self._send(Response(303, headers={
                "Location": url.path + (f"?{urlencode(rest)}" if rest else ""),
                "Set-Cookie": f"ucatlas={app.token}; HttpOnly; SameSite=Strict; Path=/"}))
        if not known:
            return self._send(Response(403, _page("Refused", "<p>Open the address that "
                                                  "<code>ucatlas serve</code> printed, with "
                                                  "its token.</p>")))
        try:
            if method == "GET":
                response = app.get(url.path, query)
            else:
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                response = app.post(url.path, form)
        except Refused as e:
            response = Response(400, _page("Refused", f'<p class="bad">{escape(str(e))}</p>'))
        self._send(response)

    def _send(self, r: Response) -> None:
        body = r.body.encode()
        self.send_response(r.status)
        headers = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                   "Referrer-Policy": "no-referrer", **r.headers}
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def make_server(app: App, port: int) -> ThreadingHTTPServer:
    handler = type("AppHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    app.port = server.server_address[1]
    return server


def serve(snapshots: Path, project: Path | None, port: int, work: Path) -> int:
    if project is None and Path("project.yml").exists():
        project = Path(".")
    if project is not None and not (project / "project.yml").exists():
        print(f"ucatlas: {project / 'project.yml'} not found", file=sys.stderr)
        return 2
    snapshots.mkdir(parents=True, exist_ok=True)
    app = App(snapshots, project, work)
    server = make_server(app, port)
    print(f"ucatlas serve: open http://127.0.0.1:{app.port}/?token={app.token}\n"
          "It answers only on this machine. Ctrl-C stops it.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
