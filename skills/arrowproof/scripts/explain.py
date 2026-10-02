#!/usr/bin/env python3
"""Turn a checked diagram into an interactive, step-by-step explainer page.

usage: explain.py DIAGRAM [--repo DIR] [--out DIR] [--repo-label NAME]

DIAGRAM is a spec or an .excalidraw file. A spec may carry a "tour": a list of
steps, each with a "title", a short "text", the ids of the boxes it is about
("focus") and, if needed, the arrows it talks about ("arrows": [[from, to]]).
Without a tour, a tour is built from the verified arrows alone.

The page opens with an overview and ends with the claims that the code does
not support, the arrows that the imports cannot prove, and the imports that
the diagram leaves out. Every arrow on a step shows its verdict and the import
lines behind it, so the story cannot claim more than the code shows.

Writes <name>.explainer.html next to the diagram (or into --out), together
with the outputs of verify.py.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagram as dg  # noqa: E402
import render  # noqa: E402
import verify  # noqa: E402

STAGE_ASPECT = 1.3   # width / height of the diagram panel on a laptop screen
FAN_OUT = 4          # a step of the automatic tour shows at most this many arrows

TEXT = {
    "overview": "{verified} of {checked} checked arrows have an import line behind them. "
                "Use Next or the arrow keys to walk through the change.",
    "uses": "{a} uses {list}.",
    "problems_title": "Claims without evidence",
    "problems": "The diagram draws these links, but no import in the code supports them.",
    "unchecked_title": "What the imports cannot prove",
    "unchecked": "These arrows are run-time flows or touch parts without code. "
                 "Read them as the author's claim, not as a fact from the code.",
    "missing_title": "What the diagram leaves out",
    "missing": "The code has these dependencies, but the diagram does not draw them.",
}

UI = {
    "step": "Step", "of": "of", "back": "Back", "next": "Next", "play": "Play", "pause": "Pause",
    "verified": "verified", "indirect": "indirect", "not_in_code": "not in code",
    "reversed": "reversed in code", "unchecked": "not provable from imports",
    "undrawn": "in the code, not drawn", "evidence": "Proof from the code", "via": "only through",
    "no_evidence": "No import line supports this arrow.", "imports_instead": "It imports instead",
    "imports": "imports", "keys": "← → to move, space to play",
    "why_actor": "One end is a person or an external system, not code.",
    "why_no_code": "One end has no Python or JS/TS files.",
    "why_flow": "A run-time flow. No import links these boxes, and imports cannot show such a flow.",
    "why_missing_box": "One end is not in the repository.",
    "why_unmapped": "One end is not mapped to code.",
    "why_unattached": "The arrow does not connect two boxes.",
    "why_skip": "The arrow is marked as conceptual.",
    "why_not_in_code": "No import connects these boxes.",
    "why_reversed": "The import goes the other way.",
}


def first_line(label: str) -> str:
    return (label or "").split("\n")[0].strip()


def _topological(ids: list, pairs: list) -> list:
    succ, indeg = defaultdict(list), {i: 0 for i in ids}
    for a, b in pairs:
        if a in indeg and b in indeg and a != b:
            succ[a].append(b)
            indeg[b] += 1
    queue = [i for i in ids if indeg[i] == 0]
    order, seen = [], set()
    while queue:
        n = queue.pop(0)
        if n in seen:
            continue
        seen.add(n)
        order.append(n)
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    return order + [i for i in ids if i not in seen]   # cycles keep their input order


def _with_endpoints(step: dict, rows: dict) -> dict:
    nodes = list(dict.fromkeys(step["nodes"] + [end for a in step["arrows"] if a in rows
                                                for end in (rows[a]["from"], rows[a]["to"]) if end]))
    return {**step, "nodes": nodes}


def tour_from_spec(spec: dict) -> list:
    """The author's tour, with box and arrow names turned into element ids."""
    node_ids = {n["id"] for n in spec.get("nodes") or []}
    edges = spec.get("edges") or []
    by_pair = defaultdict(list)
    for k, e in enumerate(edges):
        by_pair[(e.get("from"), e.get("to"))].append(f"ap-edge-{k}")
    steps = []
    for n, step in enumerate(spec.get("tour") or [], 1):
        focus = [f for f in step.get("focus") or [] if isinstance(f, str)]
        unknown = [f for f in focus if f not in node_ids]
        if unknown:
            raise ValueError(f"tour step {n} names boxes that are not in the diagram: {', '.join(unknown)}")
        if step.get("arrows") is None:
            inside = set(focus)
            arrows = [f"ap-edge-{k}" for k, e in enumerate(edges) if e.get("from") in inside and e.get("to") in inside]
        else:
            arrows = []
            for pair in step["arrows"]:
                key = tuple(pair) if isinstance(pair, (list, tuple)) and len(pair) == 2 else None
                if key not in by_pair:
                    raise ValueError(f"tour step {n} names an arrow that is not in the diagram: {pair}")
                arrows += by_pair[key]
        steps.append({"kind": "story", "title": str(step.get("title") or ""), "text": str(step.get("text") or ""),
                      "nodes": [f"ap-node-{f}" for f in focus], "arrows": arrows})
    return steps


def tour_from_code(result: dict) -> list:
    """One step per box that uses others, in dependency order, built from verified arrows only."""
    t = TEXT
    label = {n["id"]: first_line(n["label"]) or n["id"] for n in result["nodes"]}
    rows = [r for r in result["arrows"] if r["verdict"] in ("verified", "indirect")]
    out = defaultdict(list)
    for r in rows:
        out[r["from"]].append(r)
    order = _topological([n["id"] for n in result["nodes"]], [(r["from"], r["to"]) for r in rows])
    steps = []
    for n in order:
        targets = out.get(n) or []
        parts = math.ceil(len(targets) / FAN_OUT)
        for k in range(parts):   # a box with many arrows gets several close-ups
            part = targets[k * FAN_OUT:(k + 1) * FAN_OUT]
            steps.append({"kind": "story", "title": label[n] + (f" ({k + 1}/{parts})" if parts > 1 else ""),
                          "text": t["uses"].format(a=label[n], list=", ".join(label[r["to"]] for r in part)),
                          "nodes": [n] + [r["to"] for r in part], "arrows": [r["id"] for r in part]})
    if len(steps) > 12:   # keep the tour short: pack small neighbouring steps together
        packed = []
        for s in steps:
            last = packed[-1] if packed else None
            if last and len(last["arrows"]) + len(s["arrows"]) <= FAN_OUT and last["count"] < 3:
                last.update(title=last["title"] + " · " + s["title"], text=last["text"] + " " + s["text"],
                            nodes=last["nodes"] + s["nodes"], arrows=last["arrows"] + s["arrows"],
                            count=last["count"] + 1)
            else:
                packed.append({**s, "count": 1})
        steps = [{k: v for k, v in s.items() if k != "count"} for s in packed]
    return steps


def build_steps(spec: dict, result: dict, title: str) -> list:
    t = TEXT
    rows = {r["id"]: r for r in result["arrows"]}
    c = result["counts"]
    checked = sum(c.get(k, 0) for k in ("verified", "indirect", "not_in_code", "reversed"))
    overview = {"kind": "overview", "title": title,
                "text": t["overview"].format(verified=c.get("verified", 0) + c.get("indirect", 0), checked=checked),
                "nodes": [n["id"] for n in result["nodes"]], "arrows": list(rows)}
    story = tour_from_spec(spec) if spec.get("tour") else tour_from_code(result)
    steps = [overview] + [_with_endpoints(s, rows) for s in story]
    bad = [r["id"] for r in result["arrows"] if r["verdict"] in ("not_in_code", "reversed")]
    if bad:
        steps.append(_with_endpoints({"kind": "problems", "title": t["problems_title"], "text": t["problems"],
                                      "nodes": [], "arrows": bad}, rows))
    unchecked = [r["id"] for r in result["arrows"] if r["verdict"] == "unchecked"]
    if unchecked:
        steps.append(_with_endpoints({"kind": "unchecked", "title": t["unchecked_title"], "text": t["unchecked"],
                                      "nodes": [], "arrows": unchecked}, rows))
    if result["undrawn"]:
        top = result["undrawn"][:8]
        steps.append({"kind": "missing", "title": t["missing_title"], "text": t["missing"],
                      "nodes": list(dict.fromkeys(n for u in top for n in (u["from"], u["to"]))),
                      "arrows": [], "undrawn": top})
    return steps


def write_page(path, marked: dict, result: dict, steps: list, meta: dict) -> None:
    arrows = [{**r, "reason_kind": verify._unchecked_kind(r["reason"]) if r["verdict"] == "unchecked"
               else r["verdict"]} for r in result["arrows"]]
    payload = {"steps": steps, "arrows": arrows, "nodes": result["nodes"],
               "counts": result["counts"], "ui": UI, "repo": meta.get("repo", ""),
               "title": meta.get("title", "")}
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    head, rest = _PAGE.split("__SVG__")
    middle, tail = rest.split("__DATA__")
    title = html.escape(meta.get("title", "explainer"))
    page = (head.replace("__TITLE__", title)
            + render.svg(dg.live(marked), interactive=True) + middle + blob + tail)
    Path(path).write_text(page, encoding="utf-8")


def run(diagram_path: str, repo: str = ".", out_dir: str | None = None,
        repo_label: str | None = None) -> dict:
    source = Path(diagram_path)
    data = json.loads(source.read_text(encoding="utf-8"))
    spec = data if data.get("type") != "excalidraw" else {}
    if spec.get("tour"):
        tour_from_spec(spec)   # fail early on a broken tour, before the slow part
    # The page has its own stage, so it picks the layout that fills that stage best.
    result = verify.run(diagram_path, repo, out_dir, report=True, repo_label=repo_label, fit=STAGE_ASPECT)
    marked = json.loads(Path(result["outputs"]["excalidraw"]).read_text(encoding="utf-8"))
    title = spec.get("title") or source.name
    steps = build_steps(spec, result, title)
    target = Path(result["outputs"]["excalidraw"]).with_name(verify._stem(source.name) + ".explainer.html")
    write_page(target, marked, result, steps, {"title": title,
                                               "repo": repo_label or str(Path(repo).resolve())})
    result["outputs"]["explainer"] = str(target)
    result["steps"] = steps
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("diagram")
    ap.add_argument("--repo", default=".", help="repository root (default: .)")
    ap.add_argument("--out", help="output folder (default: next to the diagram)")
    ap.add_argument("--repo-label", help="name to show for the repository")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = run(args.diagram, args.repo, args.out, args.repo_label)
    except (OSError, ValueError) as exc:
        print(f"explain: {exc}", file=sys.stderr)
        return 2
    print(result["text"])
    print(f"\nTour: {len(result['steps'])} steps")
    print("Wrote: " + ", ".join(result["outputs"].values()))
    return 0


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · arrowproof</title>
<style>
:root{--bg:#f4f3ee;--fg:#1c1c1a;--muted:#6b6a64;--card:#fff;--line:#e3e1d9;--soft:#f6f5f0;--accent:#1971c2;
--ok:#2f9e44;--ind:#0c8599;--bad:#e03131;--rev:#d9480f;--gray:#868e96;--add:#9c36b5;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121211;--fg:#ecebe5;--muted:#a19f98;
--card:#1c1c1a;--line:#33322e;--soft:#232321;--accent:#74c0fc;--ok:#51cf66;--ind:#3bc9db;--bad:#ff6b6b;
--rev:#ff922b;--gray:#adb5bd;--add:#da77f2;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#121211;--fg:#ecebe5;--muted:#a19f98;--card:#1c1c1a;--line:#33322e;--soft:#232321;
--accent:#74c0fc;--ok:#51cf66;--ind:#3bc9db;--bad:#ff6b6b;--rev:#ff922b;--gray:#adb5bd;--add:#da77f2;color-scheme:dark}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
display:flex;flex-direction:column;min-height:100vh}
header,main,.bar{width:100%;max-width:1760px;margin:0 auto;padding-left:20px;padding-right:20px}
header{padding-top:18px;padding-bottom:10px}
.brand{font:600 12px/1 ui-monospace,"Cascadia Code",Consolas,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
h1{font-size:22px;line-height:1.25;margin:8px 0 0;letter-spacing:-.01em;word-break:break-word}
main{flex:1;display:grid;grid-template-columns:minmax(0,2.2fr) minmax(320px,1fr);gap:16px;padding-bottom:12px;min-height:0}
.paper{position:relative;background:#fff;border:1px solid var(--line);border-radius:16px;overflow:hidden;height:calc(100vh - 190px);min-height:380px}
.paper svg{display:block;width:100%;height:100%}
.paper svg > *{transition:opacity .4s ease}
.paper svg.focus > *:not(.on):not(.bg){opacity:.1}
.ap-edge.on.v-verified .vis,.ap-edge.on.v-indirect .vis{stroke-dasharray:12 9;animation:march 1.1s linear infinite}
@keyframes march{to{stroke-dashoffset:-21}}
.narration{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px 22px 18px;overflow:auto;
height:calc(100vh - 190px);min-height:380px;display:flex;flex-direction:column;min-width:0}
.narration .text,.claim .pair,.claim .why,h1,.narration h2{overflow-wrap:anywhere}
.stepno{font:600 12px/1 ui-monospace,"Cascadia Code",Consolas,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.narration h2{font-size:23px;line-height:1.25;margin:12px 0 10px;letter-spacing:-.01em}
.narration .text{font-size:17px;line-height:1.6;margin:0 0 18px}
.claims{display:grid;gap:10px;margin:0 0 8px}
.claim{border:1px solid var(--line);border-left:4px solid var(--c);border-radius:10px;padding:10px 12px;background:var(--soft)}
.claim .head{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.claim .badge{font-size:12.5px;font-weight:650;color:var(--c);white-space:nowrap}
.claim .pair{font-weight:600;font-size:14.5px}
.claim .why{margin:6px 0 0;color:var(--muted);font-size:14px}
.ev{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:6px}
.ev li{display:grid;gap:2px}
.ev .loc{font:12px/1.3 ui-monospace,"Cascadia Code",Consolas,monospace;color:var(--muted)}
.ev code{font:12.8px/1.45 ui-monospace,"Cascadia Code",Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.section-label{font-size:12px;font-weight:650;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:4px 0 8px}
.hint{margin-top:auto;padding-top:14px;color:var(--muted);font-size:12.5px}
footer{position:sticky;bottom:0;background:color-mix(in srgb,var(--bg) 90%,transparent);backdrop-filter:blur(8px);border-top:1px solid var(--line)}
.bar{display:flex;align-items:center;gap:10px;padding-top:10px;padding-bottom:10px}
.ctl{appearance:none;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;height:40px;min-width:40px;
padding:0 16px;font:600 14px/1 system-ui,-apple-system,"Segoe UI",sans-serif;cursor:pointer;display:inline-flex;align-items:center;gap:8px}
.ctl:hover{border-color:var(--accent)}
.ctl:focus-visible,.dots button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.ctl.primary{background:var(--accent);border-color:var(--accent);color:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .ctl.primary{color:#0b1b2b}}
.track{flex:1;display:grid;gap:6px;min-width:0}
.dots{display:flex;gap:5px;margin:0;padding:0;list-style:none}
.dots li{flex:1;min-width:0}
.dots button{display:block;width:100%;height:8px;border:0;border-radius:4px;background:var(--line);cursor:pointer;padding:0}
.dots button.done{background:color-mix(in srgb,var(--accent) 40%,var(--line))}
.dots button.cur{background:var(--accent)}
.timer{height:2px;background:transparent;border-radius:2px;overflow:hidden}
.timer span{display:block;height:100%;width:0;background:var(--accent)}
@media (max-width:900px){main{grid-template-columns:minmax(0,1fr)}.paper{height:50vh;min-height:300px}.narration{height:auto;min-height:0}
header,main,.bar{padding-left:16px;padding-right:16px}
.ctl .label{display:none}.ctl{padding:0 12px}}
@media (prefers-reduced-motion:reduce){.paper svg > *{transition:none}.ap-edge.on .vis{animation:none}}
</style>
</head>
<body>
<header>
  <div class="brand">arrowproof · explainer</div>
  <h1 id="doc-title"></h1>
</header>
<main>
  <div class="paper" id="paper">__SVG__</div>
  <section class="narration" aria-live="polite">
    <div class="stepno" id="stepno"></div>
    <h2 id="st-title"></h2>
    <p class="text" id="st-text"></p>
    <div id="st-claims"></div>
    <div class="hint" id="hint"></div>
  </section>
</main>
<footer>
  <div class="bar">
    <button class="ctl" id="prev" type="button"><span aria-hidden="true">←</span><span class="label" id="prev-l"></span></button>
    <button class="ctl primary" id="play" type="button"><span id="play-i" aria-hidden="true">▶</span><span class="label" id="play-l"></span></button>
    <button class="ctl" id="speed" type="button" title="Playback speed" aria-label="Playback speed">1×</button>
    <button class="ctl" id="next" type="button"><span class="label" id="next-l"></span><span aria-hidden="true">→</span></button>
    <div class="track"><ol class="dots" id="dots"></ol><div class="timer"><span id="timer"></span></div></div>
  </div>
</footer>
<script id="ap-data" type="application/json">__DATA__</script>
<script>
(() => {
const D = JSON.parse(document.getElementById('ap-data').textContent);
const T = D.ui;
const paper = document.getElementById('paper');
const svg = paper.querySelector('svg');
svg.removeAttribute('width'); svg.removeAttribute('height');
const vb = svg.viewBox.baseVal;
const FULL = {x: vb.x, y: vb.y, w: vb.width, h: vb.height};
let view = {...FULL};
const arrows = new Map(D.arrows.map(a => [a.id, a]));
const COLOR = {verified: '--ok', indirect: '--ind', not_in_code: '--bad', reversed: '--rev', unchecked: '--gray', undrawn: '--add'};
const SYM = {verified: '✓', indirect: '↝', not_in_code: '✗', reversed: '⇄', unchecked: '?', undrawn: '+'};
svg.querySelectorAll('.ap-edge').forEach(g => { const a = arrows.get(g.dataset.id); if (a) g.classList.add('v-' + a.verdict); });
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const first = s => (s || '').split('\n')[0];
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v; else if (k === 'style') el.setAttribute('style', v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v); else if (v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
document.getElementById('doc-title').textContent = D.title;
document.getElementById('prev-l').textContent = T.back;
document.getElementById('next-l').textContent = T.next;
document.getElementById('hint').textContent = T.keys;

function boxOf(ids) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  svg.querySelectorAll('[data-id],[data-for]').forEach(el => {
    if (!ids.has(el.dataset.id || el.dataset.for)) return;
    const b = el.getBBox();
    if (!b.width && !b.height) return;
    x0 = Math.min(x0, b.x); y0 = Math.min(y0, b.y); x1 = Math.max(x1, b.x + b.width); y1 = Math.max(y1, b.y + b.height);
  });
  if (!isFinite(x0)) return FULL;
  const pad = 70, cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  // Frame the focus with some room around it, but never closer than a readable minimum.
  let w = Math.max(x1 - x0 + 2 * pad, 520, FULL.w * 0.12), h = Math.max(y1 - y0 + 2 * pad, 340, FULL.h * 0.12);
  const r = paper.clientWidth / Math.max(1, paper.clientHeight);
  if (w / h > r) h = w / r; else w = h * r;
  if (w >= FULL.w && h >= FULL.h) return FULL;
  return {x: cx - w / 2, y: cy - h / 2, w, h};
}
let raf = 0;
function camera(to, instant) {
  const from = {...view}, t0 = performance.now(), dur = instant || reduced ? 0 : 800;
  cancelAnimationFrame(raf);
  const frame = now => {
    const k = dur ? Math.min(1, (now - t0) / dur) : 1;
    const e = k < .5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
    view = {x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e, w: from.w + (to.w - from.w) * e, h: from.h + (to.h - from.h) * e};
    svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
    if (k < 1) raf = requestAnimationFrame(frame);
  };
  raf = requestAnimationFrame(frame);
}
function claim(a) {
  const key = a.verdict;
  const box = h('div', {class: 'claim', style: `--c: var(${COLOR[key]})`},
    h('div', {class: 'head'}, h('span', {class: 'badge'}, SYM[key] + ' ' + T[key]),
      h('span', {class: 'pair'}, first(a.from_label) + ' → ' + first(a.to_label))));
  if (key === 'indirect' && a.via) box.append(h('p', {class: 'why'}, T.via + ': ' + a.via.join(' → ')));
  else if (key !== 'verified') box.append(h('p', {class: 'why'}, T['why_' + a.reason_kind] || a.reason || T.no_evidence));
  if (a.evidence && a.evidence.length && key !== 'unchecked')
    box.append(h('ul', {class: 'ev'}, a.evidence.slice(0, 3).map(e =>
      h('li', {}, h('span', {class: 'loc'}, `${e.file}:${e.line}`), h('code', {}, e.text)))));
  if (a.imports_instead && a.imports_instead.length) box.append(h('p', {class: 'why'}, T.imports_instead + ': ' + a.imports_instead.join(', ')));
  return box;
}
function undrawn(u) {
  const e = u.evidence[0];
  return h('div', {class: 'claim', style: '--c: var(--add)'},
    h('div', {class: 'head'}, h('span', {class: 'badge'}, '+ ' + T.undrawn),
      h('span', {class: 'pair'}, first(u.from_label) + ' → ' + first(u.to_label))),
    h('ul', {class: 'ev'}, h('li', {}, h('span', {class: 'loc'}, `${e.file}:${e.line} · ${u.count} ${T.imports}`), h('code', {}, e.text))));
}
const steps = D.steps;
let index = 0, playing = false, timer = 0;
const dots = document.getElementById('dots');
steps.forEach((s, i) => dots.append(h('li', {}, h('button', {type: 'button', title: s.title, 'aria-label': `${T.step} ${i + 1}: ${s.title}`, onclick: () => go(i)}))));
function render(i, instant) {
  const s = steps[i];
  document.getElementById('stepno').textContent = `${T.step} ${i + 1} ${T.of} ${steps.length}`;
  document.getElementById('st-title').textContent = s.title;
  document.getElementById('st-text').textContent = s.text;
  const claims = document.getElementById('st-claims');
  claims.replaceChildren();
  const rows = s.kind === 'overview' ? [] : s.arrows.map(id => arrows.get(id)).filter(Boolean);
  if (rows.length) claims.append(h('div', {class: 'section-label'}, T.evidence), h('div', {class: 'claims'}, rows.map(claim)));
  if (s.undrawn) claims.append(h('div', {class: 'claims'}, s.undrawn.map(undrawn)));
  const ids = new Set([...s.nodes, ...s.arrows]);
  const all = s.kind === 'overview';
  svg.classList.toggle('focus', !all);
  svg.querySelectorAll('[data-id],[data-for]').forEach(el => el.classList.toggle('on', !all && ids.has(el.dataset.id || el.dataset.for)));
  camera(all ? FULL : boxOf(ids), instant);
  [...dots.querySelectorAll('button')].forEach((b, k) => { b.className = k < i ? 'done' : k === i ? 'cur' : ''; b.setAttribute('aria-current', k === i ? 'step' : 'false'); });
  document.getElementById('prev').disabled = i === 0;
  document.getElementById('next').disabled = i === steps.length - 1;
  try { history.replaceState(null, '', '#step=' + (i + 1)); } catch (e) {}
}
// Reading time: about 0.25 s per word plus 1.2 s per evidence card, kept between 6 and 14 s.
const SPEEDS = [1, 1.5, 2];
let speed = 1;
function duration(s) {
  const words = (s.title + ' ' + s.text).split(/\s+/).filter(Boolean).length;
  const cards = s.kind === 'overview' ? 0 : Math.min(4, s.arrows.length + (s.undrawn ? s.undrawn.length : 0));
  return Math.max(6000, Math.min(14000, 2500 + 250 * words + 1200 * cards)) / speed;
}
function schedule() {
  clearTimeout(timer);
  const bar = document.getElementById('timer');
  bar.style.transition = 'none'; bar.style.width = '0';
  if (!playing) return;
  const ms = duration(steps[index]);
  requestAnimationFrame(() => { bar.style.transition = `width ${ms}ms linear`; bar.style.width = '100%'; });
  timer = setTimeout(() => { if (index < steps.length - 1) go(index + 1); else setPlaying(false); }, ms);
}
function go(i) { index = Math.max(0, Math.min(steps.length - 1, i)); render(index); schedule(); }
function setPlaying(on) {
  playing = on;
  document.getElementById('play-i').textContent = on ? '❚❚' : '▶';
  document.getElementById('play-l').textContent = on ? T.pause : T.play;
  if (on && index === steps.length - 1) { index = 0; render(0); }
  schedule();
}
document.getElementById('prev').onclick = () => go(index - 1);
document.getElementById('next').onclick = () => go(index + 1);
document.getElementById('play').onclick = () => setPlaying(!playing);
document.getElementById('speed').onclick = () => {
  speed = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length];
  document.getElementById('speed').textContent = speed + '×';
  schedule();
};
document.addEventListener('keydown', ev => {
  const target = ev.target instanceof Element ? ev.target : document.body;
  if (target.closest('input,textarea,select')) return;
  if (ev.key === 'ArrowRight') { ev.preventDefault(); go(index + 1); }
  else if (ev.key === 'ArrowLeft') { ev.preventDefault(); go(index - 1); }
  else if (ev.key === ' ' && !target.closest('button')) { ev.preventDefault(); setPlaying(!playing); }
  else if (ev.key === 'Home') go(0); else if (ev.key === 'End') go(steps.length - 1);
});
addEventListener('resize', () => render(index, true));
// Links can open a step, a speed and autoplay: #step=3&speed=2&play=1
const hash = new URLSearchParams(location.hash.slice(1));
const wanted = parseInt(hash.get('step'), 10);
index = wanted >= 1 && wanted <= steps.length ? wanted - 1 : 0;
const askedSpeed = parseFloat(hash.get('speed'));
if (SPEEDS.includes(askedSpeed)) { speed = askedSpeed; document.getElementById('speed').textContent = speed + '×'; }
render(index, true);
setPlaying(hash.get('play') === '1');
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
