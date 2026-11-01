# ucstate

Not affiliated with or endorsed by Databricks.

Desired-state management for a Databricks Unity Catalog. Schemas, tables,
views, functions, volumes and grants are declared as SQL source files, one
object per file. `ucstate` compiles them locally into a model, or builds them
in an empty shadow catalog for what the compiler declines, compares the
result with a target catalog, and plans and applies the changes. It reads
the catalogs with `ucmodel`.

```bash
uv run ucstate --project catalog --profile my-profile plan --target dev
uv run ucstate --project catalog --profile my-profile apply --target dev
uv run ucstate --project catalog --profile my-profile history --target dev
uv run ucstate --project catalog --profile my-profile transition --target dev --object shop.orders
uv run ucstate --project catalog --profile my-profile rehearse --target dev
uv run ucstate --project catalog plan --against snapshots/ucstate_dev-20261101T120000Z-3f2a9c01b7de.json
uv run ucstate --project catalog rename-column shop.products sku stock_code
uv run ucstate --project catalog allow drop_table shop.old_orders
```

It does six things:

- imports a target's live state as SQL source files;
- compiles those files into the desired model, and builds a shadow catalog
  only for what the compiler declines. `apply` always builds one, and stops
  if the two disagree. `--shadow` forces the shadow;
- plans the changes that bring a target catalog to the source;
- rehearses a plan that depends on the rows, on clones of the target's
  tables;
- applies a plan under a lock, verifies the result, and records the
  deployment and the deployed model in the target's `_ucstate` schema;
- lists a target's deployments, and releases a lock left by an apply that
  died.

Each planned change carries a safety class, and runs only once it has what
its class needs:

- a precheck that finds no rows in the way, for `checked`;
- the table property declared in the source, for `one-way`;
- an allowance in `project.yml`, for `destructive`;
- a transition script in `migrations/`, for `rewrite`;
- for an `ambiguous` change, an entry in `refactor.yml` if it's a rename,
  or an allowance if it's a drop.

A blocked change lists the commands that would give it what it needs:
`rename-table`, `rename-column` and `allow` each add one entry to
`refactor.yml` or `project.yml`, and `transition` drafts a script. They
need no workspace, except `transition --target`.

`plan`, `compare` and `transition` take `--against` a snapshot from
`ucmodel snapshot` in place of the live target. They then need no workspace,
provided the compiler accepts the whole source. Prechecks don't run, and
every `refactor.yml` entry and script counts as not run yet. `apply` refuses
a plan made this way.

`scenarios/` is the planner's test corpus: before and after source trees for
each kind of change, run against the workspace. `examples/` holds what
`import` writes for `ucmodel`'s two samples.

The design is the repo's [`docs/design.md`](https://github.com/taslater/databricks-catalog-as-code/blob/main/docs/design.md). It needs a SQL warehouse, for the
shadow and for applying, and has been tested only on a Databricks Free
Edition workspace.
