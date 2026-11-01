# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.1.0a1

The first release, an alpha. It has been tested only on Databricks Free
Edition.

### ucmodel

- Reads a Unity Catalog over REST into a normalised model, with no SQL
  warehouse: schemas, managed tables, views, functions and volumes, with
  columns, defaults, generated and identity columns, constraints, clustering,
  masks, row filters, comments, tags and grants.
- Compares two models and hashes an object. `compare(..., exact=True)` counts
  every property and clustering on both sides, for comparing two catalogs.
- Compiles source DDL into the same model, offline, for the statements it
  knows, and declines the rest.
- Reads table and column lineage from the lineage API.
- A `ucmodel` command. `snapshot` reads a catalog over REST into a snapshot
  file, which holds a hash of its contents and is never replaced. `diff`
  reports what was added, removed and changed between two snapshots, as
  text, markdown or JSON.
- Ships saved REST payloads as samples for offline tests.

### ucstate

- Imports a catalog's live state as SQL source files, one object per file.
- Plans the changes that bring a target catalog to its source, and classes each
  one as `auto`, `checked`, `one-way`, `destructive`, `rewrite` or `ambiguous`.
- Runs `checked` changes after a precheck, `one-way` changes when the source
  declares the table property they need, and `destructive` changes only with an
  allowance in `project.yml`.
- Renames through `refactor.yml`, and hand-written transition scripts and
  one-time scripts, recorded with checksums.
- Applies a plan under a lock, refuses a plan whose target has changed since it
  was made, verifies the result, and records each deployment and the deployed
  model in the target's `_ucstate` schema.
- Rehearses a plan on clones of the target's tables before applying it.
- Builds the desired model with the local compiler, and with a shadow catalog
  for what the compiler declines. `apply` cross-checks the two.
- `plan`, `compare` and `transition` take `--against` a snapshot, with no
  workspace. `apply` refuses a plan made this way.
- A blocked change lists the commands that would unblock it, in the plan's
  text and JSON. `rename-table`, `rename-column` and `allow` each add one
  entry to `refactor.yml` or `project.yml`, keeping the file's comments.
- Commands: `import`, `model`, `shadow`, `compare`, `plan`, `apply`, `rehearse`,
  `transition`, `history`, `unlock`, `rename-table`, `rename-column` and `allow`.
- A scenario corpus of 34 before-and-after cases, with recordings that replay
  offline, and live tests that run on a budget against the metastore's quota.

### ucatlas

- Draws a catalog's schemas as a static site: an ER diagram from declared keys
  and view reads, a lineage graph per schema, and column lineage on each
  table's page. Reads over REST and the lineage API, or from a snapshot with
  no workspace, and needs Graphviz.
- `serve`, a local UI over the commands, on 127.0.0.1 with a token. It lists,
  compares, draws and takes snapshots, and previews plans with each blocked
  change's options as buttons. Each button runs one command and shows it.
- Commands: `draw` and `serve`.

### Repository

- GitHub Actions workflows to test, plan on a pull request, apply on a merged
  pull request, and run the live conformance tests.
- A sample project, `catalog/`, and a script to set up the catalogs and the
  service principal it deploys with.
