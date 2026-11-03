"""The explorer's layouts, worked out when the site is built, with Graphviz.

One layout per arrangement, over the whole catalog: every object gets a place.
The page keeps the order of those places in every view it draws, so two
objects never trade places when the view changes, and nothing is laid out
from scratch in the browser. The map shows everything, so the page draws it
exactly as built: places, curves and schema boxes. So does the map's overview,
one box per schema, which a big catalog opens on.

The search for a tidy layout happens here, where time is cheap. `dot`'s
crossing minimization starts from the order it reads the graph in, so each
try feeds it a different order, and the try with the lowest score is kept:
its crossings, plus two for each edge drawn across an object it doesn't
connect. `neato` (the free arrangement) varies by its random start instead.
Tries are seeded, so the same model always gets the same layout.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Iterator, Sequence
from itertools import pairwise

W, H = 250, 30  # a card with its header only, in px; a view keeps the order, not the size
PX = 72  # Graphviz works in inches
Point = tuple[float, float]
Route = tuple[str, str, list[Point]]


class LayoutError(Exception):
    pass


BIG = 400  # above this many objects: one try each, and no overlap removal in the free layout


def tries_for(nodes: int) -> int:
    """Fewer tries for a bigger catalog. A try is cheap for `dot` (under a second at 900
    objects) but scoring it isn't, and with one try there is nothing to score."""
    return 24 if nodes <= 150 else 4 if nodes <= BIG else 1


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _source(nodes: Sequence[dict], edges: Sequence[dict], *, direction: str, group: bool,
            size: tuple[int, int] = (W, H), gap: int = 24, layer: int = 90) -> str:
    rank = "TB" if direction == "down" else "LR"
    lines = [f"digraph G {{ rankdir={rank}; nodesep={gap / PX:.3f};"
             f" ranksep={layer / PX:.3f}; mclimit=4; remincross=true; splines=true;",
             f'  node [shape=box fixedsize=true width={size[0] / PX:.3f} '
             f'height={size[1] / PX:.3f} label=""];']
    if group:
        by: dict[str, list[str]] = {}
        for n in nodes:
            by.setdefault(n["schema"], []).append(n["id"])
        for i, (schema, ids) in enumerate(by.items()):
            lines.append(f"  subgraph cluster_{i} {{ label={_q(schema)}; margin=12;")
            lines += [f"    {_q(x)};" for x in ids]
            lines.append("  }")
    else:
        lines += [f"  {_q(n['id'])};" for n in nodes]
    for e in edges:
        # data flow decides the layers; a foreign key is drawn and untangled, but doesn't rank
        attrs = {"flow": "weight=3", "runtime": "weight=2", "join": "weight=1",
                 "fk": "weight=1 constraint=false"}[e["kind"]]
        if "weight" in e:
            attrs = f"weight={e['weight']}"
        lines.append(f"  {_q(e['s'])} -> {_q(e['t'])} [{attrs}];")
    lines.append("}")
    return "\n".join(lines)


def _run(engine: str, source: str, *args: str) -> dict:
    exe = shutil.which(engine)
    if exe is None:
        raise LayoutError("Graphviz isn't installed: brew install graphviz, "
                          "or apt-get install graphviz")
    done = subprocess.run([exe, "-Tjson", *args], input=source, capture_output=True, text=True)
    if done.returncode:
        raise LayoutError(f"{engine}: {done.stderr.strip()[:300]}")
    return json.loads(done.stdout)


def _drawing(g: dict) -> tuple[dict[str, Point], list[tuple[str, str, list[Point], Point | None]],
                               list[tuple[str, float, float, float, float]], tuple[float, float]]:
    """Graphviz's drawing: centers (px, y down), each edge's Bezier control points and the
    point its arrow ends at, each cluster's box, and the drawing's size."""
    _, _, width, height = map(float, g["bb"].split(","))

    def pt(s: str) -> Point:
        x, y = s.split(",")[-2:]
        return float(x), height - float(y)

    objects = g.get("objects", [])
    names = {o["_gvid"]: o["name"] for o in objects}
    pos = {o["name"]: pt(o["pos"]) for o in objects if "pos" in o}
    clusters = []
    for o in objects:
        if o["name"].startswith("cluster") and "bb" in o:
            x0, y0, x1, y1 = map(float, o["bb"].split(","))
            clusters.append((o.get("label", ""), x0, height - y1, x1 - x0, y1 - y0))
    routes = []
    for e in g.get("edges", []):
        parts = e.get("pos", "").split()
        end = next((pt(p) for p in parts if p.startswith("e,")), None)
        pts = [pt(p) for p in parts if not p.startswith(("e,", "s,"))]
        routes.append((names[e["tail"]], names[e["head"]], pts, end))
    return pos, routes, clusters, (width, height)


def _orient(p: Point, q: Point, r: Point) -> int:
    v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (v > 0) - (v < 0)


def _crosses(a: Point, b: Point, c: Point, d: Point) -> bool:
    return _orient(a, b, c) * _orient(a, b, d) < 0 and _orient(c, d, a) * _orient(c, d, b) < 0


def _box(pts: list[Point]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


CELL = 120.0  # px: the grid that finds segments near each other


def _cells(box: tuple[float, float, float, float]) -> Iterator[tuple[int, int]]:
    for gx in range(int(box[0] // CELL), int(box[2] // CELL) + 1):
        for gy in range(int(box[1] // CELL), int(box[3] // CELL) + 1):
            yield gx, gy


def crossings(routes: Sequence[Route]) -> int:
    """Pairs of drawn edges that cross. Edges between the same two objects count once,
    and edges that share an object never count: the measure the page shows. A curve is
    taken as the line through its control points. Only segments that share a cell of a
    grid are tested against each other."""
    seen, lines = set(), []
    for s, t, pts in routes:
        if len(pts) >= 2 and (k := tuple(sorted((s, t)))) not in seen:
            seen.add(k)
            lines.append((s, t, pts))
    grid: dict[tuple[int, int], list[tuple[int, Point, Point]]] = defaultdict(list)
    for li, (_, _, pts) in enumerate(lines):
        for a, b in pairwise(pts):
            box = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
            for cell in _cells(box):
                grid[cell].append((li, a, b))
    found: set[tuple[int, int]] = set()
    for segments in grid.values():
        for i, (l1, a, b) in enumerate(segments):
            for l2, c, d in segments[i + 1:]:
                if l1 == l2:
                    continue
                pair = (l1, l2) if l1 < l2 else (l2, l1)
                if pair in found:
                    continue
                (s1, t1, _), (s2, t2, _) = lines[l1], lines[l2]
                if s1 in (s2, t2) or t1 in (s2, t2):
                    continue
                if _crosses(a, b, c, d):
                    found.add(pair)
    return len(found)


def through(routes: Sequence[Route], pos: dict[str, Point], size: tuple[int, int] = (W, H)) -> int:
    """Each edge and object it is drawn across without connecting, counted as a pair."""
    boxes = {k: (x - size[0] / 2 + 3, y - size[1] / 2 + 3, x + size[0] / 2 - 3, y + size[1] / 2 - 3)
             for k, (x, y) in pos.items()}
    grid: dict[tuple[int, int], list[str]] = defaultdict(list)
    for nid, box in boxes.items():
        for cell in _cells(box):
            grid[cell].append(nid)
    n = 0
    for s, t, pts in routes:
        hit: set[str] = set()
        for a, b in pairwise(pts):
            near = {nid for cell in _cells((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]),
                                            max(a[1], b[1]))) for nid in grid.get(cell, ())}
            for nid in near - hit - {s, t}:
                x0, y0, x1, y1 = boxes[nid]
                corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                if (x0 < a[0] < x1 and y0 < a[1] < y1) or (x0 < b[0] < x1 and y0 < b[1] < y1) \
                        or any(_crosses(a, b, corners[k], corners[(k + 1) % 4]) for k in range(4)):
                    hit.add(nid)
        n += len(hit)
    return n


def layered(nodes: Sequence[dict], edges: Sequence[dict], *, direction: str, group: bool,
            tries: int, whole: bool = False, size: tuple[int, int] = (W, H),
            gap: int = 24, layer: int = 90) -> dict:
    """The best of `tries` drawings by `dot`. With `whole`, keep its curves and cluster
    boxes too, for a view of everything that the page draws as built."""
    best: tuple | None = None
    first = None
    for seed in range(tries):
        rng = random.Random(seed)
        ns, es = sorted(nodes, key=lambda n: n["id"]), list(edges)
        if seed:
            rng.shuffle(ns)
            rng.shuffle(es)
        g = _run("dot", _source(ns, es, direction=direction, group=group, size=size, gap=gap,
                                layer=layer))
        pos, routes, clusters, extent = _drawing(g)
        lines = [(s, t, pts) for s, t, pts, _ in routes]
        counted = (crossings(lines), through(lines, pos, size)) if tries > 1 else (None, None)
        score = counted[0] + 2 * counted[1] if counted[0] is not None else 0
        first = score if first is None else first
        if best is None or score < best[0]:
            best = (score, counted, pos, routes, clusters, extent)
    assert best is not None
    score, counted, pos, routes, clusters, extent = best

    def r1(p: Point) -> list[float]:
        return [round(p[0], 1), round(p[1], 1)]

    out: dict = {"pos": {k: [round(x), round(y)] for k, (x, y) in pos.items()},
                 "crossings": counted[0], "through": counted[1],
                 "score": score if tries > 1 else None, "first": first if tries > 1 else None,
                 "tries": tries}
    if whole:
        out |= {"routes": [[s, t, [r1(p) for p in pts], end and r1(end)]
                           for s, t, pts, end in routes],
                "groups": [[label, *map(round, box)] for label, *box in clusters],
                "size": [round(extent[0]), round(extent[1])], "card": list(size)}
    return out


def free(nodes: Sequence[dict], edges: Sequence[dict], *, tries: int) -> dict:
    """The best of `tries` stress layouts by `neato`, scored on straight lines."""
    # Graphviz's overlap removal takes most of a minute at 900 objects; the page removes
    # overlaps in each view anyway (keeping which object is left of, or above, which)
    overlap = "false" if len(nodes) <= BIG else "true"
    src = _source(sorted(nodes, key=lambda n: n["id"]), edges, direction="right", group=False)
    src = src.replace("digraph G {", f'digraph G {{ mode=major; overlap={overlap}; sep="+16"; '
                                     f'len={360 / PX:.2f};', 1)
    best: tuple | None = None
    first = None
    for seed in range(1, tries + 1):
        pos, _, _, _ = _drawing(_run("neato", src, f"-Gstart={seed}"))
        lines = [(e["s"], e["t"], [pos[e["s"]], pos[e["t"]]]) for e in edges]
        counted = (crossings(lines), through(lines, pos)) if tries > 1 else (None, None)
        score = counted[0] + 2 * counted[1] if counted[0] is not None else 0
        first = score if first is None else first
        if best is None or score < best[0]:
            best = (score, counted, pos)
    assert best is not None
    return {"pos": {k: [round(x), round(y)] for k, (x, y) in best[2].items()},
            "crossings": best[1][0], "through": best[1][1],
            "score": best[0] if tries > 1 else None, "first": first if tries > 1 else None,
            "tries": tries}


def build(nodes: Sequence[dict], edges: Sequence[dict], tries: int | None = None) -> dict:
    """Every arrangement the page offers, and the map. `nodes` have `id`, `kind` and
    `schema`; `edges` have `s`, `t` and `kind` (flow, join, runtime or fk)."""
    given, tries = tries, tries or tries_for(len(nodes))
    out = {}
    for direction in ("right", "down"):
        for group in (False, True):
            out[f"{direction}{'/grouped' if group else ''}"] = layered(
                nodes, edges, direction=direction, group=group, tries=tries)
    out["free"] = free(nodes, edges, tries=tries)
    # the map: objects in the catalog only (no files or outside objects), no keys
    tables = [n for n in nodes if n["kind"] in ("table", "view")]
    ids = {n["id"] for n in tables}
    inside = [e for e in edges if e["kind"] != "fk" and e["s"] in ids and e["t"] in ids]
    out["map"] = layered(tables, inside, direction="right", group=True, tries=tries, whole=True,
                         size=(180, 34), gap=14, layer=50)
    out["schemas"] = overview(tables, inside, tries=given)
    return out


SCHEMA_CARD = (220, 58)


def overview(tables: Sequence[dict], edges: Sequence[dict], *, tries: int | None = None) -> dict:
    """The map's overview: one box per schema, and an edge wherever a table's data
    reaches another schema's, weighted by how many table edges it stands for. A big
    catalog's map is too tall to read whole, since its schemas stack, so the page
    opens on this."""
    schema = {n["id"]: n["schema"] for n in tables}
    weight: dict[tuple[str, str], int] = defaultdict(int)
    for e in edges:
        a, b = schema[e["s"]], schema[e["t"]]
        if a != b:
            weight[a, b] += 1
    names = sorted(set(schema.values()))
    return layered([{"id": s, "kind": "schema", "schema": s} for s in names],
                   [{"s": a, "t": b, "kind": "flow", "weight": min(w, 100)}
                    for (a, b), w in sorted(weight.items())],
                   direction="right", group=False, tries=tries or tries_for(len(names)), whole=True,
                   size=SCHEMA_CARD, gap=30, layer=90)
