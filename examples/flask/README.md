# Flask, drawn from memory

On 2026-10-02, two Claude models got the same prompt: draw the internal architecture of Flask (pallets/flask), map every box to a file or a package, and use no tools. They answered from memory only. Both answers are here unchanged, as `haiku.spec.json` (Claude Haiku 4.5) and `opus.spec.json` (Claude Opus 5.5).

arrowproof checked both against pallets/flask at commit `d73fa1c`.

| | Haiku | Opus |
|---|---:|---:|
| Arrows | 20 | 20 |
| ✓ verified | 15 | 20 |
| ↝ indirect | 1 | 0 |
| ✗ not in code | 2 | 0 |
| ? unchecked (box not in repo) | 2 | 0 |
| ✗ boxes not in repo | 1 | 0 |

What Haiku got wrong:

- "View Functions → Request/Response Wrappers" and "View Functions → Templating": `views.py` imports only `globals.py` and the type aliases in `typing.py`.
- "Flask Application → Configuration" is indirect: `app.py` subclasses `sansio/app.py`, which imports `config.py`.
- The Blueprints box points to `src/flask/blueprint.py`. The file is `blueprints.py`, so the two arrows on that box are unchecked.

Files:

- `*.spec.json`: the model output
- `*.verified.excalidraw`: the checked diagram (open it in excalidraw.com)
- `*.report.html`: the evidence report
- `*.summary.txt`: the text that the agent sees
- `opus.explainer.html`: a step-by-step tour of the Opus diagram, built from the verified arrows alone

Open them in the browser: the reports for [Haiku](https://ahmtsahin.github.io/arrowproof/examples/flask/haiku.report.html) and [Opus](https://ahmtsahin.github.io/arrowproof/examples/flask/opus.report.html), [the Opus explainer](https://ahmtsahin.github.io/arrowproof/examples/flask/opus.explainer.html#play=1), and the checked diagrams in Excalidraw for [Haiku](https://excalidraw.com/#url=https://raw.githubusercontent.com/ahmtsahin/arrowproof/main/examples/flask/haiku.verified.excalidraw) and [Opus](https://excalidraw.com/#url=https://raw.githubusercontent.com/ahmtsahin/arrowproof/main/examples/flask/opus.verified.excalidraw).

To run the check yourself:

```bash
git clone --depth 1 https://github.com/pallets/flask /tmp/flask
```

```bash
python skills/arrowproof/scripts/verify.py examples/flask/haiku.spec.json --repo /tmp/flask
```

The numbers can change when Flask changes.
