#!/usr/bin/env python3
"""Lay out a diagram spec as an Excalidraw file, without checking it.

usage: build.py SPEC.json [-o OUT.excalidraw] [--svg OUT.svg]

The spec format is in SKILL.md. verify.py accepts a spec too, and checks it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagram as dg  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", help="output .excalidraw (default: next to the spec)")
    ap.add_argument("--svg", help="also write an SVG preview")
    args = ap.parse_args(argv)
    src = Path(args.spec)
    try:
        data = dg.build(json.loads(src.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError) as exc:
        print(f"build: {exc}", file=sys.stderr)
        return 2
    name = src.name.removesuffix(".json").removesuffix(".spec")
    out = Path(args.out) if args.out else src.with_name(name + ".excalidraw")
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(out)
    if args.svg:
        import render
        render.write_svg(args.svg, dg.live(data))
        print(args.svg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
