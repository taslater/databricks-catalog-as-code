# databricks-catalog-as-code

Guidance for anyone, human or agent, working in this repo.

## What it is

Three Python packages in one uv workspace, under `packages/`:

| Package | Does |
| --- | --- |
| `ucmodel` | Reads a Unity Catalog over REST into a normalised model, compiles source DDL into the same model, and compares two models. Takes snapshots and reports on them (`ucmodel` command). No warehouse needed |
| `ucstate` | Plans and applies the changes that bring a catalog to its SQL source files (`ucstate` command) |
| `ucatlas` | Draws a catalog or a snapshot as a static site of ER diagrams and lineage graphs, writes an interactive explorer of a snapshot, and serves a local UI over the commands (`ucatlas` command). Needs Graphviz |

`ucstate` and `ucatlas` depend on `ucmodel` and not on each other. The UI
(`ucatlas serve`) runs `ucstate` as a subprocess when it is installed, and
never imports it. The README's first sections give the model the tools
follow: the spec, snapshots and reports, each with one writer. The design
is in `docs/design.md`, the offline compiler in `docs/offline.md`, the
diagrams in `docs/diagrams.md`, and the explorer in `docs/explorer.md`.

## Layout

```text
packages/ucmodel/src/ucmodel/    workspace, reader, normalise, model, compiler, sqltext, lineage,
                                 snapshot, report, progress, cli, samples/
packages/ucstate/src/ucstate/    project, intent, choices, render, shadow, plan, state, apply, cli
packages/ucstate/scenarios/      the planner's test corpus (before/, after/, expect.yml, recorded.json)
packages/ucstate/examples/       what `ucstate import` writes for ucmodel's samples
packages/ucatlas/src/ucatlas/    diagram, serve, sqllineage, explore, layout, explorer/, cli
packages/ucatlas/src/ucatlas/explorer/  the page, and d3 and elkjs, vendored unmodified
packages/ucatlas/tests/atlas_demo/  a synthetic catalog, its SQL and lineage, for the explorer
catalog/                         the sample project the workflows deploy (synthetic)
probes/                          scripts that check what a workspace supports
scripts/setup_workspace.py       creates the catalogs and CI principal a deploy needs but doesn't create
.github/workflows/               test, plan, apply, conformance
```

## Setup and checks

```bash
uv sync
uv run ruff check
uv run mypy
uv run --with pyright pyright
uv run pytest -q
```

Run all of these before a pull request. `uv run pytest packages/ucatlas` runs
one package. The `ucatlas` tests that run `dot` skip without Graphviz.

## Test conventions

- The test folders have no `__init__.py`. pytest and mypy import a test
  module by its file name, so **test file names must be unique across the
  three packages** (`test_atlas_cli.py`, not `test_cli.py`, in `ucatlas`).
- A test imports a helper by its bare name, as in `from scenarios import load`.
- **Each package must pass installed alone**, with only `ucmodel` beside it.
  Its tests may use only its own folder and `ucmodel`. Shared saved REST
  payloads live in `ucmodel.samples`, which ships in `ucmodel`'s wheel.
- A new check needs a known-bad control: an input that must fail it. A
  harness that passes the control is broken.
- `saved.SavedWorkspace` (in `packages/ucmodel/tests`) answers the reader's
  REST calls from a saved sample, so a whole read runs offline, and
  `reader.read` must give back the sample it was served.
- Library functions take `progress=` and write nothing without one. Only
  the command lines make a `Progress`, so a test sees progress only when it
  passes one or runs a command.

## Offline and live

The workspace is an oracle, asked on a budget, and what it answers is saved.
Most tests need no workspace.

- **Samples** (`ucmodel.samples`): raw REST payloads with the SQL that built
  them, for the reader and the normaliser. Save one again with
  `uv run python -m ucmodel.samples --profile P --catalog C --schema S > FILE`.
  It strips emails and storage paths.
- **Recordings** (`packages/ucstate/scenarios/<name>/recorded.json`): what a
  passing live scenario read, namely the target built from `after/`,
  `before/` with its seed rows, and each precheck's result.
- **Replays** (`test_scenario_replayed`): make the plans from a recording with
  no workspace and check them as the live test does. A recording goes stale
  when `before/`, `after/` or `seed.sql` change, and its replay fails until
  the scenario runs live again. A change to the planner is tested by the
  replays.
- **The compiler** (`ucmodel.compiler`) builds the desired model from the
  source for the statements it knows, and raises `Unsupported` for the rest.
  `plan`, `compare`, `transition` and `rehearse` compile first and build the
  **shadow** catalog only for what the compiler declines. `apply` always
  builds a shadow too, and stops if it disagrees with the compiler.
  `--shadow` forces the shadow. A compiler rule needs a pinned case, or the
  compiler declines the statement.
- **Oracles** (`test_compile_oracles.py`, `test_compiler.py`) compare compiled
  models with models the platform built, using `compare`.

## Live runs on a budget

Free Edition caps a metastore at 500 tables (views included) and 50 volumes.
**A dropped table counts for about 7 days.** Past the cap every `CREATE` fails
with `UC_RESOURCE_QUOTA_EXCEEDED`, and the shadow, `apply` and CI stop with it.
So live runs share a quota with everyone using the workspace: record once,
replay after, and narrow a run with `-k`.

```bash
UCSTATE_DRY_RUN=1 uv run pytest -m live packages/ucstate/tests   # estimate only, no credentials
UCSTATE_PROFILE=<profile> uv run pytest -m live -k add_columns packages/ucstate/tests
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `UCSTATE_PROFILE` | unset | Databricks CLI profile the live tests use |
| `UCSTATE_LIVE` | unset | `1` runs them with `DATABRICKS_*` variables instead, for CI |
| `UCSTATE_WAREHOUSE` | none | SQL warehouse id the live tests use |
| `UCSTATE_BUDGET` | 60 | most tables one run may create |
| `UCSTATE_RESERVE` | 250 | tables kept free for the workspace's owner |
| `UCSTATE_BUDGET_VOLUMES`, `UCSTATE_RESERVE_VOLUMES` | 5, 25 | the same for volumes |
| `UCSTATE_RECORD` | unset | `1` runs recorded scenarios again |
| `UCSTATE_DRY_RUN` | unset | `1` prints the estimate and the scenarios that would run, and creates nothing |

- `conftest.py` prints the estimate first and refuses a run when
  `used + created since the last count + N > limit - reserve`, or when N is over
  the budget. A run that creates nothing is never refused.
- A scenario whose recording is current is skipped.
- **The metastore's count lags.** `quota_count` refreshes only every 45 to 50
  minutes, so runs in a row can each see the old count. A run that starts is
  written to a ledger, `.ucstate/live-runs.jsonl` (ignored by git), with its
  start and, at the end, its `ended` line. The guard adds what runs since the
  last refresh created. The ledger is local, so a run elsewhere isn't in it.
- The `conformance` workflow runs the scenarios with a missing or stale
  recording (Mondays, or by hand) and uploads changed recordings as an
  artifact. Commit them through a pull request.

## Warehouse and CI settings

- `ucstate` takes the SQL warehouse from `--warehouse`, else
  `DATABRICKS_WAREHOUSE_ID`, else `warehouse_id` in `project.yml` (optional).
- Credentials: `--profile`, else `DATABRICKS_*` variables, else a one-line
  error (exit 2). **Never auto-select a Databricks CLI profile**: a command
  with no profile must fail rather than reach some workspace.
- CI reads the repository variables `DATABRICKS_HOST`,
  `DATABRICKS_WAREHOUSE_ID` and `DATABRICKS_CLIENT_ID`, and the secret
  `DATABRICKS_CLIENT_SECRET` (an OAuth M2M service principal). Without
  `DATABRICKS_HOST`, the `plan`, `apply` and `conformance` jobs skip, so a
  fork or a new copy of the repo runs only `test`.
- `apply` refuses a commit that didn't come through a merged pull request,
  so a direct push to `main` can't deploy, whether or not `main` is protected.
  Changes to `catalog/`, `packages/ucmodel/` and `packages/ucstate/` go
  branch, pull request, merge.

## Constraints

- **Only synthetic or public SQL and data**, in any file, commit, issue or
  table. Fixture emails are `owner@example.com`.
- **Never print secrets or tokens.** Samples and recordings are scrubbed by
  `ucmodel.samples.scrub`.
- **Snapshots aren't scrubbed:** they hold owners' names. `snapshots/` and
  `.ucatlas/` are ignored by git; tests make snapshots from the samples.
- Free Edition is for non-commercial use. It has serverless compute only,
  one 2X-Small warehouse, and a daily compute quota. Inactive accounts may be
  deleted, so keep nothing only in the workspace.
- Docs are written for outside readers: present tense, no diary, no workspace
  hosts or ids, no names of people.
- Keep `CLAUDE.md` as it is. It includes this file, and nothing
  Claude-specific belongs there.
