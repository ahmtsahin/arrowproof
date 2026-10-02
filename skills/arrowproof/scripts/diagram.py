"""Read, mark and build Excalidraw diagrams.

The arrowproof metadata lives in `customData.arrowproof` on each element:
  box:   {"paths": ["src/api"], "packages": ["stripe"], "skip": false}
  arrow: {"kind": "uses" | "flow" | "skip"}
"""
from __future__ import annotations

import math
import random
import re
import textwrap
import time
from collections import defaultdict
from dataclasses import dataclass, field

SHAPES = {"rectangle", "ellipse", "diamond", "frame", "magicframe", "image",
          "embeddable", "iframe"}

COLORS = {
    "verified": "#2f9e44",
    "indirect": "#1098ad",
    "not_in_code": "#e03131",
    "reversed": "#f08c00",
    "unchecked": "#868e96",
    "undrawn": "#9c36b5",
    "ink": "#1e1e1e",
}
FONT_FAMILY = 5          # Excalifont, the current Excalidraw default
_RNG = random.Random(7)  # stable seeds make stable files


@dataclass
class Node:
    id: str
    el: dict
    label: str
    paths: list = field(default_factory=list)
    packages: list = field(default_factory=list)
    skip: bool = False
    source: str = ""     # explicit | link | label
    note: str = ""


@dataclass
class Arrow:
    id: str
    el: dict
    src: str | None
    dst: str | None
    undirected: bool
    kind: str            # uses | flow | skip
    label: str
    attached: str = ""   # bound | near | ""
    declared: bool = False   # the kind comes from customData, not from the arrowheads


def live(data: dict) -> list:
    return [e for e in data.get("elements", [])
            if isinstance(e, dict) and e.get("id") and not e.get("isDeleted")]


def bbox(el: dict) -> tuple:
    if el.get("type") in ("arrow", "line", "freedraw") and el.get("points"):
        xs = [el["x"] + p[0] for p in el["points"]]
        ys = [el["y"] + p[1] for p in el["points"]]
        return min(xs), min(ys), max(xs), max(ys)
    x, y = el.get("x", 0), el.get("y", 0)
    w, h = el.get("width", 0), el.get("height", 0)
    return min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h)


def center(el: dict) -> tuple:
    x0, y0, x1, y1 = bbox(el)
    return (x0 + x1) / 2, (y0 + y1) / 2


def _rect_distance(box: tuple, x: float, y: float) -> float:
    x0, y0, x1, y1 = box
    dx = max(x0 - x, 0, x - x1)
    dy = max(y0 - y, 0, y - y1)
    return math.hypot(dx, dy)


def _area(box: tuple) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _metadata(el: dict) -> dict:
    data = el.get("customData") or {}
    meta = data.get("arrowproof") if isinstance(data, dict) else None
    return meta if isinstance(meta, dict) else {}


def _as_list(value) -> list:
    if isinstance(value, str):
        return [value]
    return [v for v in value or [] if isinstance(v, str)]


def parse(data: dict) -> tuple:
    """Return ({node_id: Node}, [Arrow])."""
    els = live(data)
    by_id = {e["id"]: e for e in els}
    bound_text = {}
    for e in els:
        if e.get("type") == "text" and e.get("containerId"):
            bound_text[e["containerId"]] = (e.get("originalText") or e.get("text") or "").strip()
    raw_arrows = [e for e in els if e.get("type") == "arrow"]
    bound_ids = set()
    for a in raw_arrows:
        for key in ("startBinding", "endBinding"):
            target = (a.get(key) or {}).get("elementId")
            if target:
                bound_ids.add(target)

    shapes = [e for e in els if e.get("type") in SHAPES]
    free_texts = [e for e in els if e.get("type") == "text" and not e.get("containerId")]
    inside: dict = {}
    for t in free_texts:
        if t["id"] in bound_ids:
            continue
        cx, cy = center(t)
        hosts = [s for s in shapes if _rect_distance(bbox(s), cx, cy) == 0]
        if hosts:
            host = min(hosts, key=lambda s: _area(bbox(s)))
            inside.setdefault(host["id"], []).append(t)

    nodes = {}
    for s in shapes:
        label = bound_text.get(s["id"]) or s.get("name") or ""
        if not label and s["id"] in inside:
            texts = sorted(inside[s["id"]], key=lambda t: (t.get("y", 0), t.get("x", 0)))
            label = "\n".join((t.get("text") or "").strip() for t in texts)
        nodes[s["id"]] = _node(s, label)
    for t in free_texts:
        if t["id"] in bound_ids:
            nodes[t["id"]] = _node(t, t.get("text") or "")

    arrows = []
    for a in raw_arrows:
        pts = a.get("points") or [[0, 0]]
        start = (a.get("x", 0) + pts[0][0], a.get("y", 0) + pts[0][1])
        end = (a.get("x", 0) + pts[-1][0], a.get("y", 0) + pts[-1][1])
        s_id, s_how = _endpoint(a.get("startBinding"), start, nodes, by_id)
        e_id, e_how = _endpoint(a.get("endBinding"), end, nodes, by_id)
        head_start, head_end = a.get("startArrowhead"), a.get("endArrowhead")
        if head_start and not head_end:
            src, dst, undirected = e_id, s_id, False
        else:
            src, dst, undirected = s_id, e_id, not (head_end and not head_start)
        meta = _metadata(a)
        declared = meta.get("kind") in ("uses", "flow", "skip")
        kind = meta["kind"] if declared else ("flow" if undirected else "uses")
        how = "bound" if s_how == e_how == "bound" else ("near" if s_how and e_how else "")
        arrows.append(Arrow(a["id"], a, src, dst, undirected, kind,
                            bound_text.get(a["id"], ""), how, declared))
    return nodes, arrows


def _node(el: dict, label: str) -> Node:
    meta = _metadata(el)
    stored = meta.get("label")
    # The box text may be wrapped. Use the label stored at build time, unless
    # someone has since edited the text in Excalidraw.
    if isinstance(stored, str) and re.sub(r"\s+", "", label).startswith(re.sub(r"\s+", "", stored)):
        label = stored
    node = Node(el["id"], el, label.strip())
    node.paths = [p.strip().strip("/").removeprefix("./") for p in _as_list(meta.get("paths") or meta.get("path"))]
    node.packages = _as_list(meta.get("packages") or meta.get("package"))
    node.skip = bool(meta.get("skip"))
    if node.paths or node.packages or node.skip:
        node.source = "explicit"
    else:
        link = el.get("link")
        if isinstance(link, str) and link and "://" not in link and not link.startswith("#"):
            node.paths = [link.strip().strip("/").removeprefix("./")]
            node.source = "link"
    return node


def _endpoint(binding, point: tuple, nodes: dict, by_id: dict) -> tuple:
    target = (binding or {}).get("elementId")
    if target:
        el = by_id.get(target)
        if el and el.get("type") == "text" and el.get("containerId"):
            target = el["containerId"]
        if target in nodes:
            return target, "bound"
    best = None
    for node in nodes.values():
        box = bbox(node.el)
        dist = _rect_distance(box, *point)
        if dist <= 30:
            key = (dist, _area(box))
            if best is None or key < best[0]:
                best = (key, node.id)
    return (best[1], "near") if best else (None, "")


# ------------------------------------------------------------------ editing

def _bump(el: dict) -> None:
    el["version"] = int(el.get("version", 1)) + 1
    el["versionNonce"] = _RNG.randrange(1, 2**31)
    el["updated"] = int(time.time() * 1000)


def new_element(kind: str, x: float, y: float, w: float, h: float, **extra) -> dict:
    el = {
        "id": extra.pop("id", None) or f"ap-{_RNG.randrange(16**10):010x}",
        "type": kind, "x": x, "y": y, "width": w, "height": h, "angle": 0,
        "strokeColor": COLORS["ink"], "backgroundColor": "transparent",
        "fillStyle": "solid", "strokeWidth": 2, "strokeStyle": "solid",
        "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None,
        "roundness": None, "seed": _RNG.randrange(1, 2**31), "version": 1,
        "versionNonce": _RNG.randrange(1, 2**31), "isDeleted": False,
        "boundElements": [], "updated": int(time.time() * 1000), "link": None,
        "locked": False,
    }
    el.update(extra)
    return el


def text_size(text: str, size: float) -> tuple:
    lines = text.split("\n")
    return max(len(line) for line in lines) * size * 0.6, len(lines) * size * 1.25


def new_text(text: str, x: float, y: float, size: float = 16, color: str = COLORS["ink"],
             container: str | None = None, align: str = "left", **extra) -> dict:
    w, h = text_size(text, size)
    return new_element(
        "text", x, y, w, h, strokeColor=color, text=text, originalText=text,
        fontSize=size, fontFamily=FONT_FAMILY, textAlign=align,
        verticalAlign="middle" if container else "top", containerId=container,
        autoResize=True, lineHeight=1.25, **extra)


def mark_arrow(el: dict, verdict: str, keep_colors: bool = False) -> None:
    if verdict == "verified":
        if not keep_colors:
            el["strokeColor"] = COLORS["verified"]
        el["strokeStyle"] = "solid"
    else:
        el["strokeColor"] = COLORS[verdict]
        el["strokeStyle"] = "dotted" if verdict == "unchecked" else "dashed"
        if verdict in ("not_in_code", "reversed"):
            el["strokeWidth"] = max(float(el.get("strokeWidth") or 2), 3)
    _bump(el)


def mark_box(el: dict) -> None:
    el["strokeColor"] = COLORS["not_in_code"]
    el["strokeStyle"] = "dashed"
    _bump(el)


def midpoint(el: dict) -> tuple:
    pts = el.get("points") or [[0, 0]]
    if len(pts) == 1:
        return el["x"] + pts[0][0], el["y"] + pts[0][1]
    lengths = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    half, run = sum(lengths) / 2, 0.0
    for i, seg in enumerate(lengths):
        if run + seg >= half and seg > 0:
            t = (half - run) / seg
            return (el["x"] + pts[i][0] + t * (pts[i + 1][0] - pts[i][0]),
                    el["y"] + pts[i][1] + t * (pts[i + 1][1] - pts[i][1]))
        run += seg
    return el["x"] + pts[-1][0], el["y"] + pts[-1][1]


def label_anchor(el: dict) -> tuple:
    """Where Excalidraw draws the label of an arrow, whatever x and y it stores.

    With an odd number of points it is the middle point. Otherwise it is the
    middle of the middle segment, on the curve when the arrow is round.
    """
    pts = el.get("points") or [[0, 0]]
    x, y, n = el.get("x", 0), el.get("y", 0), len(pts)
    if n % 2:
        return x + pts[n // 2][0], y + pts[n // 2][1]
    i = n // 2 - 1
    p, q = pts[i], pts[i + 1]
    if n > 2 and el.get("roundness"):
        p0 = pts[i - 1] if i > 0 else p
        p3 = pts[i + 2] if i + 2 < n else q
        c1 = (p[0] + (q[0] - p0[0]) / 6, p[1] + (q[1] - p0[1]) / 6)
        c2 = (q[0] - (p3[0] - p[0]) / 6, q[1] - (p3[1] - p[1]) / 6)
        return (x + (p[0] + 3 * c1[0] + 3 * c2[0] + q[0]) / 8,
                y + (p[1] + 3 * c1[1] + 3 * c2[1] + q[1]) / 8)
    return x + (p[0] + q[0]) / 2, y + (p[1] + q[1]) / 2


def _collinear(pts: list, tolerance: float = 0.5) -> bool:
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    length = math.hypot(x1 - x0, y1 - y0)
    if not length:
        return False
    return all(abs((x1 - x0) * (y0 - y) - (x0 - x) * (y1 - y0)) / length <= tolerance
               for x, y in pts[1:-1])


def _boundary(el: dict, toward: tuple, gap: float = 6) -> tuple:
    cx, cy = center(el)
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == dy == 0:
        return cx, cy
    x0, y0, x1, y1 = bbox(el)
    hw, hh = max((x1 - x0) / 2, 1), max((y1 - y0) / 2, 1)
    kind = el.get("type")
    if kind == "ellipse":
        t = 1 / math.sqrt((dx / hw) ** 2 + (dy / hh) ** 2)
    elif kind == "diamond":
        t = 1 / (abs(dx) / hw + abs(dy) / hh)
    else:
        t = min(hw / abs(dx) if dx else math.inf, hh / abs(dy) if dy else math.inf)
    norm = math.hypot(dx, dy)
    return cx + dx * t + dx / norm * gap, cy + dy * t + dy / norm * gap


def connect(src_el: dict, dst_el: dict, waypoints: list | None = None, **extra) -> dict:
    """An arrow between two shapes, bound on both ends, through optional waypoints."""
    way = [tuple(p) for p in waypoints or []]
    a = _boundary(src_el, way[0] if way else center(dst_el))
    b = _boundary(dst_el, way[-1] if way else center(src_el))
    pts = [a, *way, b]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    arrow = new_element(
        "arrow", a[0], a[1], max(xs) - min(xs), max(ys) - min(ys),
        points=[[p[0] - a[0], p[1] - a[1]] for p in pts], lastCommittedPoint=None,
        startBinding={"elementId": src_el["id"], "focus": 0, "gap": 6},
        endBinding={"elementId": dst_el["id"], "focus": 0, "gap": 6},
        startArrowhead=None, endArrowhead="arrow", elbowed=False,
        roundness={"type": 2} if way and not _collinear(pts) else None, **extra)
    for el in (src_el, dst_el):
        el.setdefault("boundElements", [])
        if el["boundElements"] is None:
            el["boundElements"] = []
        el["boundElements"].append({"type": "arrow", "id": arrow["id"]})
    return arrow


def label_arrow(arrow: dict, text: str, size: float = 14, color: str = COLORS["ink"]) -> dict:
    text = _wrap(text, 24)
    mx, my = label_anchor(arrow)
    w, h = text_size(text, size)
    label = new_text(text, mx - w / 2, my - h / 2, size, color, container=arrow["id"], align="center")
    arrow.setdefault("boundElements", [])
    arrow["boundElements"].append({"type": "text", "id": label["id"]})
    return label


# ------------------------------------------------------------------ building

def _layering(ids: list, pairs: list, sinks_last=()) -> dict:
    """Longest-path layers after the cycles are broken. Returns {id: layer}."""
    succ = {i: [] for i in ids}
    for u, v in pairs:
        if u != v and v not in succ[u]:
            succ[u].append(v)
    state, back = {}, set()
    for root in ids:
        if state.get(root):
            continue
        state[root] = 1
        stack = [(root, iter(succ[root]))]
        while stack:
            u, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[u] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                back.add((u, nxt))
            elif not state.get(nxt):
                state[nxt] = 1
                stack.append((nxt, iter(succ[nxt])))
    dag = {i: [] for i in ids}
    indeg = {i: 0 for i in ids}
    for u in ids:
        for v in succ[u]:
            a, b = (v, u) if (u, v) in back else (u, v)
            if b not in dag[a]:
                dag[a].append(b)
                indeg[b] += 1
    layer = {i: 0 for i in ids}
    queue = [i for i in ids if indeg[i] == 0]
    while queue:
        u = queue.pop(0)
        for v in dag[u]:
            layer[v] = max(layer[v], layer[u] + 1)
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    top = max(layer.values(), default=0)
    for n in sinks_last:          # third-party packages line up in the last column
        if n in dag and not dag[n] and any(n in dag[u] for u in ids):
            layer[n] = top
    return layer


def _order(groups: list, succ: dict, pred: dict) -> None:
    """Barycenter sweeps that reduce crossings, in place."""
    pos = {s: n for g in groups for n, s in enumerate(g)}

    def reorder(group, neighbours):
        group.sort(key=lambda s: sum(pos[n] for n in neighbours[s]) / len(neighbours[s])
                   if neighbours[s] else pos[s])
        for n, s in enumerate(group):
            pos[s] = n

    for _ in range(6):
        for g in groups[1:]:
            reorder(g, pred)
        for g in reversed(groups[:-1]):
            reorder(g, succ)


def _spread(desired: list, sizes: list, gap: float) -> list:
    """Centers on one line, each as near its desired place as it can be with
    no overlap. Overlapping neighbours merge into a block centered on their
    mean, until no two blocks overlap."""
    blocks = []                      # [items, offsets from the first center, first center]
    for i in sorted(range(len(desired)), key=lambda i: desired[i]):
        block = [[i], [0.0], float(desired[i])]
        while blocks:
            items, offs, x = blocks[-1]
            reach = offs[-1] + sizes[items[-1]] / 2 + gap + sizes[block[0][0]] / 2
            if x + reach <= block[2]:
                break
            blocks.pop()
            items, offs = items + block[0], offs + [reach + o for o in block[1]]
            block = [items, offs, sum(desired[j] - o for j, o in zip(items, offs)) / len(items)]
        blocks.append(block)
    out = [0.0] * len(desired)
    for items, offs, x in blocks:
        for j, o in zip(items, offs):
            out[j] = x + o
    return out


def _wrap(text: str, width: int) -> str:
    lines = []
    for line in text.split("\n"):
        lines += textwrap.wrap(line, width, break_long_words=True, break_on_hyphens=False) or [""]
    return "\n".join(lines)


def _box_text(node: dict) -> str:
    label = node.get("label") or node["id"]
    text = _wrap(label, 26)
    if node.get("show_paths", True):
        paths = _as_list(node.get("paths"))
        packages = _as_list(node.get("packages") or node.get("package"))
        hint = ", ".join(paths[:2]) + (f" +{len(paths) - 2}" if len(paths) > 2 else "")
        if not hint and packages:
            hint = "package: " + ", ".join(packages[:2])
        if hint and hint not in label:
            text += "\n" + _wrap(hint, 32)
    return text


def build(spec: dict, aspect: float = 1.6) -> dict:
    """Lay out a spec ({title, direction, nodes, edges}) as an Excalidraw file.

    `direction` is LR, TB or absent. When absent, the layout whose width to
    height ratio comes closer to `aspect` wins (1.6 suits a wide screen).
    """
    direction = str(spec.get("direction") or "auto").upper()
    if direction in ("LR", "TB"):
        return _build(spec, direction == "LR")
    best = None
    for horizontal in (True, False):
        data = _build(spec, horizontal)
        boxes = [bbox(e) for e in data["elements"]]
        width = max(b[2] for b in boxes) - min(b[0] for b in boxes)
        height = max(b[3] for b in boxes) - min(b[1] for b in boxes)
        miss = abs(math.log(max(width, 1) / max(height, 1) / aspect))
        if best is None or miss < best[0]:
            best = (miss, data)
    return best[1]


def _build(spec: dict, horizontal: bool) -> dict:
    nodes = spec.get("nodes") or []
    ids = [n["id"] for n in nodes]
    if len(set(ids)) != len(ids):
        raise ValueError("node ids must be unique")
    known = set(ids)
    edges = spec.get("edges") or []
    for e in edges:
        if e.get("from") not in known or e.get("to") not in known:
            raise ValueError(f"edge {e.get('from')} -> {e.get('to')} names an unknown node")
    by_id = {n["id"]: n for n in nodes}
    packages_only = [n["id"] for n in nodes if _as_list(n.get("packages") or n.get("package"))
                     and not _as_list(n.get("paths") or n.get("path"))]
    pairs = [(e["from"], e["to"]) for e in edges]
    layer = _layering(ids, pairs, packages_only)

    # An edge that skips layers passes through placeholder slots, so it bends
    # around the boxes in between instead of crossing them.
    groups = [[] for _ in range(max(layer.values(), default=0) + 1)]
    for i in ids:
        groups[layer[i]].append(i)
    succ, pred = defaultdict(list), defaultdict(list)
    chains = []
    for k, (u, v) in enumerate(pairs):
        if u == v:
            chains.append(None)
            continue
        a, b = (u, v) if layer[u] < layer[v] else (v, u)
        chain = [a]
        for lay in range(layer[a] + 1, layer[b]):
            slot = f"~{k}~{lay}"
            groups[lay].append(slot)
            chain.append(slot)
        chain.append(b)
        for x, y in zip(chain, chain[1:]):
            succ[x].append(y)
            pred[y].append(x)
        chains.append((chain, a != u))
    _order(groups, succ, pred)

    size = 18
    boxes = {}
    for n in nodes:
        w, h = text_size(_box_text(n), size)
        boxes[n["id"]] = (max(160, w + 48), max(72, h + 36))
    slot_extent = 8
    gap_main, gap_cross = 150, 48

    # A label takes room in the layout, as an edge label does in dot. It sits
    # on the middle point of its arrow, which is where Excalidraw draws it:
    # in the middle slot of a long arrow, or else in the gap between layers.
    room, in_gap = {}, {}
    for k, e in enumerate(edges):
        if not e.get("label") or chains[k] is None:
            continue
        chain = chains[k][0]
        span = len(chain) - 1
        w, h = text_size(_wrap(str(e["label"]), 24), 14)
        if span % 2:
            in_gap[k] = layer[chain[0]] + span // 2
            room[f"~L{k}"] = (w + 12, h + 8)
        else:
            room[chain[span // 2]] = (w + 12, h + 8)

    def along(s):    # size in the direction of the flow
        size_ = boxes.get(s) or room.get(s)
        return size_[0 if horizontal else 1] if size_ else 0

    def across(s):   # size across the flow
        size_ = boxes.get(s) or room.get(s)
        return size_[1 if horizontal else 0] if size_ else slot_extent

    lengths = [max((along(s) for s in g), default=0) for g in groups]
    extents = [sum(across(s) for s in g) + gap_cross * (len(g) - 1) for g in groups]
    widest = max(extents, default=0)
    gaps = [gap_main] * len(groups)
    for k, g in in_gap.items():
        gaps[g] = max(gaps[g], along(f"~L{k}") + 60)

    elements, shapes, centers = [], {}, {}
    title_h = 0
    if spec.get("title"):
        title = new_text(spec["title"], 0, 0, 28, id="ap-title")
        elements.append(title)
        title_h = title["height"] + 48
    offset, starts = 0.0, []
    for g, length, extent, gap in zip(groups, lengths, extents, gaps):
        starts.append(offset)
        cursor = (widest - extent) / 2
        for s in g:
            mid_main = offset + length / 2
            mid_cross = cursor + across(s) / 2
            cx, cy = (mid_main, title_h + mid_cross) if horizontal else (mid_cross, title_h + mid_main)
            centers[s] = (cx, cy)
            if s in boxes:
                w, h = boxes[s]
                shape = _shape(by_id[s], cx - w / 2, cy - h / 2, w, h)
                label = new_text(_box_text(by_id[s]), 0, 0, size, container=shape["id"],
                                 align="center", id=f"ap-label-{s}")
                label["x"] = cx - label["width"] / 2
                label["y"] = cy - label["height"] / 2
                shape["boundElements"] = [{"type": "text", "id": label["id"]}]
                shapes[s] = shape
                elements += [shape, label]
            cursor += across(s) + gap_cross
        offset += length + gap

    # Labels in a gap start where their arrow would cross the middle of the
    # gap, then move apart along it until no two overlap.
    main = 0 if horizontal else 1          # the axis of the flow, in (x, y)
    by_gap = defaultdict(list)
    for k, g in in_gap.items():
        by_gap[g].append(k)
    for g, ks in by_gap.items():
        line = starts[g] + lengths[g] + gaps[g] / 2 + (0 if horizontal else title_h)
        desired = []
        for k in ks:
            chain = chains[k][0]
            p, q = centers[chain[(len(chain) - 2) // 2]], centers[chain[len(chain) // 2]]
            t = (line - p[main]) / (q[main] - p[main]) if q[main] != p[main] else 0.5
            desired.append(p[1 - main] + t * (q[1 - main] - p[1 - main]))
        for k, c in zip(ks, _spread(desired, [across(f"~L{k}") for k in ks], 8)):
            centers[f"~L{k}"] = (line, c) if horizontal else (c, line)

    parallel = defaultdict(int)
    for k, e in enumerate(edges):
        if chains[k] is None:
            continue
        chain, flipped = chains[k]
        inner = list(chain[1:-1])
        if k in in_gap:
            inner.insert((len(chain) - 2) // 2, f"~L{k}")
        way = [centers[s] for s in inner]
        if flipped:
            way.reverse()
        key = frozenset((e["from"], e["to"]))
        nth = parallel[key]
        parallel[key] += 1
        if nth and not way:   # a second arrow between the same two boxes bows out
            (x1, y1), (x2, y2) = centers[e["from"]], centers[e["to"]]
            dist = math.hypot(x2 - x1, y2 - y1) or 1
            bend = 28 * ((nth + 1) // 2) * (1 if nth % 2 else -1)
            way = [((x1 + x2) / 2 - (y2 - y1) / dist * bend, (y1 + y2) / 2 + (x2 - x1) / dist * bend)]
        arrow = connect(shapes[e["from"]], shapes[e["to"]], way, id=f"ap-edge-{k}",
                        customData={"arrowproof": {"kind": e.get("kind", "uses")}})
        elements.append(arrow)
        if e.get("label"):
            elements.append(label_arrow(arrow, e["label"]))
    return {"type": "excalidraw", "version": 2, "source": "arrowproof",
            "elements": elements,
            "appState": {"viewBackgroundColor": "#ffffff", "gridSize": 20}, "files": {}}


def _shape(node: dict, x: float, y: float, w: float, h: float) -> dict:
    paths = _as_list(node.get("paths") or node.get("path"))
    packages = _as_list(node.get("packages") or node.get("package"))
    meta = {"label": node.get("label") or node["id"]}
    if paths:
        meta["paths"] = paths
    if packages:
        meta["packages"] = packages
    if node.get("skip") or not (paths or packages):
        meta["skip"] = True
    common = {"id": f"ap-node-{node['id']}", "customData": {"arrowproof": meta}}
    if paths:
        return new_element("rectangle", x, y, w, h, strokeColor="#1971c2",
                           backgroundColor="#d0ebff", roundness={"type": 3}, **common)
    if packages:
        return new_element("ellipse", x, y, w, h, strokeColor="#495057",
                           backgroundColor="#e9ecef", **common)
    return new_element("ellipse", x, y, w, h, strokeColor="#868e96", **common)
