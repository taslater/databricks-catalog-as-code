# Contributing

Thanks for looking. This file says how to set up, what to check, and what a
pull request needs. `AGENTS.md` has the same detail for coding agents, and
`docs/design.md` explains how the tool works.

## Setup

You need Python 3.11 or later and [uv](https://docs.astral.sh/uv/). For the
`ucatlas` tests that draw diagrams, install Graphviz (`brew install graphviz`
or `apt-get install graphviz`). Those tests skip without it.

```bash
uv sync
```

## Checks

Run all of these before you open a pull request. CI runs them too.

```bash
uv run ruff check
uv run mypy
uv run --with pyright pyright
uv run pytest -q
```

`uv run pytest packages/ucatlas` runs one package. None of these need
credentials: the live tests are skipped unless you ask for them.

## Tests

- The test folders have no `__init__.py`, so a test file's name must be unique
  across `packages/ucmodel`, `packages/ucstate` and `packages/ucatlas`. Helpers
  are imported by their bare module name.
- Each package must pass when installed alone with `ucmodel`. Its tests may use
  only its own folder and `ucmodel`.
- A new check needs a known-bad control, an input it must reject. A check that
  passes its control is broken.
- A change to the planner is tested by the replays of the recorded scenarios,
  which need no workspace. A change to the compiler is tested by its oracles.
  See `docs/offline.md`.

## No proprietary SQL

Every SQL statement, schema, table name and data value in this repo must be
synthetic or public. Don't paste SQL, schemas or data from your work or a
client into a file, test, issue, commit message or pull request, even
redacted. Use `owner@example.com` for email addresses in fixtures. Never
commit tokens, secrets or workspace credentials.

## Live tests

The live tests run against a real Databricks workspace, so you need your own.
A free [Databricks Free Edition](https://www.databricks.com/learn/free-edition)
workspace is enough.

```bash
UCSTATE_DRY_RUN=1 uv run pytest -m live packages/ucstate/tests   # estimate only
UCSTATE_PROFILE=<your profile> UCSTATE_WAREHOUSE=<warehouse id> \
  uv run pytest -m live -k add_columns packages/ucstate/tests
```

Free Edition caps a metastore at 500 tables, and a dropped table counts for
about 7 days. So live runs are on a budget. Before a run starts, the test
suite prints how many tables it will create and refuses the run if it is over
`UCSTATE_BUDGET` or would leave less than `UCSTATE_RESERVE` free. A scenario
with a current recording is skipped, because its replay already covers it.
Record a scenario once, commit its `recorded.json`, and replay it after that.
Run only the scenarios your change touches, with `-k`. `AGENTS.md` lists the
variables.

You don't need a workspace to contribute a planner or compiler change. If your
change needs a new answer from the platform, say so in the pull request, and a
maintainer with a workspace can record it.

## Pull requests

- Branch from `main` and open a pull request. `main` is deployed by CI from a
  merged pull request, so don't push to it directly.
- Keep a pull request to one change, with tests, and say what you ran.
- Changes to `catalog/`, `packages/ucmodel/` or `packages/ucstate/` trigger a
  plan of the sample project in CI. That needs the repository's credentials,
  which a fork doesn't have, so a maintainer may run it for you.
- Write docs in plain, present-tense sentences. Don't name workspaces,
  hosts or people.
- By contributing you agree that your work is released under the MIT licence.
