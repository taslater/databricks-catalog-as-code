# The explorer

`ucatlas explore` writes one interactive page for a whole snapshot: every
table and view, how their columns are computed, which objects feed which,
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
  column, and the files each `COPY INTO` loads. Without it, only the views'
  definitions are read.
- `-o` is the folder to write (default `explorer`). The page is
  `index.html`, with the data inlined. Beside it go the two libraries it
  draws with, d3 and elkjs, and their licenses, so it works without
  internet access. Opened without them, it loads them from jsDelivr.
- `--tries` is how many layouts each arrangement tries (default: by the
  catalog's size; see Layouts).

It needs Graphviz's `dot` and `neato` on the PATH, like `ucatlas draw`.

**The page holds what the snapshot holds**, owners' names and view
definitions included, plus the text of the SQL files given. Keep it where
the catalog's metadata may go.

## Where the edges come from

Two sources, kept apart in the data so the page can say which one an edge
came from:

- **The SQL.** Each view's definition, and each statement in the `--sql`
  files, read with sqlglot. They say how each column is computed (the
  expression, and whether it aggregates), and which objects a statement
  reads only to join or filter.
- **What ran.** The table and column lineage the platform recorded, which
  the snapshot holds with when each edge was last seen. It covers writes
  the SQL files don't, such as notebooks, but not how a column was
  computed.

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
query) is not a gap. The command prints how many gaps there are.

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

Color shows each object's schema, or how recently the lineage saw it
written, as of the snapshot.

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

- what a write did (rows written, the operation, who ran it), or how big a
  table is, which `DESCRIBE HISTORY` and `DESCRIBE DETAIL` say;
- what the data looks like: row counts, nulls, distinct values, ranges;
- whether a declared key holds. Primary and foreign keys in Unity Catalog
  are informational, not enforced, so the explorer takes them as declared;
- which job, notebook or query wrote or read a table, and how often tables
  are read and joined, which the system tables record.

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
