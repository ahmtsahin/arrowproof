"""The real import graph of a repository, for Python and JavaScript/TypeScript.

Standard library only. Every edge keeps its evidence: the file, the line and
the import statement that creates it. The graph is file-level. Callers group
files into diagram boxes.
"""
from __future__ import annotations

import ast
import json
import os
import posixpath
import re
import sys
import warnings
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

ALWAYS_SKIP = {"node_modules", "__pycache__", "site-packages", "bower_components", "venv"}
# Hidden folders hold caches, toolchains and generated files, so they are
# skipped. These few hold code that a diagram can be about.
HIDDEN_KEPT = {".github", ".claude", ".claude-plugin", ".codex", ".agents", ".storybook"}
# Skipped unless they are Python packages (pypa/build ships src/build/).
BUILD_DIRS = {"dist", "build", "out", "coverage", "target"}

JS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs",
           ".vue", ".svelte", ".astro")
JS_RESOLVE_EXTS = (".ts", ".tsx", ".d.ts", ".mts", ".cts", ".js", ".jsx",
                   ".mjs", ".cjs", ".vue", ".svelte", ".astro", ".json")
MAX_BYTES = 1_500_000

STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "abc", "argparse", "ast", "asyncio", "base64", "collections", "contextlib",
    "copy", "csv", "dataclasses", "datetime", "email", "enum", "functools",
    "glob", "hashlib", "http", "importlib", "inspect", "io", "itertools",
    "json", "logging", "math", "os", "pathlib", "pickle", "random", "re",
    "shutil", "socket", "sqlite3", "string", "subprocess", "sys", "tempfile",
    "threading", "time", "types", "typing", "unittest", "urllib", "uuid",
    "warnings", "weakref", "xml", "zipfile",
}
NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib", "test", "sqlite", "sea", "quic",
}

_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|__mocks__|test-d|spec|specs|e2e|testing|fixtures)(/|$)"
    r"|(^|/)(test_[^/]*\.py|[^/]*_test\.py|conftest\.py|[^/]*\.(test|spec)\.[cm]?[jt]sx?)$"
)


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def is_builtin(package: str) -> bool:
    return package in STDLIB or package in NODE_BUILTINS


@dataclass
class Evidence:
    file: str
    line: int
    text: str
    type_only: bool = False

    def as_dict(self) -> dict:
        out = {"file": self.file, "line": self.line, "text": self.text}
        if self.type_only:
            out["type_only"] = True
        return out


@dataclass
class CodeGraph:
    root: str
    files: list                # every file in the repository (posix, relative)
    code_files: list           # the files that the scanner parsed
    edges: dict = field(default_factory=dict)     # (src, dst) -> [Evidence]
    packages: dict = field(default_factory=dict)  # (src, package) -> [Evidence]
    unresolved: list = field(default_factory=list)
    parse_errors: list = field(default_factory=list)
    declared: set = field(default_factory=set)    # dependency names from manifests (dist_name form)

    def add_edge(self, src: str, dst: str, ev: Evidence) -> None:
        if src != dst:
            self.edges.setdefault((src, dst), []).append(ev)

    def add_package(self, src: str, name: str, ev: Evidence) -> None:
        self.packages.setdefault((src, name), []).append(ev)


def scan_files(root: Path, include=()) -> list:
    """Every file under root, except dependency, cache and build folders.

    `include` names hidden folders to read anyway, for example `.scripts`.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        keep = []
        for name in dirnames:
            if name in ALWAYS_SKIP or name.endswith(".egg-info"):
                continue
            if name.startswith(".") and name not in HIDDEN_KEPT and name not in include:
                continue
            sub = here / name
            if (sub / "pyvenv.cfg").is_file():
                continue
            if name in BUILD_DIRS and not (sub / "__init__.py").is_file():
                continue
            keep.append(name)
        dirnames[:] = sorted(keep)
        rel = here.relative_to(root).as_posix()
        for name in sorted(filenames):
            found.append(name if rel == "." else f"{rel}/{name}")
    return found


def _shared_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("/"), b.split("/")):
        if x != y:
            break
        n += 1
    return n


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= 160 else text[:157] + "..."


# --------------------------------------------------------------------- Python

def _is_type_checking(test) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def python_imports(source: str):
    """Yield (module, names, level, lineno, type_only) for each import."""
    tree = ast.parse(source)
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def type_only(node) -> bool:
        child, up = node, parent.get(node)
        while up is not None:
            if isinstance(up, ast.If) and _is_type_checking(up.test) and child in up.body:
                return True
            child, up = up, parent.get(up)
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            flag = type_only(node)
            for alias in node.names:
                yield alias.name, None, 0, node.lineno, flag
        elif isinstance(node, ast.ImportFrom):
            yield (node.module, [a.name for a in node.names], node.level,
                   node.lineno, type_only(node))


class PyResolver:
    """Maps dotted module names to files, the way sys.path roots would."""

    def __init__(self, files: list):
        py = [f for f in files if f.endswith(".py")]
        self.file_set = set(files)
        pkg_dirs = {posixpath.dirname(f) for f in py if posixpath.basename(f) == "__init__.py"}
        roots = {""}
        for d in pkg_dirs:
            if posixpath.dirname(d) not in pkg_dirs:
                roots.add(posixpath.dirname(d))   # parent of a top-level package
        for f in py:
            if posixpath.dirname(f) not in pkg_dirs:
                roots.add(posixpath.dirname(f))   # a plain directory of modules
        self.index: dict = {}
        for f in py:
            parts = f[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for root in roots:
                rparts = root.split("/") if root else []
                if len(parts) > len(rparts) and parts[:len(rparts)] == rparts:
                    self.index.setdefault(".".join(parts[len(rparts):]), []).append(f)

    def lookup(self, name: str, importer: str):
        cands = self.index.get(name)
        if not cands:
            return None
        best = max(cands, key=lambda f: (_shared_prefix(f, importer), -f.count("/")))
        top = name.split(".")[0]
        if top in STDLIB:
            # `import json` means the standard library, unless a package of that
            # name exists or a module of that name sits next to the importer.
            top_is_pkg = any(f.endswith("__init__.py") for f in self.index.get(top, []))
            if not top_is_pkg and posixpath.dirname(best) != posixpath.dirname(importer):
                return None
        return best

    def _file_at(self, base: str, parts: list):
        stem = "/".join(p for p in [base, *parts] if p)
        if not parts:
            init = f"{stem}/__init__.py" if stem else "__init__.py"
            return init if init in self.file_set else None
        for cand in (f"{stem}.py", f"{stem}/__init__.py"):
            if cand in self.file_set:
                return cand
        return None

    def resolve(self, importer: str, module, names, level: int) -> list:
        out = []
        if level:
            base = posixpath.dirname(importer)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            mod_parts = module.split(".") if module else []
            for name in names or []:
                if name != "*":
                    hit = self._file_at(base, mod_parts + [name])
                    if hit:
                        out.append(("file", hit))
            if not out:
                hit = self._file_at(base, mod_parts)
                if hit:
                    out.append(("file", hit))
            return out or [("unresolved", "." * level + (module or ""))]
        if names is not None:
            for name in names:
                if name != "*":
                    hit = self.lookup(f"{module}.{name}", importer)
                    if hit:
                        out.append(("file", hit))
            if out:
                return out
        parts = module.split(".")
        for i in range(len(parts), 0, -1):
            hit = self.lookup(".".join(parts[:i]), importer)
            if hit:
                return [("file", hit)]
        return [("package", parts[0])]


# ------------------------------------------------------ JavaScript/TypeScript

_JS_TOKENS = re.compile(
    r"""//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`""",
    re.S)
_JS_FROM = re.compile(
    r"""\b(?:import|export)\s+(type\s+)?[\w*${},\s]*?\bfrom\s*(['"])([^'"\n]+)\2""")
_JS_BARE = re.compile(r"""\bimport\s*(['"])([^'"\n]+)\1""")
_JS_CALL = re.compile(r"""\b(?:import|require)\s*\(\s*(['"])([^'"\n]+)\1\s*\)""")


# Import code written inside a string, for example a test that writes a module.
_IMPORT_IN_STRING = re.compile(
    r"""\b(?:import|export)\b[^'"`]*?\bfrom\s*['"]|\b(?:require|import)\s*\(\s*['"]|\bimport\s*['"]""")


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def strip_js_comments(src: str) -> str:
    """Blank out comments, keep strings and line numbers."""
    def repl(m):
        tok = m.group(0)
        return _blank(tok) if tok[0] == "/" else tok
    return _JS_TOKENS.sub(repl, src)


def mask_js(src: str) -> str:
    """Blank out comments, template literals and strings that hold import code.

    What is left are the real import statements. Line numbers do not change.
    """
    def repl(m):
        tok = m.group(0)
        if tok[0] == "/":
            return _blank(tok)
        if tok[0] == "`" or _IMPORT_IN_STRING.search(tok[1:-1]):
            return tok[0] + _blank(tok[1:-1]) + tok[-1]
        return tok
    return _JS_TOKENS.sub(repl, src)


def js_imports(source: str):
    """Yield (specifier, lineno, statement, type_only) for each import."""
    text = mask_js(source)
    newlines = [i for i, ch in enumerate(text) if ch == "\n"]
    seen = set()
    for pattern, group, type_group in ((_JS_FROM, 3, 1), (_JS_BARE, 2, None), (_JS_CALL, 2, None)):
        for m in pattern.finditer(text):
            start = m.start(group)
            if start in seen:
                continue
            seen.add(start)
            line = bisect_right(newlines, m.start()) + 1
            type_only = bool(type_group and m.group(type_group))
            yield m.group(group), line, _snippet(m.group(0)), type_only


def read_jsonc(text: str):
    text = strip_js_comments(text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


def _package_name(spec: str) -> str:
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


class JsResolver:
    def __init__(self, root: Path, files: list):
        self.root = root
        self.file_set = set(files)
        self.configs = self._load_configs(files)
        self.workspaces = self._load_workspaces(files)

    def _read(self, rel: str):
        try:
            return read_jsonc((self.root / rel).read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None

    def _config_options(self, rel: str, depth: int = 0) -> dict:
        """compilerOptions with baseUrl/paths made relative to the repo root."""
        data = self._read(rel)
        if not isinstance(data, dict) or depth > 5:
            return {}
        here = posixpath.dirname(rel)
        merged: dict = {}
        ext = data.get("extends")
        for parent in ([ext] if isinstance(ext, str) else ext or []):
            if isinstance(parent, str) and parent.startswith("."):
                target = posixpath.normpath(posixpath.join(here, parent))
                if not target.endswith(".json"):
                    target += ".json"
                if target in self.file_set:
                    merged.update(self._config_options(target, depth + 1))
        opts = data.get("compilerOptions") or {}
        if isinstance(opts.get("baseUrl"), str):
            merged["baseUrl"] = posixpath.normpath(posixpath.join(here, opts["baseUrl"]))
        if isinstance(opts.get("paths"), dict):
            anchor = merged.get("baseUrl", here)
            merged["paths"] = [
                (key, [posixpath.normpath(posixpath.join(anchor, t)) for t in targets
                       if isinstance(t, str)])
                for key, targets in opts["paths"].items() if isinstance(targets, list)
            ]
        return merged

    def _load_configs(self, files: list) -> dict:
        configs = {}
        for name in ("jsconfig.json", "tsconfig.json"):   # tsconfig wins
            for f in files:
                if posixpath.basename(f) == name:
                    opts = self._config_options(f)
                    if opts:
                        configs[posixpath.dirname(f)] = opts
        return configs

    def _load_workspaces(self, files: list) -> dict:
        found = {}
        for f in files:
            if posixpath.basename(f) == "package.json":
                data = self._read(f)
                if isinstance(data, dict) and isinstance(data.get("name"), str):
                    found[data["name"]] = (posixpath.dirname(f), data)
        return found

    def _nearest_config(self, importer: str):
        d = posixpath.dirname(importer)
        while True:
            if d in self.configs:
                return self.configs[d]
            if not d:
                return None
            d = posixpath.dirname(d)

    def _try(self, base: str):
        base = posixpath.normpath(base)
        if base.startswith(".."):
            return None
        if base == ".":
            base = ""
        if base in self.file_set:
            return base
        for ext in JS_RESOLVE_EXTS:
            if base and base + ext in self.file_set:
                return base + ext
        index = f"{base}/index" if base else "index"
        for ext in JS_RESOLVE_EXTS:
            if index + ext in self.file_set:
                return index + ext
        stem, ext = posixpath.splitext(base)
        if ext in (".js", ".jsx", ".mjs", ".cjs"):
            for alt in (".ts", ".tsx", ".mts", ".cts"):
                if stem + alt in self.file_set:
                    return stem + alt
        return None

    def _absolute(self, spec: str):
        """An absolute path: a package in some node_modules, or a file in this repo."""
        path = spec.replace("\\", "/")
        if "/node_modules/" in path:
            return ("package", _package_name(path.rsplit("/node_modules/", 1)[1]))
        root = self.root.as_posix().rstrip("/") + "/"
        if path.lower().startswith(root.lower()):
            hit = self._try(path[len(root):])
            if hit:
                return ("file", hit)
        return ("unresolved", spec)

    def resolve(self, importer: str, spec: str):
        spec = spec.split("?")[0]
        if not spec or spec.startswith("#"):
            return ("unresolved", spec)
        if spec.startswith("."):
            hit = self._try(posixpath.join(posixpath.dirname(importer), spec))
            return ("file", hit) if hit else ("unresolved", spec)
        if re.match(r"[A-Za-z]:[\\/]", spec) or spec.startswith("\\\\"):
            return self._absolute(spec)
        if spec.startswith("/"):
            hit = self._try(spec.lstrip("/"))   # root-relative, as Vite reads it
            return ("file", hit) if hit else self._absolute(spec)
        cfg = self._nearest_config(importer)
        if cfg:
            for key, targets in cfg.get("paths", []):
                if "*" in key:
                    pre, _, post = key.partition("*")
                    if spec.startswith(pre) and spec.endswith(post) and len(spec) >= len(pre) + len(post):
                        star = spec[len(pre):len(spec) - len(post)]
                        for t in targets:
                            hit = self._try(t.replace("*", star))
                            if hit:
                                return ("file", hit)
                elif spec == key:
                    for t in targets:
                        hit = self._try(t)
                        if hit:
                            return ("file", hit)
            if cfg.get("baseUrl") is not None:
                hit = self._try(posixpath.join(cfg["baseUrl"], spec))
                if hit:
                    return ("file", hit)
        name = _package_name(spec)
        if name in self.workspaces:
            pkg_dir, meta = self.workspaces[name]
            sub = spec[len(name):].lstrip("/")
            tries = ([posixpath.join(pkg_dir, sub), posixpath.join(pkg_dir, "src", sub)] if sub else
                     [posixpath.join(pkg_dir, meta[k]) for k in ("source", "module", "main", "types")
                      if isinstance(meta.get(k), str)]
                     + [posixpath.join(pkg_dir, "src/index"), posixpath.join(pkg_dir, "index")])
            for t in tries:
                hit = self._try(t)
                if hit:
                    return ("file", hit)
            return ("file", posixpath.join(pkg_dir, "package.json") if pkg_dir else "package.json")
        if name.startswith("node:"):
            name = name[5:].split("/")[0]
        return ("package", name)


# --------------------------------------------------------------- manifests

def dist_name(name: str) -> str:
    """A package name in one comparable form: lower case, runs of -_. as one dash."""
    return re.sub(r"[-_.]+", "-", name).lower()


_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9@][A-Za-z0-9._/-]*)")


def declared_packages(root: Path, files: list) -> set:
    """Dependencies that package.json, requirements*.txt and pyproject.toml declare."""
    names = set()
    for f in files:
        base = posixpath.basename(f)
        try:
            if base == "package.json":
                data = json.loads((root / f).read_text(encoding="utf-8", errors="replace"))
                for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                    if isinstance(data.get(key), dict):
                        names.update(data[key])
            elif re.fullmatch(r"requirements[\w.-]*\.txt", base):
                for line in (root / f).read_text(encoding="utf-8", errors="replace").splitlines():
                    m = _REQUIREMENT.match(line)
                    if m and not line.lstrip().startswith(("#", "-")):
                        names.add(m.group(1))
            elif base == "pyproject.toml":
                names.update(_pyproject_dependencies((root / f).read_text(encoding="utf-8", errors="replace")))
        except (OSError, ValueError):
            continue
    return {dist_name(n) for n in names}


def _pyproject_dependencies(text: str) -> set:
    try:
        import tomllib
    except ImportError:   # Python < 3.11: read the [project] dependencies list by pattern
        block = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", text)
        return {m.group(1) for m in re.finditer(r"""['"]\s*([A-Za-z0-9][A-Za-z0-9._-]*)""",
                                                block.group(1))} if block else set()
    data = tomllib.loads(text)
    project = data.get("project") or {}
    specs = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        specs += list(group or [])
    found = {m.group(1) for s in specs if isinstance(s, str) for m in [_REQUIREMENT.match(s)] if m}
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    return found | {k for k in poetry if k.lower() != "python"}


# ------------------------------------------------------------------- build

def build_graph(root, include=()) -> CodeGraph:
    """`include` names hidden folders to read anyway (see scan_files)."""
    root = Path(root).resolve()
    files = scan_files(root, include)
    code = [f for f in files
            if (f.endswith(".py") or f.endswith(JS_EXTS))
            and not f.endswith((".d.ts", ".min.js"))]
    graph = CodeGraph(root=str(root), files=files, code_files=code,
                      declared=declared_packages(root, files))
    py = PyResolver(files)
    js = JsResolver(root, files)
    for f in code:
        path = root / f
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            source = path.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
        except OSError:
            continue
        lines = source.splitlines()
        if f.endswith(".py"):
            try:
                with warnings.catch_warnings():   # e.g. "invalid escape sequence" in the source
                    warnings.simplefilter("ignore")
                    found = list(python_imports(source))
            except (SyntaxError, ValueError) as exc:
                graph.parse_errors.append(f"{f}: {exc.__class__.__name__}")
                continue
            for module, names, level, lineno, type_only in found:
                text = _snippet(lines[lineno - 1]) if 0 < lineno <= len(lines) else ""
                ev = Evidence(f, lineno, text, type_only)
                for kind, target in py.resolve(f, module, names, level):
                    _record(graph, f, kind, target, ev)
        else:
            for spec, lineno, text, type_only in js_imports(source):
                kind, target = js.resolve(f, spec)
                _record(graph, f, kind, target, Evidence(f, lineno, text, type_only))
    return graph


def _record(graph: CodeGraph, src: str, kind: str, target: str, ev: Evidence) -> None:
    if kind == "file":
        graph.add_edge(src, target, ev)
    elif kind == "package":
        graph.add_package(src, target, ev)
    elif len(graph.unresolved) < 200:
        graph.unresolved.append({"file": src, "line": ev.line, "spec": target})
