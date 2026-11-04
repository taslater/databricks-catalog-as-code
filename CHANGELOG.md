# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### ucmodel

- An object outside the model that has columns (a materialized view, a
  streaming table, a metric view, an external or foreign table) keeps its
  columns, its definition and its tags, and its pipeline's id in `meta`.
  It is still `unsupported`, and `ucstate` still leaves it alone. A diff
  between a snapshot from 0.1.0 and a later one shows these as changes.
- Lineage is read for those objects too.
- `snapshot` names them by type in what it prints ("1 materialized view").
- `samples.scrub` also replaces cloud storage URLs, which a pipeline's
  properties hold, and `anonymise` also replaces UUIDs written with
  underscores. A new sample, `kinds`, has one object of each kind.

### ucatlas

- The explorer draws materialized views, streaming tables, metric views and
  external and foreign tables, in every mode and on the map, each with its
  own icon and name. A materialized view's and a streaming table's lineage
  comes from their definitions and from what the platform recorded, and
  says when they were last written. A metric view's measures link to the
  columns they read, from its YAML, which needs PyYAML.
- A column edge in the platform's lineage to a column the object doesn't
  have (a materialized view's backing table's) is left out.

- When Graphviz is missing, the error names the program it needs and how to
  install Graphviz on this platform (Homebrew, winget, apt or dnf), or with
  conda anywhere.
- Graphviz is also looked for where its installers put it, so a PATH
  without Homebrew's folder (an editor started from the Dock, say) or a
  Windows install that didn't change the PATH still finds it.

## 0.1.0

The first release that isn't a pre-release. `ucmodel` and `ucatlas` are
beta and `ucstate` is alpha, as their classifiers say.

### ucmodel

- `snapshot` writes its progress to stderr: listing schemas, reading their
  objects, and reading table and column lineage. On a terminal that is one
  line drawn again in place, with an estimate of the time left; elsewhere,
  a line as each step starts and ends. `--quiet` turns it off.
- Schemas are listed eight at a time, as objects already were, and every
  object of every schema is read in one pool rather than schema by schema.
  What a read returns is unchanged.

### ucatlas

- `draw` and `explore` write their progress the same way: reading the
  catalog (`draw --catalog`), reading the SQL, laying out, and drawing.
  `--quiet` turns it off.
- Needs ucmodel 0.1.0.

### ucstate

- No changes. Released again so that the three packages keep one version.

## 0.1.0a2

### ucatlas

- `explore` writes one interactive page for a whole snapshot, with no
  workspace or warehouse. Column lineage comes from the views' definitions
  and from SQL files given with `--sql`, read with sqlglot, and from the
  lineage the snapshot recorded; each edge says which. Explore follows an
  object's lineage, down to how each column is computed. Compare lines up
  tables' columns by where their values come from, and writes the SQL
  checks that would confirm them. Map shows the catalog by schema or by
  table, finds how to join two tables, and lights up tables by search.
- Statements that write an object but that sqlglot can't read are listed as
  lineage gaps, and their targets marked.
- The layouts are worked out with Graphviz when the page is built, so a view
  keeps its objects in place as it changes, and the same snapshot always
  builds the same page.
- The explorer's page comes with the libraries it draws with, d3 7.9.0 and
  elkjs 0.9.3, so it works without internet access. Their licenses are in
  `THIRD-PARTY-NOTICES.txt`, which is written beside the page.
- Depends on sqlglot directly.

### ucmodel and ucstate

- No changes. Released again so that the three packages keep one version.

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
