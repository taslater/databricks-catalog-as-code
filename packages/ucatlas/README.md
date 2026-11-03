# ucatlas

Not affiliated with or endorsed by Databricks.

Draws a Databricks Unity Catalog, or a snapshot of one, as a static site: an
ER diagram and a lineage graph for each schema, and a list of its objects
with their columns, keys, comments and column lineage. It reads the catalog
with `ucmodel` and the lineage API, over REST, so it needs no SQL warehouse.
It also writes an interactive explorer of a snapshot, and serves a local UI
over the commands.

```bash
uv run ucatlas --profile my-profile draw --catalog my_prod --schema shop -o site
uv run ucatlas draw --snapshot snapshots/my_prod-20261101T120000Z-3f2a9c01b7de.json -o site
python3 -m http.server --bind 127.0.0.1 --directory site 8000   # then open localhost:8000
```

`--snapshot` draws a file that `ucmodel snapshot` wrote, with no workspace
and no credentials. Without `--schema` it draws every schema but
`information_schema`, or every schema in the snapshot. `--no-lineage` skips
column lineage, which takes one API call per column.

## The explorer

```bash
uv run ucatlas explore --snapshot snapshots/my_dev-20261101T120000Z-3f2a9c01b7de.json --sql jobs/ -o explorer
python3 -m http.server --bind 127.0.0.1 --directory explorer 8000
```

One interactive page for a whole snapshot: which objects feed which, how
each column is computed, and how tables join. Explore follows an object's
lineage, Compare lines up tables' columns by where their values come from,
and Map shows the catalog by schema or by table. `--sql` adds the lineage
of the SQL files that load the catalog. It reads no workspace and no
warehouse. The page holds what the snapshot holds, owners' names included.
It comes with the two libraries it draws with, d3 and elkjs, so it works
without internet access. The guide is the repo's [`docs/explorer.md`](https://github.com/taslater/databricks-catalog-as-code/blob/main/docs/explorer.md).

## The local UI

```bash
uv run ucatlas serve [--snapshots snapshots] [--project .] [--port 8765]
```

A UI for one person, on 127.0.0.1 only. It prints an address with a token,
keeps the token in a cookie, and refuses requests without it or for another
host name. It lists the snapshots and compares, draws or plans against one.
It takes a new snapshot with a profile you choose; it never picks one. With
`ucstate` installed and a project, it plans the spec against a snapshot and
shows the commands that would settle each blocked change, as buttons.

Each button runs one command as a subprocess and shows its command line and
output. The commands are `ucmodel snapshot` and `diff`, `ucatlas draw`, and
`ucstate plan --against`, `rename-table`, `rename-column`, `allow` and
`transition --against`. Only `ucmodel snapshot` reaches a workspace, and it
only reads. The `ucstate` commands write one entry or one script into the
project, for review in git. Drawings and previews are kept in `.ucatlas/`,
by snapshot hash.

It needs Graphviz on the PATH (`dot`, and `neato` for the explorer): `brew install graphviz`, or
`apt-get install graphviz`. The plan is the repo's [`docs/diagrams.md`](https://github.com/taslater/databricks-catalog-as-code/blob/main/docs/diagrams.md).
