#!/usr/bin/env python3
"""
mimir.py
========
Mimir - a lightweight, local MCP (Model Context Protocol) server that produces
dense *structural blueprints* of source files instead of dumping raw contents.
The goal is to cut the number of tokens a cloud LLM has to read when it explores
or verifies a codebase.

(Named for Mimir, the keeper of the well of wisdom at the root of the Norse
world-tree Yggdrasil: you ask it about the tree, and it tells you the truth.
Here the "tree" is your code's syntax tree.)

Design priorities (in order):
  1. Don't make the host machine heavy  -> aggressive blacklisting, bounded
     in-memory cache, file-size guards, no recursive full-text reads.
  2. Never crash the stdio stream       -> every tool returns a clean string;
     exceptions are caught and turned into instructional output.
  3. Be genuinely useful to the model   -> compact output, clear tool docs that
     say *when* and *why* to call each tool.

Transport: stdio (the default for Claude Code / Copilot CLI / Cursor, which
launch the server as a subprocess and pipe JSON-RPC over stdin/stdout).

Dependencies:
    pip install fastmcp tree-sitter tree-sitter-language-pack
(tree-sitter is optional; if it's missing the server falls back to regex.)

Environment variables:
    MCP_WORKSPACE_ROOT   Absolute path to the repo root. Default: current dir.
    MCP_MAX_FILE_BYTES   Skip files larger than this. Default: 2_000_000.
    MCP_ENABLE_SANDBOX   "1" to allow execute_local_sandbox, "0" to disable.
                         Default: "1".
    MCP_SANDBOX_TIMEOUT  Hard ceiling (seconds) for sandbox runs. Default: 10.
"""

from __future__ import annotations

import os
import re
import sys
import signal
import subprocess
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Iterable, Optional

from fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Optional tree-sitter. We import lazily and degrade gracefully. If anything in
# this block fails (package missing, grammar download blocked, ABI mismatch),
# TREE_SITTER_OK stays False and every tool uses the regex tokenizer instead.
# --------------------------------------------------------------------------- #
TREE_SITTER_OK = False
try:
    from tree_sitter_language_pack import get_parser as _ts_get_parser  # type: ignore
    TREE_SITTER_OK = True
except Exception:  # noqa: BLE001 - we genuinely want to swallow everything here
    _ts_get_parser = None  # type: ignore


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
WORKSPACE_ROOT = Path(os.environ.get("MCP_WORKSPACE_ROOT", os.getcwd())).resolve()
MAX_FILE_BYTES = int(os.environ.get("MCP_MAX_FILE_BYTES", "2000000"))
SANDBOX_ENABLED = os.environ.get("MCP_ENABLE_SANDBOX", "1") == "1"
SANDBOX_TIMEOUT_CEIL = int(os.environ.get("MCP_SANDBOX_TIMEOUT", "10"))

# Directories that are never worth parsing. Matched against any path segment, so
# "node_modules" anywhere in the tree is skipped. This is the single biggest
# lever for keeping the server light on large repos.
BLACKLIST_DIRS = {
    "node_modules", ".build", "DerivedData", ".git", "dist", "build",
    ".next", ".venv", "venv", "__pycache__", ".gradle", ".idea", ".vs",
    "Pods", ".dart_tool", "target", "out", "coverage", ".cache",
}

# Map file extensions -> (tree-sitter language name, regex profile key).
# The tree-sitter name is what tree_sitter_language_pack.get_parser() expects.
EXT_LANG = {
    ".py":   ("python",     "python"),
    ".pyi":  ("python",     "python"),
    ".js":   ("javascript", "cstyle"),
    ".jsx":  ("javascript", "cstyle"),
    ".mjs":  ("javascript", "cstyle"),
    ".ts":   ("typescript", "cstyle"),
    ".tsx":  ("tsx",        "cstyle"),
    ".cs":   ("csharp",     "cstyle"),
    ".kt":   ("kotlin",     "kotlin"),
    ".kts":  ("kotlin",     "kotlin"),
    ".swift":("swift",      "swift"),
    ".java": ("java",       "cstyle"),
    ".go":   ("go",         "go"),
    ".rs":   ("rust",       "rust"),
    ".rb":   ("ruby",       "ruby"),
    ".c":    ("c",          "cstyle"),
    ".h":    ("c",          "cstyle"),
    ".cpp":  ("cpp",        "cstyle"),
    ".cc":   ("cpp",        "cstyle"),
    ".hpp":  ("cpp",        "cstyle"),
    ".m":    ("objc",       "cstyle"),
}

# A node is a "definition" if its type ends with one of these suffixes. This
# convention covers almost every grammar: Python function_definition /
# class_definition; JS/TS/C#/Java/Kotlin/Swift *_declaration; Go *_declaration;
# Rust *_item; C/C++ *_specifier and function_definition. Suffix-matching keeps
# us correct as grammars evolve, without a giant per-language table.
DEF_SUFFIXES = (
    "_definition", "_declaration", "_declarator", "_item", "_specifier",
)
# Nodes that match DEF_SUFFIXES but are never genuine definitions. The inner
# definition node (e.g. function_definition inside decorated_definition) is
# always present and produces a cleaner signature, so the wrapper is redundant.
DEF_EXCLUDE = frozenset({
    "format_specifier",    # f-string :<5 etc. match _specifier but are not defs
    "decorated_definition", # wrapper around def/class; inner node captured separately
    "variable_declarator",  # always inside lexical_declaration; parent gives const/let sig
})
# A few grammars (notably Ruby) name definitions with bare words. These are only
# honored when the node is a *named* compound node, which excludes bare keyword
# tokens (e.g. the literal `class` token in C++, whose type is also "class").
DEF_EXACT = {"method", "singleton_method", "constructor", "class", "module", "function"}
# File-root container node types - never treat these as definitions.
ROOT_TYPES = {
    "module", "program", "source_file", "translation_unit",
    "compilation_unit", "document",
}
# Child node types that mark "implementation starts here" - we slice the
# signature to end right before these so loops/logic are stripped.
BODY_TYPES = {
    "block", "body", "class_body", "declaration_list", "field_declaration_list",
    "function_body", "enum_body", "statement_block", "compound_statement",
}
# Common English words that are never useful as symbol search terms.
_SCOPE_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "so", "as", "if", "in", "on", "at",
    "to", "of", "by", "for", "with", "from", "into", "that", "this", "it", "is",
    "are", "was", "be", "do", "we", "i", "my", "its", "has", "have", "will",
    "not", "now", "how", "what", "when", "where", "why", "can", "could", "would",
    "should", "just", "also", "then", "some", "other", "there", "about", "which",
    "make", "made", "get", "set", "use", "new", "old", "used", "using", "based",
    "add", "added", "fix", "fixed", "change", "update", "modify", "refactor",
    "create", "build", "need", "want", "like", "work", "works", "working",
    "implement", "improve", "feature", "issue", "bug", "code", "file", "module",
    "class", "method", "function", "logic", "value", "type", "data", "list",
    "hook", "page", "show", "hide", "open", "close", "load", "save", "send",
    "check", "display", "render", "return", "handle", "fetch", "call", "click",
    "style", "color", "text", "size", "time", "date", "name", "path", "link",
})

# Token-cheap regex profiles for the no-tree-sitter fallback. Each entry is a
# list of compiled patterns; a matching line is reported verbatim (trimmed).
def _rx(*pats: str) -> list[re.Pattern]:
    return [re.compile(p) for p in pats]

REGEX_PROFILES: dict[str, list[re.Pattern]] = {
    "python": _rx(
        r"^\s*(async\s+)?def\s+\w+\s*\(",
        r"^\s*class\s+\w+",
    ),
    "cstyle": _rx(
        r"^\s*(export\s+)?(default\s+)?(public|private|protected|internal|static|abstract|final|async|override|sealed|partial|\s)*"
        r"(class|interface|enum|struct|record|namespace)\s+\w+",
        r"^\s*(public|private|protected|internal|static|async|override|virtual|final|export|function|func|fun|def|fn|\s)+"
        r"[\w<>\[\],\s\.]*\b\w+\s*\([^;{]*\)\s*[:{].*$",
        r"^\s*(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s+)?\(?.*=>",
        r"^\s*\w[\w<>\[\],\s\.\*&]*\s+\w+\s*\([^;]*\)\s*\{?\s*$",
    ),
    "kotlin": _rx(
        r"^\s*(public|private|protected|internal|open|abstract|final|sealed|data|inner|override|suspend|\s)*"
        r"(class|interface|object|enum\s+class|fun)\s+\w+",
    ),
    "swift": _rx(
        r"^\s*(public|private|internal|fileprivate|open|final|static|class|override|\s)*"
        r"(class|struct|enum|protocol|extension|func|init)\b.*",
    ),
    "go": _rx(
        r"^\s*func\s+(\([^)]*\)\s*)?\w+\s*\(",
        r"^\s*type\s+\w+\s+(struct|interface)\b",
    ),
    "rust": _rx(
        r"^\s*(pub\s+)?(async\s+)?fn\s+\w+",
        r"^\s*(pub\s+)?(struct|enum|trait|impl|mod)\s+\w+",
    ),
    "ruby": _rx(
        r"^\s*(def|class|module)\s+\w+",
    ),
    "generic": _rx(
        r"^\s*(def|func|fun|fn|function|class|struct|interface|enum|trait|impl|type|object|protocol)\b.*\w",
    ),
}


# --------------------------------------------------------------------------- #
# Bounded in-memory cache (the "fast path")
# --------------------------------------------------------------------------- #
# Key: absolute file path. Value: (mtime, size, blueprint_string).
# OrderedDict gives us cheap FIFO eviction so memory stays bounded no matter how
# big the repo is - this is part of the anti-heaviness contract.
_CACHE: "OrderedDict[str, tuple[float, int, str]]" = OrderedDict()
_CACHE_MAX = 2048


def _cache_get(path: Path) -> Optional[str]:
    """Return cached blueprint iff the file is unchanged since we cached it."""
    try:
        st = path.stat()
    except OSError:
        return None
    hit = _CACHE.get(str(path))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        _CACHE.move_to_end(str(path))  # mark as recently used
        return hit[2]
    return None


def _cache_put(path: Path, blueprint: str) -> None:
    try:
        st = path.stat()
    except OSError:
        return
    _CACHE[str(path)] = (st.st_mtime, st.st_size, blueprint)
    _CACHE.move_to_end(str(path))
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)  # evict oldest


# --------------------------------------------------------------------------- #
# Path helpers / safety
# --------------------------------------------------------------------------- #
def _is_blacklisted(path: Path) -> bool:
    return any(part in BLACKLIST_DIRS for part in path.parts)


def _resolve_in_workspace(rel_path: str) -> Path:
    """Resolve a user-supplied relative path and refuse to escape the root."""
    candidate = (WORKSPACE_ROOT / rel_path).resolve()
    # Reject path traversal (e.g. ../../etc/passwd).
    # is_relative_to() is case-aware on Windows and avoids the startswith prefix bug.
    if not candidate.is_relative_to(WORKSPACE_ROOT):
        raise ValueError("path escapes the configured workspace root")
    return candidate


def _iter_source_files() -> Iterable[Path]:
    """Walk the workspace, pruning blacklisted dirs as early as possible."""
    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        # In-place prune so os.walk never even descends into junk dirs.
        dirs[:] = [d for d in dirs if d not in BLACKLIST_DIRS]
        for name in files:
            if Path(name).suffix in EXT_LANG:
                yield Path(root) / name


# --------------------------------------------------------------------------- #
# Structure extraction
# --------------------------------------------------------------------------- #
_FN_NODE_KINDS = frozenset({
    "arrow_function", "function_expression", "function", "generator_function",
    "generator_function_expression", "method_definition",
})


def _signature_from_node(node, src: bytes) -> str:
    """Slice a definition node down to just its signature (no body)."""
    cut = node.end_byte()
    for i in range(node.child_count()):
        child = node.child(i)
        kind = child.kind()
        if kind in BODY_TYPES or kind.endswith("_body"):
            cut = child.start_byte()
            break
        # Compound fn child (not a bare keyword token) — look inside for body
        if kind in _FN_NODE_KINDS and child.child_count() > 0:
            for j in range(child.child_count()):
                gc = child.child(j)
                gk = gc.kind()
                if gk in BODY_TYPES or gk.endswith("_body"):
                    cut = gc.start_byte()
                    break
                if gk == "=>":  # expression-body arrow: cut right after =>
                    cut = gc.end_byte()
                    break
            else:
                cut = child.end_byte()
            break
        # const/let fn = () => { body } — declarator wraps the fn, go two levels
        if kind == "variable_declarator":
            for j in range(child.child_count()):
                gc = child.child(j)
                if gc.kind() in _FN_NODE_KINDS and gc.child_count() > 0:
                    for k in range(gc.child_count()):
                        ggc = gc.child(k)
                        ggk = ggc.kind()
                        if ggk in BODY_TYPES or ggk.endswith("_body"):
                            cut = ggc.start_byte()
                            break
                        if ggk == "=>":
                            cut = ggc.end_byte()
                            break
                    else:
                        cut = gc.end_byte()
                    break
            break
    raw = src[node.start_byte():cut].decode("utf-8", "replace")
    sig = " ".join(raw.split()).rstrip("{:( =>").strip()
    return sig[:150] if len(sig) > 150 else sig


def _is_def_node(node) -> bool:
    """True only for genuine definition nodes - not the file root, not keyword tokens."""
    if node.parent() is None:          # the file-root container itself
        return False
    t = node.kind()
    if t in DEF_EXCLUDE:
        return False
    # const/let/var x = ...: only a definition when the value is a function
    if t in ("lexical_declaration", "variable_declaration"):
        for i in range(node.child_count()):
            child = node.child(i)
            if child.kind() == "variable_declarator":
                for j in range(child.child_count()):
                    if child.child(j).kind() in _FN_NODE_KINDS:
                        return True
        return False
    if t.endswith(DEF_SUFFIXES):
        return True
    if t in DEF_EXACT and node.is_named() and node.child_count() > 0:
        return True
    return False


def _extract_tree_sitter(path: Path, src: bytes, ts_lang: str) -> Optional[str]:
    """Deterministic AST extraction. Returns None to signal 'fall back to regex'."""
    if not TREE_SITTER_OK:
        return None
    try:
        parser = _ts_get_parser(ts_lang)  # type: ignore[misc]
        tree = parser.parse(src.decode("utf-8", "replace"))
    except Exception:  # grammar missing/broken -> let caller use regex
        return None

    lines: list[str] = []

    # Signatures that are just bare keywords have no name to reference — skip them
    _anon = re.compile(r'^(?:async\s+)?(?:function|class|interface|enum)\s*[<({]?$')

    def walk(node, depth: int) -> None:
        child_depth = depth
        if _is_def_node(node):
            sig = _signature_from_node(node, src)
            if sig and not _anon.match(sig):
                line_no = node.start_position().row + 1
                indent = "  " * min(depth, 6)
                lines.append(f"L{line_no:<5}{indent}{sig}")
                child_depth = depth + 1
        for i in range(node.child_count()):
            walk(node.child(i), child_depth)

    walk(tree.root_node(), 0)
    return "\n".join(lines)


def _extract_regex(text: str, profile_key: str) -> str:
    """Ultra-fast line tokenizer fallback. No parsing, just pattern matching."""
    patterns = REGEX_PROFILES.get(profile_key) or REGEX_PROFILES["generic"]
    lines: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if len(line) > 400:  # skip absurdly long minified lines cheaply
            continue
        for pat in patterns:
            if pat.match(line):
                # Indent roughly by leading whitespace so nesting is visible.
                indent = "  " * min((len(line) - len(line.lstrip())) // 4, 6)
                lines.append(f"L{i:<5}{indent}{line.strip()[:200]}")
                break
    return "\n".join(lines)


def _build_blueprint(path: Path) -> str:
    """Core: produce a compact structural map for one file (cache-aware)."""
    cached = _cache_get(path)
    if cached is not None:
        return cached

    suffix = path.suffix
    ts_lang, profile = EXT_LANG.get(suffix, (None, "generic"))
    rel = path.relative_to(WORKSPACE_ROOT) if str(path).startswith(str(WORKSPACE_ROOT)) else path

    try:
        size = path.stat().st_size
    except OSError as e:
        return f"Error: cannot stat '{rel}' ({e.strerror}). Check the path is correct."

    if size > MAX_FILE_BYTES:
        return (f"Error: '{rel}' is {size:,} bytes (over the {MAX_FILE_BYTES:,} cap) "
                f"and was skipped to protect memory. Read specific line ranges with "
                f"your native file tool instead, or raise MCP_MAX_FILE_BYTES.")

    try:
        raw = path.read_bytes()
    except OSError as e:
        return f"Error: cannot read '{rel}' ({e.strerror})."

    engine = "regex"
    body: Optional[str] = None
    if ts_lang:
        body = _extract_tree_sitter(path, raw, ts_lang)
        if body is not None:
            engine = "tree-sitter"
    if body is None:
        body = _extract_regex(raw.decode("utf-8", "replace"), profile)

    header = f"# {rel}  [{suffix.lstrip('.') or '?'} · {engine}]"
    blueprint = header + "\n" + (body if body.strip() else "  (no top-level symbols found)")
    _cache_put(path, blueprint)
    return blueprint


# --------------------------------------------------------------------------- #
# Symbol search helpers (shared by verify_symbol_existence and scope_task)
# --------------------------------------------------------------------------- #

def _symbol_hits(name: str, max_results: int = 25) -> list[tuple[str, str, str]]:
    """Search blueprints for definitions of *name*. Returns (rel_path, line_no, sig) tuples."""
    needle = name.encode("utf-8")
    word_def = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
    hits: list[tuple[str, str, str]] = []
    for path in _iter_source_files():
        if len(hits) >= max_results:
            break
        try:
            if needle not in path.read_bytes():
                continue
        except OSError:
            continue
        blueprint = _build_blueprint(path)
        rel = str(path.relative_to(WORKSPACE_ROOT))
        for bl in blueprint.splitlines():
            if bl.startswith("#") or not word_def.search(bl):
                continue
            m = re.match(r"L(\d+)\s*(.*)", bl.strip())
            if m:
                hits.append((rel, m.group(1), m.group(2).strip()))
                if len(hits) >= max_results:
                    break
    return hits


def _extract_scope_keywords(task: str) -> list[str]:
    """Pull candidate symbol names out of a plain-English task description."""
    seen: set[str] = set()
    out: list[str] = []

    # CamelCase — almost certainly code symbols
    for m in re.finditer(r'\b[A-Z][a-zA-Z0-9]+\b', task):
        w = m.group()
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)

    # snake_case — almost certainly code symbols
    for m in re.finditer(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b', task):
        w = m.group()
        if w not in seen:
            seen.add(w)
            out.append(w)

    # Longer plain words not in stopwords — likely domain terms
    for m in re.finditer(r'\b[a-zA-Z]{5,}\b', task):
        w = m.group()
        wl = w.lower()
        if wl not in seen and wl not in _SCOPE_STOPWORDS:
            seen.add(wl)
            out.append(w)

    return out[:12]  # cap to keep searches fast


def _extract_path_keywords(task: str) -> list[str]:
    """Looser keyword set for path matching — includes short words, fewer stopwords."""
    _path_stops = frozenset({
        "the", "and", "for", "with", "how", "add", "fix", "new", "get", "set",
        "use", "make", "do", "in", "on", "at", "to", "of", "by", "or", "a",
    })
    words = re.findall(r'[a-zA-Z]{3,}', task.lower())
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w not in seen and w not in _path_stops:
            seen.add(w)
            out.append(w)
    return out[:15]


def _parse_import_entries(path: Path, text: str) -> list[tuple[str, str]]:
    """Return (specifier, names) pairs for every import statement in the file."""
    suffix = path.suffix
    entries: list[tuple[str, str]] = []

    if suffix in ('.ts', '.tsx', '.js', '.jsx', '.mjs'):
        # import [type] { A, B } from 'path'  |  import X from 'path'  |  export ... from 'path'
        for m in re.finditer(
            r'(?:import|export)\s+(?:type\s+)?(\{[^}]*\}|[\w*]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w*]+(?:\s+as\s+\w+)?)*)?\s*'
            r'(?:,\s*\{[^}]*\})?\s*(?:from\s+)?[\'"]([^\'"]+)[\'"]',
            text,
        ):
            names = re.sub(r'\s+', ' ', (m.group(1) or '').strip())[:80]
            spec = m.group(2)
            if spec:
                entries.append((spec, names))
        # bare import 'side-effect'
        for m in re.finditer(r'\bimport\s+[\'"]([^\'"]+)[\'"]', text):
            entries.append((m.group(1), ''))
        # require()
        for m in re.finditer(r'\brequire\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)', text):
            if not any(e[0] == m.group(1) for e in entries):
                entries.append((m.group(1), 'require'))

    elif suffix in ('.py', '.pyi'):
        for m in re.finditer(r'^from\s+([\w.]+)\s+import\s+(.+)', text, re.MULTILINE):
            entries.append((m.group(1), m.group(2).strip()[:80]))
        for m in re.finditer(r'^import\s+([\w, ]+)', text, re.MULTILINE):
            for mod in m.group(1).split(','):
                mod = mod.strip().split(' as ')[0].strip()
                if mod:
                    entries.append((mod, ''))

    elif suffix == '.go':
        for m in re.finditer(r'^\s*(?:\w+\s+)?"([^"]+)"', text, re.MULTILINE):
            entries.append((m.group(1), ''))

    elif suffix == '.rs':
        for m in re.finditer(r'^use\s+([\w:]+(?:::\{[^}]+\})?);', text, re.MULTILINE):
            entries.append((m.group(1), ''))

    elif suffix == '.cs':
        for m in re.finditer(r'^(?:global\s+)?using\s+(?:static\s+)?([\w.]+)\s*;', text, re.MULTILINE):
            entries.append((m.group(1), ''))

    elif suffix in ('.kt', '.kts'):
        for m in re.finditer(r'^import\s+([\w.*]+)', text, re.MULTILINE):
            entries.append((m.group(1), ''))

    elif suffix == '.swift':
        for m in re.finditer(r'^(?:@\w+\s+)?import\s+(\w+)', text, re.MULTILINE):
            entries.append((m.group(1), ''))

    # deduplicate by specifier, preserve order
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for spec, names in entries:
        if spec not in seen:
            seen.add(spec)
            out.append((spec, names))
    return out


_NS_CACHE: dict[str, str] = {}  # suffix → detected root namespace


def _detect_root_namespace(suffix: str) -> str:
    """Infer the project's own root namespace/package by sampling source files."""
    if suffix in _NS_CACHE:
        return _NS_CACHE[suffix]

    prefixes: dict[str, int] = {}
    count = 0
    for path in _iter_source_files():
        if path.suffix != suffix or count >= 20:
            break
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if suffix == '.kt' or suffix == '.kts':
            m = re.search(r'^package\s+([\w.]+)', text, re.MULTILINE)
        elif suffix == '.cs':
            m = re.search(r'^namespace\s+([\w.]+)', text, re.MULTILINE)
        elif suffix == '.swift':
            # Swift doesn't declare package in source; skip namespace detection
            m = None
        else:
            m = None
        if m:
            pkg = m.group(1)
            root = '.'.join(pkg.split('.')[:2])  # take first two segments
            prefixes[root] = prefixes.get(root, 0) + 1
            count += 1

    ns = max(prefixes, key=lambda k: prefixes[k]) if prefixes else ''
    _NS_CACHE[suffix] = ns
    return ns


def _resolve_import(specifier: str, source_file: Path) -> tuple[str, str]:
    """Resolve an import specifier to (kind, display).
    kind: 'workspace' | 'unresolved' | 'external'
    display: workspace-relative path for workspace hits, package name otherwise.
    """
    suffix = source_file.suffix

    if suffix in ('.ts', '.tsx', '.js', '.jsx', '.mjs'):
        if specifier.startswith(('./', '../')):
            base = (source_file.parent / specifier).resolve()
            candidates = [
                base,
                *[Path(str(base) + ext) for ext in ('.ts', '.tsx', '.js', '.jsx', '.mjs')],
                *[base / f'index{ext}' for ext in ('.ts', '.tsx', '.js', '.jsx')],
            ]
            for c in candidates:
                if c.is_file():
                    try:
                        return ('workspace', str(c.relative_to(WORKSPACE_ROOT)))
                    except ValueError:
                        pass
            return ('unresolved', specifier)

        if specifier.startswith('@/'):
            rel = specifier[2:]
            base = WORKSPACE_ROOT / rel
            candidates = [
                base,
                *[Path(str(base) + ext) for ext in ('.ts', '.tsx', '.js', '.jsx')],
                *[base / f'index{ext}' for ext in ('.ts', '.tsx', '.js', '.jsx')],
            ]
            for c in candidates:
                if c.is_file():
                    try:
                        return ('workspace', str(c.relative_to(WORKSPACE_ROOT)))
                    except ValueError:
                        pass
            return ('unresolved', rel)

        return ('external', specifier)

    if suffix in ('.py', '.pyi'):
        if specifier.startswith('.'):
            dots = len(specifier) - len(specifier.lstrip('.'))
            parts = specifier.lstrip('.')
            base = source_file.parent
            for _ in range(dots - 1):
                base = base.parent
            if parts:
                mod_path = base / parts.replace('.', '/')
                for c in [mod_path.with_suffix('.py'), mod_path / '__init__.py']:
                    if c.is_file():
                        try:
                            return ('workspace', str(c.relative_to(WORKSPACE_ROOT)))
                        except ValueError:
                            pass
            return ('unresolved', specifier)
        return ('external', specifier)

    if suffix in ('.cs', '.kt', '.kts', '.swift'):
        # Namespace/module imports can't be resolved to specific files without
        # knowing the project's root namespace. Flag as workspace if the specifier
        # prefix matches any namespace declared in the workspace, else external.
        root_ns = _detect_root_namespace(source_file.suffix)
        if root_ns and specifier.startswith(root_ns):
            return ('workspace', specifier)
        return ('external', specifier)

    return ('external', specifier)


# --------------------------------------------------------------------------- #
# MCP server + tools
# --------------------------------------------------------------------------- #
mcp = FastMCP("mimir")


@mcp.tool()
def get_file_structure(file_path: str) -> str:
    """Return a compact structural blueprint of ONE source file: only classes,
    functions, methods, structs, and their signatures - all bodies, loops, and
    implementation text stripped out.

    WHEN TO USE: call this BEFORE reading a source file's raw contents. It gives
    you the symbol map (with line numbers) at a fraction of the token cost, which
    is almost always enough to decide what to do next. Only read raw lines after
    this, and only the specific ranges you actually need.

    Args:
        file_path: path to the file, relative to the workspace root.

    Returns a dense text map like:
        # src/auth/session.py  [py · tree-sitter]
        L12  class SessionManager
        L18    def create(self, user_id) -> Session
        L40  def revoke_all(user_id)
    """
    try:
        path = _resolve_in_workspace(file_path)
    except ValueError as e:
        return f"Error: {e}. Pass a path inside the workspace root ({WORKSPACE_ROOT})."
    if not path.exists():
        return (f"Error: '{file_path}' not found under {WORKSPACE_ROOT}. "
                f"Double-check the relative path.")
    if path.is_dir():
        return f"Error: '{file_path}' is a directory, not a file. Pass a single source file."
    if _is_blacklisted(path):
        return f"Error: '{file_path}' lives in a blacklisted directory and is not mapped."
    try:
        return _build_blueprint(path)
    except Exception as e:  # last-resort guard: never break the stdio stream
        return f"Error mapping '{file_path}': {type(e).__name__}: {e}. Try a smaller file or a line range."


@mcp.tool()
def verify_symbol_existence(symbol_name: str, max_results: int = 25) -> str:
    """Confirm whether a symbol (class / function / method / struct / etc.) is
    actually DEFINED anywhere in the workspace, and report exactly where, with
    its signature.

    WHEN TO USE: any time you are about to assume a function or type exists -
    before you call it, import it, or claim in an answer that it's defined. This
    returns ground truth from the source tree, so prefer it over guessing or
    grepping raw files yourself.

    Args:
        symbol_name: the exact identifier to look for (case-sensitive).
        max_results: cap on matches returned (default 25).

    Returns 'FOUND' lines with file:line and signature, or a clear 'NOT FOUND'.
    """
    name = symbol_name.strip()
    if not name or not re.match(r"^\w[\w]*$", name):
        return "Error: pass a single bare identifier, e.g. 'createSession' (no parens, no dots)."
    try:
        raw_hits = _symbol_hits(name, max_results)
    except Exception as e:
        return f"Error during symbol scan: {type(e).__name__}: {e}."
    if not raw_hits:
        return (f"NOT FOUND: no definition of '{name}' in the workspace. "
                f"It may be undefined, external, or spelled differently.")
    out = "\n".join(f"FOUND  {r}:{l}  ->  {s}" for r, l, s in raw_hits)
    if len(raw_hits) >= max_results:
        out += f"\n... (truncated at {max_results}; refine the name to narrow down)"
    return out


@mcp.tool()
def scope_task(task: str, max_files: int = 5) -> str:
    """Map a plain-English task description to the specific files and symbols it
    touches — before reading any raw file contents.

    WHEN TO USE: call this as the FIRST step on any task that involves existing
    code. It extracts candidate symbol names from your description, searches the
    workspace for their definitions, and returns full structural blueprints for the
    most-matched files. This replaces open-ended codebase exploration with a
    single targeted call and tells you exactly which line ranges to read next.

    Args:
        task: plain-English description of what you want to do, e.g.
              "add retry logic to the live tutor session handler".
        max_files: how many files to include blueprints for (default 5).

    Returns a scoped context block: keywords searched, every matching symbol
    definition with file:line, and structural blueprints for the top files.
    After calling this, read only the specific line ranges you need.
    """
    keywords = _extract_scope_keywords(task)
    if not keywords:
        return (
            "No searchable terms found in task description. "
            "Try including class names (e.g. 'TutorSession') or function names."
        )

    file_hit_count: dict[str, int] = {}
    all_hits: list[tuple[str, str, str, str]] = []  # (keyword, rel, line, sig)

    # Regex to detect a definition line vs a usage (import/call)
    _def_line_pat = re.compile(
        r'(?:^|\s)(?:export\s+)?(?:default\s+)?(?:async\s+)?'
        r'(?:function|class|def|interface|type|enum|struct|impl)\s+\w|'
        r'(?:export\s+)?(?:const|let|var)\s+\w+\s*[=(]'
    )

    for kw in keywords:
        if not re.match(r"^\w[\w]*$", kw):
            continue
        try:
            hits = _symbol_hits(kw, max_results=10)
        except Exception:
            continue
        for rel, line, sig in hits:
            all_hits.append((kw, rel, line, sig))
            # Definition hits outrank usage hits so the source file surfaces first
            weight = 3 if _def_line_pat.search(sig) else 1
            file_hit_count[rel] = file_hit_count.get(rel, 0) + weight

    # Path-based supplement: finds files whose path contains task terms.
    # Fills gaps when no symbol exists by that name (e.g. "admin" → app/admin/page.tsx).
    path_kws = _extract_path_keywords(task)
    try:
        for src_path in _iter_source_files():
            rel = str(src_path.relative_to(WORKSPACE_ROOT))
            rel_norm = rel.lower().replace("-", " ").replace("/", " ").replace("_", " ")
            score = sum(1 for kw in path_kws if kw in rel_norm)
            if score > 0 and rel not in file_hit_count:
                file_hit_count[rel] = score
    except Exception:
        pass

    if not file_hit_count:
        return (
            f"No matches found for: {', '.join(keywords or path_kws)}.\n"
            "Try more specific terms — class names, function names, or file path segments."
        )

    # Definition files always surface first, then by total score
    def _file_rank(f: str) -> tuple:
        has_def = any(_def_line_pat.search(s) for _, r, _, s in all_hits if r == f)
        return (0 if has_def else 1, -file_hit_count[f])

    top_files = sorted(file_hit_count, key=_file_rank)[:max_files]
    top_set = set(top_files)

    parts: list[str] = [f"# Scope: {task!r}\n"]
    parts.append(f"Keywords searched: {', '.join(keywords)}\n")

    # Deduplicated symbol hits for top files
    relevant = [(r, l, s) for _, r, l, s in all_hits if r in top_set]
    seen_hits: set[tuple[str, str]] = set()
    unique = []
    for r, l, s in relevant:
        if (r, l) not in seen_hits:
            seen_hits.add((r, l))
            unique.append((r, l, s))

    if unique:
        parts.append("## Matched symbols\n")
        for r, l, s in unique:
            parts.append(f"  {r}:{l}  {s}")
        parts.append("")

    parts.append("## File blueprints\n")
    for rel in top_files:
        n = file_hit_count[rel]
        try:
            path = _resolve_in_workspace(rel)
            blueprint = _build_blueprint(path)
        except Exception as e:
            parts.append(f"### {rel}\n  (error reading: {e})\n")
            continue
        parts.append(f"### {rel}  ({n} {'match' if n == 1 else 'matches'})\n{blueprint}\n")

    return "\n".join(parts)


@mcp.tool()
def get_imports(file_path: str) -> str:
    """List every import in a source file and resolve workspace-local ones to actual paths.

    WHEN TO USE: after get_file_structure reveals an unfamiliar symbol, call this
    to find which file it comes from — without reading raw file contents. Resolves
    relative paths (./foo, ../lib/bar) and the @/ alias (Next.js root). Use the
    returned workspace paths as arguments to get_file_structure to inspect those
    files next.

    Args:
        file_path: path to the file, relative to the workspace root.

    Returns each import as [workspace] resolved/path.ts or [external] package-name,
    with the names being imported shown inline.
    """
    try:
        path = _resolve_in_workspace(file_path)
    except ValueError as e:
        return f"Error: {e}."
    if not path.exists():
        return f"Error: '{file_path}' not found under {WORKSPACE_ROOT}."
    if path.suffix not in EXT_LANG:
        return f"Error: '{file_path}' is not a recognised source file type."

    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        return f"Error reading file: {e}."

    entries = _parse_import_entries(path, text)
    if not entries:
        return f"No imports found in '{file_path}'."

    workspace_lines: list[str] = []
    external_lines: list[str] = []

    for spec, names in entries:
        kind, resolved = _resolve_import(spec, path)
        suffix = f"  ← {names}" if names else ""
        if kind == 'workspace':
            workspace_lines.append(f"  [workspace]  {resolved}{suffix}")
        elif kind == 'unresolved':
            workspace_lines.append(f"  [workspace?] {spec}  (not found on disk){suffix}")
        else:
            external_lines.append(f"  [external]   {resolved}{suffix}")

    rel = str(path.relative_to(WORKSPACE_ROOT))
    parts: list[str] = [f"# Imports: {rel}\n"]
    if workspace_lines:
        parts.append("## Workspace")
        parts.extend(workspace_lines)
        parts.append("")
    if external_lines:
        parts.append("## External")
        parts.extend(external_lines)
    parts.append(f"\n{len(workspace_lines)} workspace, {len(external_lines)} external")
    return "\n".join(parts)


@mcp.tool()
def execute_local_sandbox(language: str, code: str, timeout_seconds: int = 10) -> str:
    """Run a SHORT python or bash snippet locally and return its combined output.
    Intended for quick verification - run a test, check a value, list files,
    confirm a build command - not for long-running jobs.

    RESOURCE LIMITS (the safety this actually provides):
      - hard timeout (default 10s, ceiling enforced by the server);
      - the whole process group is killed on timeout, so hung children die too;
      - stdout/stderr is captured and truncated so it can't flood your context;
      - the working directory is pinned to the workspace root.

    NOT a security boundary: code runs as your user with your full permissions
    and network access. Only run snippets you would be willing to run yourself.

    Args:
        language: "python" or "bash".
        code: the snippet to execute.
        timeout_seconds: wall-clock limit; capped at the server's ceiling.
    """
    if not SANDBOX_ENABLED:
        return "Error: execute_local_sandbox is disabled (set MCP_ENABLE_SANDBOX=1 to allow it)."

    lang = language.strip().lower()
    if lang not in ("python", "bash"):
        return "Error: language must be 'python' or 'bash'."
    if lang == "bash" and os.name != "posix":
        return "Error: bash sandbox is not available on Windows. Use language='python' instead."
    if not code.strip():
        return "Error: no code provided."

    timeout = max(1, min(int(timeout_seconds or SANDBOX_TIMEOUT_CEIL), SANDBOX_TIMEOUT_CEIL))

    # Write to a temp file so multi-line snippets and quoting behave predictably.
    suffix = ".py" if lang == "python" else ".sh"
    tmp = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    try:
        tmp.write(code)
        tmp.close()
        if lang == "python":
            cmd = [sys.executable, "-I", tmp.name]  # -I = isolated, ignore env/site
        else:
            cmd = ["bash", tmp.name]

        # start_new_session=True puts the child in its own process group (POSIX)
        # so we can kill the entire group - not just the shell - on timeout.
        popen_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(WORKSPACE_ROOT), text=True,
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        else:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        proc = subprocess.Popen(cmd, **popen_kwargs)
        try:
            out, _ = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            out, _ = proc.communicate()
            return (f"Error: snippet exceeded the {timeout}s limit and was killed "
                    f"(process group terminated).\n--- partial output ---\n{_clip(out)}")
        status = "ok" if rc == 0 else f"exit={rc}"
        return f"[{lang} · {status}]\n{_clip(out) or '(no output)'}"
    except Exception as e:  # never break the stream
        return f"Error executing snippet: {type(e).__name__}: {e}."
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the child's entire process group so orphaned children don't linger."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _clip(text: str, limit: int = 6000) -> str:
    """Truncate output so a runaway print loop can't blow up the context."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, {len(text) - limit:,} more chars)"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def setup() -> None:
    """Console-script entry point: drop MCP config files into the current project."""
    import json

    cwd = Path.cwd()

    claude_cfg = cwd / ".mcp.json"
    if claude_cfg.exists():
        print(f"skipped  {claude_cfg}  (already exists)")
    else:
        claude_cfg.write_text(json.dumps({
            "mcpServers": {
                "mimir": {
                    "command": "mimir",
                    "env": {"MCP_WORKSPACE_ROOT": "."}
                }
            }
        }, indent=2) + "\n")
        print(f"created  {claude_cfg}")

    vscode_dir = cwd / ".vscode"
    vscode_dir.mkdir(exist_ok=True)
    copilot_cfg = vscode_dir / "mcp.json"
    if copilot_cfg.exists():
        print(f"skipped  {copilot_cfg}  (already exists)")
    else:
        copilot_cfg.write_text(json.dumps({
            "servers": {
                "mimir": {
                    "type": "stdio",
                    "command": "mimir",
                    "env": {"MCP_WORKSPACE_ROOT": "${workspaceFolder}"}
                }
            }
        }, indent=2) + "\n")
        print(f"created  {copilot_cfg}")

    print("\nDone. Restart Claude Code or reload VS Code to pick up the config.")


def main() -> None:
    """Console-script / module entry point. Starts the stdio MCP server.

    This is what the `mimir` command (declared in pyproject.toml under
    [project.scripts]) calls, and it's also what runs on `python mcp_optimized_mapper.py`.
    """
    # A short banner on stderr (NOT stdout - stdout is the JSON-RPC channel).
    print(
        f"[mimir] root={WORKSPACE_ROOT} "
        f"tree_sitter={'on' if TREE_SITTER_OK else 'off (regex fallback)'} "
        f"sandbox={'on' if SANDBOX_ENABLED else 'off'}",
        file=sys.stderr,
    )
    mcp.run()  # defaults to stdio transport


if __name__ == "__main__":
    main()
