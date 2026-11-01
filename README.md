# databricks-catalog-as-code

Look at a Databricks Unity Catalog, and manage it as SQL files in git.

Status: alpha (0.1.0a1). It has been tested only on Databricks Free Edition.

## Three kinds of file, each with one writer

| File | What it says | Written by | Read by |
| --- | --- | --- | --- |
| **The spec** | What the catalog should be: SQL `CREATE` statements, one object per file, with `project.yml`, `refactor.yml` and `migrations/` | People, in git | `ucstate` |
| **A snapshot** | What a catalog was at one time: its model, and optionally its lineage, as JSON | `ucmodel snapshot`, once. It is never edited | `ucmodel diff`, `ucatlas draw`, `ucstate plan --against` |
| **A report** | How two of those differ | `ucmodel diff` (snapshot to snapshot), `ucstate plan` (spec to a catalog or a snapshot) | People |

Information goes one way. A catalog is read into snapshots, and snapshots
into reports and drawings. The spec reaches a catalog only through
`ucstate apply`. Nothing writes the spec from a catalog except
`ucstate import`, which starts a project.

A snapshot is read over REST with no SQL warehouse. Reading takes about three
API calls per table, and lineage takes one more per table and per column, so
a catalog is read once and looked at from the file as often as needed.
Comparing two snapshots takes a moment and no workspace. A snapshot file
holds a hash of its contents, and the tools refuse one that was edited.

## Two ways to use it

**Looking only.** `ucmodel` and `ucatlas` read; they never change a
catalog. Take snapshots, compare them, and draw them as ER diagrams and
lineage graphs, as a static site to publish or in a local UI. Nobody has to
change how they work.

**Desired state.** `ucstate` treats the spec as the whole truth about every
object it names. It leaves alone the objects it doesn't name. `plan` lists
the changes that would bring a catalog to the spec, and `apply` runs them.
This is the model of SQL Server database projects. For a
migration-script team, a plan against a snapshot is a draft of the next
script. It needs no workspace.

### What a plan does with a change made by hand

The plan compares the spec with the catalog as it is, so a change made by
hand in a target shows up as a difference:

| Made by hand in the target | What the next plan does |
| --- | --- |
| A comment, tag, mask, row filter, column default, view definition, function body or grant changed, on an object the spec names | Puts it back as the spec says (`auto`) |
| A table property that the spec doesn't declare | Leaves it: only declared properties count |
| A column added | Drops it only once `project.yml` allows it by name (`destructive`). Until then the plan is blocked |
| A column dropped | Adds it back, empty (`auto`, or `checked` if it is `NOT NULL`) |
| An object dropped | Creates it again. A table comes back empty |
| A table renamed, with its columns unchanged | Stops (`ambiguous`) until `refactor.yml` or an allowance says what happened |
| A new table or volume in a managed schema | Lists it under "Left alone". Under `policy: unmanaged_objects: drop`, drops it once it is allowed by name |
| A new view or function in a managed schema | Lists it under "Left alone". Under `policy: unmanaged_objects: drop`, drops it |
| Anything in a schema the project doesn't manage, owners, and the rows in tables | Doesn't compare them. A precheck only counts rows |

A plan made with `--against` a fresh snapshot shows these before anyone
runs anything.

### How the planner picks a path

It follows fixed rules. It doesn't search:

- An object is the same object when it has the same name. The planner never
  infers a rename: a rename is an entry in `refactor.yml`.
- Where a difference could mean two things, the plan stops and lists both as
  commands. A table can be gone with a new one of the same columns in its
  place. A column can be gone with a new one of the same type.
- It never drops and re-creates a table to make a change. A change that
  `ALTER` can't make needs a transition script.
- Every change has a class, and runs only once it has what its class needs:

| Class | Runs when | For example |
| --- | --- | --- |
| `auto` | Always | A comment, a grant, a new nullable column |
| `checked` | Its precheck finds no rows in the way, at plan and again at apply | `NOT NULL` on a column, a `CHECK` |
| `one-way` | The spec declares the table property it needs | Widening a type, renaming a column |
| `destructive` | `project.yml` allows it by name | Dropping a table or a column |
| `rewrite` | A transition script in `migrations/` replaces it | Narrowing a type, changing partitioning |
| `ambiguous` | Never. A `refactor.yml` entry or an allowance turns it into a rename or a drop | A column gone and a new one of the same type |
| `script` | Once per target | A data fix in `migrations/once/` |

A blocked change lists the commands that would unblock it:
`ucstate rename-table`, `rename-column`, `allow` or `transition`. Each
writes one entry or one script into the spec, for review in git. None of
them touches a catalog.

## The packages

| Package | Does |
| --- | --- |
| `ucmodel` | Reads a Unity Catalog over REST into a normalised model, compiles source DDL into the same model, and compares two models. Its command takes snapshots and reports on them. No SQL warehouse needed |
| `ucstate` | Imports a catalog as source files, plans and applies the changes that bring a catalog to its source, and records each deployment in the catalog |
| `ucatlas` | Draws a catalog, or a snapshot of one, as a static site of ER diagrams and lineage graphs, and serves a local UI over the commands. Needs Graphviz |

`ucstate` and `ucatlas` each depend on `ucmodel`, not on each other.

## What `ucstate` does

- **Source as SQL.** One object per file, with two-part names, so the same
  files deploy to `dev`, `prod` or any other catalog.
- **A model of both sides.** Source and catalog are both turned into the same
  normalised model, so the comparison is exact and ignores what the platform
  adds by itself.
- **An offline compiler with a shadow cross-check.** A local compiler builds
  the model from the source with no workspace. For anything it declines, and
  at every `apply`, the source is built in an empty shadow catalog and read
  back. `apply` stops if the two models differ.
- **Change classes,** as in the table above.
- **Apply under a lock.** `apply` takes a lock, checks that the target still
  matches the plan, runs the statements, verifies the result by comparing it
  with the desired model, and records the deployment in the target's
  `_ucstate` schema. It can first rehearse the plan on clones of the tables.
- **Diagrams.** `ucatlas` reads the same model and the lineage API.

The design is in [`docs/design.md`](docs/design.md).

## Install

```bash
pip install ucatlas    # snapshots, reports, diagrams and the local UI; installs ucmodel
pip install ucstate    # the planner and applier; installs ucmodel
```

`ucatlas` also needs Graphviz's `dot` on the PATH, to draw.

Both need Python 3.11 or later. To work from source:

```bash
git clone <this repository>
cd databricks-catalog-as-code
uv sync
uv run ucstate --help
```

## Looking: snapshots, reports and diagrams

You need a Databricks workspace and a Databricks CLI profile for it. No SQL
warehouse is needed. The commands below use the profile `my-profile`.

```bash
uv run ucmodel --profile my-profile snapshot --catalog my_dev
uv run ucmodel diff snapshots/OLDER.json snapshots/NEWER.json
uv run ucatlas draw --snapshot snapshots/NEWER.json -o site
```

- `snapshot` writes `snapshots/<catalog>-<time>-<hash>.json` and prints its
  path. `--schema` reads only the schemas named, and `--no-lineage` skips
  lineage. It never replaces a file. A snapshot holds what the API returns,
  owners' names included, so keep it where the catalog's metadata may go:
  this repository ignores `snapshots/`.
- `diff` reports what was added, removed and changed between two snapshots,
  and exits 1 when anything was. `--format markdown` suits a merge request
  comment or a job summary; `--format json` suits a program.
- `draw` writes a static site, one page per schema, from a snapshot or with
  `--catalog` from the live catalog. See [`docs/diagrams.md`](docs/diagrams.md).

### The local UI

```bash
uv run ucatlas serve            # in a folder with snapshots/, and project.yml if there is one
```

It prints an address on 127.0.0.1 with a token, and answers nobody else.
It lists the snapshots, compares two, draws one, and takes a new one with a
profile you choose. With `ucstate` installed and a project, it also plans the
spec against a snapshot, and shows each blocked change's options as
buttons. Each button runs one of the commands above and shows the command
line, so anything done there can be done in a terminal. Only
`ucmodel snapshot` reaches a workspace, and it only reads. The UI never
applies.

## Managing: a quickstart

You need a Databricks workspace, a Databricks CLI profile for it, and a SQL
warehouse. Free Edition works. The commands below use the profile `my-profile`.

1. **Make a project.** A folder with a `project.yml`:

   ```yaml
   # myproject/project.yml
   warehouse_id: <your SQL warehouse id>   # optional; see below
   shadow: {catalog: ucstate_shadow}
   schemas: [sales]                        # the schemas this project manages
   targets:
     dev:  {catalog: my_dev}
     prod: {catalog: my_prod}
   ```

   The target catalogs must already exist: `apply` never creates them. The
   shadow catalog is created when it is missing, which needs the
   `CREATE CATALOG` privilege. Give it a name no target uses, because every
   shadow build drops its schemas.

   The warehouse comes from `--warehouse`, else the `DATABRICKS_WAREHOUSE_ID`
   environment variable, else `warehouse_id` in `project.yml`.

2. **Import what is there.** This writes `src/` from a target's live state:

   ```bash
   uv run ucstate --project myproject --profile my-profile import --target dev
   ```

   The layout is `src/<schema>/schema.sql` and `tables/`, `views/`,
   `functions/` and `volumes/` folders holding one `.sql` file per object.

3. **Compare.** Source and target should agree after an import:

   ```bash
   uv run ucstate --project myproject --profile my-profile compare --target dev
   ```

   It lists every difference and exits 1 if there are any.

4. **Change the source, then plan.** For example, add a column to a table
   file. The plan lists each change with its class and its SQL:

   ```bash
   uv run ucstate --project myproject --profile my-profile plan --target dev
   uv run ucstate --project myproject --profile my-profile plan --target dev -o plan.json
   ```

   `plan` exits 1 if any change is blocked, and lists the commands that would
   unblock it. `--against snapshots/FILE.json` plans against a snapshot
   instead of the live target, with no workspace. It needs the compiler to
   accept the whole source, and `apply` refuses such a plan.

5. **Apply.**

   ```bash
   uv run ucstate --project myproject --profile my-profile apply --target dev
   uv run ucstate --project myproject --profile my-profile history --target dev
   ```

   `apply` asks before it runs. `--yes` skips the question, and
   `--plan plan.json` applies a saved plan, which it refuses if the target
   changed since the plan was made. `history` lists the recorded deployments.
   `unlock` releases a lock left by an apply that died.

Without `--profile`, the commands read `DATABRICKS_*` environment variables,
and with neither they stop with an error. They never pick a profile for you.

The other commands are `model` (prints the target's live model as JSON),
`shadow` (builds the shadow and prints the desired model), `transition`
(writes a transition script for one table's change) and `rehearse` (runs the
plan on clones of the target's tables). `rename-table`, `rename-column` and
`allow` each add one entry to `refactor.yml` or `project.yml`, with no
workspace. `uv run ucstate --help` lists them.

## CI

[`.github/workflows/`](.github/workflows/) holds a working set:

- `test` runs lint, types and the offline tests on every pull request.
- `plan` plans `prod` on a pull request that touches the project and puts the
  plan in the job summary. A blocked change fails the check.
- `apply` runs on a merge to `main`. It applies only a commit that came
  through a merged pull request, then runs `ucstate apply --target prod --yes`.
- `conformance` runs the live tests that save the platform's answers, by hand
  and weekly.

They sign in as a service principal with OAuth. Set the repository variables
`DATABRICKS_HOST`, `DATABRICKS_WAREHOUSE_ID` and `DATABRICKS_CLIENT_ID`, and the
secret `DATABRICKS_CLIENT_SECRET`. The principal needs the `workspace-access`
and `databricks-sql-access` entitlements, and `ALL PRIVILEGES` and `MANAGE` on
the catalogs it deploys to and on the shadow and rehearsal catalogs.
`scripts/setup_workspace.py` creates the catalogs and the principal for the
sample project.

## Limits

- **The compiler declines** `CREATE TABLE ... AS SELECT`; a primary key or
  identity column without `NOT NULL`; unnamed constraints and constraint names
  with capitals; a view column that would take a comment through an alias or
  past a column list; functions returning anything but a simple type;
  `CHAR`, `VARCHAR`, intervals and collations other than `UTF8_BINARY`; struct
  fields with `NOT NULL` or a comment; and anything outside the source format.
  A declined file costs a shadow build, never a wrong plan, and a plan
  `--against` a snapshot stops instead. See [`docs/offline.md`](docs/offline.md).
- **Out of scope:** the data in ordinary tables, materialized views and
  streaming tables, external locations and credentials, and jobs and
  pipelines. The model does not hold owners yet.
- **Not built:** declarative reference data, drift detection against the
  recorded state, scripts that run on every deploy, lineage warnings on
  destructive changes, and writing a plan as a script for another migration
  tool. Planning compares the source with the live catalog, or a snapshot
  of it.
- **A snapshot's hash catches an edit, not a forgery.** Someone who changes
  the file and recomputes the hash isn't caught. Where that matters, keep
  snapshots somewhere they can't be changed, such as CI job artifacts.
- **Free Edition's quotas.** A metastore holds at most 500 tables (views
  count) and 50 volumes, and a dropped table counts for about 7 days. The
  shadow build, `apply`'s lock and the live tests all create tables. Past the
  cap, every `CREATE` fails with `UC_RESOURCE_QUOTA_EXCEEDED`. The compiler
  keeps `plan` from building a shadow, and the live tests run on a budget
  and replay saved recordings (see [`CONTRIBUTING.md`](CONTRIBUTING.md)).
- **Tested only on Free Edition,** which may not be used commercially.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report a security problem as
described in [`SECURITY.md`](SECURITY.md). Changes are listed in
[`CHANGELOG.md`](CHANGELOG.md).

## Licence

MIT. Not affiliated with or endorsed by Databricks.
