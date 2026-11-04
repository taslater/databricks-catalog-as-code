# ucmodel

Not affiliated with or endorsed by Databricks.

Reads a Databricks Unity Catalog over REST into a normalised model,
compiles source DDL into the same model, and compares two models. It needs
no SQL warehouse, and one makes a snapshot faster. `ucstate` and `ucatlas`
are built on it.

Its command takes a snapshot of a catalog, a file that is written once and
never edited, and reports what changed between two snapshots:

```bash
ucmodel --profile my-profile snapshot --catalog my_dev   # prints snapshots/my_dev-<time>-<hash>.json
ucmodel diff snapshots/OLDER.json snapshots/NEWER.json   # exits 1 when anything changed
ucmodel diff OLDER.json NEWER.json --format markdown     # or json
```

`snapshot` reads every schema but `information_schema`, or those named with
`--schema`, and their lineage unless `--no-lineage`. Reading takes about
two API calls per table, and lineage one more per table and per column.
With `--warehouse ID`, tags, grants, functions' names and lineage come from
five queries for the whole catalog, on `system.information_schema` and the
`system.access` lineage tables, and only the schemas' lists are read over
REST. The model is the same either way. A query that fails falls back to
REST, and says so. Three more queries read, from `system.query.history`, the
statements that wrote each object in the last 90 days, how often each
object was read, and the queries that read objects together, unless
`--no-history`; the jobs those statements ran from come with their tasks,
from the jobs API. The snapshot then holds that SQL, who ran it, and the
jobs' notebook and file paths. One more query reads each table's size from
`system.storage.table_metrics_history`.
The file holds a hash of its contents, and `diff` refuses a file that was
changed after it was taken. It holds what the API returns, owners' names
included.

- `workspace.py` makes the REST and SQL calls. It connects with a named CLI
  profile or with `DATABRICKS_*` variables, and never with a profile nobody
  named.
- `reader.py` reads the raw REST state of some schemas.
- `bulk.py` reads tags, grants, functions' names and lineage for a whole
  catalog with queries on a SQL warehouse.
- `normalise.py` turns that into the model: one dict per object, keyed
  `kind:schema.name`, without the catalog.
- `compiler.py` turns source DDL into the model a shadow catalog would
  give, offline, for the statements it knows, and declines the rest
  ([`docs/offline.md`](https://github.com/taslater/databricks-catalog-as-code/blob/main/docs/offline.md)).
- `model.py` compares two models, hashes an object, and says what an object
  depends on.
- `lineage.py` reads table and column lineage over REST.
- `snapshot.py` takes, writes and loads snapshots, and `report.py` says what
  changed between two. `cli.py` is the `ucmodel` command.
- `sqltext.py` splits statements, strips catalogs, compares SQL text by
  tokens, and quotes names and strings.
- `samples/` holds four schemas' raw REST state, so that tests need no
  workspace. Its docstring says how to save them again.

```python
from ucmodel import reader
from ucmodel.normalise import normalise
from ucmodel.workspace import Workspace

ws = Workspace.connect("my-profile")
model = normalise(reader.read(ws, "ucstate_prod", reader.schemas(ws, "ucstate_prod")))
```

It has been tested only on a Databricks Free Edition workspace.
