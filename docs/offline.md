# Development and CI without a live workspace

How `ucstate` builds the desired model without a workspace where it can, and
how the project is tested on a budget. It refines the shadow build in
[design.md](design.md), section 5.

## The short version

- The workspace stays the source of truth for what Databricks does, but it is
  not the test runner. It is consulted on a budget, and what it answers is
  saved.
- A local compiler turns the source SQL into the model, for the statements it
  knows. It declines the rest, and the shadow catalog builds those.
- The plan check in CI uses the compiler and reads the target over REST, so it
  creates no tables. Apply cross-checks the compiler against a shadow before it
  changes anything.
- Every rule the compiler applies beyond the text is pinned by a saved answer
  from the workspace. Live runs, on a schedule, save new answers and find
  platform changes.
- Contributors without a workspace can run every test but the live ones.

## 1. What needs the workspace

Free Edition caps a metastore at 500 tables, views included, and a dropped
table counts for about 7 days (`AGENTS.md`, "Live runs on a budget"). So the
budget is about 500 tables created per week, less those that stand.

| Use | Tables created | How often |
| --- | --- | --- |
| The offline tests | none | every change |
| The plan check, when the compiler declines a file: a shadow of `catalog/` | about 5 | every pull request |
| Apply: a shadow, the lock, `_ucstate` on first use | about 6 | every merge |
| The full live suite, nothing recorded | about 225 | when something under test needs it |
| The same, with every scenario recorded | about 120 | when something else is under test |

The 225 is about 105 for the 34 scenarios and about 120 for the round trip and
the apply tests. A scenario is skipped while its recording is current, so only
a change to its files, or `UCSTATE_RECORD=1`, spends tables on it. Every live
run prints its estimate first, and is refused when it is over its budget or
would leave the workspace's owner less than the reserve.

Once the metastore is over the cap, nothing can create a table, and that stops
the shadow build and so `apply` and any plan that needs a shadow. The
compiler is what lets a plan run without one.

## 2. The design

### The workspace as an oracle

The live tests stay, but they are the way to ask the platform a question and
save the answer, rather than the way to test a change:

- **Scenario recordings.** A passing live scenario saves what it read: the
  target built from after/, the target's before/ with its seed rows and each
  precheck's result, in `recorded.json` beside the scenario. The replay
  tests make the plans from that offline.
- **Samples.** `ucmodel.samples` holds raw REST payloads with the SQL that
  built them, for the reader and the normaliser.
- **Free reads.** Reading a catalog over REST creates nothing and costs no
  quota. A target's current state can be read and saved at any time.

### The compiler

`ucmodel.compiler` reads the source format of [design.md](design.md), section
1, and gives the model the shadow would. Its input is narrow: plain `CREATE`s
of schemas, tables, views, functions and volumes, one object per file and
two-part names, then tags, constraints and grants on what was created.

- **Structure is parsed and expressions are kept.** Defaults, generated
  columns, CHECKs, view queries and function bodies are kept as written, which
  is how the platform keeps them. So the compiler parses DDL, not SQL
  expressions, and a construct it doesn't know can't be misread inside an
  expression.
- **Normalisation is the platform's.** Each rule that maps what was written to
  what the platform stores is written down and pinned by a saved answer:
  `INTEGER` is `int`, an undeclared view schema mode is `COMPENSATION`, a
  Python function is `NO_SQL`, and so on (section 3).
- **A view's column types are derived, by Spark's rules.** It's the one thing
  the platform works out rather than keeps. The compiler types a small set of
  expressions (column references, casts, literals, `+`, `-`, `*`, `count`,
  `sum`, `avg`, `min` and `max`) with Spark's decimal rules. sqlglot's own type
  annotation isn't used, because it gets decimal arithmetic wrong: it types
  `sum(int * decimal(10,2))` as `decimal(10,2)`, and Spark says
  `decimal(31,2)`.
- **It fails closed.** Anything it doesn't model, or models without a pinned
  rule, raises `Unsupported` with the reason. The caller then builds a shadow.
  A gap costs a shadow build, never a wrong plan.
- **It refuses what the platform is known to refuse,** so the shadow reports
  the real error: an inline CHECK, an FK to its own table inside
  `CREATE TABLE`, mixed tag forms, a mask beside a CHECK, a default without
  `allowColumnDefaults`, and a generated column whose type isn't its
  expression's.

### What uses it

`shadow.desired(ws, project, mode=, log=)` gives the desired model in the shape
the shadow build returns. The compiler's output needs no `declared` pass, and
its `meta` is already empty.

- **`ucstate plan`, `compare`, `transition`, `rehearse` (`auto`):** compile,
  and build a shadow only if the compiler raises `Unsupported`.
- **`ucstate apply` (`checked`):** compile and build a shadow, and the two
  must agree under `compare` before anything runs. Apply already pays for a
  shadow, and this makes every merge a test of the compiler. If they disagree,
  apply stops with the differences (`CompilerDisagrees`, exit 1, nothing run).
  A compiler that declines leaves the shadow alone.
- **`--shadow`** (and `ucstate shadow`) forces the shadow.
- **The scenario harness** builds after/ once, from empty, in the target. The
  desired model is the compiler's, and a shadow is built only if it declines
  (`rebuildable`, a CTAS). The target's build is the recording's `after`, and
  the compiler's model is checked against it as `compare` checks a plan. Any
  difference fails the test as a failure of the compiler. A scenario whose
  recording is current isn't run at all.

## 3. Testing the compiler

The oracle is always the platform. Each test compares the compiler's model
with one the platform built, using `compare`, the planner's own comparison. So
"no differences" means the plan can't tell them apart.

| Oracle | Cases | Needs |
| --- | --- | --- |
| The round-trip sample and its import, `packages/ucstate/examples/roundtrip` | 11 objects, every feature of the model | nothing |
| The `shop` sample, a read of the sample project's `prod` catalog, and `shop.sql`, its source | 6 objects, including a view with derived types | nothing |
| Each sample, rendered as source by `ucstate import`'s renderer | the three samples | nothing |
| Each scenario's after/ against its recording | 34 scenarios | nothing, once recorded |
| What earlier live runs left in the workspace | up to 34 | a free read |

The `shop` case pins the view rules, which nothing else does: `total` is
`decimal(31,2)`, and the other columns take their tables' comments.

Unit tests pin each rule on its own, with the case it came from. The
known-bad control is a rule changed on purpose: a compiler that types
`int * decimal(10,2)` as `decimal(10,2)` must fail the `shop` case.

**Rules and what pins them.** Each starts out pinned by one case. A rule with
no case is declined rather than applied.

| Rule | Pinned by |
| --- | --- |
| Object names are lower-case; column names keep their case | round-trip sample |
| Types are lower-case without spaces: `decimal(10,2)`, `map<string,string>`, `struct<width:double,Height:double>` | round-trip sample |
| A struct field's backticks go when the name doesn't need them | round-trip sample |
| `DECIMAL` alone is `decimal(10,0)` | Spark's documented default |
| Defaults, generated expressions and CHECKs are kept as written | round-trip sample |
| Identity defaults to `START WITH 1 INCREMENT BY 1` | Delta's documented default |
| An undeclared view schema mode is `COMPENSATION` | `shop` sample |
| A view column that names a table column, with no column list, takes its comment | `shop` sample |
| `int * decimal(10,2)` is `decimal(21,2)`, and its `sum` is `decimal(31,2)` | `shop` sample |
| `decimal(10,2) * 2` is `decimal(12,2)`: a literal has its own precision | a probe |
| A SQL function is deterministic and `CONTAINS_SQL` unless declared, and `READS_SQL_DATA` when its body reads a table | round-trip sample |
| A Python function is `NO_SQL`, and not deterministic unless declared | round-trip sample |
| A scalar function's return type is upper-case: `DOUBLE` | round-trip sample |

**Declined until a case pins them:**

- `CREATE TABLE ... AS SELECT`, whose columns and nullability come from the
  query;
- a PK or identity column without `NOT NULL`, whose nullability the platform
  decides;
- a constraint without a name, which the platform names, and one with
  capitals;
- a view column that would take a comment through an alias or past a column
  list;
- a function returning anything but a simple type;
- `CHAR` and `VARCHAR`, intervals, collations other than `UTF8_BINARY`, and
  struct fields with `NOT NULL` or a comment;
- any statement or clause outside the source format.

## 4. Live runs on a budget

The quota is shared with whoever else uses the workspace, and a dropped table
counts for about a week. So the live suite records once and replays.

- **The estimate comes first.** For tables and for volumes, `conftest.py`
  prints how many the run creates, how many the metastore has used (and when
  that was counted), how many runs since then created, and the reserve. It
  refuses the run (exit code 1) when `used + created since + N > limit -
  reserve`, and when N is over the budget. A run that creates nothing is never
  refused. A test other than a scenario counts as 10 tables.
- **Variables:** `UCSTATE_BUDGET` (default 60 tables), `UCSTATE_RESERVE` (250),
  `UCSTATE_BUDGET_VOLUMES` (5), `UCSTATE_RESERVE_VOLUMES` (25), `UCSTATE_RECORD`
  (`1` runs recorded scenarios again), `UCSTATE_DRY_RUN` (`1` prints the
  estimate and creates nothing; needs no credentials) and `UCSTATE_LIVE` (`1`
  runs with `DATABRICKS_*` variables, for CI).
- **The metastore's count lags.** `quota_count` is refreshed only every 45 to
  50 minutes (`quota_info.last_refreshed_at`), so runs in a row can each see
  the old count. A run that is allowed to start is written to a local ledger,
  `.ucstate/live-runs.jsonl`, and a second line marks its end. The guard adds
  what runs on the same metastore created since the last refresh, counting a
  run the count partly saw as whole, so it errs towards refusing. A dry run or
  a refused run writes nothing. A run elsewhere, such as CI, isn't in the
  ledger; the conformance workflow's concurrency group keeps CI runs apart.
- **The estimate holds.** Recording all 34 scenarios in a fresh workspace
  created about what the estimates added up to. A plan that adds a table, or
  rebuilds one, creates a little more than `creates()` says, so leave a few
  percent of slack.
- **A recording goes stale** when the statements it was built from change:
  before/, after/ or `seed.sql`, as `ucstate` splits them. A stale replay fails
  until the scenario runs live again. `UCSTATE_RECORD=1` records again anyway,
  for a change to the reader. The live test replays a recording before saving
  it, and fails if the replay plans differently from the run.
- **Recordings are scrubbed** like the samples (`ucmodel.samples.scrub`), so
  an owner's email and storage paths aren't saved. The scrub can change the
  model but not the plan, which compares only declared properties. That is why
  the replay guard compares plans.

## 5. CI

- **`test`**, on every pull request: the offline tests, which include the
  compiler's oracles and the replays. No secrets.
- **`plan`**, on pull requests that touch the deployed paths: the compiler, a
  read of `prod` over REST and the prechecks, which are queries. It creates no
  tables, so a pull request gets its plan even while the quota is spent. If the
  compiler declines a file, the check builds a shadow.
- **`apply`**, on merge: the compiler and a shadow cross-check, then apply.
- **`conformance`**, by hand and on Mondays
  (`.github/workflows/conformance.yml`): it runs the live suite with the
  project's service principal, within a budget and a reserve that
  `conftest.py` checks first, and uploads every recording it changed as an
  artifact, to be committed through a pull request. By default it runs only the
  scenarios whose recording is missing or stale, so a quiet week creates
  nothing. A full re-record, about 105 tables, is asked for by hand with the
  `record` input, and a changed recording then means the platform changed, or
  the scenario did. Apply's cross-check notices a platform change on what is
  deployed. The workflow shares the plan check's concurrency group, so it never
  overlaps a shadow build.

## 6. Contributors without a workspace

Every test but the live ones runs anywhere: the unit tests, the replays and the
compiler's oracles. A change to the planner is tested by the replays. A change
to the compiler is tested by the oracles, and by apply's cross-check once
merged. A change that needs a new answer from the platform needs a live run, by
someone with a workspace. Free Edition is open to anyone for non-commercial
use, so that can be any contributor.

## 7. What this gives up

[design.md](design.md) gives three reasons for building the desired model in a
shadow. Each still holds, so each needs an answer:

- **"Databricks is the only complete Databricks SQL parser."** The compiler
  doesn't try to be one. It reads one narrow format and declines the rest, and
  anything declined still goes to the platform.
- **"Normalisation is symmetric for free."** With the compiler it isn't, and
  each rule needs a case. A platform change to a rule shows up as a changed
  recording, or as an apply that stops on the cross-check.
- **"Every pull request proves its DDL compiles."** A pull request's plan no
  longer does, for what the compiler accepts. DDL the platform would refuse,
  and the compiler doesn't know it refuses, is found at apply's cross-check,
  before anything changes, rather than in review.

## 8. Choices made

1. **The plan check when the compiler declines a file** builds a shadow, so the
   check costs quota only then. The alternative was to fail the check and keep
   CI free of table creation.
2. **Apply's cross-check** builds a shadow on every apply and stops on any
   disagreement. It costs about 5 tables per merge. The alternative was to
   trust the compiler and cross-check only in the conformance run.
3. **The conformance run** is by hand and weekly. The weekly run records only
   what is missing or stale, and a full re-record fits in a week's quota with
   room for the plan checks and applies.
4. **New recordings** are uploaded as an artifact and committed through a pull
   request. The workflow doesn't open the pull request itself.

## 9. Next

Rules for what the compiler declines (CTAS, more expressions), each with its
case, driven by what real sources need.
