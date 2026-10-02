#!/usr/bin/env python3
"""Print the real import graph of a repository, grouped for diagram design.

usage: graph.py [REPO] [--by auto|file|dir] [--depth N] [--top N] [--tests] [--json]

Read this before you draw: every arrow you draw between two groups should
appear in the IMPORTS list.
"""
from __future__ import annotations

import argparse
import json
import posixpath
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codegraph import build_graph, is_builtin, is_test_path  # noqa: E402


def group_of(path: str, by: str, depth: int | None) -> str:
    if by == "file":
        return path
    folder = posixpath.dirname(path)
    parts = folder.split("/") if folder else []
    return "/".join(parts[:depth]) or "."


def choose(code: list, by: str, depth: int | None) -> tuple:
    if by != "auto":
        return by, depth or 2
    if len(code) <= 60:
        return "file", None
    for d in range(1, 7):
        if len({group_of(f, "dir", d) for f in code}) >= 8:
            return "dir", d
    return "dir", 6


def summarize(repo: str, by: str = "auto", depth: int | None = None, tests: bool = False,
              top: int = 40) -> dict:
    graph = build_graph(repo)
    code = [f for f in graph.code_files if tests or not is_test_path(f)]
    by, depth = choose(code, by, depth)
    unit = {f: group_of(f, by, depth) for f in code}
    counts, examples = Counter(), {}
    for (src, dst), evs in graph.edges.items():
        if src in unit and dst in unit and unit[src] != unit[dst]:
            key = (unit[src], unit[dst])
            counts[key] += len(evs)
            examples.setdefault(key, evs[0])
    packages = defaultdict(set)
    for src, pkg in graph.packages:
        if src in unit and not is_builtin(pkg):
            packages[pkg].add(unit[src])
    sizes = Counter(unit.values())
    fan_out, fan_in = Counter(), Counter()
    for (a, b) in counts:
        fan_out[a] += 1
        fan_in[b] += 1
    return {
        "repo": str(Path(repo).resolve()), "by": by, "depth": depth,
        "python": sum(f.endswith(".py") for f in code), "js": sum(not f.endswith(".py") for f in code),
        "tests_included": tests,
        "groups": [{"name": g, "files": n, "imports_out": fan_out[g], "imported_by": fan_in[g]}
                   for g, n in sorted(sizes.items())],
        "imports": [{"from": a, "to": b, "count": n, "example": examples[(a, b)].as_dict()}
                    for (a, b), n in counts.most_common(top)],
        "imports_total": len(counts),
        "packages": [{"name": p, "used_by": sorted(users)}
                     for p, users in sorted(packages.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
        "unresolved": graph.unresolved[:10], "parse_errors": graph.parse_errors[:10],
    }


def as_text(s: dict) -> str:
    grouping = "file" if s["by"] == "file" else f"folder, depth {s['depth']}"
    out = [f"Repository {s['repo']}: {s['python']} Python and {s['js']} JS/TS files"
           f"{'' if s['tests_included'] else ' (tests left out)'}",
           f"Grouped by {grouping}: {len(s['groups'])} groups", "", "GROUPS"]
    width = max((len(g["name"]) for g in s["groups"]), default=10)
    for g in s["groups"]:
        out.append(f"  {g['name']:<{width}}  {g['files']:>4} files  "
                   f"imports {g['imports_out']:>2} groups  imported by {g['imported_by']:>2}")
    shown = len(s["imports"])
    out += ["", f"IMPORTS between groups ({shown} of {s['imports_total']}, most first)"]
    for e in s["imports"]:
        ex = e["example"]
        out.append(f"  {e['from']} -> {e['to']}  ({e['count']})  e.g. {ex['file']}:{ex['line']}")
    if s["packages"]:
        out += ["", "PACKAGES (standard library left out)"]
        for p in s["packages"][:30]:
            users = ", ".join(p["used_by"][:4]) + (" ..." if len(p["used_by"]) > 4 else "")
            out.append(f"  {p['name']}: {users}")
    if s["unresolved"]:
        out += ["", f"UNRESOLVED imports (first {len(s['unresolved'])}):"]
        out += [f"  {u['file']}:{u['line']}  {u['spec']}" for u in s["unresolved"]]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--by", choices=("auto", "file", "dir"), default="auto")
    ap.add_argument("--depth", type=int, help="folder depth when grouping by dir")
    ap.add_argument("--top", type=int, default=40, help="how many group imports to list")
    ap.add_argument("--tests", action="store_true", help="include test files")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not Path(args.repo).is_dir():
        print(f"graph: {args.repo} is not a folder", file=sys.stderr)
        return 2
    result = summarize(args.repo, args.by, args.depth, args.tests, args.top)
    print(json.dumps(result, indent=2) if args.json else as_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
