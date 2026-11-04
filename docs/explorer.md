# The explorer

`ucatlas explore` writes one interactive page for a whole snapshot: every
table and view, materialized view, streaming table, metric view and external
or foreign table, how their columns are computed, which objects feed which,
and how they join. It is for understanding a catalog's structure, not for
changing it. It reads no workspace and no warehouse: a snapshot, and
optionally the SQL files that load the catalog, are enough.

```bash
ucmodel --profile my-profile snapshot --catalog my_dev
ucatlas explore --snapshot snapshots/my_dev-....json --sql jobs/ -o explorer
python3 -m http.server --bind 127.0.0.1 --directory explorer 8000
```

- `--snapshot` (required) is a file `ucmodel snapshot` wrote. A snapshot
  edited after it was taken is refused.
- `--sql` is a SQL file, or a folder searched for `*.sql`. Give it once for
  each. Its statements that write tables add their lineage: what each
  `INSERT`, `MERGE` and `CREATE TABLE ... AS` reads, and how it computes each
  column, and the files each `COPY INTO` loads. Without it, only the
  definitions the catalog holds are read.
- `-o` is the folder to write (default `explorer`). The page is
  `index.html`, with the data inlined. Beside it go the two libraries it
  draws with, d3 and elkjs, and their licenses, so it works without
  internet access. Opened without them, it loads them from jsDelivr.
- `--tries` is how many layouts each arrangement tries (default: by the
  catalog's size; see Layouts).

It needs Graphviz's `dot` and `neato` on the PATH, like `ucatlas draw`.

**The page holds what the snapshot holds**, owners' names and view
definitions included, plus the text of the SQL files given. A snapshot read
with a warehouse also holds the statements from query history, with any
values written in them, who ran each, and the paths of the notebooks and
files its jobs run, which often hold a person's name. Keep the page where
the catalog's metadata, and that SQL, may go.

## Where the edges come from

Two sources, kept apart in the data so the page can say which one an edge
came from:

- **The SQL.** Each view's, materialized view's and streaming table's
  definition, and each statement in the `--sql` files, read with sqlglot.
  They say how each column is computed (the expression, and whether it
  aggregates), and which objects a statement reads only to join or filter.
  `STREAM(t)` reads `t`. A metric view's YAML is read as one `SELECT` of its
  dimensions and measures from its source and joins, so a measure links to
  the columns it aggregates: the platform's lineage records nothing for a
  metric view. A file's `CREATE` of an object the catalog holds a definition
  of is skipped, since the catalog's definition wins.
- **What ran.** The table and column lineage the platform recorded, which
  the snapshot holds with when each edge was last seen. It covers writes
  the SQL files don't, such as notebooks, but not how a column was
  computed.
- **Query history**, when the snapshot was read with a warehouse
  (`ucmodel snapshot --warehouse`): each statement that wrote an object in
  the last 90 days, from the lineage joined to `system.query.history`, one
  per text, the latest ten for each object. Its SQL is read as a file's is,
  so it gives column lineage, joins and filters for writes no SQL file
  holds, and an object's panel says how often each ran, who ran it last and
  from where (a job and its run, a notebook, a dashboard, the CLI). A
  statement the lineage says wrote one object but that reads as writing
  another is a gap. One with no lineage to read (`DELETE`, `INSERT ...
  VALUES`, a `CREATE TABLE` with columns) is still listed as a writer, and
  counts as a write for "last written". Query history covers SQL warehouses
  and serverless compute; a pipeline's refreshes have no statement there,
  so a materialized view's or a streaming table's panel names the pipeline
  that refreshes it instead. The queries that read two or more objects and
  wrote nothing, the ten most run for each set of objects, give the joins
  people make (see Who reads an object). They have no lineage of their own,
  so one sqlglot can't read is no gap.

A materialized view and a streaming table hold data, so, as for a table,
the lineage says when each was last written (refreshed, for them). A view
and a metric view are computed when read. The lineage of a materialized
view also names columns of its hidden backing table; a column edge to a
column the snapshot's object doesn't have is left out.

An edge both sources have is marked as seen. An edge only the lineage has is
drawn in its own color, as "seen by the lineage, not in any SQL". An edge
only the SQL has can be marked too ("Mark flows in the SQL the lineage never
saw"), which is where code that no longer runs shows up.

An object in another catalog, in a schema the snapshot didn't read, or a set
of files read with `read_files` or `COPY INTO`, gets a small node of its own,
so an edge to it is drawn rather than dropped.

Names resolve as Databricks resolves them: a three-part name in the
snapshot's catalog is the model's object, and one in another catalog stays
outside. `USE SCHEMA` and `USE CATALOG` hold to the end of their file. A
one-part name with no `USE` resolves only when exactly one schema has an
object by that name; otherwise it is reported, not guessed.

## Lineage gaps

A statement that writes an object but that sqlglot can't read, such as
`APPLY CHANGES INTO`, is a **lineage gap**. Its target is still drawn,
marked ⚠, and the page's ⚠ chip lists every gap with the statement and the
reason. A statement that writes no object (`GRANT`, `OPTIMIZE`, a bare
query) is not a gap, nor is one from query history that the lineage saw
read no table, which has no lineage to miss. The command prints how many
gaps there are.

## Who reads an object

With query history, an object's panel also says how many queries read it
in the last 90 days, by how many people, and when last: queries that wrote
nothing, as the lineage recorded them. A run answered from a warehouse's
result cache records no lineage, so each query's runs are counted in query
history by its text, cached runs included. A statement that reads an object to
write another is upstream of that one instead. An object no query read in
that time says so.

**Joined with** lists the tables an object is joined to, on which columns,
and where each join comes from: a declared key, a statement that writes, or
queries that ran, with how many times. Route uses the same joins, so a path
analysts already take is preferred.

## Jobs

A statement from query history that ran in a job says which job, which task
and, from the jobs API, what the task runs (a notebook, a SQL file, a
pipeline), with the path shortened to its folder and file. An object's
writers from one job are listed together under it. A job's name opens its
panel: its tasks in the order they run, what each wrote and read, and the
objects its pipeline tasks refresh. Those objects are highlighted wherever
they are drawn, and **Show them all** brings them into Explore. The map's
Compass lists every job the snapshot has a statement from.

## Size

With a warehouse, a snapshot holds each table's size from
`system.storage.table_metrics_history`: the data in its current version and
the files that hold it, on the latest day the metrics have, and the days in
the last 90 it changed. A table's panel says how much it holds and how that
changed, with a small chart. The metrics have each table once a day, so a
table created since has no size yet.

## The three modes

- **Explore** starts on the object with the most relationships. Pick
  another with the search box, or double-click a card. Depth sets how far
  upstream and downstream to show. Relationships turn on data flow, reads
  only to join or filter, lineage-only edges and foreign keys. Clicking a
  card highlights its edges. Clicking a column traces where its values come
  from and what reads it, through every expression on the way.
- **Compare** lines up two or more tables' columns by where their values
  come from, so `revenue` in one and `net_amount` in another share a row
  when they trace back to the same source. It lists what differs (grain,
  filters, how a measure is computed, when each was last written) and the
  sources the tables share. It writes the SQL checks that would confirm
  them, such as whether a declared key has duplicates or whether two tables
  hold the same keys, for you to copy into a SQL editor. It runs nothing.
- **Map** shows the whole catalog. Search for business words and the
  matching tables light up, with the most trusted listed first (certified,
  then those the most objects depend on). Route finds how to join two
  tables, through declared keys and the joins the SQL already makes, and
  says whether each join multiplies rows. **Level** switches between a box
  per schema and every table. A catalog too big to read whole at one scale
  opens on its schemas; double-click a schema to open its tables.

Color shows each object's schema, how recently the lineage saw it written,
how many queries read it, or how much it holds, as of the snapshot. Reads
and size go in four steps on a log scale from the least to the most in the
catalog, with a gray for none, and the map's cards then say the number. The
Compass, the map's panel with nothing selected, lists the most read
objects, those no query read with nothing downstream either, which may be
unused, and the largest tables.

## Layouts

The layouts are worked out when the page is built, not in the browser, so
the page opens at once and nothing moves unless the view changes.

- **One layout per arrangement, over the whole catalog**: left to right and
  top to bottom, each grouped by schema or not, and a free arrangement. An
  Explore view shows part of the catalog and keeps the layout's order, so
  two objects never trade places when the depth or the filters change.
- **The map and its schema overview are drawn exactly as built**: places,
  curves and schema regions.
- **Each arrangement tries several layouts and keeps the tidiest**: the
  fewest edge crossings, plus two for each edge drawn across an object it
  doesn't connect. `dot` (the layered arrangements) starts each try from a
  different order, and `neato` (the free one) from a different random
  start. Tries are seeded, so **the same snapshot always builds the same
  page**, byte for byte.
- **Fewer tries for a bigger catalog**: 24 up to 150 objects, 4 up to 400,
  then 1. Scoring a try costs more than making it.
- Compare's drawing of the tables' shared sources depends on which tables
  are picked, so the page lays it out, with elkjs.

Measured on a synthetic catalog of 900 objects (750 tables, 150 views, 1,350
table edges): reading the lineage takes about 20 seconds and the layouts about
6, and the page is 3 MB. In a browser it draws its first view in under half a
second, and the map in under one.

## What it doesn't know

A snapshot holds structure and the lineage the platform recorded, and that
is all the explorer reads. It doesn't know what only a warehouse can say:

- what a write did to a table beyond its SQL (rows written, files), which
  `DESCRIBE HISTORY` says; and, without a warehouse, how big a table is;
- what the data looks like: row counts, nulls, distinct values, ranges;
- whether a declared key holds. Primary and foreign keys in Unity Catalog
  are informational, not enforced, so the explorer takes them as declared;
- without query history, which job, notebook or query wrote or read a
  table. With it, a job and a pipeline go by their names, from
  `system.lakeflow`, which has a job some time after it is created, and a
  job's notebook or file by its path; a notebook run outside a job, a
  dashboard and a saved query go by their ids.

The checks Compare writes are where to start with the first three.

## Where the code is

| File | Does |
| --- | --- |
| `ucatlas/sqllineage.py` | Column lineage of each statement, with sqlglot, and the gaps |
| `ucatlas/explore.py` | The page's data, from a snapshot and the SQL files, and the page |
| `ucatlas/layout.py` | The layouts, with Graphviz, and their scores |
| `ucatlas/explorer/index.html` | The page: d3 for drawing, elkjs for Compare's layout |
| `ucatlas/explorer/d3.min.js`, `elk.bundled.js` | d3 7.9.0 and elkjs 0.9.3, unmodified; their licenses are in `THIRD-PARTY-NOTICES.txt` |

The tests use `packages/ucatlas/tests/atlas_demo/`, a synthetic catalog of
nine schemas with the SQL that loads it and lineage as the platform might
record it. `python packages/ucatlas/tests/demo.py OUT` writes it as a
snapshot, to try the explorer on.
