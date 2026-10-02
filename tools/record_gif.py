#!/usr/bin/env python3
"""Record an explainer page as an animated GIF.

usage: record_gif.py URL OUT.gif [--seconds 24] [--fps 8] [--size 1280x720] [--width 960] [--chrome PATH]

Serve the repository first (`python -m http.server 8765`), then give the URL
of an explainer page with autoplay, for example
http://localhost:8765/examples/flask/opus.explainer.html#play=1&speed=2

Each frame is a headless Chrome screenshot after a given amount of virtual
time, so the result is smooth even on a slow machine. Needs Google Chrome
and Pillow (`pip install pillow`).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
]


def find_chrome(given: str | None) -> str:
    for candidate in ([given] if given else CHROME_PATHS):
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return candidate
    sys.exit("record_gif: Chrome not found; pass --chrome PATH")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=24)
    ap.add_argument("--fps", type=int, default=8)
    ap.add_argument("--size", default="1280x720", help="browser window, WIDTHxHEIGHT")
    ap.add_argument("--width", type=int, default=960, help="width of the GIF")
    ap.add_argument("--chrome")
    args = ap.parse_args(argv)
    try:
        from PIL import Image, ImageChops
    except ImportError:
        sys.exit("record_gif: needs Pillow (pip install pillow)")
    chrome = find_chrome(args.chrome)
    win_w, win_h = (int(v) for v in args.size.lower().split("x"))
    step = round(1000 / args.fps)
    times = list(range(250, int(args.seconds * 1000), step))
    with tempfile.TemporaryDirectory() as tmp:
        def shoot(t: int) -> None:
            subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            "--force-device-scale-factor=1", f"--virtual-time-budget={t}",
                            f"--window-size={win_w},{win_h}", f"--screenshot={Path(tmp) / f'{t:07d}.png'}",
                            args.url], capture_output=True, timeout=120)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(shoot, times))
        height = round(args.width * win_h / win_w)
        frames, durations = [], []
        for t in times:
            path = Path(tmp) / f"{t:07d}.png"
            if not path.is_file():
                continue
            img = Image.open(path).convert("RGB").resize((args.width, height), Image.LANCZOS)
            if frames and not ImageChops.difference(frames[-1], img).getbbox():
                durations[-1] += step   # an unchanged frame only lengthens the previous one
                continue
            frames.append(img)
            durations.append(step)
    if not frames:
        sys.exit("record_gif: no frames; is the page served at that URL?")
    durations[-1] += 1500               # rest on the last frame before the loop restarts
    palette = frames[len(frames) // 2].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    gif = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    gif[0].save(args.out, save_all=True, append_images=gif[1:], duration=durations, loop=0,
                optimize=True, disposal=1)
    print(f"{len(frames)} frames, {sum(durations) / 1000:.1f} s, {Path(args.out).stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
