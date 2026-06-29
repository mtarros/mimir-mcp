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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Optional file watcher — graceful fallback if watchdog is not installed
_WATCHER_OK = False
try:
    from watchdog.observers import Observer as _WatchdogObserver          # type: ignore
    from watchdog.events import FileSystemEventHandler as _WatchdogHandler # type: ignore
    _WATCHER_OK = True
except Exception:
    _WatchdogObserver = None   # type: ignore
    _WatchdogHandler  = None   # type: ignore


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

def _load_mimirignore() -> list[str]:
    """Read glob patterns from .mimirignore in the workspace root (gitignore-style)."""
    p = WORKSPACE_ROOT / ".mimirignore"
    if not p.exists():
        return []
    patterns = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns

_MIMIRIGNORE_PATTERNS: list[str] = _load_mimirignore()


def _load_mimiraliases() -> dict[str, list[str]]:
    """Read domain→code mappings from .mimiraliases in the workspace root.

    File format (one mapping per line):
        corrective actions = RectificationFilter
        live tutor = LiveTutor, GeminiLive
        # comments are ignored
    Returns a dict of lowercased domain phrase → list of code name strings.
    """
    p = WORKSPACE_ROOT / ".mimiraliases"
    if not p.exists():
        return {}
    aliases: dict[str, list[str]] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        domain, _, codes = line.partition("=")
        domain = domain.strip().lower()
        code_names = [c.strip() for c in codes.split(",") if c.strip()]
        if domain and code_names:
            aliases[domain] = code_names
    return aliases


_MIMIRALIASES: dict[str, list[str]] = _load_mimiraliases()


def _expand_task_with_aliases(task: str) -> str:
    """Append alias code names to a task string when domain phrases match.

    Checks every alias domain phrase (longest first to prefer specific matches)
    against the lowercased task. Appends matched code names so that
    scope_task keyword extraction picks them up alongside the original terms.
    """
    if not _MIMIRALIASES:
        return task
    task_lower = task.lower()
    additions: list[str] = []
    for domain in sorted(_MIMIRALIASES, key=len, reverse=True):
        if domain in task_lower:
            additions.extend(_MIMIRALIASES[domain])
    if additions:
        return task + " " + " ".join(additions)
    return task



# Tokens that appear on almost every blueprint line but are never searched as
# symbol names. Filtering these cuts ~40% of symbol index rows.
_SYMBOL_STOPWORDS = frozenset({
    # Visibility / access modifiers
    "public", "private", "protected", "internal", "extern",
    # Storage / mutability
    "static", "readonly", "const", "volatile", "transient",
    # Inheritance / polymorphism
    "abstract", "virtual", "override", "sealed", "final",
    # Type modifiers
    "partial", "async", "await", "synchronized", "native",
    # Structural keywords
    "class", "interface", "struct", "enum", "namespace", "package",
    # Imports
    "using", "import", "require",
    # Primitive / built-in types
    "void", "bool", "boolean", "int", "uint", "long", "ulong",
    "short", "ushort", "byte", "sbyte", "char", "float", "double",
    "decimal", "string", "str", "object", "var", "dynamic", "any",
    # Python-specific
    "def", "self", "cls", "pass",
    # JS/TS-specific
    "export", "default", "function", "let", "extends",
    "implements", "typeof", "instanceof",
    # Go-specific
    "func",
    # Literal values
    "null", "true", "false", "undefined", "nil", "None", "True", "False",
    # Object-orientation keywords
    "new", "this", "base", "super",
})


# Increment when the blueprint text format changes so cached blueprints are
# automatically invalidated and re-parsed on the next server start.
BLUEPRINT_VERSION = "3"


def _init_disk_cache() -> "Optional[object]":
    """Open (or create) a per-workspace SQLite blueprint cache in ~/.cache/mimir/."""
    try:
        import sqlite3, hashlib
        ws_hash = hashlib.sha256(str(WORKSPACE_ROOT).encode()).hexdigest()[:16]
        cache_dir = Path.home() / ".cache" / "mimir"
        cache_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(cache_dir / f"{ws_hash}.db"), check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA cache_size=-32768")   # 32MB page cache
        db.execute(
            "CREATE TABLE IF NOT EXISTS blueprints"
            " (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, blueprint TEXT)"
        )
        # Normalized symbol index: lines holds context (one row per definition line),
        # symbols holds tokens (one row per token — no context duplication).
        # Migrate from the old denormalized schema if needed.
        try:
            db.execute("SELECT context FROM symbols LIMIT 0")
            # Old schema with context column present — rebuild both tables.
            db.execute("DROP TABLE symbols")
            db.execute("DROP TABLE IF EXISTS lines")
        except Exception:
            pass
        db.execute(
            "CREATE TABLE IF NOT EXISTS lines"
            " (file TEXT NOT NULL, lineno TEXT NOT NULL, context TEXT NOT NULL,"
            "  PRIMARY KEY (file, lineno))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS symbols"
            " (token TEXT NOT NULL, file TEXT NOT NULL, lineno TEXT NOT NULL)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_symbols_token ON symbols (token)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        # Invalidate all cached blueprints when the format version changes.
        stored = db.execute(
            "SELECT value FROM meta WHERE key='blueprint_version'"
        ).fetchone()
        if stored is None or stored[0] != BLUEPRINT_VERSION:
            db.execute("DELETE FROM blueprints")
            db.execute("DELETE FROM lines")
            db.execute("DELETE FROM symbols")
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('blueprint_version', ?)",
                (BLUEPRINT_VERSION,)
            )
        db.commit()
        return db
    except Exception:
        return None


_DISK_CACHE = _init_disk_cache()
_FTS_READY = False       # True once the symbols inverted index is built and queryable
_WARMUP_COMPLETE = False  # True once _warm_cache() finishes its first full pass
_REVERSE_IMPORTS: dict[str, list[str]] = {}  # rel_target → sorted list of rel files that import it
_FILE_WATCHER_ACTIVE = False  # True if a watchdog observer started successfully
_ARCHITECTURE_MAP: str = ""   # cached architecture overview; built once in _warm_cache


def _load_disk_cache() -> int:
    """Populate the in-memory cache from SQLite. Returns number of valid entries loaded."""
    global _FTS_READY
    if _DISK_CACHE is None:
        return 0
    loaded = 0
    try:
        for path_str, mtime, size, blueprint in _DISK_CACHE.execute(
            "SELECT path, mtime, size, blueprint FROM blueprints"
        ):
            try:
                st = Path(path_str).stat()
                if st.st_mtime == mtime and st.st_size == size:
                    _CACHE[path_str] = (mtime, size, blueprint)
                    _CACHE.move_to_end(path_str)
                    loaded += 1
            except OSError:
                pass
    except Exception:
        pass
    try:
        count = _DISK_CACHE.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        if count > 0:
            _FTS_READY = True
    except Exception:
        pass
    return loaded


# Filename suffixes that indicate machine-generated files — never useful to read or search.
_GENERATED_SUFFIXES = (".g.cs", ".generated.cs", ".g.dart", ".pb.go", ".pb.swift")
_GENERATED_NAMES = frozenset({"AssemblyInfo.cs"})

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
    ".vue":  ("vue",        "cstyle"),
    ".xml":  (None,         "xml"),   # custom extractor; not tree-sitter
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
    "import_declaration",   # Java/C# using/import lines are not definitions
    "using_directive",      # C# `using Foo.Bar;`
    "import_specifier",     # JS/TS named import items: `import { Foo } from '...'`
    "namespace_import",     # JS/TS `import * as foo from '...'`
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
_CACHE_MAX = 8192  # raised from 2048 — blueprints are ~2KB each so 8192 ≈ 16MB


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
    if _DISK_CACHE is not None:
        try:
            rel = str(path.relative_to(WORKSPACE_ROOT))
            _DISK_CACHE.execute(
                "INSERT OR REPLACE INTO blueprints VALUES (?,?,?,?)",
                (str(path), st.st_mtime, st.st_size, blueprint),
            )
            if _FTS_READY:
                line_rows = _extract_blueprint_lines(rel, blueprint)
                sym_rows  = _index_blueprint_rows(rel, blueprint)
                _DISK_CACHE.execute("DELETE FROM symbols WHERE file = ?", (rel,))
                _DISK_CACHE.execute("DELETE FROM lines WHERE file = ?", (rel,))
                if line_rows:
                    _DISK_CACHE.executemany("INSERT OR IGNORE INTO lines VALUES (?,?,?)", line_rows)
                if sym_rows:
                    _DISK_CACHE.executemany("INSERT OR IGNORE INTO symbols VALUES (?,?,?)", sym_rows)
            _DISK_CACHE.commit()
        except Exception:
            pass


def _cache_evict(path: Path) -> None:
    """Remove a single file's blueprint from memory and disk caches.

    Called by the file watcher on modification/deletion so the next access
    rebuilds a fresh blueprint rather than returning stale structure.
    """
    global _FILE_LIST, _FILE_LIST_TS
    _CACHE.pop(str(path), None)
    if _DISK_CACHE is not None:
        try:
            rel = str(path.relative_to(WORKSPACE_ROOT))
            _DISK_CACHE.execute("DELETE FROM blueprints WHERE path = ?", (str(path),))
            _DISK_CACHE.execute("DELETE FROM symbols WHERE file = ?", (rel,))
            _DISK_CACHE.execute("DELETE FROM lines WHERE file = ?", (rel,))
            _DISK_CACHE.commit()
        except Exception:
            pass
    # Force a file-list re-walk so new/deleted files are picked up immediately
    _FILE_LIST = []
    _FILE_LIST_TS = 0.0


# --------------------------------------------------------------------------- #
# Path helpers / safety
# --------------------------------------------------------------------------- #
def _is_blacklisted(path: Path) -> bool:
    if any(part in BLACKLIST_DIRS for part in path.parts):
        return True
    for pat in _MIMIRIGNORE_PATTERNS:
        # Path.match supports ** for recursive matching
        if path.match(pat):
            return True
    return False


def _resolve_in_workspace(rel_path: str) -> Path:
    """Resolve a user-supplied relative path and refuse to escape the root."""
    candidate = (WORKSPACE_ROOT / rel_path).resolve()
    # Reject path traversal (e.g. ../../etc/passwd).
    # is_relative_to() is case-aware on Windows and avoids the startswith prefix bug.
    if not candidate.is_relative_to(WORKSPACE_ROOT):
        raise ValueError("path escapes the configured workspace root")
    return candidate


_FILE_LIST: list[Path] = []
_FILE_LIST_TS: float = 0.0
_FILE_LIST_TTL: float = 30.0  # seconds between re-walks


def _iter_source_files() -> list[Path]:
    """Return the list of indexable source files, re-walking at most every 30 s."""
    global _FILE_LIST, _FILE_LIST_TS
    now = time.monotonic()
    if _FILE_LIST and now - _FILE_LIST_TS < _FILE_LIST_TTL:
        return _FILE_LIST
    result: list[Path] = []
    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if d not in BLACKLIST_DIRS]
        for name in files:
            if Path(name).suffix not in EXT_LANG:
                continue
            if name in _GENERATED_NAMES or any(name.endswith(s) for s in _GENERATED_SUFFIXES):
                continue
            p = Path(root) / name
            if not _is_blacklisted(p):
                result.append(p)
    _FILE_LIST = result
    _FILE_LIST_TS = now
    # Auto-scale cache so it always fits the whole workspace — prevents thrashing.
    global _CACHE_MAX
    if len(result) > _CACHE_MAX:
        _CACHE_MAX = len(result) + 256
    return result


def _build_reverse_imports() -> None:
    """Build _REVERSE_IMPORTS: for each workspace file, which other files import it.

    Only covers languages where _resolve_import can produce 'workspace' hits
    (TypeScript/JS and Python). Called from _warm_cache and incrementally
    when the file watcher detects a change.
    """
    global _REVERSE_IMPORTS
    rev: dict[str, set[str]] = {}
    for src_path in _iter_source_files():
        try:
            text = src_path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        entries = _parse_import_entries(src_path, text)
        if not entries:
            continue
        rel_src = str(src_path.relative_to(WORKSPACE_ROOT))
        for spec, _ in entries:
            try:
                kind, resolved = _resolve_import(spec, src_path)
            except Exception:
                continue
            if kind == 'workspace':
                rev.setdefault(resolved, set()).add(rel_src)
    _REVERSE_IMPORTS = {k: sorted(v) for k, v in rev.items()}


# ---------------------------------------------------------------------------
# Architecture map
# ---------------------------------------------------------------------------

_BLUEPRINT_TOPLEVEL_KWS = (
    'export default ', 'export ', 'async ', 'public ', 'private ',
    'protected ', 'static ', 'abstract ', 'override ', 'sealed ',
    'class ', 'def ', 'function* ', 'function ', 'const ', 'let ',
    'var ', 'type ', 'interface ', 'struct ', 'enum ', 'impl ',
)

# Names that look like symbols but are actually declarations/keywords (Java
# package, C# namespace/internal, import statements). Filter them from the
# architecture map so they don't pollute the symbol list.
_ARCH_EXCLUDE_NAMES = frozenset({
    'package', 'namespace', 'import', 'using', 'internal', 'extern',
    'module', 'require', 'include', 'pragma', 'partial',
})

# Directories with more files than this are summarised as a symbol list rather
# than a file-by-file listing — keeps large Java/C# modules to one compact line.
_ARCH_LARGE_DIR_THRESHOLD = 12

# Max directory sections shown. Dirs are ranked by file count so the most
# populated (most important) modules always appear first.
_ARCH_MAX_DIRS = 60


def _toplevel_names_from_blueprint(blueprint: str) -> list[str]:
    """Return top-level symbol names from a blueprint string (depth-0 only)."""
    names: list[str] = []
    for line in blueprint.splitlines():
        # Blueprint lines: L{num:<5}{indent}{sig} — top-level has no indent after the
        # 6-char prefix (L + 5-char left-aligned number field).
        if not line.startswith('L') or len(line) <= 6:
            continue
        if line[6] == ' ':   # indent present → nested symbol, skip
            continue
        sig = line[6:].strip()
        for kw in _BLUEPRINT_TOPLEVEL_KWS:
            if sig.startswith(kw):
                sig = sig[len(kw):]
        m = re.match(r'([A-Za-z_]\w*)', sig)
        if m and m.group(1) not in _ARCH_EXCLUDE_NAMES:
            names.append(m.group(1))
    return names


def _build_architecture_map() -> str:
    """Build a compact directory → file → symbols overview of the workspace.

    Small directories (≤ _ARCH_LARGE_DIR_THRESHOLD files) are shown file-by-file.
    Large directories are summarised as a single line of aggregated unique symbol
    names — keeps Java/C# modules with 100+ files from blowing up the output.

    Sections are ranked by file count so the most populated (most important)
    modules appear first. Caps at _ARCH_MAX_DIRS total sections.
    """
    global _ARCHITECTURE_MAP
    dir_files: dict[str, list[Path]] = {}
    for p in _iter_source_files():
        key = str(p.parent.relative_to(WORKSPACE_ROOT))
        dir_files.setdefault(key, []).append(p)

    # Sort by descending file count so high-value modules come first
    ranked = sorted(dir_files.items(), key=lambda kv: -len(kv[1]))
    total_dirs = len(ranked)
    shown = ranked[:_ARCH_MAX_DIRS]
    omitted = total_dirs - len(shown)

    parts: list[str] = [f"# Workspace architecture: {WORKSPACE_ROOT.name}\n"]
    if omitted > 0:
        parts.append(
            f"(showing top {_ARCH_MAX_DIRS} of {total_dirs} directories by file count — "
            f"add patterns to .mimirignore to exclude tests/build dirs)\n"
        )

    for dir_key, files in shown:
        files = sorted(files)
        label = dir_key if dir_key != '.' else '(root)'
        n = len(files)
        parts.append(f"## {label}/  ({n} {'file' if n == 1 else 'files'})")

        if n > _ARCH_LARGE_DIR_THRESHOLD:
            # Large directory: collect all unique top-level symbol names, show compactly
            seen: set[str] = set()
            all_names: list[str] = []
            for fp in files:
                for name in _toplevel_names_from_blueprint(_build_blueprint(fp)):
                    if name not in seen:
                        seen.add(name)
                        all_names.append(name)
            sym_str = ', '.join(all_names[:16]) + ('…' if len(all_names) > 16 else '')
            parts.append(f"  {sym_str or '(no top-level symbols extracted)'}")
        else:
            # Small directory: show file-by-file
            for fp in files:
                names = _toplevel_names_from_blueprint(_build_blueprint(fp))
                name_str = ', '.join(names[:5]) + ('…' if len(names) > 5 else '')
                parts.append(f"  {fp.name:<40}  {name_str}")

        parts.append('')

    if omitted > 0:
        parts.append(
            f"... {omitted} more {'directory' if omitted == 1 else 'directories'} omitted. "
            f"Use get_directory_structure for a specific path."
        )

    result = '\n'.join(parts).rstrip()
    _ARCHITECTURE_MAP = result
    return result


# ---------------------------------------------------------------------------
# Symbol body extraction (used by get_symbol)
# ---------------------------------------------------------------------------

def _extract_symbol_body(path: Path, symbol_name: str) -> Optional[str]:
    """Return the full source text of a named symbol (function/class/method).

    Tries tree-sitter first; falls back to indentation/brace heuristics. Returns
    None when the symbol is not found in the file.
    """
    try:
        raw = path.read_bytes()
        text = raw.decode('utf-8', 'replace')
    except OSError:
        return None

    suffix = path.suffix
    ts_lang, _ = EXT_LANG.get(suffix, (None, None))
    pat = re.compile(r'\b' + re.escape(symbol_name) + r'\b')

    # --- Tree-sitter path ---
    if TREE_SITTER_OK and ts_lang:
        try:
            parser = _ts_get_parser(ts_lang)  # type: ignore[misc]
            tree = parser.parse(text)

            def _find(node) -> Optional[object]:
                if _is_def_node(node):
                    sig = _signature_from_node(node, raw)
                    if sig and pat.search(sig):
                        return node
                for i in range(node.child_count()):
                    result = _find(node.child(i))
                    if result is not None:
                        return result
                return None

            node = _find(tree.root_node())
            if node is not None:
                return raw[node.start_byte():node.end_byte()].decode('utf-8', 'replace')
        except Exception:
            pass

    # --- Indentation/brace fallback (Python + simple languages) ---
    lines = text.splitlines(keepends=True)
    start_idx: Optional[int] = None
    base_indent = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if pat.search(stripped):
            # Confirm it looks like a definition, not a call or comment
            if re.match(
                r'(?:(?:async|public|private|protected|static|abstract|sealed)\s+)*'
                r'(?:class|def|function\*?|const|let|var|interface|type|struct|enum|impl)\s',
                stripped,
            ) or re.match(r'\w[\w<>[\]]*\s*\(', stripped):
                start_idx = i
                base_indent = len(line) - len(stripped)
                break

    if start_idx is None:
        return None

    # Collect body by indentation (Python) or brace counting (C-style)
    brace_count = lines[start_idx].count('{') - lines[start_idx].count('}')
    use_braces = brace_count > 0 or suffix in ('.ts', '.tsx', '.js', '.jsx', '.java', '.kt', '.swift', '.cs', '.go', '.rs')

    end_idx = len(lines) - 1
    if use_braces:
        for i in range(start_idx + 1, len(lines)):
            brace_count += lines[i].count('{') - lines[i].count('}')
            if brace_count <= 0:
                end_idx = i
                break
    else:
        for i in range(start_idx + 1, len(lines)):
            line = lines[i]
            if line.strip() == '':
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= base_indent:
                end_idx = i - 1
                while end_idx > start_idx and lines[end_idx].strip() == '':
                    end_idx -= 1
                break

    return ''.join(lines[start_idx:end_idx + 1])


def _symbol_name_from_sig(sig: str) -> Optional[str]:
    """Extract bare symbol name from a blueprint signature line."""
    sig = sig.strip()
    for kw in _BLUEPRINT_TOPLEVEL_KWS:
        if sig.startswith(kw):
            sig = sig[len(kw):]
    m = re.match(r'([A-Za-z_]\w*)', sig)
    return m.group(1) if m else None


def _start_file_watcher() -> bool:
    """Start a background watchdog observer to evict stale blueprints instantly.

    Returns True if the watcher started, False if watchdog is not installed
    or failed to start (falls back to the existing 30-second mtime re-walk).
    """
    global _FILE_WATCHER_ACTIVE
    if not _WATCHER_OK:
        return False

    class _Handler(_WatchdogHandler):  # type: ignore[misc]
        def dispatch(self, event) -> None:
            if event.is_directory:
                # Directory added/removed — force a file-list re-walk
                global _FILE_LIST, _FILE_LIST_TS
                _FILE_LIST = []
                _FILE_LIST_TS = 0.0
                return
            p = Path(getattr(event, 'dest_path', event.src_path))
            if p.suffix in EXT_LANG and not _is_blacklisted(p):
                _cache_evict(p)
                # Incrementally refresh reverse-import index in the background
                import threading
                threading.Thread(
                    target=_build_reverse_imports, daemon=True,
                    name="mimir-reimports"
                ).start()

    try:
        obs = _WatchdogObserver()
        obs.schedule(_Handler(), str(WORKSPACE_ROOT), recursive=True)
        obs.daemon = True
        obs.start()
        _FILE_WATCHER_ACTIVE = True
        return True
    except Exception:
        return False


def _warm_cache() -> None:
    """Parse all source files in parallel so the first search is fast."""
    global _WARMUP_COMPLETE
    files = _iter_source_files()
    workers = min(8, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in as_completed(ex.submit(_build_blueprint, p) for p in files):
            pass
    _build_cs_ns_index()
    _build_symbol_index()
    _build_reverse_imports()
    _build_architecture_map()
    _WARMUP_COMPLETE = True


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


_XML_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)
_XML_PI_RE      = re.compile(r'<\?.*?\?>', re.DOTALL)
_XML_TAG_RE     = re.compile(r'<(/?)(\w[\w:.-]*)([^>]*?)(/?)>', re.DOTALL)

# Tags that add no semantic value to the blueprint
_XML_SKIP_TAGS = frozenset({
    'resources', 'merge', 'data', 'import', 'variable', 'layout',
    'declare-styleable', 'eat-comment',
})

def _extract_xml_blueprint(path: Path, text: str) -> str:
    """Extract a structural blueprint from XML files.

    Handles Android layout XML (view hierarchy with @id attributes) and
    Android resource XML (strings, colors, dimensions). Produces one line
    per meaningful element with key attributes shown inline.
    """
    # Strip comments and processing instructions before scanning tags
    clean = _XML_COMMENT_RE.sub('', text)
    clean = _XML_PI_RE.sub('', clean)

    # Build a map from character offset → line number for the ORIGINAL text
    # (use original so reported line numbers match the actual file)
    line_starts: list[int] = [0]
    for ch in text:
        if ch == '\n':
            line_starts.append(line_starts[-1] + 1)
        else:
            line_starts[-1] += 1
    # Rebuild as cumulative offsets
    cumulative = [0]
    for raw_line in text.splitlines():
        cumulative.append(cumulative[-1] + len(raw_line) + 1)

    def _lineno(pos: int) -> int:
        lo, hi = 0, len(cumulative) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if cumulative[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    out_lines: list[str] = []
    depth = 0

    for m in _XML_TAG_RE.finditer(clean):
        is_close    = m.group(1) == '/'
        tag_raw     = m.group(2)
        attrs       = m.group(3)
        is_self_close = m.group(4) == '/'
        tag = tag_raw.split(':')[-1]   # strip namespace prefix (e.g. android:, app:)

        if is_close:
            depth = max(0, depth - 1)
            continue

        if tag in _XML_SKIP_TAGS:
            if not is_self_close:
                depth += 1
            continue

        lineno = _lineno(m.start())
        indent = '  ' * min(depth, 8)

        # Pull out the attributes we care about (in priority order)
        android_id  = re.search(r'android:id="@\+?id/(\w+)"',    attrs)
        name_attr   = re.search(r'\bname="([^"]+)"',              attrs)
        text_attr   = re.search(r'\btext="([^"@][^"]*)"',         attrs)
        hint_attr   = re.search(r'android:hint="([^"@][^"]*)"',   attrs)
        style_attr  = re.search(r'\bstyle="([^"]+)"',             attrs)

        parts: list[str] = [tag]
        if android_id:
            parts.append(f'@id/{android_id.group(1)}')
        elif name_attr:
            parts.append(f'"{name_attr.group(1)}"')
        if text_attr and len(text_attr.group(1)) <= 50:
            parts.append(f'text="{text_attr.group(1)}"')
        elif hint_attr and len(hint_attr.group(1)) <= 50:
            parts.append(f'hint="{hint_attr.group(1)}"')

        # String resource: grab the inline value between tags
        if tag == 'string' and name_attr:
            after = clean[m.end():m.end() + 120]
            val_m = re.match(r'\s*>?\s*([^<]{1,80})\s*<', after)
            if val_m:
                val = val_m.group(1).strip()
                if val:
                    parts.append(f'= "{val[:60]}"')

        out_lines.append(f"L{lineno}  {indent}{' '.join(parts)}")

        if not is_self_close:
            depth += 1

    return '\n'.join(out_lines)


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
    seen_line_nos: set[int] = set()

    # Signatures that are just bare keywords have no name to reference — skip them
    _anon = re.compile(r'^(?:async\s+)?(?:function|class|interface|enum)\s*[<({]?$')

    def walk(node, depth: int) -> None:
        child_depth = depth
        if _is_def_node(node):
            sig = _signature_from_node(node, src)
            if sig and not _anon.match(sig):
                line_no = node.start_position().row + 1
                if line_no not in seen_line_nos:
                    seen_line_nos.add(line_no)
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

    # Skip minified files: if average line length > 500 bytes the file is almost
    # certainly a bundled/minified artifact — parsing it produces a useless
    # single-line blob and pollutes search results.
    if size > 1000:
        newlines = raw.count(b"\n") or 1
        if size / newlines > 500:
            blueprint = f"# {rel}  [skipped: minified/bundled file]"
            _cache_put(path, blueprint)
            return blueprint

    # XML files use a dedicated extractor (not tree-sitter/regex)
    if suffix == '.xml':
        text = raw.decode('utf-8', 'replace')
        body = _extract_xml_blueprint(path, text)
        line_count = raw.count(b'\n') + 1
        header = f"# {rel}  [xml · {line_count} lines]"
        blueprint = header + '\n' + (body if body.strip() else '  (no structured elements found)')
        _cache_put(path, blueprint)
        return blueprint

    # Vue SFCs store script content as opaque raw_text in the Vue AST.
    # Extract the <script> block and reparse it as TypeScript so the normal
    # AST walker finds real definitions.
    parse_raw, parse_lang = raw, ts_lang
    if suffix == ".vue":
        m = re.search(
            rb'<script(?:\s[^>]*)?>[ \t]*\n?(.*?)</script>',
            raw, re.DOTALL | re.IGNORECASE,
        )
        if m:
            parse_raw = m.group(1)
            parse_lang = "typescript"
        else:
            parse_lang = None  # no <script> block — fall through to regex

    engine = "regex"
    body: Optional[str] = None
    if parse_lang:
        body = _extract_tree_sitter(path, parse_raw, parse_lang)
        if body is not None:
            engine = "tree-sitter"
    if body is None:
        body = _extract_regex(raw.decode("utf-8", "replace"), profile)

    line_count = raw.count(b"\n") + 1
    header = f"# {rel}  [{suffix.lstrip('.') or '?'} · {engine} · {line_count} lines]"
    blueprint = header + "\n" + (body if body.strip() else "  (no top-level symbols found)")
    _cache_put(path, blueprint)
    return blueprint


# --------------------------------------------------------------------------- #
# Symbol search helpers (shared by verify_symbol_existence and scope_task)
# --------------------------------------------------------------------------- #

def _symbol_hits(name: str, max_results: int = 25) -> list[tuple[str, str, str]]:
    """Search blueprints for definitions of *name*. Returns (rel_path, line_no, sig) tuples."""
    if _FTS_READY and _DISK_CACHE is not None:
        try:
            word_re = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
            rows = _DISK_CACHE.execute(
                "SELECT l.file, l.lineno, l.context"
                " FROM symbols s JOIN lines l ON s.file=l.file AND s.lineno=l.lineno"
                " WHERE s.token=? LIMIT ?",
                (name, max_results * 4),
            ).fetchall()
            hits = [(f, l, c) for f, l, c in rows if word_re.search(c)]
            return hits[:max_results]
        except Exception:
            pass  # fall through to linear scan

    word_def = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
    hits: list[tuple[str, str, str]] = []
    for path in _iter_source_files():
        if len(hits) >= max_results:
            break
        cached = _cache_get(path)
        if cached is not None:
            if name not in cached:
                continue
            blueprint = cached
        else:
            try:
                if name.encode() not in path.read_bytes():
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


def _symbol_hits_multi(
    names: list[str], max_per_kw: int = 10
) -> dict[str, list[tuple[str, str, str]]]:
    """Multi-keyword search across blueprints.

    Uses the SQLite inverted index when available (fast path), otherwise
    falls back to a single-pass linear scan through the blueprint cache.
    Returns {keyword: [(rel_path, line_no, sig), ...]} for every keyword.
    """
    if _FTS_READY and _DISK_CACHE is not None:
        try:
            hits: dict[str, list[tuple[str, str, str]]] = {n: [] for n in names}
            for name in names:
                word_re = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
                rows = _DISK_CACHE.execute(
                    "SELECT l.file, l.lineno, l.context"
                    " FROM symbols s JOIN lines l ON s.file=l.file AND s.lineno=l.lineno"
                    " WHERE s.token=? LIMIT ?",
                    (name, max_per_kw * 4),
                ).fetchall()
                for f, l, c in rows:
                    if word_re.search(c):
                        hits[name].append((f, l, c))
                        if len(hits[name]) >= max_per_kw:
                            break
            return hits
        except Exception:
            pass  # fall through to linear scan

    patterns = {n: re.compile(rf"(?<![\w]){re.escape(n)}(?![\w])") for n in names}
    hits = {n: [] for n in names}
    saturated: set[str] = set()

    for path in _iter_source_files():
        if len(saturated) == len(names):
            break
        cached = _cache_get(path)
        blueprint = cached if cached is not None else _build_blueprint(path)
        rel = str(path.relative_to(WORKSPACE_ROOT))

        for name in names:
            if name in saturated or name not in blueprint:
                continue
            pat = patterns[name]
            for bl in blueprint.splitlines():
                if bl.startswith("#") or not pat.search(bl):
                    continue
                m = re.match(r"L(\d+)\s*(.*)", bl.strip())
                if m:
                    hits[name].append((rel, m.group(1), m.group(2).strip()))
                    if len(hits[name]) >= max_per_kw:
                        saturated.add(name)
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
    """Looser keyword set for path matching — includes short words, fewer stopwords.

    Also splits CamelCase query terms into sub-components so that e.g.
    'RectificationFilterVC' generates 'rectification' + 'filter' as path keywords,
    enabling substring-matching against 'RectificationFilterDialogFragment'.
    """
    _path_stops = frozenset({
        "the", "and", "for", "with", "how", "add", "fix", "new", "get", "set",
        "use", "make", "do", "in", "on", "at", "to", "of", "by", "or", "a",
    })
    words = re.findall(r'[a-zA-Z]{3,}', task.lower())
    # Split CamelCase terms into sub-components (min 4 chars to avoid noise)
    for camel in re.findall(r'\b[A-Z][a-zA-Z]{3,}\b', task):
        for part in re.findall(r'[A-Z][a-z0-9]+', camel):
            if len(part) >= 4:
                words.append(part.lower())
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w not in seen and w not in _path_stops:
            seen.add(w)
            out.append(w)
    return out[:20]


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
_TS_ALIAS_CACHE: "dict[str, dict[str, Path]]" = {}  # tsconfig path → {alias_prefix → base_dir}


def _find_ts_aliases(source_file: Path) -> "dict[str, Path]":
    """Walk up from source_file looking for tsconfig.json with compilerOptions.paths."""
    import json as _json
    for parent in (source_file.parent, *source_file.parents):
        cfg = parent / "tsconfig.json"
        key = str(cfg)
        if key in _TS_ALIAS_CACHE:
            return _TS_ALIAS_CACHE[key]
        if cfg.is_file():
            try:
                data = _json.loads(cfg.read_text(encoding="utf-8", errors="replace"))
                paths = data.get("compilerOptions", {}).get("paths", {})
                result: dict[str, Path] = {}
                for alias, targets in paths.items():
                    if alias.endswith("/*") and targets:
                        prefix = alias[:-1]          # "@/*" → "@/"
                        target = targets[0].rstrip("/*").rstrip("/") or "."
                        result[prefix] = (parent / target).resolve()
                _TS_ALIAS_CACHE[key] = result
                return result
            except Exception:
                _TS_ALIAS_CACHE[key] = {}
                return {}
        if parent == WORKSPACE_ROOT or parent == parent.parent:
            break
    return {}


_CS_NS_INDEX: "dict[str, list[str]]" = {}   # namespace → [rel_path, ...]
_CS_NS_INDEX_READY = False


def _build_cs_ns_index() -> None:
    """Build a namespace→files index from already-cached C# blueprints.

    Called at the end of _warm_cache() so all blueprints are populated first.
    Uses the blueprint text (which already contains namespace declarations) so
    no extra file reads are needed.
    """
    global _CS_NS_INDEX, _CS_NS_INDEX_READY
    ns_re = re.compile(r'L\d+\s+namespace\s+([\w.]+)')
    index: dict[str, list[str]] = {}
    for path in _iter_source_files():
        if path.suffix != '.cs':
            continue
        cached = _cache_get(path)
        if cached is None:
            continue
        rel = str(path.relative_to(WORKSPACE_ROOT))
        for line in cached.splitlines():
            m = ns_re.match(line)
            if m:
                index.setdefault(m.group(1), []).append(rel)
                break  # one namespace declaration per file
    _CS_NS_INDEX = index
    _CS_NS_INDEX_READY = True


def _extract_blueprint_lines(rel: str, blueprint: str) -> list[tuple[str, str, str]]:
    """Extract (file, lineno, context) rows for the lines table from a blueprint.

    Deduplicates by lineno, keeping the first occurrence (the outermost/most
    descriptive node when tree-sitter emits multiple nodes at the same line).
    """
    rows = []
    seen: set[str] = set()
    for line in blueprint.splitlines():
        if not line or line.startswith('#'):
            continue
        m = re.match(r'L(\d+)\s*(.*)', line.strip())
        if not m:
            continue
        lineno = m.group(1)
        if lineno in seen:
            continue
        seen.add(lineno)
        context = m.group(2).strip()
        if context:
            rows.append((rel, lineno, context))
    return rows


def _index_blueprint_rows(rel: str, blueprint: str) -> list[tuple[str, str, str]]:
    """Extract (token, file, lineno) rows for the symbols table from a blueprint.

    Stopwords and tokens shorter than 2 chars are excluded to keep the index small.
    Context is stored separately in the lines table — no duplication here.
    """
    rows = []
    for line in blueprint.splitlines():
        if not line or line.startswith('#'):
            continue
        m = re.match(r'L(\d+)\s*(.*)', line.strip())
        if not m:
            continue
        lineno, context = m.group(1), m.group(2).strip()
        if not context:
            continue
        seen: set[str] = set()
        for tok in re.findall(r'\w+', context):
            if len(tok) >= 2 and tok not in _SYMBOL_STOPWORDS and tok not in seen:
                seen.add(tok)
                rows.append((tok, rel, lineno))
    return rows


def _build_symbol_index() -> None:
    """Build the normalized symbol index in SQLite from all cached blueprints.

    Called at the end of _warm_cache(). Skips rebuild if the index is already
    populated (persisted from a previous run). Incremental updates happen via
    _cache_put when individual files change.
    """
    global _FTS_READY
    if _DISK_CACHE is None:
        return
    try:
        indexed = _DISK_CACHE.execute(
            "SELECT COUNT(DISTINCT file) FROM symbols"
        ).fetchone()[0]
        total = len(_iter_source_files())
        if indexed > 0 and indexed >= total:
            _FTS_READY = True
            return
    except Exception:
        pass
    line_rows: list[tuple[str, str, str]] = []
    sym_rows:  list[tuple[str, str, str]] = []
    for path in _iter_source_files():
        cached = _cache_get(path)
        if cached is None:
            continue
        rel = str(path.relative_to(WORKSPACE_ROOT))
        line_rows.extend(_extract_blueprint_lines(rel, cached))
        sym_rows.extend(_index_blueprint_rows(rel, cached))
    try:
        _DISK_CACHE.execute("DELETE FROM lines")
        _DISK_CACHE.execute("DELETE FROM symbols")
        _DISK_CACHE.executemany("INSERT OR IGNORE INTO lines VALUES (?,?,?)", line_rows)
        _DISK_CACHE.executemany("INSERT OR IGNORE INTO symbols VALUES (?,?,?)", sym_rows)
        _DISK_CACHE.commit()
        _DISK_CACHE.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _FTS_READY = True
    except Exception:
        pass


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

        if specifier.startswith('@/') or any(specifier.startswith(p) for p in _find_ts_aliases(source_file)):
            # Try each alias defined in the nearest tsconfig.json paths config
            aliases = _find_ts_aliases(source_file)
            bases_to_try: list[Path] = []
            for prefix, base_dir in aliases.items():
                if specifier.startswith(prefix):
                    bases_to_try.append(base_dir / specifier[len(prefix):])
            # Fallback: @/ relative to workspace root
            if specifier.startswith('@/') and not bases_to_try:
                bases_to_try.append(WORKSPACE_ROOT / specifier[2:])
            for base in bases_to_try:
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
            rel = specifier[2:] if specifier.startswith('@/') else specifier
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
        # Absolute import: try to resolve against the workspace root
        mod_path = WORKSPACE_ROOT / specifier.replace('.', '/')
        for c in [mod_path.with_suffix('.py'), mod_path / '__init__.py']:
            if c.is_file():
                try:
                    return ('workspace', str(c.relative_to(WORKSPACE_ROOT)))
                except ValueError:
                    pass
        return ('external', specifier)

    if suffix in ('.cs', '.kt', '.kts', '.swift'):
        if suffix == '.cs' and _CS_NS_INDEX_READY:
            best: list[str] = []
            for ns, ns_files in _CS_NS_INDEX.items():
                if ns == specifier or specifier.startswith(ns + '.') or ns.startswith(specifier + '.'):
                    best.extend(ns_files)
            if best:
                best.sort(key=lambda f: abs(len(f) - len(specifier)))
                return ('workspace', best[0])
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
def get_file_structure(path: str) -> str:
    """Return a compact structural blueprint of ONE source file: only classes,
    functions, methods, structs, and their signatures - all bodies, loops, and
    implementation text stripped out.

    WHEN TO USE: call this BEFORE reading a source file's raw contents. It gives
    you the symbol map (with line numbers) at a fraction of the token cost, which
    is almost always enough to decide what to do next. Only read raw lines after
    this, and only the specific ranges you actually need.

    Args:
        path: path to the file, relative to the workspace root.

    Returns a dense text map like:
        # src/auth/session.py  [py · tree-sitter]
        L12  class SessionManager
        L18    def create(self, user_id) -> Session
        L40  def revoke_all(user_id)
    """
    try:
        resolved = _resolve_in_workspace(path)
    except ValueError as e:
        return f"Error: {e}. Pass a path inside the workspace root ({WORKSPACE_ROOT})."
    if not resolved.exists():
        return (f"Error: '{path}' not found under {WORKSPACE_ROOT}. "
                f"Double-check the relative path.")
    if resolved.is_dir():
        return f"Error: '{path}' is a directory, not a file. Pass a single source file."
    if _is_blacklisted(resolved):
        return f"Error: '{path}' lives in a blacklisted directory and is not mapped."
    try:
        return _build_blueprint(resolved)
    except Exception as e:  # last-resort guard: never break the stdio stream
        return f"Error mapping '{path}': {type(e).__name__}: {e}. Try a smaller file or a line range."


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
def scope_task(task: str, max_files: int = 5, include_blueprints: bool = False) -> str:
    """Map a plain-English task description to the specific files and symbols it
    touches — before reading any raw file contents.

    WHEN TO USE: call this as the FIRST step on any task that involves existing
    code. It extracts candidate symbol names from your description, searches the
    workspace for their definitions, and returns the ranked file list with matched
    symbols. Then call get_file_structure on the specific files you need.

    TIP: use technical/class names when known ("RectificationFilter") rather than
    domain language ("corrective actions filter") — the symbol index matches code
    names, not feature names.

    Args:
        task: plain-English description of what you want to do, e.g.
              "add retry logic to the live tutor session handler".
        max_files: how many files to rank and return (default 5).
        include_blueprints: set True to include full structural blueprints inline
                            (useful for small repos; may produce large output on
                            large repos — prefer calling get_file_structure after).

    Returns a compact context block: keywords searched, matched symbols with
    file:line locations, and ranked files by relevance score.
    """
    expanded = _expand_task_with_aliases(task)
    keywords = _extract_scope_keywords(expanded)
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

    valid_kws = [kw for kw in keywords if re.match(r"^\w[\w]*$", kw)]
    try:
        multi_hits = _symbol_hits_multi(valid_kws, max_per_kw=10)
    except Exception:
        multi_hits = {}
    for kw in valid_kws:
        for rel, line, sig in multi_hits.get(kw, []):
            all_hits.append((kw, rel, line, sig))
            weight = 3 if _def_line_pat.search(sig) else 1
            file_hit_count[rel] = file_hit_count.get(rel, 0) + weight

    # Path-based supplement: finds files whose path contains task terms.
    # Filename matches score 3x vs directory matches (score 1x) so that
    # "RectificationFilterDialogFragment.java" outranks generic files that merely
    # live in a directory named "filter". Also boosts files already found via
    # symbol search so cross-platform counterparts (iOS ↔ Android) surface together.
    path_kws = _extract_path_keywords(expanded)
    try:
        for src_path in _iter_source_files():
            rel = str(src_path.relative_to(WORKSPACE_ROOT))
            fname_norm = src_path.stem.lower()
            dir_norm = str(src_path.parent.relative_to(WORKSPACE_ROOT)).lower()
            score = sum(3 if kw in fname_norm else (1 if kw in dir_norm else 0)
                        for kw in path_kws)
            if score > 0:
                file_hit_count[rel] = file_hit_count.get(rel, 0) + score
    except Exception:
        pass

    if not file_hit_count:
        return (
            f"No matches found for: {', '.join(keywords or path_kws)}.\n"
            "Try more specific terms — class names, function names, or file path segments."
        )

    # Rank purely by total score — symbol hits already carry higher per-hit weight (3)
    # so they naturally outrank path-only matches without a hard binary split that
    # buries cross-platform counterparts (e.g. Java files alongside Swift files).
    def _file_rank(f: str) -> tuple:
        return (-file_hit_count[f],)

    top_files = sorted(file_hit_count, key=_file_rank)[:max_files]
    top_set = set(top_files)

    parts: list[str] = [f"# Scope: {task!r}\n"]
    if expanded != task:
        alias_additions = expanded[len(task):].strip()
        parts.append(f"Aliases expanded: {alias_additions}\n")
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

    parts.append("## Ranked files\n")
    for i, rel in enumerate(top_files, 1):
        n = file_hit_count[rel]
        parts.append(f"  {i}. {rel}  ({n} {'match' if n == 1 else 'matches'})")

    # Suggest targeted get_symbol calls for definition hits — faster than reading a whole file
    sym_suggestions: list[tuple[str, str]] = []
    seen_syms: set[tuple[str, str]] = set()
    for r, l, s in unique:
        if _def_line_pat.search(s):
            name = _symbol_name_from_sig(s)
            if name and (r, name) not in seen_syms:
                seen_syms.add((r, name))
                sym_suggestions.append((r, name))
    if sym_suggestions:
        parts.append("\n## Suggested next calls (read just the symbols you need)")
        for file_rel, sym in sym_suggestions[:6]:
            parts.append(f'  get_symbol("{file_rel}", "{sym}")')
    else:
        parts.append("\nCall get_file_structure on the files above for full symbol maps.")

    # Show files that import the top results — useful when the task involves changing
    # a shared module and the AI needs to know what else may need updating.
    if _REVERSE_IMPORTS and top_files:
        dependent_set: set[str] = set()
        for rel in top_files:
            for dep in _REVERSE_IMPORTS.get(rel, []):
                if dep not in top_set:
                    dependent_set.add(dep)
        if 0 < len(dependent_set) <= 8:
            parts.append("\n## Files that import these results\n")
            for dep in sorted(dependent_set):
                parts.append(f"  {dep}")
            parts.append("(May need updates if the above files change — call get_dependents for full details)")

    if include_blueprints:
        parts.append("\n## File blueprints\n")
        for rel in top_files:
            n = file_hit_count[rel]
            try:
                bp_path = _resolve_in_workspace(rel)
                blueprint = _build_blueprint(bp_path)
            except Exception as e:
                parts.append(f"### {rel}\n  (error reading: {e})\n")
                continue
            parts.append(f"### {rel}  ({n} {'match' if n == 1 else 'matches'})\n{blueprint}\n")

    return "\n".join(parts)


@mcp.tool()
def get_imports(path: str) -> str:
    """List every import in a source file and resolve workspace-local ones to actual paths.

    WHEN TO USE: after get_file_structure reveals an unfamiliar symbol, call this
    to find which file it comes from — without reading raw file contents. Resolves
    relative paths (./foo, ../lib/bar) and the @/ alias (Next.js root). Use the
    returned workspace paths as arguments to get_file_structure to inspect those
    files next.

    Args:
        path: path to the file, relative to the workspace root.

    Returns each import as [workspace] resolved/path.ts or [external] package-name,
    with the names being imported shown inline.
    """
    try:
        resolved = _resolve_in_workspace(path)
    except ValueError as e:
        return f"Error: {e}."
    if not resolved.exists():
        return f"Error: '{path}' not found under {WORKSPACE_ROOT}."
    if resolved.suffix not in EXT_LANG:
        return f"Error: '{path}' is not a recognised source file type."

    try:
        text = resolved.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        return f"Error reading file: {e}."

    entries = _parse_import_entries(resolved, text)
    if not entries:
        return f"No imports found in '{path}'."

    workspace_lines: list[str] = []
    external_lines: list[str] = []

    for spec, names in entries:
        kind, imp_resolved = _resolve_import(spec, resolved)
        suffix = f"  ← {names}" if names else ""
        if kind == 'workspace':
            workspace_lines.append(f"  [workspace]  {imp_resolved}{suffix}")
        elif kind == 'unresolved':
            workspace_lines.append(f"  [workspace?] {spec}  (not found on disk){suffix}")
        else:
            external_lines.append(f"  [external]   {imp_resolved}{suffix}")

    rel = str(resolved.relative_to(WORKSPACE_ROOT))
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
def get_dependents(path: str) -> str:
    """Find all workspace files that directly import the given file.

    WHEN TO USE: after identifying a file you are about to change, call this
    to see the blast radius — which other files in the workspace will be
    directly affected. Returns only first-hop importers (files with a direct
    import statement pointing at this file).

    Works for languages where mimir resolves imports to workspace paths:
    TypeScript, JavaScript, and Python. For other languages use find_callers
    to trace symbol-level usage instead.

    Args:
        path: workspace-relative path to the file being changed.

    Returns a list of files that import it, or a clear message if none do.
    """
    try:
        resolved = _resolve_in_workspace(path)
    except ValueError as e:
        return f"Error: {e}."
    rel = str(resolved.relative_to(WORKSPACE_ROOT))

    if not _WARMUP_COMPLETE:
        return "Reverse import index is still building — try again in a few seconds."

    dependents = _REVERSE_IMPORTS.get(rel, [])
    if not dependents:
        return (
            f"No workspace files directly import '{rel}'.\n"
            "It may be an entry point, have only external consumers, "
            "or use a language where import resolution is not supported "
            "(Kotlin, Swift, Java, C#, Go, Rust — use find_callers for those)."
        )

    count = len(dependents)
    lines = [f"# Dependents of '{rel}'  ({count} direct {'importer' if count == 1 else 'importers'})\n"]
    for dep in dependents:
        lines.append(f"  {dep}")
    lines.append(
        f"\nThese {count} {'file' if count == 1 else 'files'} import '{rel}' directly "
        "and may need updates if its exports change."
    )
    return "\n".join(lines)


@mcp.tool()
def get_symbol(path: str, symbol_name: str) -> str:
    """Return the complete source code of ONE named symbol (function, class, or method)
    — bodies included.

    This is the efficient middle ground between get_file_structure (signatures only,
    no bodies) and reading the raw file (everything). Use it when you know which
    symbol you need and want just its implementation.

    WHEN TO USE: after scope_task or get_file_structure identifies the symbol you
    need, call get_symbol to read only that function or class instead of the entire
    file. Typically 10-50× fewer tokens than reading the whole file.

    Args:
        path: workspace-relative path to the source file (e.g. "src/services/auth.py")
        symbol_name: exact name of the function, class, or method (e.g. "authenticate")

    Returns the full source of the symbol with its original indentation, including
    docstrings, decorators on the definition line, and the complete body.
    """
    try:
        resolved = _resolve_in_workspace(path)
    except ValueError as e:
        return f"Error: {e}"
    if not resolved.exists():
        return f"Error: '{path}' not found in workspace."
    body = _extract_symbol_body(resolved, symbol_name)
    if body is None:
        # Offer a helpful fallback hint
        blueprint = _build_blueprint(resolved)
        return (
            f"Symbol '{symbol_name}' not found in '{path}'.\n\n"
            f"Available symbols in this file:\n{blueprint}"
        )
    line_count = body.count('\n') + 1
    return f"# {path}  symbol={symbol_name}  ({line_count} lines)\n\n{body}"


@mcp.tool()
def get_changed_files(base: str = "main") -> str:
    """Return structural blueprints of every source file changed vs a git base branch.

    Combines committed branch changes, uncommitted edits, and untracked new files
    into a single compact overview — the ideal first call at the start of a session
    to orient yourself on what is currently in flight.

    WHEN TO USE: at the start of a session when you need to know which files are
    actively being worked on, before deciding where to focus.

    Args:
        base: the branch or commit to diff against (default "main"; try "master" or
              "HEAD~1" if main does not exist).

    Returns blueprints of each changed source file (bodies stripped, symbols + line
    numbers only) so you get the full structural picture in one call.
    """
    try:
        def _git(*args: str) -> "subprocess.CompletedProcess[str]":
            return subprocess.run(
                ["git", "-C", str(WORKSPACE_ROOT)] + list(args),
                capture_output=True, text=True, timeout=10,
            )

        # Committed changes on this branch vs base
        r_branch = _git("diff", "--name-only", f"{base}...HEAD")
        # Uncommitted staged + unstaged changes
        r_uncommitted = _git("diff", "--name-only", "HEAD")
        # New untracked source files
        r_untracked = _git("ls-files", "--others", "--exclude-standard")

        if r_branch.returncode != 0 and r_uncommitted.returncode != 0:
            stderr = (r_branch.stderr or r_uncommitted.stderr or "").strip()
            if "not a git repository" in stderr:
                return "Not a git repository — get_changed_files requires git."
            return (
                f"Error: could not diff against '{base}'. "
                "Try base='master' or base='HEAD~1'.\n" + stderr
            )

        changed: set[str] = set()
        for r in (r_branch, r_uncommitted, r_untracked):
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    line = line.strip()
                    if line:
                        changed.add(line)

        source_files = sorted(f for f in changed if Path(f).suffix in EXT_LANG)
        if not source_files:
            return f"No source file changes found vs '{base}'."

        parts = [
            f"# Changed files vs '{base}'  ({len(source_files)} source "
            f"{'file' if len(source_files) == 1 else 'files'})\n"
        ]
        for rel in source_files:
            p = WORKSPACE_ROOT / rel
            if not p.exists():
                parts.append(f"# {rel}  [deleted]\n")
            else:
                parts.append(_build_blueprint(p) + "\n")
        return "\n".join(parts)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_architecture() -> str:
    """Return a high-level map of the entire workspace: directories, files, and
    their top-level symbols. One compact overview instead of dozens of
    get_file_structure calls.

    WHEN TO USE: at the very start of a session to understand the project layout
    before diving into specific files. Much cheaper than exploring directory by
    directory. For a specific directory use get_directory_structure instead.

    The map is built from cached blueprints during startup warmup so this call
    is nearly instant after the index is ready (check get_status).
    """
    global _ARCHITECTURE_MAP
    if _ARCHITECTURE_MAP:
        return _ARCHITECTURE_MAP
    return _build_architecture_map()


@mcp.tool()
def find_callers(symbol_name: str, max_results: int = 20) -> str:
    """Find every call site and usage of a symbol across the workspace.

    Unlike verify_symbol_existence (which searches only definition blueprints),
    this searches raw source text to find where the symbol is called, passed,
    or referenced in implementation code.

    WHEN TO USE: after verify_symbol_existence tells you WHERE something is
    defined, use find_callers to trace WHO calls it — for impact analysis,
    understanding data flow, or finding all consumers of an interface.

    Args:
        symbol_name: exact identifier to search for (case-sensitive).
        max_results: cap on results returned (default 20).
    """
    if not re.match(r'^\w+$', symbol_name):
        return "Error: symbol_name must be a bare identifier (letters, digits, underscore)."

    needle = symbol_name.encode('utf-8')
    word_re = re.compile(rf'(?<!\w){re.escape(symbol_name)}(?!\w)')
    files = _iter_source_files()
    results: list[tuple[str, int, str]] = []
    cutoff = max_results * 4

    def _search_file(path: Path) -> list[tuple[str, int, str]]:
        try:
            raw = path.read_bytes()
        except OSError:
            return []
        if needle not in raw:
            return []
        rel = str(path.relative_to(WORKSPACE_ROOT))
        hits = []
        for i, line in enumerate(raw.decode('utf-8', 'replace').splitlines(), 1):
            if word_re.search(line):
                hits.append((rel, i, line.strip()[:120]))
        return hits

    workers = min(8, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_search_file, p): p for p in files}
        for fut in as_completed(futs):
            results.extend(fut.result())
            if len(results) >= cutoff:
                break

    if not results:
        return f"No usages of '{symbol_name}' found in the workspace."

    results = results[:max_results]
    lines = [f"# Usages of '{symbol_name}'  ({len(results)} shown)\n"]
    for rel, lineno, ctx in results:
        lines.append(f"  {rel}:{lineno}  {ctx}")
    if len(results) >= max_results:
        lines.append(f"\n... capped at {max_results}; use a more specific name to narrow down")
    return '\n'.join(lines)


@mcp.tool()
def get_directory_structure(dir_path: str, max_files: int = 10) -> str:
    """Get structural blueprints for every source file under a directory.

    Use this to understand a module, layer, or namespace at a glance — before
    diving into individual files. Good for orientation when scope_task points
    you at a file and you want to see what else lives alongside it.

    WHEN TO USE: when you know WHERE to look but not WHICH file — e.g.
    "show me all the controllers", "what services are in this layer?".
    For finding code by what it DOES, use scope_task instead.

    Args:
        dir_path: path relative to workspace root, e.g. "src/api/controllers".
        max_files: cap on blueprints returned (default 10). Increase if the
                   directory is large, or narrow the path to a subdirectory.
    """
    try:
        target = (WORKSPACE_ROOT / dir_path).resolve()
        target.relative_to(WORKSPACE_ROOT)   # safety: must stay inside workspace
    except ValueError:
        return f"Error: '{dir_path}' resolves outside the workspace root."
    except Exception as e:
        return f"Error: invalid path '{dir_path}': {e}."

    if not target.exists():
        return f"Not found: '{dir_path}' does not exist in the workspace."
    if not target.is_dir():
        return f"'{dir_path}' is a file, not a directory — use get_file_structure instead."

    target_str = str(target) + os.sep
    matches = sorted(
        (p for p in _iter_source_files() if str(p).startswith(target_str)),
        key=lambda p: str(p),
    )
    total = len(matches)
    if not matches:
        return f"No source files found under '{dir_path}'."

    shown = matches[:max_files]
    parts = [f"# {dir_path}  ({total} source file{'s' if total != 1 else ''}"
             f"{', showing first ' + str(len(shown)) if total > max_files else ''})\n"]
    for path in shown:
        try:
            parts.append(_build_blueprint(path))
            parts.append("")
        except Exception:
            pass

    if total > max_files:
        parts.append(
            f"... {total - max_files} more file{'s' if total - max_files != 1 else ''} not shown."
            f" Increase max_files or narrow dir_path to a subdirectory."
        )

    ignore_hint = (
        f"\nTip: to exclude noisy files or subdirectories from mimir's index, add"
        f" gitignore-style patterns to .mimirignore in the workspace root"
        f" (e.g. '**/obj/**' or '**/*.generated.cs')."
    )
    if not (WORKSPACE_ROOT / ".mimirignore").exists():
        parts.append(ignore_hint)

    return "\n".join(parts)


@mcp.tool()
def record_alias(domain_term: str, code_name: str) -> str:
    """Record a domain/feature name → code name mapping so future scope_task
    calls find the right files even when described in non-technical language.

    WHEN TO USE: call this whenever you discover that a feature name used in
    task descriptions maps to a different name in the codebase. For example,
    if searching for "corrective actions filter" returns the wrong files but
    "RectificationFilter" finds them immediately, call:
        record_alias("corrective actions", "RectificationFilter")

    The mapping is saved to .mimiraliases in the workspace root and applied
    automatically to all future scope_task calls in this project.

    Args:
        domain_term: the plain-English feature/domain name (e.g. "corrective actions filter")
        code_name: the actual class/module/file prefix used in the codebase
                   (e.g. "RectificationFilter"). For multiple code names, call
                   record_alias once per name or separate them with commas.

    Returns a confirmation message showing the saved mapping.
    """
    domain = domain_term.strip().lower()
    code = code_name.strip()
    if not domain or not code:
        return "Error: both domain_term and code_name must be non-empty strings."

    alias_path = WORKSPACE_ROOT / ".mimiraliases"

    # Load existing content
    existing: dict[str, list[str]] = {}
    lines_raw: list[str] = []
    if alias_path.exists():
        lines_raw = alias_path.read_text(encoding="utf-8").splitlines()
        for line in lines_raw:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                d, _, c = stripped.partition("=")
                d = d.strip().lower()
                names = [n.strip() for n in c.split(",") if n.strip()]
                existing[d] = names

    # Merge: add code_name to existing entry or create new one
    new_codes = [c.strip() for c in code.split(",") if c.strip()]
    if domain in existing:
        merged = existing[domain]
        added = [c for c in new_codes if c not in merged]
        if not added:
            return f"Already recorded: '{domain}' → {', '.join(merged)}"
        merged.extend(added)
        existing[domain] = merged
    else:
        existing[domain] = new_codes

    # Rewrite file preserving comments and blank lines, updating changed entries
    out_lines: list[str] = []
    written: set[str] = set()
    for line in lines_raw:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            d, _, _ = stripped.partition("=")
            d = d.strip().lower()
            if d in existing:
                out_lines.append(f"{d} = {', '.join(existing[d])}")
                written.add(d)
            else:
                out_lines.append(line)
        else:
            out_lines.append(line)

    # Append any brand-new entries not yet in the file
    for d, codes in existing.items():
        if d not in written:
            out_lines.append(f"{d} = {', '.join(codes)}")

    if not alias_path.exists():
        out_lines = [
            "# mimir domain aliases — maps feature/domain names to code names",
            "# Format:  domain phrase = CodeName1, CodeName2",
            "# scope_task expands these automatically before searching.",
            "",
        ] + out_lines

    alias_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    # Update in-memory cache
    _MIMIRALIASES[domain] = existing[domain]

    return f"Saved: '{domain}' → {', '.join(existing[domain])}  (.mimiraliases updated)"


@mcp.tool()
def get_status() -> str:
    """Report the current state of the mimir index for this workspace.

    Call this at the start of a session to understand what mimir knows about
    the workspace before using other tools. Key things to check:

    - symbol_index=warm means scope_task and verify_symbol_existence use a fast
      SQL index; symbol_index=building means they fall back to a slower linear
      scan and may miss recently added symbols
    - blueprints_cached shows how many files are already parsed
    - ignored_patterns lists active .mimirignore rules

    If the index is still building, you can proceed — tools still work, just
    slower. For large repos (8000+ files) the index typically builds in <60s.
    """
    try:
        total_files = len(_iter_source_files())
        cached = len(_CACHE)
        disk_count = 0
        if _DISK_CACHE is not None:
            try:
                disk_count = _DISK_CACHE.execute(
                    "SELECT COUNT(*) FROM blueprints"
                ).fetchone()[0]
            except Exception:
                pass

        sym_count = 0
        if _FTS_READY and _DISK_CACHE is not None:
            try:
                sym_count = _DISK_CACHE.execute(
                    "SELECT COUNT(DISTINCT token) FROM symbols"
                ).fetchone()[0]
            except Exception:
                pass

        index_state = "warm" if _FTS_READY else "building (tools still work, slower until complete)"
        warmup_state = "complete" if _WARMUP_COMPLETE else "in progress"

        ignore_path = WORKSPACE_ROOT / ".mimirignore"
        if _MIMIRIGNORE_PATTERNS:
            ignore_section = (
                f"ignored_patterns ({len(_MIMIRIGNORE_PATTERNS)} active):\n"
                + "\n".join(f"  {p}" for p in _MIMIRIGNORE_PATTERNS)
            )
        elif ignore_path.exists():
            ignore_section = "ignored_patterns: .mimirignore exists but contains no active patterns"
        else:
            ignore_section = (
                "ignored_patterns: none  (.mimirignore not found)\n"
                "  → create .mimirignore in the workspace root to exclude noisy directories\n"
                "    e.g. '**/obj/**', '**/bin/**', '**/*.generated.cs', '**/vendor/**'"
            )

        alias_path = WORKSPACE_ROOT / ".mimiraliases"
        if _MIMIRALIASES:
            alias_section = (
                f"domain_aliases ({len(_MIMIRALIASES)} active):\n"
                + "\n".join(f"  {d} → {', '.join(codes)}" for d, codes in _MIMIRALIASES.items())
            )
        elif alias_path.exists():
            alias_section = "domain_aliases: .mimiraliases exists but contains no active mappings"
        else:
            alias_section = (
                "domain_aliases: none  (.mimiraliases not found)\n"
                "  → call record_alias(domain_term, code_name) when you discover a\n"
                "    feature name maps to a different code name in the codebase"
            )

        rev_count = len(_REVERSE_IMPORTS)
        watcher_line = (
            "file_watcher:       on (changes invalidate cache instantly)"
            if _FILE_WATCHER_ACTIVE
            else "file_watcher:       off  (pip install watchdog to enable)"
        )
        rev_line = (
            f"reverse_imports:    {rev_count:,} files mapped"
            if rev_count > 0
            else "reverse_imports:    building..."
        )

        lines = [
            f"workspace:          {WORKSPACE_ROOT}",
            f"source_files:       {total_files}",
            f"blueprints_cached:  {cached} in memory, {disk_count} on disk",
            f"symbol_index:       {index_state}",
            f"  indexed_tokens:   {sym_count:,}" if _FTS_READY else "  indexed_tokens:   (not yet built)",
            f"warmup:             {warmup_state}",
            f"tree_sitter:        {'on' if TREE_SITTER_OK else 'off (regex fallback)'}",
            f"sandbox:            {'on' if SANDBOX_ENABLED else 'off'}",
            watcher_line,
            rev_line,
            "",
            ignore_section,
            "",
            alias_section,
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"EXCEPTION in get_status: {e}"


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

    claude_md = cwd / "CLAUDE.md"
    mimir_marker = "## Code exploration — use mimir tools"
    if claude_md.exists() and mimir_marker in claude_md.read_text(encoding="utf-8"):
        print(f"skipped  {claude_md}  (mimir section already present)")
    else:
        snippet = (
            f"\n{mimir_marker}\n\n"
            "This project has mimir MCP tools available. Use them before reading raw files.\n\n"
            "At the start of any coding session:\n"
            "1. Call `get_status` to check the index is ready and see any active exclusions\n"
            "2. Call `scope_task(\"describe what you want to do\")` to find relevant files\n\n"
            "For any task involving existing code:\n"
            "- Use `scope_task` before opening files — it finds the right files in one call\n"
            "- Use `get_file_structure` to see a file's symbol map before reading it line by line\n"
            "- Use `verify_symbol_existence` before assuming a function or type exists\n"
            "- Use `find_callers` after `verify_symbol_existence` to trace impact\n"
            "- Use `get_directory_structure` when you know the directory but not which file\n"
            "- Use `get_imports` when an unfamiliar symbol appears and you need to trace its origin\n"
            "- Call `record_alias(domain_term, code_name)` when you discover a feature name maps\n"
            "  to a different code name — future scope_task searches will expand it automatically\n"
        )
        with open(claude_md, "a", encoding="utf-8") as f:
            f.write(snippet)
        action = "updated" if claude_md.exists() else "created"
        print(f"{action}  {claude_md}  (mimir section appended)")

    github_dir = cwd / ".github"
    github_dir.mkdir(exist_ok=True)
    copilot_instructions = github_dir / "copilot-instructions.md"
    copilot_marker = "## Code exploration — use mimir tools"
    if copilot_instructions.exists() and copilot_marker in copilot_instructions.read_text(encoding="utf-8"):
        print(f"skipped  {copilot_instructions}  (mimir section already present)")
    else:
        copilot_snippet = (
            f"{copilot_marker}\n\n"
            "This project has mimir MCP tools. Always use them before using built-in search or reading files.\n\n"
            "Workflow for any task involving existing code:\n"
            "1. Call `get_status` to confirm the index is ready\n"
            "2. Call `scope_task` with a description of the task — use its ranked file list, "
            "do NOT fall back to glob or grep even if the output is large\n"
            "   - Tip: use technical/class names when known (e.g. 'RectificationFilter') "
            "rather than feature names (e.g. 'corrective actions filter')\n"
            "3. Call `get_file_structure` on the files scope_task returns before reading raw lines\n"
            "4. Use `find_callers` or `verify_symbol_existence` to trace symbols\n"
            "5. When you discover a domain/feature term maps to a code name "
            "(e.g. 'corrective actions' → 'RectificationFilter'), call `record_alias` "
            "to save it — future scope_task searches will expand it automatically\n\n"
            "Do not use built-in file search, glob, or grep if mimir tools are available. "
            "Read the full scope_task output even if it is saved to a temp file — it contains the answer.\n"
        )
        with open(copilot_instructions, "a", encoding="utf-8") as f:
            f.write(copilot_snippet)
        action = "updated" if copilot_instructions.exists() else "created"
        print(f"{action}  {copilot_instructions}  (mimir section appended)")

    print("\nDone. Restart Claude Code or reload VS Code to pick up the config.")


_CLI_HELP = """\
mimir — structural code index for Claude Code and GitHub Copilot

SETUP (run once per project)
  cd your-project
  mimir-setup               Creates .mcp.json, .vscode/mcp.json, and CLAUDE.md
                            then restart Claude Code or reload VS Code

TERMINAL COMMANDS
  mimir scope  "<task>"    Find files relevant to a plain-English task description
  mimir find   <Symbol>    Locate a symbol definition across the workspace
  mimir callers <Symbol>   Find every call site and usage of a symbol
  mimir status             Show index state, file count, and active exclusions
  mimir --help             Show this help

EXAMPLES
  mimir scope "change how jobs are retried on failure"
  mimir find   JobScheduler
  mimir callers authenticate
  mimir status

MCP TOOLS (available to Claude Code and GitHub Copilot)
  get_status               Index state, file count, ignore patterns, domain aliases — call first
  get_architecture         High-level workspace map: directories, files, top-level symbols
  scope_task               Find relevant files from a task description
  get_file_structure       Compact symbol map of a single file (classes, methods, line nos)
  get_symbol               Full source of ONE named symbol — efficient middle ground
  get_directory_structure  Symbol maps for every file under a directory
  get_changed_files        Blueprints of files changed vs a git branch (session orientation)
  get_imports              Resolve imports to workspace files or external packages
  get_dependents           Find all files that directly import a given file (blast radius)
  verify_symbol_existence  Confirm a symbol is defined and find its location
  find_callers             Find every call site and usage of a symbol
  record_alias             Save a domain/feature name → code name mapping for future searches
  execute_local_sandbox    Run a Python or bash snippet with a timeout

EXCLUDING FILES
  Create .mimirignore in the project root with gitignore-style patterns:
    **/obj/**
    **/bin/**
    **/*.generated.cs
    **/vendor/**
  Mimir reloads it automatically. Run `mimir status` to confirm active patterns.

DOMAIN ALIASES
  Create .mimiraliases in the project root to map feature names to code names:
    corrective actions = RectificationFilter
    live tutor = LiveTutor, GeminiLive
  Or let Claude/Copilot call record_alias() to build it automatically.
  Mimir applies aliases before every scope_task search.

ENVIRONMENT VARIABLES
  MCP_WORKSPACE_ROOT       Root of the repo mimir maps (default: current dir)
  MCP_MAX_FILE_BYTES       Skip files larger than this in bytes (default: 2000000)
  MCP_ENABLE_SANDBOX       Set to 0 to disable execute_local_sandbox (default: 1)
  MCP_SANDBOX_TIMEOUT      Max seconds a sandbox snippet can run (default: 10)

Without arguments, mimir starts as an MCP server on stdio — this is what your
AI client launches. You never need to run this manually.
"""


def _cli_run(subcommand: str, arg: str) -> None:
    """Run a single tool query, print the result, and exit."""
    _load_disk_cache()
    _warm_cache()   # synchronous — small wait for full accuracy on first run

    if subcommand == "scope":
        print(scope_task(arg))
    elif subcommand == "find":
        print(verify_symbol_existence(arg))
    elif subcommand == "callers":
        print(find_callers(arg))
    elif subcommand == "status":
        print(get_status())
    else:
        print(f"Unknown subcommand '{subcommand}'. Run `mimir` with no arguments for help.")
        sys.exit(1)


def main() -> None:
    """Console-script / module entry point.

    Without arguments: starts the MCP stdio server (used by AI clients).
    With a subcommand:  runs a single query and prints the result.
    """
    args = sys.argv[1:]

    if args and args[0] in ("scope", "find", "callers", "status"):
        subcommand = args[0]
        arg = " ".join(args[1:]) if len(args) > 1 else ""
        if subcommand != "status" and not arg:
            print(f"Usage: mimir {subcommand} <{'task description' if subcommand == 'scope' else 'SymbolName'}>")
            sys.exit(1)
        _cli_run(subcommand, arg)
        return

    if args and args[0] in ("-h", "--help", "help"):
        print(_CLI_HELP)
        return

    # No subcommand — start as MCP server
    disk_loaded = _load_disk_cache()
    total_files = len(_iter_source_files())

    disk_status = f"disk_cache={disk_loaded}/{total_files} loaded" if _DISK_CACHE else "disk_cache=off"
    fts_status = f"symbol_index={'warm' if _FTS_READY else 'building'}"
    print(
        f"[mimir] root={WORKSPACE_ROOT} "
        f"tree_sitter={'on' if TREE_SITTER_OK else 'off (regex fallback)'} "
        f"sandbox={'on' if SANDBOX_ENABLED else 'off'}  {disk_status}  {fts_status}",
        file=sys.stderr,
    )
    import threading
    threading.Thread(target=_warm_cache, daemon=True, name="mimir-warmup").start()
    watcher_state = "on" if _start_file_watcher() else "off (install watchdog for auto-invalidation)"
    print(f"[mimir] file_watcher={watcher_state}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
