"""Tests for arrowproof. Run: python -m unittest discover -s tests"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "arrowproof" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import diagram as dg  # noqa: E402
import explain  # noqa: E402
import graph as graph_cli  # noqa: E402
import verify  # noqa: E402
from codegraph import build_graph, is_builtin, js_imports, strip_js_comments  # noqa: E402

PY_REPO = {
    "src/shop/__init__.py": "",
    "src/shop/api.py": "from .services import orders\nfrom shop.db import session\nimport json\n",
    "src/shop/services/__init__.py": "",
    "src/shop/services/orders.py": (
        "from typing import TYPE_CHECKING\nfrom ..db import session\nimport stripe\n"
        "if TYPE_CHECKING:\n    from shop.api import App\nelse:\n    import decimal\n"),
    "src/shop/db.py": "import sqlalchemy\n",
    "src/shop/cli.py": "import click\nfrom shop import api\n",
    "tests/test_api.py": "from shop.api import x\n",
    "examples/json.py": "x = 1\n",
    "examples/run.py": "import json\n",
    "broken.py": "def (:\n",
}

TS_REPO = {
    "tsconfig.json": '{\n  // aliases\n  "compilerOptions": {\n    "baseUrl": ".",\n'
                     '    "paths": { "@/*": ["src/*"], },\n  },\n}\n',
    "package.json": '{"name": "tsapp"}',
    "src/server.ts": "import { router } from './routes';\nimport express from 'express';\n"
                     "import fs from 'node:fs';\n",
    "src/routes/index.ts": "import {\n  getUser,\n} from '@/services/user';\nexport * from './health';\n"
                           "// import { nope } from './nope'\nconst s = \"// import x from './fake'\";\n",
    "src/routes/health.ts": "export const ok = 1;\n",
    "src/services/user.ts": "import { db } from '../db';\nimport type { User } from '../types';\n"
                            "const lazy = () => import('./audit');\n",
    "src/services/audit.ts": "export const a = 1;\n",
    "src/db.ts": "import { PrismaClient } from '@prisma/client';\n",
    "src/types.ts": "export type User = {};\n",
    "src/legacy.js": "const db = require('./db.js');\n",
    "src/esm.ts": "import { x } from './types.js';\n",
    "node_modules/express/index.js": "module.exports = {};\n",
}


def make_repo(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class PythonGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.graph = build_graph(make_repo(Path(cls.tmp.name), PY_REPO))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def edge(self, src, dst):
        return self.graph.edges.get((src, dst))

    def test_relative_and_absolute_imports(self):
        self.assertTrue(self.edge("src/shop/api.py", "src/shop/services/orders.py"))
        self.assertTrue(self.edge("src/shop/api.py", "src/shop/db.py"))
        self.assertTrue(self.edge("src/shop/services/orders.py", "src/shop/db.py"))
        self.assertTrue(self.edge("src/shop/cli.py", "src/shop/api.py"))
        self.assertTrue(self.edge("tests/test_api.py", "src/shop/api.py"))

    def test_type_checking_imports_are_flagged(self):
        evs = self.edge("src/shop/services/orders.py", "src/shop/api.py")
        self.assertTrue(evs and all(e.type_only for e in evs))
        decimal = self.graph.packages[("src/shop/services/orders.py", "decimal")]
        self.assertFalse(decimal[0].type_only)

    def test_packages_and_stdlib(self):
        self.assertIn(("src/shop/services/orders.py", "stripe"), self.graph.packages)
        self.assertIn(("src/shop/api.py", "json"), self.graph.packages)
        # a json.py next to the importer shadows the standard library, elsewhere it does not
        self.assertTrue(self.edge("examples/run.py", "examples/json.py"))
        self.assertIsNone(self.edge("src/shop/api.py", "examples/json.py"))

    def test_syntax_errors_are_reported_not_raised(self):
        self.assertTrue(any(e.startswith("broken.py") for e in self.graph.parse_errors))

    def test_evidence_has_line_and_text(self):
        ev = self.edge("src/shop/api.py", "src/shop/db.py")[0]
        self.assertEqual(ev.line, 2)
        self.assertIn("from shop.db import session", ev.text)


class JsGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.graph = build_graph(make_repo(Path(cls.tmp.name), TS_REPO))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def edge(self, src, dst):
        return self.graph.edges.get((src, dst))

    def test_relative_index_alias_and_reexport(self):
        self.assertTrue(self.edge("src/server.ts", "src/routes/index.ts"))
        self.assertTrue(self.edge("src/routes/index.ts", "src/services/user.ts"))
        self.assertTrue(self.edge("src/routes/index.ts", "src/routes/health.ts"))

    def test_dynamic_import_require_and_js_extension(self):
        self.assertTrue(self.edge("src/services/user.ts", "src/services/audit.ts"))
        self.assertTrue(self.edge("src/legacy.js", "src/db.ts"))
        self.assertTrue(self.edge("src/esm.ts", "src/types.ts"))

    def test_type_only_and_packages(self):
        self.assertTrue(self.edge("src/services/user.ts", "src/types.ts")[0].type_only)
        self.assertIn(("src/db.ts", "@prisma/client"), self.graph.packages)
        self.assertIn(("src/server.ts", "express"), self.graph.packages)
        self.assertIn(("src/server.ts", "fs"), self.graph.packages)

    def test_comments_and_strings_do_not_count(self):
        targets = {dst for (src, dst) in self.graph.edges if src == "src/routes/index.ts"}
        self.assertEqual(targets, {"src/services/user.ts", "src/routes/health.ts"})
        self.assertFalse(any("node_modules" in f for f in self.graph.code_files))

    def test_strip_keeps_lines_and_strings(self):
        src = "a // x\n/* b\nc */ 'd // e'"
        out = strip_js_comments(src)
        self.assertEqual(out.count("\n"), 2)
        self.assertIn("'d // e'", out)
        found = list(js_imports("import a from 'x'\n\nimport {\n b } from \"y\""))
        self.assertEqual([(spec, line) for spec, line, _, _ in found], [("x", 1), ("y", 3)])


SPEC = {
    "title": "tsapp",
    "nodes": [
        {"id": "user", "label": "User"},
        {"id": "server", "label": "Server", "paths": ["src/server.ts"]},
        {"id": "routes", "label": "Routes", "paths": ["src/routes"]},
        {"id": "services", "label": "Services", "paths": ["src/services"]},
        {"id": "db", "label": "DB access", "paths": ["src/db.ts"]},
        {"id": "prisma", "label": "Prisma", "packages": ["@prisma/client"]},
        {"id": "types", "label": "Types", "paths": ["src/types.ts"]},
        {"id": "legacy", "label": "Legacy", "paths": ["src/legacy.js"]},
        {"id": "ghost", "label": "Cache", "paths": ["src/cache.ts"]},
    ],
    "edges": [
        {"from": "user", "to": "server", "kind": "flow"},
        {"from": "server", "to": "routes"},
        {"from": "routes", "to": "services"},
        {"from": "services", "to": "db", "label": "queries"},
        {"from": "db", "to": "prisma"},
        {"from": "server", "to": "db"},
        {"from": "db", "to": "services"},
        {"from": "services", "to": "ghost"},
    ],
}


class VerifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = make_repo(Path(cls.tmp.name) / "repo", TS_REPO)
        cls.spec_path = Path(cls.tmp.name) / "arch.spec.json"
        cls.spec_path.write_text(json.dumps(SPEC), encoding="utf-8")
        cls.result = verify.run(str(cls.spec_path), str(cls.root))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def verdict(self, src, dst):
        for row in self.result["arrows"]:
            if row["from"] == f"ap-node-{src}" and row["to"] == f"ap-node-{dst}":
                return row["verdict"]
        raise AssertionError(f"no arrow {src} -> {dst}")

    def test_verdicts(self):
        self.assertEqual(self.verdict("server", "routes"), "verified")
        self.assertEqual(self.verdict("routes", "services"), "verified")
        self.assertEqual(self.verdict("services", "db"), "verified")
        self.assertEqual(self.verdict("db", "prisma"), "verified")
        self.assertEqual(self.verdict("server", "db"), "not_in_code")
        self.assertEqual(self.verdict("db", "services"), "reversed")
        self.assertEqual(self.verdict("user", "server"), "unchecked")
        self.assertEqual(self.verdict("services", "ghost"), "unchecked")
        self.assertEqual(self.result["counts"],
                         {"verified": 4, "not_in_code": 1, "reversed": 1, "unchecked": 2})

    def test_boxes_and_undrawn_imports(self):
        boxes = {n["id"]: n for n in self.result["nodes"]}
        self.assertEqual(boxes["ap-node-ghost"]["verdict"], "not_in_code")
        self.assertEqual(boxes["ap-node-user"]["verdict"], "skipped")
        undrawn = {(u["from"], u["to"]) for u in self.result["undrawn"]}
        self.assertIn(("ap-node-legacy", "ap-node-db"), undrawn)
        # `import type` is not a run-time dependency, so it is not reported as missing
        self.assertNotIn(("ap-node-services", "ap-node-types"), undrawn)

    def test_outputs_are_marked(self):
        out = json.loads(Path(self.result["outputs"]["excalidraw"]).read_text(encoding="utf-8"))
        arrows = {e["id"]: e for e in out["elements"] if e["type"] == "arrow"}
        bad = next(r for r in self.result["arrows"] if r["verdict"] == "not_in_code")
        self.assertEqual(arrows[bad["id"]]["strokeColor"], dg.COLORS["not_in_code"])
        self.assertEqual(arrows[bad["id"]]["strokeStyle"], "dashed")
        self.assertEqual(arrows[bad["id"]]["customData"]["arrowproof"]["verdict"], "not_in_code")
        texts = [e["text"] for e in out["elements"] if e["type"] == "text"]
        self.assertIn("✗ not in code", texts)
        self.assertIn("✗ not in repo", texts)
        report = Path(self.result["outputs"]["report"]).read_text(encoding="utf-8")
        self.assertIn('class="ap-edge"', report)
        self.assertIn('"not_in_code"', report)
        self.assertNotIn("__DATA__", report)

    def test_summary_names_the_problems(self):
        text = self.result["text"]
        self.assertIn('✗ "Server" -> "DB access"', text)
        self.assertIn('⇄ "DB access" -> "Services"', text)
        self.assertIn("src/cache.ts", text)


class IndirectTest(unittest.TestCase):
    def test_one_hop_chains_and_type_only_imports(self):
        files = {**PY_REPO, "src/shop/hooks.py":
                 "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from shop import api\n"}
        spec = {"nodes": [
            {"id": "api", "label": "API", "paths": ["src/shop/api.py"]},
            {"id": "db", "label": "DB", "paths": ["src/shop/db.py"]},
            {"id": "cli", "label": "CLI", "paths": ["src/shop/cli.py"]},
            {"id": "hooks", "label": "Hooks", "paths": ["src/shop/hooks.py"]},
            {"id": "stripe", "label": "Stripe", "packages": ["stripe"]}],
            "edges": [{"from": "cli", "to": "db"}, {"from": "api", "to": "stripe"},
                      {"from": "hooks", "to": "db"}, {"from": "hooks", "to": "api"}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo", files)
            path = Path(tmp) / "chain.spec.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            result = verify.run(str(path), str(root), report=False)
        rows = {(r["from"][8:], r["to"][8:]): r for r in result["arrows"]}
        self.assertEqual(rows[("cli", "db")]["verdict"], "indirect")
        self.assertEqual(rows[("cli", "db")]["via"], ["src/shop/api.py"])
        self.assertEqual(rows[("api", "stripe")]["verdict"], "indirect")
        self.assertEqual(rows[("api", "stripe")]["via"], ["src/shop/services/orders.py"])
        # the only way out of hooks.py is a TYPE_CHECKING import: no run-time chain
        self.assertEqual(rows[("hooks", "db")]["verdict"], "not_in_code")
        self.assertEqual(rows[("hooks", "api")]["verdict"], "verified")
        self.assertTrue(rows[("hooks", "api")]["type_only"])


class RealWorldRegressionTest(unittest.TestCase):
    """Cases found by running arrowproof on real repositories."""

    REPO = {
        # a demo script that loads Playwright from an absolute path
        "demo/check.cjs": "const { chromium } = require('C:/Users/x/.cache/rt/node_modules/playwright');\n"
                          "const other = require('/opt/elsewhere/tool.js');\n",
        # a test that writes a module: the import lives inside strings
        "tests/writer.test.mjs": "import test from 'node:test';\n"
                                 "const src = ['import { a } from \"./ghost.mjs\";',\n"
                                 "  'const b = await import(\"./ghost2.mjs\");'];\n"
                                 "const t = `import x from './ghost3.mjs'`;\n"
                                 "import { util } from './import-utils.mjs';\n",
        "tests/import-utils.mjs": "export const util = 1;\n",
        # a hidden folder of generated files, and one that holds real code
        ".tmp/copy/app.py": "import json\n",
        ".claude/skills/tool.py": "import requests\n",
        # a regex string with an invalid escape: Python warns while it parses
        "pkg/__init__.py": "",
        "pkg/patterns.py": "import re\nDIGITS = re.compile('\\d+')\n",
        # Kotlin, which arrowproof does not read
        "android/Agent.kt": "package app\nimport app.Phone\n",
        "android/Phone.kt": "package app\n",
    }

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = make_repo(Path(cls.tmp.name) / "repo", cls.REPO)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cls.graph = build_graph(cls.root)
        cls.warnings = [w for w in caught if issubclass(w.category, SyntaxWarning)]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_absolute_paths(self):
        self.assertIn(("demo/check.cjs", "playwright"), self.graph.packages)
        self.assertFalse(any(p.startswith("C:") for _, p in self.graph.packages))
        self.assertIn("/opt/elsewhere/tool.js", [u["spec"] for u in self.graph.unresolved])

    def test_imports_inside_strings_are_not_imports(self):
        targets = {d for (s, d) in self.graph.edges if s == "tests/writer.test.mjs"}
        self.assertEqual(targets, {"tests/import-utils.mjs"})
        self.assertFalse([u for u in self.graph.unresolved if "ghost" in u["spec"]])

    def test_node_prefixed_modules_are_builtins(self):
        self.assertIn(("tests/writer.test.mjs", "test"), self.graph.packages)
        self.assertTrue(is_builtin("test"))

    def test_hidden_folders(self):
        self.assertNotIn(".tmp/copy/app.py", self.graph.files)
        self.assertIn(".claude/skills/tool.py", self.graph.files)
        self.assertIn(".tmp/copy/app.py", build_graph(self.root, include={".tmp"}).files)

    def test_no_syntax_warnings_leak(self):
        self.assertEqual(self.warnings, [])
        self.assertIn(("pkg/patterns.py", "re"), self.graph.packages)

    def test_unreadable_language_is_unchecked_not_wrong(self):
        spec = {"nodes": [{"id": "agent", "label": "Agent", "paths": ["android/Agent.kt"]},
                          {"id": "phone", "label": "Phone", "paths": ["android/Phone.kt"]}],
                "edges": [{"from": "agent", "to": "phone"}]}
        path = Path(self.tmp.name) / "kotlin.spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        row = verify.run(str(path), str(self.root), report=False)["arrows"][0]
        self.assertEqual(row["verdict"], "unchecked")
        self.assertIn(".kt", row["reason"])


class FlowAndManifestTest(unittest.TestCase):
    """A declared flow can be confirmed but not refuted. Declared dependencies are real boxes."""

    def test_flows_and_declared_packages(self):
        files = {**TS_REPO,
                 "package.json": '{"name": "tsapp", "devDependencies": {"react-scripts": "5.0.1"}}',
                 "requirements.txt": "PyYAML>=6\n# a comment\n-e .\n",
                 "pyproject.toml": '[project]\nname = "x"\ndependencies = ["pandas>=2", "Requests[socks]"]\n'}
        spec = {"nodes": [
            {"id": "server", "label": "Server", "paths": ["src/server.ts"]},
            {"id": "services", "label": "Services", "paths": ["src/services"]},
            {"id": "db", "label": "DB", "paths": ["src/db.ts"]},
            {"id": "types", "label": "Types", "paths": ["src/types.ts"]},
            {"id": "rs", "label": "react-scripts", "packages": ["react-scripts"]},
            {"id": "pad", "label": "left-pad", "packages": ["left-pad"]}],
            "edges": [{"from": "services", "to": "db", "kind": "flow"},
                      {"from": "types", "to": "server", "kind": "flow"},
                      {"from": "server", "to": "services", "kind": "flow"},
                      {"from": "server", "to": "services"},
                      {"from": "server", "to": "rs"}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo", files)
            path = Path(tmp) / "flows.spec.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            result = verify.run(str(path), str(root), report=False)
            declared = build_graph(root).declared
        rows = [r["verdict"] for r in result["arrows"]]
        # direct import confirms a flow; nothing, or only a chain, leaves it unchecked
        self.assertEqual(rows[:3], ["verified", "unchecked", "unchecked"])
        # the same pair as an import claim is indirect, through src/routes/index.ts
        self.assertEqual(rows[3], "indirect")
        self.assertEqual(rows[4], "not_in_code")
        boxes = {n["id"][8:]: n for n in result["nodes"]}
        self.assertEqual(boxes["rs"]["verdict"], "ok")
        self.assertIn("declared", boxes["rs"]["reason"])
        self.assertEqual(boxes["pad"]["verdict"], "not_in_code")
        self.assertTrue({"react-scripts", "pyyaml", "pandas", "requests"} <= declared)


class ExplainTest(unittest.TestCase):
    MARK = '<script id="ap-data" type="application/json">'

    def explain(self, spec: dict) -> tuple:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo", TS_REPO)
            path = Path(tmp) / "arch.spec.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            result = explain.run(str(path), str(root))
            page = Path(result["outputs"]["explainer"]).read_text(encoding="utf-8")
        start = page.index(self.MARK) + len(self.MARK)
        return result, page, json.loads(page[start:page.index("</script>", start)])

    def test_auto_tour_covers_the_verified_arrows_and_ends_with_the_gaps(self):
        result, page, data = self.explain(SPEC)
        kinds = [s["kind"] for s in data["steps"]]
        self.assertEqual(kinds[0], "overview")
        self.assertEqual(kinds[-3:], ["problems", "unchecked", "missing"])
        verified = {r["id"] for r in result["arrows"] if r["verdict"] == "verified"}
        told = {a for s in data["steps"] if s["kind"] == "story" for a in s["arrows"]}
        self.assertTrue(verified <= told)
        self.assertIn('class="ap-edge"', page)
        self.assertNotIn("__DATA__", page)

    def test_written_tour(self):
        spec = {**SPEC, "tour": [
            {"title": "Where a request enters", "text": "The server loads the routes.",
             "focus": ["server", "routes"], "arrows": [["server", "routes"]]},
            {"title": "Services", "text": "The routes use the services.", "focus": ["routes", "services"]}]}
        _, _, data = self.explain(spec)
        story = [s for s in data["steps"] if s["kind"] == "story"]
        self.assertEqual([s["title"] for s in story], ["Where a request enters", "Services"])
        self.assertEqual([s["arrows"] for s in story], [["ap-edge-1"], ["ap-edge-2"]])
        self.assertIn("ap-node-routes", story[0]["nodes"])

    def test_fan_out_is_split_and_the_layout_fits_the_stage(self):
        mods = "abcdef"
        files = {"hub.py": "".join(f"import {m}\n" for m in mods), **{f"{m}.py": "" for m in mods},
                 **{f"chain{i}.py": f"import chain{i + 1}\n" for i in range(7)}, "chain7.py": ""}
        spec = {"direction": "LR",
                "nodes": [{"id": "hub", "label": "Hub", "paths": ["hub.py"]}]
                + [{"id": m, "label": m.upper(), "paths": [f"{m}.py"]} for m in mods]
                + [{"id": f"c{i}", "label": f"Chain step {i}", "paths": [f"chain{i}.py"]} for i in range(8)],
                "edges": [{"from": "hub", "to": m} for m in mods]
                + [{"from": f"c{i}", "to": f"c{i + 1}"} for i in range(7)]}
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo", files)
            path = Path(tmp) / "fan.spec.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            result = explain.run(str(path), str(root))
            marked = json.loads(Path(result["outputs"]["excalidraw"]).read_text(encoding="utf-8"))
        hub_steps = [s for s in result["steps"] if s["title"].startswith("Hub")]
        self.assertEqual([len(s["arrows"]) for s in hub_steps], [4, 2])
        self.assertEqual(hub_steps[0]["title"], "Hub (1/2)")
        boxes = [dg.bbox(e) for e in dg.live(marked) if e["id"].startswith("ap-node-c")]
        width = max(b[2] for b in boxes) - min(b[0] for b in boxes)
        height = max(b[3] for b in boxes) - min(b[1] for b in boxes)
        self.assertGreater(height, width)   # the long chain runs down, despite "direction": "LR"

    def test_a_broken_tour_fails_before_the_check(self):
        for tour in ([{"title": "x", "text": "y", "focus": ["nope"]}],
                     [{"title": "x", "text": "y", "focus": ["server"], "arrows": [["server", "ghost"]]}]):
            with self.assertRaises(ValueError):
                self.explain({**SPEC, "tour": tour})


class HandDrawnTest(unittest.TestCase):
    """A diagram without arrowproof metadata, arrows not bound, labels as free text."""

    def test_label_mapping_and_geometric_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo", TS_REPO)
            a = dg.new_element("rectangle", 0, 0, 200, 80, id="a")
            b = dg.new_element("rectangle", 400, 0, 200, 80, id="b")
            c = dg.new_element("ellipse", 400, 200, 200, 80, id="c")
            ta = dg.new_text("src/routes", 30, 20, 20)
            tb = dg.new_text("Services", 430, 20, 20)
            tc = dg.new_text("Redis", 450, 220, 20)
            ab = dg.new_element("arrow", 205, 40, 190, 0, points=[[0, 0], [190, 0]],
                                startArrowhead=None, endArrowhead="arrow", id="ab")
            bc = dg.new_element("arrow", 500, 85, 0, 110, points=[[0, 0], [0, 110]],
                                startArrowhead=None, endArrowhead="arrow", id="bc")
            data = {"type": "excalidraw", "elements": [a, b, c, ta, tb, tc, ab, bc]}
            path = Path(tmp) / "hand.excalidraw"
            path.write_text(json.dumps(data), encoding="utf-8")
            result = verify.run(str(path), str(root), report=False)
            rows = {r["id"]: r for r in result["arrows"]}
            self.assertEqual(rows["ab"]["verdict"], "verified")
            boxes = {n["id"]: n for n in result["nodes"]}
            self.assertEqual(boxes["a"]["source"], "label")
            self.assertEqual(boxes["b"]["paths"], ["src/services"])
            self.assertEqual(boxes["c"]["verdict"], "not_in_code")   # Redis is never imported
            self.assertEqual(rows["bc"]["verdict"], "unchecked")


class BuildTest(unittest.TestCase):
    def test_bindings_are_symmetric(self):
        data = dg.build(SPEC)
        els = {e["id"]: e for e in data["elements"]}
        for e in els.values():
            if e["type"] != "arrow":
                continue
            for key in ("startBinding", "endBinding"):
                target = els[e[key]["elementId"]]
                self.assertIn({"type": "arrow", "id": e["id"]}, target["boundElements"])
        labels = [e for e in els.values() if e["type"] == "text" and e.get("containerId")]
        self.assertTrue(all(els[t["containerId"]] for t in labels))

    def test_layers_follow_the_flow(self):
        data = dg.build({**SPEC, "direction": "LR"})
        x = {e["id"]: e["x"] for e in data["elements"] if e["id"].startswith("ap-node-")}
        self.assertLess(x["ap-node-server"], x["ap-node-routes"])
        self.assertLess(x["ap-node-routes"], x["ap-node-services"])

    def test_auto_direction_turns_a_long_chain_downward(self):
        chain = {"nodes": [{"id": f"n{i}", "label": f"Step {i} with a long descriptive label"}
                           for i in range(8)],
                 "edges": [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(7)]}
        data = dg.build(chain)
        boxes = [dg.bbox(e) for e in data["elements"]]
        width = max(b[2] for b in boxes) - min(b[0] for b in boxes)
        height = max(b[3] for b in boxes) - min(b[1] for b in boxes)
        self.assertGreater(height, width)
        labels = [e["text"] for e in data["elements"] if e["type"] == "text" and e.get("containerId")]
        self.assertTrue(all(max(len(line) for line in t.split("\n")) <= 32 for t in labels))

    def test_unknown_edge_is_rejected(self):
        with self.assertRaises(ValueError):
            dg.build({"nodes": [{"id": "a"}], "edges": [{"from": "a", "to": "b"}]})


class CliTest(unittest.TestCase):
    def test_exit_codes_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo", TS_REPO)
            good = {"nodes": SPEC["nodes"][1:3], "edges": [{"from": "server", "to": "routes"}]}
            spec = Path(tmp) / "good.json"
            spec.write_text(json.dumps(good), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = verify.main([str(spec), "--repo", str(root), "--json", "--no-report"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["counts"], {"verified": 1})
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps(SPEC), encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(verify.main([str(bad), "--repo", str(root), "--no-report"]), 1)
                self.assertEqual(verify.main([str(Path(tmp) / "missing.json")]), 2)

    def test_graph_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo", TS_REPO)
            s = graph_cli.summarize(str(root))
            self.assertEqual(s["by"], "file")
            pairs = {(e["from"], e["to"]) for e in s["imports"]}
            self.assertIn(("src/server.ts", "src/routes/index.ts"), pairs)
            self.assertIn("express", [p["name"] for p in s["packages"]])
            self.assertNotIn("fs", [p["name"] for p in s["packages"]])


if __name__ == "__main__":
    unittest.main()
