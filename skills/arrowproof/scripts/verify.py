#!/usr/bin/env python3
"""Check every arrow and box of an Excalidraw diagram against the code.

usage: verify.py DIAGRAM [--repo DIR] [--out DIR] [--json] [--draw-undrawn]
                 [--keep-colors] [--no-auto] [--strict] [--no-report]

DIAGRAM is an .excalidraw file, or a spec JSON ({title, nodes, edges}) that
is laid out first. An arrow A -> B claims that code in A imports code in B.

Writes <name>.verified.excalidraw and <name>.report.html next to DIAGRAM
(or into --out). Exit 0 when every checked arrow and box holds, 1 when a
problem was found, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import json
import posixpath
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagram as dg  # noqa: E402
from codegraph import build_graph, dist_name, is_test_path  # noqa: E402

PACKAGE_ALIASES = {
    "postgres": ["pg", "postgres", "psycopg2", "psycopg", "asyncpg", "pg-promise",
                 "@neondatabase/serverless", "@vercel/postgres"],
    "mysql": ["mysql", "mysql2", "pymysql", "MySQLdb", "aiomysql"],
    "sqlite": ["sqlite3", "better-sqlite3", "aiosqlite"],
    "redis": ["redis", "ioredis", "aioredis", "@upstash/redis"],
    "mongodb": ["mongodb", "mongoose", "pymongo", "motor"],
    "stripe": ["stripe", "@stripe/stripe-js"],
    "openai": ["openai"],
    "anthropic": ["anthropic", "@anthropic-ai/sdk"],
    "s3": ["boto3", "@aws-sdk/client-s3", "aws-sdk"],
    "kafka": ["kafkajs", "kafka", "confluent_kafka", "aiokafka"],
    "rabbitmq": ["amqplib", "pika", "aio_pika"],
    "elasticsearch": ["elasticsearch", "@elastic/elasticsearch"],
    "prisma": ["@prisma/client"],
    "supabase": ["@supabase/supabase-js", "supabase"],
    "firebase": ["firebase", "firebase-admin", "firebase_admin"],
    "graphql": ["graphql", "@apollo/client", "@apollo/server", "graphene", "strawberry"],
}
for _alias, _target in (("postgresql", "postgres"), ("pg", "postgres"), ("mongo", "mongodb"),
                        ("sqlite3", "sqlite"), ("aws", "s3"), ("amazons3", "s3")):
    PACKAGE_ALIASES[_alias] = PACKAGE_ALIASES[_target]

SYMBOL = {"verified": "✓", "indirect": "↝", "not_in_code": "✗", "reversed": "⇄", "unchecked": "?"}
WORDS = {"verified": "verified", "indirect": "indirect", "not_in_code": "not in code",
         "reversed": "reversed", "unchecked": "unchecked"}
ORDER = ("verified", "indirect", "not_in_code", "reversed", "unchecked")
MAX_HOPS = 2   # an indirect path passes through exactly one other file


def runtime(evidence: list) -> list:
    """The imports that exist at run time (not only under TYPE_CHECKING or `import type`)."""
    return [e for e in evidence if not e.type_only]


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def first_line(label: str) -> str:
    return (label or "").split("\n")[0].strip()


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


class RepoIndex:
    """Lookups for mapping a box label to files, folders or packages."""

    def __init__(self, graph):
        self.files = set(graph.files)
        self.dirs = set()
        for f in graph.files:
            d = posixpath.dirname(f)
            while d and d not in self.dirs:
                self.dirs.add(d)
                d = posixpath.dirname(d)
        code = [f for f in graph.code_files if not is_test_path(f)]
        self.by_stem: dict = defaultdict(list)
        code_dirs = set()
        for f in code:
            stem = posixpath.basename(f).split(".")[0]
            if stem not in ("__init__", "index", "main", "mod"):
                self.by_stem[norm(stem)].append(f)
            d = posixpath.dirname(f)
            while d and d not in code_dirs:
                code_dirs.add(d)
                d = posixpath.dirname(d)
        self.by_dir: dict = defaultdict(list)
        for d in code_dirs:
            if not is_test_path(d + "/"):
                self.by_dir[norm(posixpath.basename(d))].append(d)
        self.packages: dict = defaultdict(set)
        for _, pkg in graph.packages:
            self.packages[norm(pkg)].add(pkg)

    def by_suffix(self, tail: str) -> list:
        return sorted(p for p in self.files | self.dirs if p.endswith("/" + tail))


def auto_map(label: str, idx: RepoIndex) -> tuple:
    """(paths, packages, note) for a box that carries no explicit mapping."""
    paths, notes = [], []
    for tok in re.split(r"[\s,;()\[\]`'\"]+", label):
        tok = tok.strip().strip("/").removeprefix("./")
        if not tok or ("/" not in tok and not re.search(r"\.[A-Za-z0-9]{1,5}$", tok)):
            continue
        if tok in idx.files or tok in idx.dirs:
            paths.append(tok)
            continue
        hits = idx.by_suffix(tok)
        if len(hits) == 1:
            paths.append(hits[0])
        elif hits:
            notes.append(f'"{tok}" matches {len(hits)} paths')
    if paths:
        return sorted(set(paths)), [], ""
    first = label.split("\n")[0]
    for text in dict.fromkeys([label, first]):
        n = norm(text)
        if not n:
            continue
        variants = {n, n + "s", n[:-1] if n.endswith("s") else n}
        dirs = sorted({d for v in variants for d in idx.by_dir.get(v, [])})
        files = sorted({f for v in variants for f in idx.by_stem.get(v, [])})
        if len(dirs) == 1 and all(f.startswith(dirs[0] + "/") for f in files):
            return dirs, [], ""
        if not dirs and len(files) == 1:
            return files, [], ""
        if dirs or files:
            shown = ", ".join((dirs + files)[:3])
            return [], [], f"label matches {len(dirs) + len(files)} paths ({shown}...)"
        for v in variants:
            if v in PACKAGE_ALIASES:
                return [], list(PACKAGE_ALIASES[v]), ""
            if v in idx.packages:
                return [], sorted(idx.packages[v]), ""
    return [], [], "; ".join(notes)


def _members(paths: list, all_files: list, file_set: set) -> set:
    found = set()
    for p in paths:
        if any(ch in p for ch in "*?["):
            found.update(f for f in all_files if fnmatch.fnmatch(f, p))
        elif p in file_set:
            found.add(p)
        else:
            prefix = p + "/"
            found.update(f for f in all_files if f.startswith(prefix))
    return found


def evaluate(graph, nodes: dict, arrows: list, auto: bool = True, max_evidence: int = 12) -> dict:
    idx = RepoIndex(graph)
    for node in nodes.values():
        if not node.source and not node.skip and auto and node.label:
            paths, packages, note = auto_map(node.label, idx)
            if paths or packages:
                node.paths, node.packages, node.source = paths, packages, "label"
            else:
                node.note = note

    used_packages = {pkg for _, pkg in graph.packages}
    readable = set(graph.code_files)
    members, node_rows = {}, {}
    for node in nodes.values():
        files = _members(node.paths, graph.files, idx.files)
        code = files & readable
        exts = sorted({posixpath.splitext(f)[1] for f in files - code if posixpath.splitext(f)[1]})
        missing = [p for p in node.paths if not _members([p], graph.files, idx.files)]
        pkgs_found = [p for p in node.packages if p in used_packages]
        pkgs_declared = [p for p in node.packages if dist_name(p) in graph.declared]
        if node.skip:
            verdict, reason = "skipped", "marked as not code"
        elif not (node.paths or node.packages):
            verdict, reason = "unmapped", node.note or "no file, folder or package matches this box"
        elif not files and not pkgs_found and pkgs_declared:
            verdict = "ok"
            reason = "declared as a dependency, but no file imports " + " / ".join(pkgs_declared)
        elif not files and not pkgs_found:
            verdict = "not_in_code"
            reason = ("no such path in the repository: " + ", ".join(node.paths) if node.paths
                      else "no file imports " + " / ".join(node.packages)
                      + ", and no manifest declares it")
        else:
            verdict, reason = "ok", ""
            if missing:
                reason = "missing paths: " + ", ".join(missing)
        if verdict == "ok":
            members[node.id] = files
        node_rows[node.id] = {
            "id": node.id, "label": node.label, "verdict": verdict, "reason": reason,
            "source": node.source, "paths": node.paths, "packages": node.packages,
            "files": len(files), "code_files": len(code), "other_types": exts[:4],
            "package_hits": pkgs_found,
        }

    pairs = _pair_evidence(graph, nodes, members)
    chains = ChainFinder(graph, nodes, members)
    arrow_rows, drawn = [], set()
    for arrow in arrows:
        row = _judge(arrow, nodes, node_rows, pairs, chains, max_evidence)
        arrow_rows.append(row)
        if arrow.src and arrow.dst:
            drawn.add((arrow.src, arrow.dst))
            if row["mode"] == "either" or row["verdict"] == "reversed":
                drawn.add((arrow.dst, arrow.src))

    undrawn = []
    for (a, b), evs in pairs.items():
        live = runtime(evs)
        if (a, b) not in drawn and live:
            undrawn.append({"from": a, "to": b, "from_label": nodes[a].label,
                            "to_label": nodes[b].label, "count": len(live),
                            "evidence": [e.as_dict() for e in live[:max_evidence]]})
    undrawn.sort(key=lambda r: -r["count"])

    counts = defaultdict(int)
    for row in arrow_rows:
        counts[row["verdict"]] += 1
    box_counts = defaultdict(int)
    for row in node_rows.values():
        box_counts[row["verdict"]] += 1
    return {"arrows": arrow_rows, "nodes": list(node_rows.values()), "undrawn": undrawn,
            "counts": dict(counts), "box_counts": dict(box_counts)}


def _pair_evidence(graph, nodes: dict, members: dict) -> dict:
    """(box A, box B) -> evidence that code in A imports code in B."""
    of_file = defaultdict(list)
    for nid, files in members.items():
        for f in files:
            of_file[f].append(nid)
    of_package = defaultdict(list)
    for nid in members:
        for pkg in nodes[nid].packages:
            of_package[pkg].append(nid)
    subset: dict = {}

    def inside(x, y):
        if (x, y) not in subset:
            subset[(x, y)] = members[x] <= members[y]
        return subset[(x, y)]

    pairs = defaultdict(list)
    for (src, dst), evs in graph.edges.items():
        for a in of_file.get(src, ()):
            for b in of_file.get(dst, ()):
                if a == b:
                    continue
                # Nested boxes: count only imports that cross the boundary.
                if src in members[b] and not inside(a, b):
                    continue
                if dst in members[a] and not inside(b, a):
                    continue
                pairs[(a, b)].extend(evs)
    for (src, pkg), evs in graph.packages.items():
        for a in of_file.get(src, ()):
            for b in of_package.get(pkg, ()):
                if a != b and src not in members[b]:
                    pairs[(a, b)].extend(evs)
    return pairs


class ChainFinder:
    """The shortest import chain from one box to another through other files."""

    def __init__(self, graph, nodes: dict, members: dict):
        self.graph, self.nodes, self.members = graph, nodes, members
        self.adj = defaultdict(list)
        for (src, dst), evs in graph.edges.items():
            if runtime(evs):
                self.adj[src].append(dst)
        self.importers = defaultdict(set)
        for (src, pkg), evs in graph.packages.items():
            if runtime(evs):
                self.importers[pkg].add(src)

    def find(self, a: str, b: str):
        fa, fb = self.members.get(a, set()), self.members.get(b, set())
        start, goal = (fa - fb) or fa, (fb - fa) or fb
        packages = sorted(self.nodes[b].packages)
        via_package = set().union(*(self.importers.get(p, set()) for p in packages)) if packages else set()
        prev = {f: None for f in start}
        queue = deque((f, 0) for f in sorted(start))
        while queue:
            f, depth = queue.popleft()
            for nxt in self.adj.get(f, ()):
                if nxt in prev:
                    continue
                prev[nxt] = f
                hops = depth + 1
                if nxt in goal:
                    if hops >= 2:
                        return self._chain(prev, nxt, None)
                    continue
                if nxt in via_package and hops + 1 <= MAX_HOPS:
                    pkg = next(p for p in packages if (nxt, p) in self.graph.packages)
                    return self._chain(prev, nxt, pkg)
                if hops < MAX_HOPS:
                    queue.append((nxt, hops))
        return None

    def _chain(self, prev: dict, end: str, package):
        files = [end]
        while prev[files[-1]] is not None:
            files.append(prev[files[-1]])
        files.reverse()
        evidence = [runtime(self.graph.edges[(u, v)])[0] for u, v in zip(files, files[1:])]
        if package:
            evidence.append(runtime(self.graph.packages[(end, package)])[0])
        via = files[1:] if package else files[1:-1]
        return via, evidence


def _short(path: str) -> str:
    return "/".join(path.split("/")[-2:])


def _judge(arrow, nodes: dict, node_rows: dict, pairs: dict, chains: ChainFinder,
           max_evidence: int) -> dict:
    src = nodes.get(arrow.src) if arrow.src else None
    dst = nodes.get(arrow.dst) if arrow.dst else None
    row = {"id": arrow.id, "from": arrow.src, "to": arrow.dst,
           "from_label": src.label if src else "", "to_label": dst.label if dst else "",
           "label": arrow.label, "kind": arrow.kind, "mode": "forward",
           "verdict": "unchecked", "reason": "", "evidence": [], "evidence_count": 0}
    if not (src and dst):
        row["reason"] = "the arrow is not attached to two boxes"
        return row
    if arrow.kind == "skip":
        row["reason"] = "the arrow is marked skip"
        return row
    if src.id == dst.id:
        row["reason"] = "the arrow starts and ends on the same box"
        return row
    for node in (src, dst):
        state = node_rows[node.id]
        if state["verdict"] != "ok":
            why = {"skipped": "is marked as not code", "unmapped": "is not mapped to code",
                   "not_in_code": "is not in the code"}[state["verdict"]]
            row["reason"] = f'box "{first_line(node.label) or node.id}" {why}'
            return row
    fwd = pairs.get((src.id, dst.id), [])
    rev = pairs.get((dst.id, src.id), [])
    package_source = not node_rows[src.id]["files"]   # a package cannot import your code
    chain = None
    if arrow.kind == "flow" or arrow.undirected or package_source:
        row["mode"] = "either"
        evs = fwd + rev
        if evs:
            row["verdict"] = "verified"
        elif not (arrow.kind == "flow" and arrow.declared):
            # A flow that the author declared is not about imports, so an import
            # chain through a third file says nothing about it.
            chain = chains.find(src.id, dst.id) or chains.find(dst.id, src.id)
    elif fwd:
        evs = fwd
        row["verdict"] = "verified"
    else:
        evs = []
        chain = chains.find(src.id, dst.id)
        if not chain and rev:
            evs = rev
            row["verdict"] = "reversed"
            row["reason"] = "the code imports in the other direction"
    if chain:
        via, evs = chain
        row["verdict"] = "indirect"
        row["via"] = via
        row["reason"] = "no direct import, only through " + ", ".join(via)
    elif row["verdict"] == "unchecked":
        # No evidence. That means something only when both sides hold code this check reads,
        # and only for a claim about imports. A flow can run without any import.
        for node in (src, dst):
            state = node_rows[node.id]
            if node.paths and not state["code_files"] and not state["package_hits"]:
                kinds = ", ".join(state["other_types"]) or "no code"
                row["reason"] = (f'box "{first_line(node.label) or node.id}" has no Python or '
                                 f'JS/TS files ({kinds}), so this arrow cannot be checked')
                break
        else:
            if row["mode"] == "either":
                row["reason"] = ("no import links these boxes in either direction. A run-time flow "
                                 "(HTTP, events, router context, a child process) is not visible to this check")
            else:
                row["verdict"] = "not_in_code"
    if row["verdict"] == "not_in_code":
        a, b = first_line(src.label) or src.id, first_line(dst.label) or dst.id
        row["reason"] = (f'no import from "{a}" to "{b}"' if row["mode"] == "forward"
                         else f'no import between "{a}" and "{b}"')
        instead = [(len(runtime(e)), first_line(nodes[t].label) or t)
                   for (f, t), e in pairs.items() if f == src.id and t != dst.id and runtime(e)]
        row["imports_instead"] = [name for _, name in sorted(instead, key=lambda x: (-x[0], x[1]))]
    if row["verdict"] == "verified" and not runtime(evs):
        row["type_only"] = True
        row["reason"] = "only a type-checking import supports this arrow"
    evs = runtime(evs) + [e for e in evs if e.type_only]   # run-time imports first
    row["evidence"] = [e.as_dict() for e in evs[:max_evidence]]
    row["evidence_count"] = len(evs)
    return row


# --------------------------------------------------------------------- output

def marked_copy(data: dict, result: dict, keep_colors: bool, draw_undrawn: int) -> dict:
    out = copy.deepcopy(data)
    els = out.setdefault("elements", [])
    by_id = {e.get("id"): e for e in els if isinstance(e, dict)}
    added = []
    for row in result["arrows"]:
        el = by_id.get(row["id"])
        if el is None:
            continue
        dg.mark_arrow(el, row["verdict"], keep_colors)
        _set_meta(el, verdict=row["verdict"], evidence=row["evidence_count"])
        notes = {"not_in_code": "✗ not in code", "reversed": "⇄ reversed in code",
                 "indirect": "↝ via " + ", ".join(_short(p) for p in row.get("via", [])[:2])}
        if row["verdict"] in notes:
            note = notes[row["verdict"]]
            label = next((by_id[b["id"]] for b in el.get("boundElements") or []
                          if isinstance(b, dict) and b.get("type") == "text"
                          and by_id.get(b.get("id"), {}).get("containerId") == el.get("id")), None)
            if label:   # under the label of the arrow, not on top of it
                ax, ay = dg.label_anchor(el)
                x, y = ax - dg.text_size(note, 16)[0] / 2, ay + float(label.get("height") or 0) / 2 + 4
            else:
                mx, my = dg.midpoint(el)
                x, y = mx + 8, my + 6
            added.append(dg.new_text(note, x, y, 16, dg.COLORS[row["verdict"]],
                                     customData={"arrowproof": {"note_for": row["id"]}}))
    for row in result["nodes"]:
        el = by_id.get(row["id"])
        if el is None:
            continue
        _set_meta(el, verdict=row["verdict"])
        if row["verdict"] == "not_in_code":
            dg.mark_box(el)
            x0, _, _, y1 = dg.bbox(el)
            added.append(dg.new_text("✗ not in repo", x0, y1 + 6, 16, dg.COLORS["not_in_code"],
                                     customData={"arrowproof": {"note_for": row["id"]}}))
    for row in result["undrawn"][:draw_undrawn]:
        a, b = by_id.get(row["from"]), by_id.get(row["to"])
        if a is None or b is None:
            continue
        arrow = dg.connect(a, b, strokeColor=dg.COLORS["undrawn"], strokeStyle="dashed",
                           customData={"arrowproof": {"kind": "uses", "verdict": "undrawn"}})
        added += [arrow, dg.label_arrow(arrow, f"+ in code ({row['count']})", 14,
                                        dg.COLORS["undrawn"])]
    els.extend(added)
    els.extend(_legend(dg.live(out), result))
    return out


def _set_meta(el: dict, **values) -> None:
    custom = el.get("customData")
    if not isinstance(custom, dict):
        custom = el["customData"] = {}
    meta = custom.get("arrowproof")
    if not isinstance(meta, dict):
        meta = custom["arrowproof"] = {}
    meta.update(values)


def _legend(elements: list, result: dict) -> list:
    boxes = [dg.bbox(e) for e in elements]
    if not boxes:
        return []
    x = min(b[0] for b in boxes)
    y = min(b[1] for b in boxes) - 56
    counts, parts = result["counts"], []
    parts.append(("arrowproof", dg.COLORS["ink"]))
    for key in ORDER:
        if counts.get(key):
            parts.append((f"{SYMBOL[key]} {counts[key]} {WORDS[key]}", dg.COLORS[key]))
    bad_boxes = result["box_counts"].get("not_in_code", 0)
    if bad_boxes:
        parts.append((f"✗ {bad_boxes} box{'es' if bad_boxes > 1 else ''} not in repo",
                      dg.COLORS["not_in_code"]))
    out = []
    for text, color in parts:
        el = dg.new_text(text, x, y, 18, color)
        out.append(el)
        x += el["width"] + 28
    return out


def _name(label: str) -> str:
    first = (label or "?").split("\n")[0].strip()
    return f'"{first[:48]}"'


UNCHECKED_WORDS = {   # (one arrow, several arrows)
    "actor": ("touches a person or an external system", "touch a person or an external system"),
    "no_code": ("touches a box without Python or JS/TS files", "touch boxes without Python or JS/TS files"),
    "flow": ("is a run-time flow with no import behind it", "are run-time flows with no import behind them"),
    "missing_box": ("touches a box that is not in the repo", "touch boxes that are not in the repo"),
    "unmapped": ("touches a box without a mapping", "touch boxes without a mapping"),
    "unattached": ("does not connect two boxes", "do not connect two boxes"),
    "skip": ("is marked skip", "are marked skip"),
    "other": ("other", "other"),
}


def _unchecked_kind(reason: str) -> str:
    for marker, kind in (("marked as not code", "actor"), ("has no Python or JS/TS", "no_code"),
                         ("no import links these boxes", "flow"), ("is not in the code", "missing_box"),
                         ("is not mapped to code", "unmapped"), ("not attached", "unattached"),
                         ("marked skip", "skip")):
        if marker in reason:
            return kind
    return "other"


def summary(result: dict, diagram: str, repo: str, stats: dict) -> str:
    c, b = result["counts"], result["box_counts"]
    checked = sum(c.get(k, 0) for k in ORDER if k != "unchecked")
    lines = [f"arrowproof: {diagram} against {repo} "
             f"({stats['python']} Python and {stats['js']} JS/TS files)", ""]
    lines.append(f"Arrows: {checked} checked. " + ", ".join(
        f"{SYMBOL[k]} {c[k]} {WORDS[k]}" for k in ORDER if c.get(k)))
    by_label = sum(1 for n in result["nodes"] if n["source"] == "label" and n["verdict"] != "unmapped")
    box_words = (("ok", "found in the code"), ("not_in_code", "not in the repo"),
                 ("unmapped", "not mapped"), ("skipped", "not code"))
    lines.append("Boxes: " + ", ".join(f"{b[k]} {w}" for k, w in box_words if b.get(k))
                 + (f" ({by_label} mapped by label)" if by_label else ""))
    problems = [r for r in result["arrows"] if r["verdict"] in ("not_in_code", "reversed")]
    if problems or b.get("not_in_code"):
        lines += ["", "Problems:"]
    for r in problems:
        line = (f"- {SYMBOL[r['verdict']]} {_name(r['from_label'])} -> {_name(r['to_label'])}: "
                f"{r['reason']}")
        if r["evidence"]:
            e = r["evidence"][0]
            line += f" (e.g. {e['file']}:{e['line']} `{e['text']}`)"
        if r.get("imports_instead"):
            line += f". It imports {', '.join(r['imports_instead'][:4])}"
        lines.append(line)
    for n in result["nodes"]:
        if n["verdict"] == "not_in_code":
            lines.append(f"- ✗ box {_name(n['label'])}: {n['reason']}")
    indirect = [r for r in result["arrows"] if r["verdict"] == "indirect"]
    if indirect:
        lines += ["", "Indirect (the diagram skips a module):"]
        lines += [f"- ↝ {_name(r['from_label'])} -> {_name(r['to_label'])}: only through "
                  + " -> ".join(r["via"]) for r in indirect]
    unchecked = [r for r in result["arrows"] if r["verdict"] == "unchecked"]
    if unchecked:
        groups = defaultdict(list)
        for r in unchecked:
            groups[_unchecked_kind(r["reason"])].append(r)
        parts = [f"{len(rows)} {UNCHECKED_WORDS[kind][0 if len(rows) == 1 else 1]}"
                 for kind, rows in sorted(groups.items(), key=lambda kv: -len(kv[1]))]
        lines += ["", f"Unchecked: {plural(len(unchecked), 'arrow')} ({'; '.join(parts)})"]
        # People, external systems and non-code boxes are expected. List the rest.
        listed = [r for kind in ("unmapped", "missing_box", "flow", "unattached", "skip", "other")
                  for r in groups.get(kind, [])]
        for r in listed[:8]:
            lines.append(f"- ? {_name(r['from_label'])} -> {_name(r['to_label'])}: {r['reason']}")
        if len(listed) > 8:
            lines.append(f"- ... {len(listed) - 8} more in the report")
    if result["undrawn"]:
        lines += ["", "In the code but not in the diagram (top 8):"]
        for r in result["undrawn"][:8]:
            e = r["evidence"][0]
            lines.append(f"- {_name(r['from_label'])} -> {_name(r['to_label'])}: "
                         f"{plural(r['count'], 'import')} (e.g. {e['file']}:{e['line']})")
    return "\n".join(lines)


def _stem(name: str) -> str:
    for suffix in (".verified.excalidraw", ".excalidraw.json", ".excalidraw", ".spec.json", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name.rsplit(".", 1)[0]


def run(diagram_path: str, repo: str = ".", out_dir: str | None = None, auto: bool = True,
        keep_colors: bool = False, draw_undrawn: int = 0, report: bool = True,
        max_evidence: int = 12, repo_label: str | None = None, fit: float | None = None) -> dict:
    """`fit` lays a spec out for a stage of that width to height ratio, whatever its direction."""
    source = Path(diagram_path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("type") != "excalidraw":
        if "nodes" not in data:
            raise ValueError(f"{source} is neither an Excalidraw file nor a spec")
        data = dg.build({**data, "direction": None}, aspect=fit) if fit else dg.build(data)
    stem = _stem(source.name)
    target = Path(out_dir) if out_dir else source.parent
    target.mkdir(parents=True, exist_ok=True)

    nodes, arrows = dg.parse(data)
    hidden = {part for node in nodes.values() for p in node.paths for part in p.split("/")
              if part.startswith(".") and part not in (".", "..")}
    graph = build_graph(repo, include=hidden)
    result = evaluate(graph, nodes, arrows, auto=auto, max_evidence=max_evidence)
    stats = {"files": len(graph.files),
             "python": sum(f.endswith(".py") for f in graph.code_files),
             "js": sum(not f.endswith(".py") for f in graph.code_files),
             "edges": len(graph.edges), "unresolved": len(graph.unresolved),
             "parse_errors": len(graph.parse_errors)}
    marked = marked_copy(data, result, keep_colors, draw_undrawn)
    verified_path = target / f"{stem}.verified.excalidraw"
    verified_path.write_text(json.dumps(marked, indent=2, ensure_ascii=False), encoding="utf-8")
    outputs = {"excalidraw": str(verified_path)}
    if report:
        import render
        report_path = target / f"{stem}.report.html"
        render.write_report(report_path, marked, result, {
            "diagram": source.name, "repo": repo_label or str(Path(repo).resolve()),
            "stats": stats, "generated": time.strftime("%Y-%m-%d %H:%M")})
        outputs["report"] = str(report_path)
    result.update(stats=stats, outputs=outputs, text=summary(result, str(source), repo, stats))
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("diagram")
    ap.add_argument("--repo", default=".", help="repository root (default: .)")
    ap.add_argument("--out", help="output folder (default: next to the diagram)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--draw-undrawn", type=int, nargs="?", const=8, default=0, metavar="N",
                    help="draw up to N imports that the diagram leaves out (default 8)")
    ap.add_argument("--keep-colors", action="store_true", help="recolor only the problem arrows")
    ap.add_argument("--no-auto", action="store_true", help="do not map boxes by their labels")
    ap.add_argument("--strict", action="store_true", help="unchecked arrows also fail the run")
    ap.add_argument("--no-report", action="store_true", help="skip the HTML report")
    ap.add_argument("--repo-label", help="name to show for the repository in the report "
                                         "(default: its absolute path, which enables editor links)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = run(args.diagram, args.repo, args.out, auto=not args.no_auto,
                     keep_colors=args.keep_colors, draw_undrawn=args.draw_undrawn,
                     report=not args.no_report, repo_label=args.repo_label)
    except (OSError, ValueError) as exc:
        print(f"arrowproof: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "text"}, indent=2, ensure_ascii=False))
    else:
        print(result["text"])
        print("\nWrote: " + ", ".join(result["outputs"].values()))
    c = result["counts"]
    failed = c.get("not_in_code") or c.get("reversed") or result["box_counts"].get("not_in_code")
    if args.strict:
        failed = failed or c.get("unchecked")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
