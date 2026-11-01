# Generated data-model diagrams

How `ucatlas` draws a catalog: ER diagrams from declared keys and lineage
graphs from Unity Catalog's lineage, written as a static site. The ER pages
and lineage are built (section 6). Publishing and annotation are open
(section 7).

## The short version

- `ucatlas draw` reads a catalog's model with `ucmodel`, the reader that
  `ucstate plan` uses, and draws each schema as an ER diagram: tables and
  views, their columns, and their declared keys.
- Lineage comes from the lineage REST API, which needs no warehouse. Views
  also carry the dependencies their source declares.
- Graphviz renders the drawings to SVG, and `ucatlas draw` writes them
  with an index page into one directory: the static site.
- None of it creates a table, so the Free Edition quota (500 tables per
  metastore) doesn't hold it up.

**What it relies on.** Declared keys appear at once in the model and in
`information_schema`, and the lineage API has an edge within minutes of the
query that made it. `system.access.table_lineage` lags by up to a quarter of
an hour, so the API is the source. A first sketch drew the sample project's
`shop` schema in about 50 lines: its five objects, the three foreign keys,
and the lineage API's two edges into the `order_totals` view. It read only
REST and used no quota.

## 1. What the diagrams show

- **One ER diagram per schema.**
  - Each table or view is a box: its name, its columns with types, and
    NOT NULL.
  - Primary-key and foreign-key columns are marked, and each foreign key
    is an edge from its column to the parent's.
  - Comments become tooltips. Tags can mark a column, such as one that
    holds personal data.
  - A parent in another schema is drawn as a stub that links to that
    schema's page.
- **A view's reads,** as dashed edges in the ER diagram, from the tables
  and views its definition names. They come from the model, so they hold
  before anything has run.
- **One lineage graph per schema.** Table-level edges from the lineage API,
  in and out of the schema. Lineage only shows what has run.
- **A page per table** holds its column lineage, from the column-lineage
  API.
- Rows, sizes and statistics are not shown.

## 2. Where the data comes from

- **The model.** `ucmodel`'s `reader.read` and `normalise`, for a catalog.
  A plan's desired model is the same shape, so a pull request could be drawn
  as it would deploy.
- **Lineage.** `GET /api/2.0/lineage-tracking/table-lineage`, one call per
  table, and `.../column-lineage` for a table's page. Neither needs a
  warehouse. `system.access.table_lineage` holds the history, but it lags by
  up to a quarter of an hour and needs the warehouse, so it is for bulk
  questions only.
- **Upstreams outside the catalog.** These are paths, notebooks and jobs,
  which the API returns only when asked for entity lineage. They are drawn
  as plain nodes, or left out (section 7).

What `ucmodel/lineage.py` handles:

- **Each edge is a `tableInfo`** with its catalog, schema, name, type and
  `lineage_timestamp`, and column lineage gives `upstream_cols` and
  `downstream_cols` in the same terms. An edge to another catalog keeps its
  catalog: rehearsal's clone in the rehearsal catalog shows up downstream of the
  table it cloned.
- **A `CREATE OR REPLACE ... AS SELECT` from the same table** leaves an
  edge from the table to itself, and from each column to itself. These
  are dropped.
- **Asking for entity lineage added nothing** for the tables tried, all of
  them written through the SQL statement API. `read` doesn't ask for it
  until there is an edge to a notebook, job or path to test against.
- **Column lineage costs one call per column.** The calls run 8 at a time,
  and a site for a schema with 26 columns took about 9 seconds.
  `ucatlas draw --no-lineage` skips it.

Lineage only shows what has run. A catalog whose tables nothing writes
between has only its views' edges. A small workload of `INSERT ... SELECT`
into existing tables gives the lineage graph something to show, without
creating a table.

## 3. Rendering

**Graphviz (recommended).** `dot` lays out boxes with one row per column,
and can draw an edge from one column to another. Tests compare the Graphviz
source that `ucatlas` writes, not the SVG, so a different Graphviz version in
CI doesn't fail them. It runs offline, and CI installs it with apt.

The alternatives:

- **Mermaid.** GitHub renders it in Markdown, so no build is needed. But
  its ER diagrams can't draw an edge between two columns, and it gives
  little control of the layout.
- **D2 or ELK.** Their layouts are better for large graphs, but each is
  another toolchain.

Two things to know about Graphviz:

- **Graphviz guesses text widths,** and the browser's font can be wider.
  Two pieces of text side by side in one cell can overlap, as a bold name
  and a plain "(view)" did. So each cell holds one run of text.
- **A warning from `dot` is an error.** It means the drawing used
  something `dot` didn't understand, and the output would be quietly
  wrong.

Past about 30 tables, one diagram per schema gets hard to read. The fix is
grouping by subject area (section 4), drawn as Graphviz clusters, with a
page per group.

## 4. Annotation and grouping

People may want to annotate or regroup what the diagrams show. There are two
ways to do it:

- **Tags in the source (recommended to start).** For example, a
  `diagram_group` tag on a table, and comments for notes. They are reviewed
  in pull requests and deployed like any other tag, and cost no quota.
  Their limit is that changing a group needs a pull request.
- **Admin tables in `_ucstate`.** For example, `_ucstate.diagram_groups` and
  `_ucstate.diagram_notes`, which people can edit without git, read at render
  time. They need the warehouse to read and 2 or 3 tables of quota once.
  They are worth it if people outside the repo should annotate.

## 5. Publishing

The site is static, so it is served locally to test it:

```bash
uv run ucatlas --profile my-profile draw --catalog my_catalog --schema shop -o site
python3 -m http.server --bind 127.0.0.1 --directory site 8000
```

Where to publish it is open. A GitHub Pages site is public unless the
repository belongs to a GitHub Enterprise Cloud organisation, and a catalog's
diagrams usually shouldn't be. The options are:

- **A Databricks App (recommended).** Free Edition allows up to 3 apps per
  account. An app stops 24 hours after it is started or deployed, and can
  be restarted at any time. Only workspace users it is shared with can
  see it.
  `apply` would upload the site as workspace files and redeploy the app,
  so it is up for a day after each deploy. The site is static, so the app
  needs no access to data.
- **An Actions artifact.** Each apply run uploads the site. Viewing it
  means downloading the zip. No decision is needed for this, so it comes
  first either way.
- **In the pull request.** The plan check could attach the before and after
  ER diagrams of the schemas a pull request changes, as an artifact.
  Whether the job summary can show them inline is unverified.

## 6. Phases

| # | Phase | Delivers | Done when |
| --- | --- | --- | --- |
| D1 | ER | `ucatlas draw --catalog C -o DIR`: a page per schema with its drawing and its objects, and an index | A catalog renders, and offline tests check the drawing of the round-trip sample. **Built** |
| D2 | Lineage | Table lineage per schema, and column lineage on table pages | A lineage workload's edges show up in the catalog's graph. **Built**, and checked in a browser on schemas whose lineage came from a CTAS, view reads, a transition script and a rehearsal's clone |
| D3 | Publishing | The site as an apply artifact, then an app if chosen | A merge that changes a table's keys changes the published diagram, with no manual step. **Not built** |
| D4 | Annotation | Groups and notes from tags, and admin tables if wanted | A tag change regroups the diagram on the next deploy. **Not built** |

**Testing.** `packages/ucatlas/tests/test_diagram.py` draws `ucmodel`'s
samples offline.
It checks what the drawing must hold, rather than comparing it with a
saved copy:

- every table and view is a box with a row per column;
- every foreign key is an edge, and every view read a dashed one;
- every lineage edge that reaches the schema is in its lineage graph;
- names, types and comments are escaped;
- objects in other schemas link to their pages;
- the output doesn't depend on the model's order.

The known-bad controls are a drawing with a foreign key's edge removed and
a lineage graph with an edge removed: the checks must fail on each.
`test_lineage.py` reads responses in the shapes the API gave. The tests
that run `dot` skip without Graphviz, and CI installs it.

## 7. Open decisions

The recommended option comes first in each.

1. **Where the site lives** (section 5).
   - A Databricks App, redeployed by `apply`, up for a day after each
     deploy (recommended).
   - The Actions artifact only.
2. **Annotation.**
   - Tags in the source first, and admin tables later if needed
     (recommended).
   - Admin tables from the start.
3. **Upstreams outside the catalog** (paths, notebooks, jobs).
   - Drawn as plain nodes (recommended), since they explain where data
     comes from.
   - Left out, which keeps the graph to Unity Catalog objects.
4. **Pull-request diagrams.** Whether the plan check should also draw the
   schemas a pull request changes. This can wait until D3.
