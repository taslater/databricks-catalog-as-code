# Design

How `ucstate` turns SQL source files into changes to a Unity Catalog. See also
[offline.md](offline.md) (the compiler) and [diagrams.md](diagrams.md).

## Overview

The desired state is Databricks SQL, one object per file. One reader turns a
catalog into a normalised model, and the source is turned into the same model,
so the comparison is between two things of one shape. Every difference becomes
a change with a class, and a change runs only when it has what its class
needs. Apply runs the statements one by one under a lock, verifies the result
and records the deployment.

```text
src/ ──compile (or shadow build)──► desired model ─┐
                                                   ├─► compare ─► plan ─► apply ─► verify ─► record
target catalog ──► read + normalise ──► live model ┘
```

## 1. The source

The folder holds `project.yml`, `src/<schema>/schema.sql` and one `.sql` file
per object under `src/<schema>/tables/`, `views/`, `functions/` and
`volumes/`. Beside them are `refactor.yml` (renames), `migrations/<id>.sql`
(transition scripts) and `migrations/once/<id>.sql` (one-time scripts).

A source file starts with one plain `CREATE` for the object its path names,
with no `OR REPLACE` and no `IF NOT EXISTS`, because the planner decides how
to reach that state. It may continue with statements about the same object:
`COMMENT ON`, `SET TAGS`, `SET MASK`, `GRANT` and constraints as `ALTER TABLE`.
Names inside the project are two-part (`sales.orders`), so the same files build
the shadow, `dev` and `prod`. Tool metadata goes in comments:
`-- ucstate: rebuildable` marks a `CREATE TABLE ... AS SELECT` whose data can
be recomputed.

```yaml
# project.yml
warehouse_id: <id>                  # optional; --warehouse and DATABRICKS_WAREHOUSE_ID win
shadow: {catalog: ucstate_shadow}   # its schemas are dropped on every build
schemas: [sales, ref]               # the managed scope
targets:
  dev:  {catalog: my_dev}
  prod: {catalog: my_prod}
policy: {unmanaged_objects: report} # report | drop
allow: [{drop_table: sales.old_orders}]
rehearsal: {catalog: ucstate_rehearsal}
```

The shadow and rehearsal catalogs must differ from every target. `_ucstate`
holds the tool's records and can't be a managed schema.

In scope: schemas; managed Delta tables, views, SQL and Python functions and
managed volumes; comments, tags and grants; PK, FK and CHECK constraints;
column masks and row filters. Out of scope: the data in tables, materialized
views and streaming tables, external locations and credentials, and jobs and
pipelines. One tool owns each object in scope.

## 2. The model

One JSON document, keyed by logical name such as `table:sales.orders`, with the
catalog removed. A table holds its columns, constraints, clustering,
partitions, declared properties, tags, grants, row filter and comment. A view
holds its text, column comments and schema-binding mode, and a function its
signature, body and language. The live model adds `table_id`.

An object of a type the tools don't manage (a materialized view, a streaming
table, a metric view, an external or foreign table, an external volume) is
kept as `unsupported`, with its type. A table-like one keeps its columns
(name, type, nullability, comment, tags), its definition when it has one (a
metric view's is YAML), and its tags, and its pipeline's id in `meta`. It
keeps no properties, since a pipeline's change at every refresh. `ucstate` never
plans a change to one; reports and the drawings show them.

`ucmodel.reader` reads a catalog over REST with no warehouse. The table
endpoint carries defaults, generation expressions and identity in each column's
`type_json` metadata, CHECKs as `delta.constraints.<name>` properties, and
masks and row filters in their own fields.

`ucmodel.normalise` makes two reads comparable:

- Catalog names are stripped, including from FK parent tables, and types come
  from `type_text`, because `type_precision` and `type_scale` read 0 for a
  `DECIMAL(10, 2)`.
- **Properties are declared-only:** only the keys the source names are
  compared. A table carries about 60 system-managed properties and a view about
  150 captured `view.sqlConfig.*` entries.
- When `clusterByAuto` is true the platform chose the keys, so keys are
  compared only when the source names them.
- Platform-added metadata is stripped: column mapping ids, type-change history,
  the identity high-water mark and the duplicate `comment`.
- A key-only tag reads back as `''`, so `('flag')` equals `('flag' = '')`. View
  text is compared with whitespace and a trailing semicolon normalised.

`ucmodel.model.compare` lists the differences between two models, and
`object_hash` hashes one object. The planner compares the desired model with
the live one. Apply records each deployed model in `_ucstate.state`, but
planning is two-way: nothing reads the record back. `ucstate compare` exits 1
if there are differences, and is also the criterion for the compiler and for
verify.

## 3. Changes and their classes

| Class | Meaning | Runs |
| --- | --- | --- |
| `auto` | Metadata only. Can't lose data or fail on data | Always |
| `checked` | Metadata only, but fails if rows violate it | After a precheck query finds none |
| `one-way` | Upgrades a Delta feature, with no easy way back | When the source declares the property it needs |
| `destructive` | Loses data | With an `allow:` entry naming the object |
| `rewrite` | Needs the data copied into a new layout | Through a transition script |
| `ambiguous` | The diff can't tell the intent, such as rename versus drop and add | Never; `refactor.yml` or an allowance turns it into another change |

A change that can't run says why in `blocked`. One blocked change blocks the
whole plan, and `plan` exits 1.

| Change | Class | Generated SQL |
| --- | --- | --- |
| New object | auto | The source file's own statements |
| Nullable column added; column order changed | auto | `ADD COLUMN ... AFTER x`; `ALTER COLUMN c AFTER x` or `FIRST` |
| Comments, tags, declared properties, grants, masks, row filters, clustering | auto | `COMMENT ON`, `SET TAGS`, `SET TBLPROPERTIES`, `GRANT`/`REVOKE`, `SET MASK`, `SET ROW FILTER`, `CLUSTER BY` |
| View or function body changed | auto | `CREATE OR REPLACE` from the file, then its other statements again, because a replace drops grants and tags |
| PK or FK changed | auto | `ADD` or `DROP CONSTRAINT`. A PK with incoming FKs can't be dropped, so those FKs are dropped first and added back |
| NOT NULL, default or CHECK dropped | auto | `ALTER` |
| NOT NULL added | checked | Precheck `count_if(c IS NULL)`, then `SET NOT NULL` |
| CHECK added | checked | Precheck `count_if(NOT coalesce((expr), false))`: in Delta a NULL violates a CHECK |
| NOT NULL column with a default added | checked | Add nullable, `SET DEFAULT`, backfill with `UPDATE`, `SET NOT NULL`. Delta refuses the one-statement form |
| First column default | one-way | Needs `delta.feature.allowColumnDefaults` |
| Type widened | one-way | Needs `delta.enableTypeWidening`, then `ALTER COLUMN ... TYPE` |
| Column renamed or dropped | one-way, plus `refactor.yml` or an allowance | Needs `delta.columnMapping.mode = 'name'` |
| Table renamed | `refactor.yml` | `RENAME TO`, then dependent views are replaced, because views over the old name break |
| Table, view, function, volume or column dropped | destructive | `DROP`; `UNDROP` reverses a table within the retention window |
| Type narrowed, partitioning changed, identity or generated column added | rewrite | A transition script |
| A column or table disappears and one of the same shape appears | ambiguous | Blocked; the plan prints the entry it suspects |
| Any change to a `rebuildable` table | auto | `CREATE OR REPLACE TABLE ... AS SELECT`; the `ucstate.rebuild` property holds the statement's checksum, so a new query shows as a change |

**One-way changes are declared, not inferred.** If a rename needs column
mapping and the file doesn't declare it, the plan stops and asks for that line.

**Allowances** (`drop_table`, `drop_view`, `drop_function`, `drop_volume`,
`drop_column`) permit a change, and the plan still checks that the object
exists. An allowance no change used is noted, so it can be deleted.

**Unmanaged objects**, in a managed schema but not in the source, follow
`policy.unmanaged_objects`: `report` lists and ignores them, `drop` drops views
and functions and treats anything else as `destructive`.

**Renames** are entries in `refactor.yml`, applied once per target and
recorded in `_ucstate.refactor_log`:

```yaml
- id: customer-full-name
  rename_column: {table: sales.customers, from: name, to: full_name}
- id: order-headers
  rename_table: {from: sales.orders, to: sales.order_headers}
```

If the old name exists and the new one doesn't, it renames. If the new name
exists, it is marked applied. If neither exists, it is skipped. Renames are
applied to a copy of the live model before the diff, so the rest of the plan
sees the new names.

**A plan runs in phases:** one-time `pre` scripts; schemas; renames; FK drops;
PK and CHECK drops; every other create, replace and alter in dependency order,
with FKs added back; drops of objects the source no longer has; one-time
`post` scripts. The planner never uses `CASCADE`, and a difference that no
rule plans for is a `PlanError`, not a silent gap.

## 4. Building the desired model

**The compiler** (`ucmodel.compiler`, [offline.md](offline.md)) parses DDL
structure with sqlglot's tokens, keeps expressions as written and applies
pinned normalisation rules, with no workspace. It raises `Unsupported` for
anything it doesn't model.

**The shadow** applies the source to an empty catalog and reads the result back
with the same reader as the target, so Databricks does the parsing and
normalisation is symmetric. Statements run with the catalog and schema set on
the request, so there is no templating. sqlglot only orders them: views after
the tables they read, and a file it can't parse is retried until a pass makes
no progress. A missing FK parent or table fails with a not-found error and
creates nothing, so retry is safe. The shadow catalog is created only if it is
missing, because even `CREATE CATALOG IF NOT EXISTS` needs the privilege.

| Mode | Used by | Behaviour |
| --- | --- | --- |
| `auto` | `plan`, `compare`, `transition`, `rehearse` | Compile; build the shadow only if the compiler declines |
| `checked` | `apply` | Compile and build the shadow; stop with the differences (`CompilerDisagrees`, exit 1, nothing run) unless `compare` finds none |
| `shadow` | `--shadow`, `ucstate shadow` | Always the shadow |

## 5. Hand-written SQL

Some intent can't be read from two end states.

**Transition scripts** (`migrations/<id>.sql`) replace the generated statements
for one table's change. `ucstate transition --target T --object S.TABLE`
writes one with its hashes and a starting point.

```sql
-- ucstate: transition
-- id: split-customer-name
-- object: sales.customers
-- from: 3f9a1c...
-- to:   b71e04...
ALTER TABLE sales.customers ADD COLUMNS (first_name STRING, last_name STRING);
UPDATE sales.customers SET first_name = split_part(name, ' ', 1);
ALTER TABLE sales.customers DROP COLUMN name;
```

- It runs only when the live table's hash equals `from`. `to` is the desired
  object's hash; live objects carry undeclared properties, so landing is
  checked by comparing with the desired model, not by hashing the live table.
- The plan blocks a script whose `from` the table no longer has, or whose `to`
  the source no longer declares. A table that doesn't exist yet is created as
  declared, and the script is recorded as skipped.
- A script that ran is recorded in `_ucstate.migration_history` with a
  checksum. Editing it afterwards is a `PlanError`.

**One-time scripts** (`migrations/once/<id>.sql`) are data fixes unrelated to a
schema change. Each runs once per target, at the phase its header declares
(`pre` or `post`). **Prechecks** run at plan time and again at apply time,
before any `pre` script, so a data fix a precheck needs goes in an earlier
deploy.

## 6. Apply

`ucstate apply --target T [--plan FILE] [--yes]`:

- **Lock.** The lock is the table `_ucstate.lock`. Creating a table is atomic,
  so of two runs one gets it. Its comment is JSON naming the holder. Apply
  drops it when it finishes, SIGTERM releases it, and `ucstate unlock` drops
  one left by a run that died.
- **Fingerprint.** A plan records an `object_hash` of every live object a
  change touches. Apply reads them again and refuses if any changed, for a
  fresh plan or one saved with `plan -o`.
- **Statements run one by one,** because DDL is atomic per statement and
  `BEGIN ATOMIC` takes only DML. A failure leaves the target partly migrated,
  and planning again finds only what is left.
- **Verify.** The target is read and compared with the plan's desired model,
  ignoring what the plan leaves alone. No difference means planning again
  would find nothing.
- **Record.** `_ucstate` in the target catalog (the catalog itself must exist)
  holds `deployments` (a row per apply, with each statement's timing and error
  and each touched table's Delta versions), `state` (a row per object per
  deployment; a failure leaves the previous deployment's rows current),
  `refactor_log` and `migration_history`.
- **No generated down-migration.** `RESTORE TABLE ... TO VERSION AS OF` with
  the recorded versions rolls back data and schema within the retention
  window, but not a protocol upgrade, and `UNDROP` restores a dropped table.

**Rehearsal.** When a plan has a `checked`, `rewrite` or script change and
`project.yml` names a rehearsal catalog, apply first runs the plan on clones of
the target's tables there, verifies it, and only then runs it for real. The
empty shadow can't test a migration on data; this can. `ucstate rehearse` runs
it alone. Every table is cloned, so keys and views have what they need. Tables
the plan changes are deep clones and the rest shallow, because a shallow clone
keeps its PK through `CREATE OR REPLACE ... AS SELECT` where the table loses
it, and a script that drops the PK could pass on one and fail for real.

## 7. CI

[`.github/workflows/`](../.github/workflows/):

- **`test`:** ruff, mypy and the offline tests. No secrets.
- **`plan`:** on pull requests that touch the deployed paths, plans `prod` and
  writes the plan to the job summary. A blocked change fails the check. It
  compiles first, so it creates no tables unless the compiler declines a file.
- **`apply`:** on pushes to `main` touching the same paths, and by hand. It
  refuses a commit that didn't come through a merged pull request, runs the
  offline tests, then `ucstate apply --target prod --yes`. The check holds
  whatever the repository's branch protection, which a free GitHub plan
  doesn't offer on a private repository.
- **`conformance`:** live tests that save the platform's answers, weekly and by
  hand, uploading changed recordings as an artifact.
- Plans and applies have separate concurrency groups and shadow catalogs, so a
  queued plan can't cancel a pending apply.
- **Authentication** is a service principal with OAuth M2M, from the repository
  variables `DATABRICKS_HOST`, `DATABRICKS_WAREHOUSE_ID` and
  `DATABRICKS_CLIENT_ID` and the secret `DATABRICKS_CLIENT_SECRET`. It needs the
  `workspace-access` and `databricks-sql-access` entitlements, and `ALL
  PRIVILEGES` and `MANAGE` on the target, shadow and rehearsal catalogs. It
  can't create catalogs, so `scripts/setup_workspace.py` does that once.

## 8. Limits of the approach

- **Intent isn't in the end state.** Renames, splits and changes of meaning
  need `refactor.yml`, a transition script or review.
- **Whether a change works depends on the data.** Prechecks turn a failed
  deploy into a failed plan. PKs are informational, so Databricks never
  catches duplicate keys.
- **Undo is bounded by retention,** no transaction spans several DDL
  statements, and views are late-bound, so a change can break readers outside
  the model.
- **Some side effects are silent.** Dropping a table, replacing one without its
  PK, and `DROP CONSTRAINT ... CASCADE` on a PK delete the FKs that point at
  it; renaming a table breaks views over the old name; `CREATE OR REPLACE` on a
  view or function drops its grants and tags. Verify catches lost FKs, and the
  planner replaces dependent views and re-runs grants and tags.
- **The shadow proves the DDL, not the migration,** because it is empty.
  Rehearsal closes that gap.
- **Free Edition** caps a metastore at 500 tables and 50 volumes, and a dropped
  table counts for about 7 days.

## 9. Not built

Declarative reference data; drift detection against the recorded state; scripts
that run on every deploy; lineage warnings on destructive and type changes;
and, in the model, owners, tags on functions, external tables and volumes, and
a table's default collation.
