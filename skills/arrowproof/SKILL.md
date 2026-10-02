---
name: arrowproof
description: Draw architecture and dependency diagrams of a codebase as Excalidraw files in which every arrow is checked against the real import graph, check an existing .excalidraw diagram against the code, or turn a checked diagram into an interactive step-by-step explainer page. Use when the user asks for an architecture, module, component, dependency or data-flow diagram of a repository; asks whether a diagram is correct, accurate or up to date; wants to verify, audit or fact-check a diagram; wants a diagram or a walkthrough of what changed; or asks to have a change, feature or flow explained visually or in HTML. Works on Python and JavaScript/TypeScript repositories.
license: MIT
metadata:
  version: "0.1.0"
---

# arrowproof

A diagram that a model draws looks right even when it is wrong. This skill checks every box and every arrow against the import graph of the code. It marks what holds, marks what does not, and writes an evidence report with the import line behind each arrow.

The scripts are in `scripts/`, next to this file. Below, `<skill>` means the folder that holds this file. The scripts need Python 3.9 or later and nothing else. Use `python3` or `python`, whichever exists.

## What an arrow claims

`A -> B` claims that code in box A imports code in box B. Each arrow has a kind:

- `uses` is the default. The check runs in the arrow's direction. When the import only runs the other way, the arrow is ⇄ reversed.
- `flow` is a data or request flow: an HTTP call, a child process, a file that one part writes and another reads, a router context. An import in either direction confirms it. Without one, it stays unchecked, because a run-time flow is not visible in the imports. Use `flow` only for such flows, not to avoid a ✗.
- `skip` is a conceptual arrow. It is not checked.

A box maps to code through `paths` or `packages`. Paths are files, folders or globs, relative to the repository root. Packages are import names, for example `werkzeug` or `@prisma/client`. A box with neither is an actor, such as "User" or "Browser". Its arrows stay unchecked.

## Draw a new diagram

1. Read the real graph before you draw anything:
   `python3 <skill>/scripts/graph.py <repo>`
   For a large repository, add `--by dir --depth 2`.
2. Pick 6 to 14 boxes that answer the user's question. Map each box to paths or packages. Draw an arrow only when it appears in the IMPORTS list, or when it is a `flow` or a `skip`.
3. Write a spec file, for example `architecture.spec.json`:
   ```json
   {"title": "Checkout service",
    "nodes": [{"id": "web", "label": "Browser"},
              {"id": "api", "label": "HTTP API", "paths": ["src/api"]},
              {"id": "orders", "label": "Orders", "paths": ["src/orders"]},
              {"id": "pg", "label": "Postgres", "packages": ["pg"]}],
    "edges": [{"from": "web", "to": "api", "kind": "flow"},
              {"from": "api", "to": "orders"},
              {"from": "orders", "to": "pg", "label": "queries"}]}
   ```
   The script does the layout. Leave `direction` out, and it picks left to right or top to bottom, whichever fits a wide screen better. Set `"direction": "LR"` or `"TB"` only when the user asks for one. Keep labels short: the script wraps them, but a long label makes a large box.
4. Check the spec and render it:
   `python3 <skill>/scripts/verify.py architecture.spec.json --repo <repo>`
5. Fix every ✗ and ⇄. Remove the arrow, reverse it, or map the box again. For a ↝ indirect arrow, either add the box in between or keep the arrow and tell the user that it is a shortcut. Run the check again until the summary shows no problems. Do not mark an arrow `skip` only to make it pass.
6. Give the user `architecture.verified.excalidraw` and `architecture.report.html`. The Excalidraw file opens in excalidraw.com, in VS Code and in Obsidian. The report opens in a browser.

## Explain a change, step by step

When the user wants to understand a change, a feature or a flow, make an explainer page: the checked diagram on the left, a guided tour on the right. Each step zooms to its boxes and shows the import lines behind its arrows.

1. Draw and check the diagram as above, until it has no ✗ or ⇄.
2. Add a `tour` to the spec: 4 to 8 steps in the order a reader should follow. Each step has a `title` (at most 6 words), a `text` (2 or 3 short, plain sentences, one idea each), `focus` (the ids of 2 to 4 boxes) and `arrows` (the `[from, to]` pairs that the step talks about). Leave `arrows` out to take every arrow between the focus boxes. Say only what the code shows. Write the tour in English.
3. Run `python3 <skill>/scripts/explain.py architecture.spec.json --repo <repo>`.
4. Give the user `architecture.explainer.html`. It opens in a browser, plays with the space key, and moves with the arrow keys. Each step stays 6 to 14 seconds, by the length of its text and the number of its evidence cards. A button switches the speed between 1×, 1.5× and 2×.

Without a `tour`, the script builds one from the verified arrows alone, with at most 4 arrows on a step. The page always starts with an overview and ends with the claims that the code does not support, the arrows that the imports cannot prove, and the imports that the diagram leaves out. The page lays the diagram out to fill its own stage, so it can ignore the `direction` of the spec.

## Check an existing diagram

`python3 <skill>/scripts/verify.py path/to/diagram.excalidraw --repo <repo>`

- A box without metadata is mapped by its label: a path in the label, or a file, folder or package name. The summary tells how many boxes were mapped by label. When a box is not mapped or is mapped wrong, add `"customData": {"arrowproof": {"paths": ["src/x"]}}` to the box element, then run the check again.
- The script never changes the original file. It writes a `.verified.excalidraw` copy and the report.
- Exit code 1 means that the check found a ✗ or a ⇄. In CI, this keeps the diagrams in the docs honest.

## Read the result

- ✓ verified: an import line supports the arrow. The report shows the file and the line. When the only import is under `TYPE_CHECKING` or is an `import type`, the arrow is marked as type-only.
- ↝ indirect: there is no direct import, but the code reaches the target through exactly one other file. The summary names that file.
- ✗ not in code: no import connects the two boxes. Either the diagram is wrong, or the dependency goes through something that this check cannot see: HTTP, a queue, dependency injection, or an import with a computed name. Say which one you believe and why. The summary lists what the source box imports instead.
- ⇄ reversed: the import goes the other way.
- ? unchecked: an actor, a `skip` arrow, a `flow` with no import behind it, a box without a mapping, a box without Python or JS/TS files (for example `package.json` or Kotlin code), or an arrow that does not connect two boxes. The summary counts each group and lists the ones that need attention.
- ✗ box not in repo: the path of the box does not exist, or its package is neither imported nor declared in `package.json`, `requirements*.txt` or `pyproject.toml`.
- In the code but not in the diagram: imports between two boxes that have no arrow. Mention the large ones. `--draw-undrawn` adds them to the diagram as purple dashed arrows.

## Report to the user

Give the counts line first. Then give each ✗ and ⇄ with its reason. Then give the paths of the two output files. When arrows are unchecked, say how many. Do not call the diagram correct while arrows are unchecked.

## Limits

The check reads imports only. For Python, it reads the syntax tree. For JavaScript and TypeScript, it reads `import`, `export ... from`, `require` and `import()` with a literal string, and it resolves `tsconfig` paths and workspace packages. A runtime call without an import is not visible. Other languages are not read yet.
