"""Draw Excalidraw elements as SVG and write the HTML evidence report.

No dependencies. The SVG is a clean approximation of Excalidraw's look:
shapes, text, arrows, arrowheads, dashes and colors. It is not pixel-exact.
"""
from __future__ import annotations

import html
import json
import math
from pathlib import Path

import diagram as dg

FONTS = {
    1: "Virgil, 'Segoe Print', 'Comic Sans MS', 'Chalkboard SE', cursive",
    5: "Excalifont, Virgil, 'Segoe Print', 'Comic Sans MS', 'Chalkboard SE', cursive",
    2: "Helvetica, Arial, sans-serif",
    6: "Nunito, 'Segoe UI', Arial, sans-serif",
    3: "'Cascadia Code', Consolas, Menlo, monospace",
    8: "'Comic Shanns', 'Cascadia Code', Consolas, monospace",
    7: "'Lilita One', Impact, sans-serif",
}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _n(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _pt(p) -> str:
    return f"{_n(p[0])} {_n(p[1])}"


def _paint(e: dict, fill: bool = True) -> str:
    stroke = e.get("strokeColor") or "#1e1e1e"
    width = float(e.get("strokeWidth") or 2)
    bg = e.get("backgroundColor") or "transparent"
    color = "none" if (bg == "transparent" or not fill) else bg
    attrs = [f'stroke="{_esc(stroke)}"', f'stroke-width="{_n(width)}"', f'fill="{_esc(color)}"',
             'stroke-linejoin="round"']
    if color != "none" and e.get("fillStyle") in ("hachure", "cross-hatch", "zigzag"):
        attrs.append('fill-opacity="0.45"')
    style = e.get("strokeStyle")
    if style == "dashed":
        attrs.append(f'stroke-dasharray="{_n(width * 4)} {_n(width * 3)}"')
    elif style == "dotted":
        attrs.append(f'stroke-dasharray="{_n(width * 0.6)} {_n(width * 3)}" stroke-linecap="round"')
    opacity = e.get("opacity", 100)
    if isinstance(opacity, (int, float)) and opacity < 100:
        attrs.append(f'opacity="{_n(opacity / 100)}"')
    return " ".join(attrs)


def _rotate(e: dict) -> str:
    angle = e.get("angle") or 0
    if not angle:
        return ""
    cx, cy = dg.center(e)
    return f' transform="rotate({_n(math.degrees(angle))} {_n(cx)} {_n(cy)})"'


def _shape(e: dict) -> str:
    x0, y0, x1, y1 = dg.bbox(e)
    w, h = x1 - x0, y1 - y0
    kind, rot = e.get("type"), _rotate(e)
    if kind == "ellipse":
        return (f'<ellipse cx="{_n(x0 + w / 2)}" cy="{_n(y0 + h / 2)}" rx="{_n(w / 2)}" '
                f'ry="{_n(h / 2)}" {_paint(e)}{rot}/>')
    if kind == "diamond":
        pts = [(x0 + w / 2, y0), (x1, y0 + h / 2), (x0 + w / 2, y1), (x0, y0 + h / 2)]
        return f'<polygon points="{" ".join(_pt(p) for p in pts)}" {_paint(e)}{rot}/>'
    if kind in ("frame", "magicframe"):
        name = _esc(e.get("name") or "Frame")
        return (f'<rect x="{_n(x0)}" y="{_n(y0)}" width="{_n(w)}" height="{_n(h)}" rx="6" '
                f'fill="none" stroke="#adb5bd" stroke-width="1"/>'
                f'<text x="{_n(x0)}" y="{_n(y0 - 8)}" font-size="14" fill="#868e96" '
                f'font-family="{_esc(FONTS[2])}">{name}</text>')
    radius = 0.0
    rnd = e.get("roundness")
    if kind == "rectangle" and isinstance(rnd, dict):
        radius = min(32, 0.25 * min(w, h)) if rnd.get("type") == 3 else 0.25 * min(w, h)
    body = (f'<rect x="{_n(x0)}" y="{_n(y0)}" width="{_n(w)}" height="{_n(h)}" '
            f'rx="{_n(radius)}" {_paint(e)}{rot}/>')
    if kind != "rectangle":   # image, embeddable, iframe
        body = (f'<rect x="{_n(x0)}" y="{_n(y0)}" width="{_n(w)}" height="{_n(h)}" fill="#f1f3f5" '
                f'stroke="#adb5bd" stroke-dasharray="6 4"{rot}/>'
                f'<text x="{_n(x0 + w / 2)}" y="{_n(y0 + h / 2)}" text-anchor="middle" '
                f'font-size="14" fill="#868e96">{_esc(kind)}</text>')
    return body


def _text(e: dict) -> str:
    size = float(e.get("fontSize") or 20)
    line_h = float(e.get("lineHeight") or 1.25)
    family = FONTS.get(e.get("fontFamily"), FONTS[1])
    align = e.get("textAlign") or "left"
    anchor = {"center": "middle", "right": "end"}.get(align, "start")
    width = float(e.get("width") or 0)
    x = e.get("x", 0) + {"center": width / 2, "right": width}.get(align, 0)
    spans = []
    for i, line in enumerate(str(e.get("text") or "").split("\n")):
        y = e.get("y", 0) + i * size * line_h + size * line_h / 2 + size * 0.35
        spans.append(f'<tspan x="{_n(x)}" y="{_n(y)}">{_esc(line) or "&#160;"}</tspan>')
    opacity = e.get("opacity", 100)
    extra = f' opacity="{_n(opacity / 100)}"' if isinstance(opacity, (int, float)) and opacity < 100 else ""
    return (f'<text font-family="{_esc(family)}" font-size="{_n(size)}" '
            f'fill="{_esc(e.get("strokeColor") or "#1e1e1e")}" text-anchor="{anchor}"'
            f'{extra}{_rotate(e)}>{"".join(spans)}</text>')


def _path_d(points: list, smooth: bool) -> str:
    if not smooth or len(points) < 3:
        return "M" + " L".join(_pt(p) for p in points)
    d = [f"M{_pt(points[0])}"]
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{_pt(c1)} {_pt(c2)} {_pt(p2)}")
    return " ".join(d)


def _head(kind: str, tip: tuple, prev: tuple, e: dict) -> str:
    color = _esc(e.get("strokeColor") or "#1e1e1e")
    width = float(e.get("strokeWidth") or 2)
    angle = math.atan2(tip[1] - prev[1], tip[0] - prev[0])
    size = 10 + width * 3.5

    def back(spread: float, dist: float) -> tuple:
        a = angle + math.pi + spread
        return tip[0] + dist * math.cos(a), tip[1] + dist * math.sin(a)

    stroke = f'stroke="{color}" stroke-width="{_n(width)}" stroke-linejoin="round" stroke-linecap="round"'
    if kind in ("triangle", "triangle_outline"):
        fill = color if kind == "triangle" else "none"
        pts = " ".join(_pt(p) for p in (tip, back(0.4, size), back(-0.4, size)))
        return f'<polygon points="{pts}" fill="{fill}" {stroke}/>'
    if kind in ("dot", "circle", "circle_outline"):
        r = size * 0.3
        c = back(0, r)
        fill = "none" if kind == "circle_outline" else color
        return f'<circle cx="{_n(c[0])}" cy="{_n(c[1])}" r="{_n(r)}" fill="{fill}" {stroke}/>'
    if kind == "bar":
        a, r = angle + math.pi / 2, size * 0.45
        p1 = (tip[0] + r * math.cos(a), tip[1] + r * math.sin(a))
        p2 = (tip[0] - r * math.cos(a), tip[1] - r * math.sin(a))
        return f'<path d="M{_pt(p1)} L{_pt(p2)}" fill="none" {stroke}/>'
    if kind in ("diamond", "diamond_outline"):
        fill = color if kind == "diamond" else "none"
        pts = " ".join(_pt(p) for p in (tip, back(0.5, size * 0.6), back(0, size * 1.1),
                                         back(-0.5, size * 0.6)))
        return f'<polygon points="{pts}" fill="{fill}" {stroke}/>'
    return f'<path d="M{_pt(back(0.45, size))} L{_pt(tip)} L{_pt(back(-0.45, size))}" fill="none" {stroke}/>'


def _linear(e: dict, interactive: bool) -> str:
    pts = [(e.get("x", 0) + p[0], e.get("y", 0) + p[1]) for p in e.get("points") or []]
    if len(pts) < 2:
        return ""
    if e.get("type") == "freedraw":
        d = "M" + " L".join(_pt(p) for p in pts)
        return f'<path d="{d}" fill="none" stroke="{_esc(e.get("strokeColor") or "#1e1e1e")}" ' \
               f'stroke-width="{_n(float(e.get("strokeWidth") or 2) * 1.5)}" stroke-linecap="round" ' \
               f'stroke-linejoin="round"/>'
    d = _path_d(pts, bool(e.get("roundness")) and not e.get("elbowed"))
    parts = [f'<path class="vis" d="{d}" {_paint(e, fill=False)}/>']
    if e.get("type") == "arrow":
        if e.get("endArrowhead"):
            parts.append(_head(e["endArrowhead"], pts[-1], pts[-2], e))
        if e.get("startArrowhead"):
            parts.append(_head(e["startArrowhead"], pts[0], pts[1], e))
    if interactive and e.get("type") == "arrow":
        hit = f'<path class="hit" d="{d}" fill="none" stroke="transparent" stroke-width="18"/>'
        return (f'<g class="ap-edge" data-id="{_esc(e["id"])}" tabindex="0" role="button" '
                f'aria-label="arrow">{hit}{"".join(parts)}</g>')
    return "".join(parts)


def svg(elements: list, interactive: bool = False, pad: float = 40) -> str:
    els = [e for e in elements if isinstance(e, dict) and e.get("id") and not e.get("isDeleted")]
    boxes = [dg.bbox(e) for e in els]
    if not boxes:
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>'
    x0 = min(b[0] for b in boxes) - pad
    y0 = min(b[1] for b in boxes) - pad
    w = max(b[2] for b in boxes) + pad - x0
    h = max(b[3] for b in boxes) + pad - y0
    shape_ids = {e["id"] for e in els if e.get("type") in dg.SHAPES}
    arrow_ids = {e["id"] for e in els if e.get("type") == "arrow"}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_n(x0)} {_n(y0)} {_n(w)} {_n(h)}" '
           f'width="{_n(w)}" height="{_n(h)}" role="img">',
           f'<rect class="bg" x="{_n(x0)}" y="{_n(y0)}" width="{_n(w)}" height="{_n(h)}" fill="#ffffff"/>']
    for e in els:
        kind = e.get("type")
        if kind in ("arrow", "line", "freedraw"):
            out.append(_linear(e, interactive))
        elif kind == "text":
            body = _text(e)
            owner = e.get("containerId")
            meta = (e.get("customData") or {}).get("arrowproof") if isinstance(e.get("customData"), dict) else None
            note_for = meta.get("note_for") if isinstance(meta, dict) else None
            target = owner if owner in shape_ids | arrow_ids else note_for
            if interactive and target:
                body = f'<g class="ap-text" data-for="{_esc(target)}">{body}</g>'
            out.append(body)
        elif kind in dg.SHAPES:
            body = _shape(e)
            if interactive:
                body = (f'<g class="ap-node" data-id="{_esc(e["id"])}" tabindex="0" role="button" '
                        f'aria-label="box">{body}</g>')
            out.append(body)
    out.append("</svg>")
    return "".join(out)


def write_svg(path, elements: list) -> None:
    Path(path).write_text(svg(elements), encoding="utf-8")


def write_report(path, data: dict, result: dict, meta: dict) -> None:
    payload = {
        "arrows": result["arrows"], "nodes": result["nodes"], "undrawn": result["undrawn"],
        "counts": result["counts"], "box_counts": result["box_counts"], **meta,
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    title = Path(meta["diagram"]).name
    head, rest = _TEMPLATE.split("__SVG__")
    middle, tail = rest.split("__DATA__")
    page = (head.replace("__TITLE__", _esc(title)) + svg(dg.live(data), interactive=True)
            + middle + blob + tail)
    Path(path).write_text(page, encoding="utf-8")


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · arrowproof</title>
<style>
:root{--bg:#f6f5f1;--fg:#1d1d1b;--muted:#6b6a65;--card:#fff;--line:#e2e0d9;--soft:#efede7;
--ok:#2f9e44;--ind:#0c8599;--bad:#e03131;--rev:#d9480f;--gray:#868e96;--add:#9c36b5;--accent:#1971c2;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--fg:#ecebe5;--muted:#a3a29b;
--card:#1d1d1b;--line:#34332f;--soft:#262623;--ok:#51cf66;--ind:#3bc9db;--bad:#ff6b6b;--rev:#ff922b;--gray:#adb5bd;
--add:#da77f2;--accent:#74c0fc;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#141413;--fg:#ecebe5;--muted:#a3a29b;--card:#1d1d1b;--line:#34332f;--soft:#262623;
--ok:#51cf66;--ind:#3bc9db;--bad:#ff6b6b;--rev:#ff922b;--gray:#adb5bd;--add:#da77f2;--accent:#74c0fc;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header,main,section.lists,footer{max-width:1320px;margin:0 auto;padding:0 16px}
header{padding-top:28px;padding-bottom:8px}
.brand{font:600 13px/1 ui-monospace,"Cascadia Code",Consolas,monospace;letter-spacing:.06em;color:var(--muted);text-transform:uppercase}
h1{font-size:26px;margin:8px 0 4px;letter-spacing:-.01em;word-break:break-word}
.meta{color:var(--muted);margin:0;font-size:14px;word-break:break-word}
.chips{display:flex;flex-wrap:wrap;gap:8px;list-style:none;padding:0;margin:16px 0 0}
.chip{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);font-size:14px}
.chip b{font-size:16px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;background:currentColor}
.c-ok{color:var(--ok)}.c-ind{color:var(--ind)}.c-bad{color:var(--bad)}.c-rev{color:var(--rev)}.c-gray{color:var(--gray)}.c-add{color:var(--add)}
main{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:20px;margin-top:20px;align-items:start}
@media (max-width:1000px){main{grid-template-columns:1fr}}
.paper{background:#fff;border:1px solid var(--line);border-radius:14px;overflow:auto;max-height:78vh}
.paper svg{display:block;width:100%;height:auto;min-width:640px}
.hint{color:var(--muted);font-size:13px;margin:8px 2px 0}
.ap-edge,.ap-node,.ap-text{cursor:pointer;outline:none}
.ap-edge:hover .vis,.ap-edge:focus-visible .vis{stroke-width:4}
.ap-node:hover>*,.ap-node:focus-visible>*{filter:drop-shadow(0 0 4px rgba(25,113,194,.45))}
svg.has-sel .ap-edge:not(.sel),svg.has-sel .ap-node:not(.sel){opacity:.28}
.ap-edge.sel .vis{stroke-width:5}
.ap-node.sel>*{filter:drop-shadow(0 0 6px rgba(25,113,194,.6))}
.panel{position:sticky;top:16px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;max-height:calc(100vh - 32px);overflow:auto}
.panel h2{font-size:17px;margin:0 0 6px}
.panel h3{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:18px 0 8px}
.verdict{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:14px}
.reason{margin:6px 0 0;color:var(--muted);font-size:14px}
.ev{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.ev li{border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:var(--soft)}
.ev .loc{font:12.5px/1.4 ui-monospace,"Cascadia Code",Consolas,monospace;color:var(--muted);display:flex;justify-content:space-between;gap:8px}
.ev .loc a{color:var(--accent);text-decoration:none;white-space:nowrap}
.ev code{display:block;font:13px/1.45 ui-monospace,"Cascadia Code",Consolas,monospace;white-space:pre-wrap;word-break:break-word;margin-top:4px}
.tag{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:1px 6px;color:var(--muted);margin-left:6px}
.problems{list-style:none;margin:0;padding:0;display:grid;gap:6px}
.problems button,.rowbtn{all:unset;box-sizing:border-box;cursor:pointer;display:block;width:100%;padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:var(--soft);font-size:14px}
.problems button:hover,.problems button:focus-visible{border-color:var(--accent)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:14px;margin:8px 0 0}
.kv dt{color:var(--muted)}.kv dd{margin:0;word-break:break-word}
section.lists{margin-top:28px}
section.lists h2{font-size:18px;margin:28px 0 10px}
.tbl{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:14px}
.tbl th,.tbl td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
.tbl th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);background:var(--soft)}
.tbl tr[data-pick]{cursor:pointer}.tbl tr[data-pick]:hover td{background:var(--soft)}
.wrap{overflow-x:auto}
.empty{color:var(--muted);font-size:14px}
footer{color:var(--muted);font-size:13px;padding-top:28px;padding-bottom:40px}
</style>
</head>
<body>
<header>
  <div class="brand">arrowproof · evidence report</div>
  <h1>__TITLE__</h1>
  <p class="meta" id="meta"></p>
  <ul class="chips" id="chips"></ul>
</header>
<main>
  <section>
    <div class="paper" id="paper">__SVG__</div>
    <p class="hint">Click an arrow or a box to see the import lines behind it. Click empty space to go back.</p>
  </section>
  <aside class="panel" id="panel" aria-live="polite"></aside>
</main>
<section class="lists">
  <h2>Arrows</h2><div class="wrap"><table class="tbl" id="t-arrows"></table></div>
  <h2>In the code, not in the diagram</h2><div class="wrap" id="w-undrawn"></div>
  <h2>Boxes</h2><div class="wrap"><table class="tbl" id="t-boxes"></table></div>
</section>
<footer>An arrow A → B is verified when a file in A imports a file or a package in B. Arrows marked as flow pass with an import in either direction. Calls through HTTP, queues, dependency injection or computed imports are not visible to this check, so an unverified arrow is a claim without evidence, not proof of a bug.</footer>
<script id="ap-data" type="application/json">__DATA__</script>
<script>
(() => {
const D = JSON.parse(document.getElementById('ap-data').textContent);
const arrows = new Map(D.arrows.map(a => [a.id, a]));
const nodes = new Map(D.nodes.map(n => [n.id, n]));
const svg = document.querySelector('#paper svg');
const panel = document.getElementById('panel');
const V = {
  verified: ['✓', 'verified', 'c-ok'], indirect: ['↝', 'indirect', 'c-ind'], not_in_code: ['✗', 'not in code', 'c-bad'],
  reversed: ['⇄', 'reversed in code', 'c-rev'], unchecked: ['?', 'unchecked', 'c-gray'],
  ok: ['✓', 'found in repo', 'c-ok'], unmapped: ['?', 'not mapped', 'c-gray'],
  skipped: ['–', 'not code', 'c-gray'],
};
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v; else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
const first = s => (s || '?').split('\n')[0];
function badge(key) { const v = V[key] || V.unchecked; return h('span', {class: 'verdict ' + v[2]}, v[0] + ' ' + v[1]); }
const local = /^([a-zA-Z]:[\\/]|\/)/.test(D.repo || '');
function vscode(file, line) {
  const root = (D.repo || '').replace(/\\/g, '/');
  return 'vscode://file/' + encodeURI(root + '/' + file) + ':' + line;
}
function evidence(list, total) {
  if (!list.length) return h('p', {class: 'empty'}, 'No import line connects these boxes.');
  const ul = h('ul', {class: 'ev'}, list.map(e => h('li', {},
    h('div', {class: 'loc'}, h('span', {}, e.file + ':' + e.line, e.type_only ? h('span', {class: 'tag'}, 'type only') : null),
      local ? h('a', {href: vscode(e.file, e.line), title: 'Open in VS Code'}, 'open ↗') : null),
    h('code', {}, e.text))));
  return h('div', {}, ul, total > list.length ? h('p', {class: 'empty'}, `…and ${total - list.length} more`) : null);
}
function clear() {
  svg.classList.remove('has-sel');
  svg.querySelectorAll('.sel').forEach(el => el.classList.remove('sel'));
}
function pick(id) {
  clear();
  const g = svg.querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (g) { g.classList.add('sel'); svg.classList.add('has-sel'); }
  if (arrows.has(id)) showArrow(arrows.get(id)); else if (nodes.has(id)) showNode(nodes.get(id));
  if (window.matchMedia('(max-width:1000px)').matches) panel.scrollIntoView({behavior: 'smooth', block: 'start'});
}
function showArrow(a) {
  panel.replaceChildren(
    h('h2', {}, `${first(a.from_label)} → ${first(a.to_label)}`),
    badge(a.verdict),
    a.reason ? h('p', {class: 'reason'}, a.reason) : null,
    h('dl', {class: 'kv'}, h('dt', {}, 'claim'), h('dd', {}, a.mode === 'either'
      ? 'these boxes import each other in some direction' : `code in "${first(a.from_label)}" imports code in "${first(a.to_label)}"`),
      a.label ? [h('dt', {}, 'label'), h('dd', {}, a.label)] : null),
    h('h3', {}, a.verdict === 'indirect' ? 'The shortest import chain'
      : a.evidence_count ? `Evidence · ${a.evidence_count} import${a.evidence_count > 1 ? 's' : ''}` : 'Evidence'),
    evidence(a.evidence, a.evidence_count),
    h('div', {}, a.imports_instead ? [h('h3', {}, `What "${first(a.from_label)}" imports`),
      a.imports_instead.length ? h('p', {class: 'reason'}, a.imports_instead.join(', '))
        : h('p', {class: 'empty'}, 'No other box in this diagram.')] : null));
}
function showNode(n) {
  const touching = D.arrows.filter(a => a.from === n.id || a.to === n.id);
  panel.replaceChildren(
    h('h2', {}, first(n.label) || n.id), badge(n.verdict),
    n.reason ? h('p', {class: 'reason'}, n.reason) : null,
    h('dl', {class: 'kv'},
      h('dt', {}, 'mapped by'), h('dd', {}, {explicit: 'customData', label: 'label match', link: 'element link'}[n.source] || '—'),
      n.paths.length ? [h('dt', {}, 'paths'), h('dd', {}, n.paths.join(', '))] : null,
      n.packages.length ? [h('dt', {}, 'packages'), h('dd', {}, n.packages.join(', '))] : null,
      h('dt', {}, 'files'), h('dd', {}, String(n.files))),
    h('h3', {}, 'Arrows'),
    touching.length ? h('ul', {class: 'problems'}, touching.map(a => h('li', {},
      h('button', {onclick: () => pick(a.id)}, (V[a.verdict] || V.unchecked)[0] + ' ' + first(a.from_label) + ' → ' + first(a.to_label)))))
      : h('p', {class: 'empty'}, 'No arrows touch this box.'));
}
function overview() {
  const bad = D.arrows.filter(a => a.verdict === 'not_in_code' || a.verdict === 'reversed');
  const badBoxes = D.nodes.filter(n => n.verdict === 'not_in_code');
  panel.replaceChildren(
    h('h2', {}, bad.length || badBoxes.length ? 'Claims without evidence' : 'Every checked claim has evidence'),
    h('p', {class: 'reason'}, 'Pick one to see why. Green arrows have import lines behind them.'),
    bad.length || badBoxes.length ? h('ul', {class: 'problems'},
      bad.map(a => h('li', {}, h('button', {onclick: () => pick(a.id)}, V[a.verdict][0] + ' ' + first(a.from_label) + ' → ' + first(a.to_label)))),
      badBoxes.map(n => h('li', {}, h('button', {onclick: () => pick(n.id)}, '✗ box ' + first(n.label)))))
      : null);
}
svg.addEventListener('click', ev => {
  const g = ev.target.closest('.ap-edge,.ap-node,.ap-text');
  if (!g) { clear(); overview(); return; }
  pick(g.classList.contains('ap-text') ? g.dataset.for : g.dataset.id);
});
svg.addEventListener('keydown', ev => {
  const g = ev.target.closest('.ap-edge,.ap-node');
  if (g && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); pick(g.dataset.id); }
});
const s = D.stats;
const meta = document.getElementById('meta');
const repoName = (D.repo || '').split(/[\\/]/).filter(Boolean).pop() || D.repo;
meta.textContent = `Checked against ${repoName} · ${s.python} Python and ${s.js} JS/TS files · ${s.edges} file-to-file imports · ${D.generated}`;
meta.title = D.repo;
const C = D.counts, B = D.box_counts;
const chips = [['verified', C.verified, 'c-ok', '✓', 'verified'], ['indirect', C.indirect, 'c-ind', '↝', 'indirect'],
  ['not_in_code', C.not_in_code, 'c-bad', '✗', 'not in code'],
  ['reversed', C.reversed, 'c-rev', '⇄', 'reversed'], ['unchecked', C.unchecked, 'c-gray', '?', 'unchecked'],
  ['boxes', B.not_in_code, 'c-bad', '✗', B.not_in_code === 1 ? 'box not in repo' : 'boxes not in repo'],
  ['undrawn', D.undrawn.length, 'c-add', '+', D.undrawn.length === 1 ? 'import not drawn' : 'imports not drawn']];
document.getElementById('chips').replaceChildren(...chips.filter(c => c[1]).map(c =>
  h('li', {class: 'chip'}, h('span', {class: 'dot ' + c[2]}), h('b', {}, String(c[1])), ' ' + c[4])));
const ta = document.getElementById('t-arrows');
ta.append(h('tr', {}, h('th', {}, ''), h('th', {}, 'Arrow'), h('th', {}, 'Why'), h('th', {}, 'Imports')));
D.arrows.forEach(a => ta.append(h('tr', {'data-pick': a.id, onclick: () => pick(a.id)},
  h('td', {class: (V[a.verdict] || V.unchecked)[2]}, (V[a.verdict] || V.unchecked)[0]),
  h('td', {}, first(a.from_label) + ' → ' + first(a.to_label)), h('td', {}, a.reason || '—'), h('td', {}, String(a.evidence_count)))));
const wu = document.getElementById('w-undrawn');
if (!D.undrawn.length) wu.append(h('p', {class: 'empty'}, 'Every import between mapped boxes has an arrow.'));
else { const t = h('table', {class: 'tbl'}, h('tr', {}, h('th', {}, 'From'), h('th', {}, 'To'), h('th', {}, 'Imports'), h('th', {}, 'Example')));
  D.undrawn.forEach(u => t.append(h('tr', {}, h('td', {}, first(u.from_label)), h('td', {}, first(u.to_label)), h('td', {}, String(u.count)),
    h('td', {}, h('code', {}, u.evidence[0].file + ':' + u.evidence[0].line)))));
  wu.append(t); }
const tb = document.getElementById('t-boxes');
tb.append(h('tr', {}, h('th', {}, ''), h('th', {}, 'Box'), h('th', {}, 'Mapped to'), h('th', {}, 'Files')));
D.nodes.forEach(n => tb.append(h('tr', {'data-pick': n.id, onclick: () => pick(n.id)},
  h('td', {class: (V[n.verdict] || V.unchecked)[2]}, (V[n.verdict] || V.unchecked)[0]), h('td', {}, first(n.label) || n.id),
  h('td', {}, [...n.paths, ...n.packages].join(', ') || n.reason || '—'), h('td', {}, String(n.files)))));
const wanted = new URLSearchParams(location.hash.slice(1)).get('select');
if (wanted && (arrows.has(wanted) || nodes.has(wanted))) pick(wanted); else overview();
})();
</script>
</body>
</html>
"""
