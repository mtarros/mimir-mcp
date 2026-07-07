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
"""

from __future__ import annotations

import functools
import json as _json_mod
import math
import os
import re
import shutil
import sys
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional

# fastmcp (and its transitive mcp-sdk/pydantic deps) is ~150-250ms of import
# time — pure MCP-protocol overhead a one-shot CLI invocation (`mimir status`,
# `mimir hint ...`) never touches. Deferred to main()'s server-start branch;
# tool functions below register with a local no-op `_tool` collector instead
# of `@mcp.tool()` so none of them need fastmcp imported to be defined.
_TOOLS: list = []


def _tool(fn):
    _TOOLS.append(fn)
    return fn


# --------------------------------------------------------------------------- #
# Optional tree-sitter. We import lazily and degrade gracefully. If anything in
# this block fails (package missing, grammar download blocked, ABI mismatch),
# TREE_SITTER_OK stays False and every tool uses the regex tokenizer instead.
# --------------------------------------------------------------------------- #
TREE_SITTER_OK = False
_TS_PARSER_LOCAL = threading.local()  # thread-local: .cache = {lang: parser}
try:
    from tree_sitter_language_pack import get_parser as _ts_get_parser, get_language as _ts_get_language  # type: ignore
    import tree_sitter as _tree_sitter  # type: ignore
    TREE_SITTER_OK = True
except Exception:  # noqa: BLE001 - we genuinely want to swallow everything here
    _ts_get_parser = None  # type: ignore
    _ts_get_language = None  # type: ignore
    _tree_sitter = None  # type: ignore


def _get_ts_parser(ts_lang: str) -> object:
    """Return a thread-local tree-sitter parser for ts_lang (parsers are not thread-safe)."""
    cache = _TS_PARSER_LOCAL.__dict__.setdefault('cache', {})
    if ts_lang not in cache:
        cache[ts_lang] = _ts_get_parser(ts_lang)  # type: ignore[misc]
    return cache[ts_lang]

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

# Directories that are never worth parsing. Matched against any path segment, so
# "node_modules" anywhere in the tree is skipped. This is the single biggest
# lever for keeping the server light on large repos.
BLACKLIST_DIRS = {
    "node_modules", ".build", "DerivedData", ".git", "dist", "build",
    ".next", ".venv", "venv", "__pycache__", ".gradle", ".idea", ".vs",
    "Pods", ".dart_tool", "target", "out", "coverage", ".cache",
}

# Filenames that are pure framework routing convention (Next.js App Router,
# Nuxt, etc.) and carry no naming signal of their own — the meaningful name
# for these files always lives in the parent directory instead.
_GENERIC_ROUTE_STEMS = frozenset({"page", "index", "route", "layout"})

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


def _load_mimirnotes() -> dict[str, list[str]]:
    """Read path-prefix -> free-text note mappings from .mimirnotes.

    File format (one note per line, notes accumulate under a prefix — this is
    an append-only log, unlike .mimiraliases which merges into one line):
        Features/Playback = background sync uses platform-native timers, not SyncService
        Features/Playback = check MainActivity.java/AppDelegate.swift for the real logic
        # comments are ignored
    Returns lowercased path-prefix -> list of note strings (insertion order).
    """
    p = WORKSPACE_ROOT / ".mimirnotes"
    if not p.exists():
        return {}
    notes: dict[str, list[str]] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        prefix, _, note = line.partition("=")
        prefix, note = prefix.strip().lower(), note.strip()
        if prefix and note:
            notes.setdefault(prefix, []).append(note)
    return notes


_MIMIRNOTES: dict[str, list[str]] = _load_mimirnotes()


def _load_focus() -> dict[str, float]:
    """Read project focus weights from .mimir-focus.

    File format (one entry per line):
        InControl.Carps.Mobile = 3.0
        InControl.Carps.Keypad = 0.3
        * = 0.2       # suppress everything else
        # comments ignored
    The special key '*' is the default weight applied to files that match
    no named prefix — use it to suppress noisy sibling directories.
    Returns {prefix_lower: multiplier}.
    """
    p = WORKSPACE_ROOT / ".mimir-focus"
    if not p.exists():
        return {}
    weights: dict[str, float] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            prefix, _, raw = line.partition("=")
            prefix = prefix.strip()
            try:
                weight = float(raw.strip())
            except ValueError:
                weight = 3.0
        else:
            prefix = line
            weight = 3.0
        if prefix:
            weights[prefix.lower()] = weight
    return weights


_FOCUS_WEIGHTS: dict[str, float] = _load_focus()


def _load_scope() -> "Optional[str]":
    """Read the active hard-scope path prefix from .mimir-scope, if set.

    Unlike .mimir-focus (a soft ranking bias — the rest of the repo still
    shows up, just lower-scored), an active scope is a hard filter: every
    search tool excludes files outside it entirely until cleared with
    set_scope(""). The two are independent and can be combined.
    """
    p = WORKSPACE_ROOT / ".mimir-scope"
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8").strip()
    return raw or None


_ACTIVE_SCOPE: "Optional[str]" = _load_scope()


def _in_scope(rel: str) -> bool:
    """True if a workspace-relative path is inside the active hard scope, or
    if no scope is set (everything is in scope). `rel` may use either '/' or
    the native OS separator (Windows callers pass str(Path) results, which
    are backslash-separated) — normalized to '/' before comparing against
    _ACTIVE_SCOPE, which is always stored posix-style."""
    if not _ACTIVE_SCOPE:
        return True
    rel = rel.replace("\\", "/")
    return rel == _ACTIVE_SCOPE or rel.startswith(_ACTIVE_SCOPE + "/")


def _parse_focus_str(raw: str) -> dict[str, float]:
    """Parse a comma-separated "prefix:weight" string into {prefix_lower: float}.

    Accepts the same format as set_focus entries so callers can pass ad-hoc
    focus weights for a single call without touching the persistent file.
    """
    result: dict[str, float] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            prefix, _, wraw = part.rpartition(":")
            key = prefix.strip().replace("\\", "/").lower()
            try:
                result[key] = float(wraw.strip())
            except ValueError:
                result[key] = 3.0
        else:
            result[part.replace("\\", "/").lower()] = 3.0
    return result


def _expand_task_with_aliases(task: str) -> str:
    """Append alias code names to a task string when domain phrases match.

    Checks every alias domain phrase (longest first to prefer specific matches)
    against the lowercased task. A phrase matches if ALL its words appear
    anywhere in the task (order-independent) — so "orange badge verify count"
    still matches a query like "orange badge category verify count bug".
    Appends matched code names so that scope_task keyword extraction picks them
    up alongside the original terms.
    """
    if not _MIMIRALIASES:
        return task
    task_lower = task.lower()
    task_words = set(re.findall(r'[a-z]+', task_lower))
    additions: list[str] = []
    for domain in sorted(_MIMIRALIASES, key=len, reverse=True):
        domain_words = re.findall(r'[a-z]+', domain)
        if all(w in task_words for w in domain_words):
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
BLUEPRINT_VERSION = "8"  # bump when index schema or tokenisation changes

# Splits PascalCase / camelCase into components: 'StartAutoRefresh' → ['Start','Auto','Refresh']
_CAMEL_SPLIT_RE = re.compile(r'[A-Z][a-z0-9]*|[a-z][a-z0-9]*')


def _init_disk_cache() -> "Optional[object]":
    """Open (or create) a per-workspace SQLite blueprint cache in ~/.cache/mimir/."""
    try:
        import sqlite3, hashlib
        ws_hash = hashlib.sha256(str(WORKSPACE_ROOT).encode()).hexdigest()[:16]
        cache_dir = Path.home() / ".cache" / "mimir"
        cache_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(cache_dir / f"{ws_hash}.db"), check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")   # safe with WAL; skips per-commit fsync
        db.execute("PRAGMA cache_size=-32768")    # 32MB page cache
        db.execute("PRAGMA temp_store=MEMORY")    # temp tables in RAM (FTS5 sort/merge)
        db.execute("PRAGMA mmap_size=134217728")  # 128MB mmap for faster sequential reads
        db.execute(
            "CREATE TABLE IF NOT EXISTS blueprints"
            " (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, blueprint TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        # Check the stored schema version BEFORE creating lines/symbols — a
        # version bump may require the symbols table itself to be dropped and
        # recreated (e.g. adding COLLATE NOCASE to `token`), which a plain
        # "CREATE TABLE IF NOT EXISTS" would silently skip on an existing table.
        stored = db.execute(
            "SELECT value FROM meta WHERE key='blueprint_version'"
        ).fetchone()
        version_changed = stored is None or stored[0] != BLUEPRINT_VERSION
        if version_changed:
            db.execute("DROP TABLE IF EXISTS symbols")
            db.execute("DROP TABLE IF EXISTS lines")

        # Normalized symbol index: lines holds context (one row per definition line),
        # symbols holds tokens (one row per token — no context duplication).
        # Migrate from the old denormalized schema if needed (belt-and-suspenders —
        # the version_changed drop above already covers this on a version bump).
        try:
            db.execute("SELECT context FROM symbols LIMIT 0")
            db.execute("DROP TABLE symbols")
            db.execute("DROP TABLE IF EXISTS lines")
        except Exception:
            pass
        db.execute(
            "CREATE TABLE IF NOT EXISTS lines"
            " (file TEXT NOT NULL, lineno TEXT NOT NULL, context TEXT NOT NULL,"
            "  PRIMARY KEY (file, lineno))"
        )
        # token is COLLATE NOCASE so lookups (and the index) are case-insensitive —
        # e.g. querying "signalr" finds a stored token "SignalR". Tokens are still
        # STORED exactly as they appear in source; only comparison/ordering ignores
        # case. Data is preserved verbatim; case-preserved values are what's shown
        # in tool output, e.g. "FOUND ... -> SignalR".
        db.execute(
            "CREATE TABLE IF NOT EXISTS symbols"
            " (token TEXT NOT NULL COLLATE NOCASE, file TEXT NOT NULL, lineno TEXT NOT NULL)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_symbols_token ON symbols (token)"
        )
        # FTS5 virtual table for semantic_search — one row per definition line.
        # file/lineno are UNINDEXED (retrieval only); symbol_name, signature, and
        # decomposed (space-joined sub-tokens from identifier decomposition) are
        # full-text indexed by FTS5's built-in BM25 scorer.
        try:
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5("
                "  file UNINDEXED,"
                "  lineno UNINDEXED,"
                "  symbol_name,"
                "  signature,"
                "  decomposed,"
                "  tokenize='unicode61'"
                ")"
            )
        except Exception:
            pass  # FTS5 unavailable in this SQLite build; semantic_search degrades gracefully
        # Reverse-import edges (src file -> resolved workspace target it imports).
        # Independent of blueprint_version — built from raw source text, not
        # blueprints — so it isn't touched by the version_changed invalidation
        # below. A 'reverse_imports_built' meta flag (set after a successful
        # full _build_reverse_imports()) distinguishes "never built" from
        # "built, but this repo has zero cross-file workspace imports".
        db.execute(
            "CREATE TABLE IF NOT EXISTS reverse_imports"
            " (src TEXT NOT NULL, target TEXT NOT NULL, PRIMARY KEY (src, target))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_revimp_target ON reverse_imports (target)"
        )
        # Invalidate all cached blueprints when the format version changes.
        if version_changed:
            db.execute("DELETE FROM blueprints")
            db.execute("DELETE FROM lines")
            db.execute("DELETE FROM symbols")
            db.execute("DELETE FROM symbol_fts")
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('blueprint_version', ?)",
                (BLUEPRINT_VERSION,)
            )
        db.commit()
        return db
    except Exception:
        return None


_DISK_CACHE = _init_disk_cache()
_FTS_READY = False        # True once the symbols inverted index is built and queryable
_SEMANTIC_READY = False   # True once symbol_fts FTS5 table is populated
_WARMUP_COMPLETE = False  # True once _warm_cache() finishes its first full pass
_WARMUP_IN_PROGRESS = False  # True during parallel parse; suppresses per-file disk writes
_REVERSE_IMPORTS: dict[str, list[str]] = {}  # rel_target → sorted list of rel files that import it
_REVERSE_IMPORTS_FWD: dict[str, set[str]] = {}  # rel_src → set of rel_targets it resolves to (for O(1) incremental updates)
_FILE_WATCHER_ACTIVE = False  # True if a watchdog observer started successfully
_ARCHITECTURE_MAP: str = ""   # cached architecture overview; built once in _warm_cache
_GIT_RECENCY_CACHE: dict = {"ts": 0.0, "scores": {}}  # TTL cache: rel_path → recency rank

# Shared pattern: detects definition lines vs usages (import/call).
# Compiled once at module load; used by scope_task and semantic_search.
_DEF_LINE_PAT = re.compile(
    r'(?:^|\s)(?:export\s+)?(?:default\s+)?(?:async\s+)?'
    r'(?:function|class|def|interface|type|enum|struct|impl|func)\s+\w|'
    r'(?:export\s+)?(?:const|let|var)\s+\w+\s*[=(]|'
    r'(?:public|private|protected|internal)(?:\s+(?:static|sealed|abstract|override'
    r'|virtual|partial|async|readonly|new|extern))*\s+'
    r'(?:class|interface|enum|record|struct|void|Task|bool|int|string|object'
    r'|IDisposable|IEnumerable|IReadOnly\w+|\w+\??)\s+\w+\s*[(<]'
)
# Pre-computed path strings for fast scope_task path scoring.
# List of (rel, stem_lower, dir_lower) tuples — avoids Path.relative_to() and .stem
# on every scope_task call. Built once in _warm_cache(); rebuilt on file-list TTL expiry.
_PATH_STRINGS: list[tuple[str, str, str]] = []
# Inverted index: lowercase path-component token -> indices into _PATH_STRINGS.
# Lets scope_task's path-matching loop visit only candidate files instead of
# scanning every source file on every call (O(matches) instead of O(files) —
# matters most on 30k+ file repos where the old linear scan dominated latency).
_PATH_TOKEN_INDEX: dict[str, list[int]] = {}
_PATH_WORD_RE = re.compile(r'[A-Za-z0-9]+')
_PATH_STRINGS_TS: float = 0.0

# Optional ripgrep accelerator for find_callers. If `rg` is on PATH it replaces
# the Python byte-scan loop with SIMD-accelerated parallel I/O (~10× faster on
# large repos). Falls back to the Python scan silently when unavailable.
_RG_BIN: str | None = shutil.which("rg")


# Document-frequency cache backing IDF-weighted ranking (BM25 IDF).
# Built once at warmup from the symbols table; only Zipf-head tokens
# (df >= floor) are stored so memory stays bounded on 60k-file repos —
# anything rarer is near-max IDF anyway and gets a default.
_TOKEN_DF: dict[str, int] = {}   # lowercased token → distinct-file count
_TOKEN_DF_N: int = 0             # corpus size (file count) at build time
_TOKEN_DF_FLOOR: int = 4         # tokens with df below this are not stored
_IDF_REF: float = 1.0            # IDF at the reference df; normalizes weights to ~1.0
_DF_DIRTY: int = 0               # file changes since the last DF build
_DF_REBUILDING: bool = False
_FILE_NLINES: dict[str, int] = {}  # rel → blueprint line count (doc-length norm)
_AVG_NLINES: float = 0.0           # mean blueprint line count across the corpus



def _load_disk_cache() -> int:
    """Populate the in-memory cache from SQLite. Returns number of valid entries loaded."""
    global _FTS_READY, _SEMANTIC_READY
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
    try:
        fts_count = _DISK_CACHE.execute("SELECT COUNT(*) FROM symbol_fts").fetchone()[0]
        if fts_count > 0:
            _SEMANTIC_READY = True
    except Exception:
        pass
    _load_reverse_imports_from_disk()  # cheap (few thousand rows at most) — no-op if never built
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
    "against", "between", "without", "within", "during", "before", "after",
    "under", "above", "below", "every", "their", "these", "those", "being",
    "been", "might", "shall", "still", "however", "because", "through",
    "themselves", "only", "them", "they",
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

# Maps abs-path → {sig: (start_byte, end_byte)} captured during blueprint build.
# Lets _extract_symbol_body skip the ~8ms tree-sitter re-parse when offsets are warm.
# Written from ThreadPoolExecutor threads during warmup — each thread writes a
# different path key so no locking is needed (dict assignment is GIL-atomic).
_OFFSET_CACHE: dict[str, dict[str, tuple[int, int]]] = {}


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
    # During parallel warmup, skip individual disk writes — _flush_warmup_blueprints
    # writes all blueprints in a single transaction after the ThreadPoolExecutor finishes.
    # This avoids N×fsync and SQLite write contention from concurrent threads.
    if _WARMUP_IN_PROGRESS:
        return
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
            if _SEMANTIC_READY:
                fts_rows = _fts_rows_for_blueprint(rel, blueprint)
                _DISK_CACHE.execute("DELETE FROM symbol_fts WHERE file = ?", (rel,))
                if fts_rows:
                    _DISK_CACHE.executemany(
                        "INSERT INTO symbol_fts(file, lineno, symbol_name, signature, decomposed)"
                        " VALUES (?,?,?,?,?)",
                        fts_rows,
                    )
            _DISK_CACHE.commit()
            _df_note_change()
        except Exception:
            pass


def _cache_evict(path: Path) -> None:
    """Remove a single file's blueprint from memory and disk caches.

    Called by the file watcher on modification/deletion so the next access
    rebuilds a fresh blueprint rather than returning stale structure.
    """
    global _FILE_LIST, _FILE_LIST_TS
    _CACHE.pop(str(path), None)
    _OFFSET_CACHE.pop(str(path), None)
    if _DISK_CACHE is not None:
        try:
            rel = str(path.relative_to(WORKSPACE_ROOT))
            _DISK_CACHE.execute("DELETE FROM blueprints WHERE path = ?", (str(path),))
            _DISK_CACHE.execute("DELETE FROM symbols WHERE file = ?", (rel,))
            _DISK_CACHE.execute("DELETE FROM lines WHERE file = ?", (rel,))
            _DISK_CACHE.execute("DELETE FROM symbol_fts WHERE file = ?", (rel,))
            _DISK_CACHE.commit()
            _df_note_change()
        except Exception:
            pass
    # Force a file-list re-walk so new/deleted files are picked up immediately
    _FILE_LIST = []
    _FILE_LIST_TS = 0.0


def _smart_invalidate(path: Path) -> None:
    """Re-index a changed file; skip DB writes when only body code changed.

    During active development most saves touch method bodies, not signatures.
    When the blueprint is unchanged we refresh mtime/size in memory and skip
    the ~15ms of SQLite DELETEs + INSERTs that a full evict+reindex would cost.
    """
    cached = _CACHE.get(str(path))
    if cached is None:
        _cache_evict(path)
        return
    old_blueprint = cached[2]
    # Remove from both caches before re-parse so _build_blueprint does a fresh read
    _CACHE.pop(str(path), None)
    _OFFSET_CACHE.pop(str(path), None)
    try:
        new_blueprint = _build_blueprint(path)  # populates _cache_put internally if new
    except Exception:
        _cache_evict(path)
        return
    if new_blueprint == old_blueprint:
        # Structure unchanged — refresh mtime/size in memory only, skip all DB I/O
        try:
            st = path.stat()
            _CACHE[str(path)] = (st.st_mtime, st.st_size, new_blueprint)
        except OSError:
            pass
    # If different: _build_blueprint already called _cache_put which wrote to DB


# --------------------------------------------------------------------------- #
# Path helpers / safety
# --------------------------------------------------------------------------- #
def _is_blacklisted(path: Path) -> bool:
    if any(part in BLACKLIST_DIRS for part in path.parts):
        return True
    for pat in _MIMIRIGNORE_PATTERNS:
        if path.match(pat):
            return True
        # Path.match() cannot handle dir-glob patterns (e.g. **/wwwroot/lib/**)
        # on file paths — the trailing /** never matches a filename. Work around
        # this by stripping the trailing /** and matching against each parent dir.
        if '/**' in pat:
            dir_pat = pat[:pat.index('/**')]
            for parent in path.parents:
                if parent.match(dir_pat):
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
        dirs[:] = [
            d for d in dirs
            if d not in BLACKLIST_DIRS and not (Path(root, d) / "pyvenv.cfg").exists()
        ]
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


# Languages where _resolve_import reliably returns 'workspace' hits.
# C# uses namespace matching (unreliable), Swift always returns 'external'.
# Including them just wastes a full file read per file with no payoff.
_REVERSE_IMPORT_EXTS = frozenset({
    '.ts', '.tsx', '.js', '.jsx', '.mjs',
    '.py', '.pyi',
    '.java', '.kt', '.kts',
})
# Imports always live at the top of the file. Reading beyond the first 8KB
# (≈ 200 lines) is wasted I/O — no language puts import blocks mid-file.
_IMPORT_READ_BYTES = 8192


def _build_reverse_imports() -> None:
    """Build _REVERSE_IMPORTS: for each workspace file, which other files import it.

    Only covers languages where _resolve_import can produce 'workspace' hits
    (TypeScript/JS, Python, Java/Kotlin). C# and Swift are skipped — their
    namespace/module resolution doesn't produce reliable file-level reverse links.
    Reads only the first 8KB per file since imports are always at the top.
    """
    global _REVERSE_IMPORTS, _REVERSE_IMPORTS_FWD
    rev: dict[str, set[str]] = {}
    fwd: dict[str, set[str]] = {}
    for src_path in _iter_source_files():
        if src_path.suffix not in _REVERSE_IMPORT_EXTS:
            continue
        try:
            raw = src_path.read_bytes()
            text = raw[:_IMPORT_READ_BYTES].decode('utf-8', 'replace') if len(raw) > _IMPORT_READ_BYTES else raw.decode('utf-8', 'replace')
        except OSError:
            continue
        entries = _parse_import_entries(src_path, text)
        if not entries:
            continue
        rel_src = str(src_path.relative_to(WORKSPACE_ROOT))
        targets: set[str] = set()
        for spec, _ in entries:
            try:
                kind, resolved = _resolve_import(spec, src_path)
            except Exception:
                continue
            if kind == 'workspace':
                rev.setdefault(resolved, set()).add(rel_src)
                targets.add(resolved)
        if targets:
            fwd[rel_src] = targets
    _REVERSE_IMPORTS = {k: sorted(v) for k, v in rev.items()}
    _REVERSE_IMPORTS_FWD = fwd

    if _DISK_CACHE is not None:
        try:
            _DISK_CACHE.execute("DELETE FROM reverse_imports")
            rows = [(src, target) for src, targets in fwd.items() for target in targets]
            if rows:
                _DISK_CACHE.executemany(
                    "INSERT OR IGNORE INTO reverse_imports VALUES (?,?)", rows
                )
            _DISK_CACHE.execute(
                "INSERT OR REPLACE INTO meta VALUES ('reverse_imports_built', '1')"
            )
            _DISK_CACHE.commit()
        except Exception:
            pass


def _load_reverse_imports_from_disk() -> bool:
    """Restore _REVERSE_IMPORTS/_REVERSE_IMPORTS_FWD from the disk cache if a
    previous full _build_reverse_imports() persisted them.

    Avoids re-reading the first 8KB of every JS/TS/Python/Java/Kotlin file in
    the workspace on every CLI invocation of `scope`/`area` — the same class
    of fix already applied to the blueprint/symbol/FTS tables. Returns False
    (caller should fall back to a full _build_reverse_imports()) if nothing
    was ever persisted; the 'reverse_imports_built' meta flag distinguishes
    that from "built, but this repo has zero cross-file workspace imports".
    """
    global _REVERSE_IMPORTS, _REVERSE_IMPORTS_FWD
    if _DISK_CACHE is None:
        return False
    try:
        flag = _DISK_CACHE.execute(
            "SELECT value FROM meta WHERE key='reverse_imports_built'"
        ).fetchone()
        if not flag:
            return False
        rows = _DISK_CACHE.execute("SELECT src, target FROM reverse_imports").fetchall()
    except Exception:
        return False
    fwd: dict[str, set[str]] = {}
    rev: dict[str, set[str]] = {}
    for src, target in rows:
        fwd.setdefault(src, set()).add(target)
        rev.setdefault(target, set()).add(src)
    _REVERSE_IMPORTS_FWD = fwd
    _REVERSE_IMPORTS = {k: sorted(v) for k, v in rev.items()}
    return True


def _update_reverse_imports_for_file(path: Path, deleted: bool) -> None:
    """Incrementally update _REVERSE_IMPORTS for a single changed/deleted file.

    Avoids the full-corpus re-read _build_reverse_imports() would otherwise do
    on every watcher event — this only touches the one file that changed, using
    _REVERSE_IMPORTS_FWD to find (and drop) its previous contributions in O(1)
    per target instead of scanning every value-set in _REVERSE_IMPORTS.
    """
    global _REVERSE_IMPORTS, _REVERSE_IMPORTS_FWD
    if path.suffix not in _REVERSE_IMPORT_EXTS:
        return
    try:
        rel_src = str(path.relative_to(WORKSPACE_ROOT))
    except ValueError:
        return

    # Drop this file's previous contributions before recomputing.
    old_targets = _REVERSE_IMPORTS_FWD.pop(rel_src, None)
    if old_targets:
        for target in old_targets:
            importers = _REVERSE_IMPORTS.get(target)
            if importers and rel_src in importers:
                remaining = [r for r in importers if r != rel_src]
                if remaining:
                    _REVERSE_IMPORTS[target] = remaining
                else:
                    _REVERSE_IMPORTS.pop(target, None)

    if deleted:
        _sync_reverse_imports_disk(rel_src, None)
        return

    try:
        raw = path.read_bytes()
        text = raw[:_IMPORT_READ_BYTES].decode('utf-8', 'replace') if len(raw) > _IMPORT_READ_BYTES else raw.decode('utf-8', 'replace')
    except OSError:
        _sync_reverse_imports_disk(rel_src, None)
        return
    entries = _parse_import_entries(path, text)
    if not entries:
        _sync_reverse_imports_disk(rel_src, None)
        return
    new_targets: set[str] = set()
    for spec, _ in entries:
        try:
            kind, resolved = _resolve_import(spec, path)
        except Exception:
            continue
        if kind == 'workspace':
            new_targets.add(resolved)
            importers = _REVERSE_IMPORTS.setdefault(resolved, [])
            if rel_src not in importers:
                importers.append(rel_src)
                importers.sort()
    if new_targets:
        _REVERSE_IMPORTS_FWD[rel_src] = new_targets
    _sync_reverse_imports_disk(rel_src, new_targets)


def _sync_reverse_imports_disk(rel_src: str, targets: "Optional[set[str]]") -> None:
    """Mirror one file's reverse-import edges into the disk cache after an
    incremental update, so a later CLI process picks up the change via
    _load_reverse_imports_from_disk() instead of a stale full-repo rebuild."""
    if _DISK_CACHE is None:
        return
    try:
        _DISK_CACHE.execute("DELETE FROM reverse_imports WHERE src=?", (rel_src,))
        if targets:
            _DISK_CACHE.executemany(
                "INSERT OR IGNORE INTO reverse_imports VALUES (?,?)",
                [(rel_src, t) for t in targets],
            )
        _DISK_CACHE.commit()
    except Exception:
        pass


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
    pat = re.compile(r'\b' + re.escape(symbol_name) + r'\b')

    # Fast path: byte offsets captured during blueprint build — skip tree-sitter re-parse
    cached_offsets = _OFFSET_CACHE.get(str(path))
    if cached_offsets:
        for sig, (start, end) in cached_offsets.items():
            if pat.search(sig):
                try:
                    raw = path.read_bytes()
                    return raw[start:end].decode('utf-8', 'replace')
                except OSError:
                    break  # fall through to full parse below

    try:
        raw = path.read_bytes()
        text = raw.decode('utf-8', 'replace')
    except OSError:
        return None

    suffix = path.suffix
    ts_lang, _ = EXT_LANG.get(suffix, (None, None))

    # --- Tree-sitter path ---
    if TREE_SITTER_OK and ts_lang:
        try:
            parser = _get_ts_parser(ts_lang)
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


_SYMBOL_NAME_STOPWORDS = frozenset({
    # C#/Java/TS modifiers and primitive types that can appear after keyword-stripping
    'async', 'await', 'static', 'readonly', 'string', 'int', 'bool', 'void',
    'Task', 'List', 'IEnumerable', 'IList', 'object', 'var', 'new',
    'override', 'virtual', 'abstract', 'sealed', 'partial', 'const',
    'event', 'delegate', 'extern', 'unsafe',
    # Python
    'def', 'cls', 'self',
})

def _symbol_name_from_sig(sig: str) -> Optional[str]:
    """Extract bare symbol name from a blueprint signature line.

    For callables, the name is the last identifier before '(' — this handles
    multi-keyword prefixes like 'public async Task MethodName(...)' correctly.
    For fields/types, fall back to stripping known modifiers.
    """
    sig = sig.strip()
    paren = sig.find('(')
    if paren > 0:
        # Method/ctor/function: find last identifier immediately before the paren
        m = re.search(r'([A-Za-z_]\w*)$', sig[:paren])
        if m:
            return m.group(1)
    # Field, property, type declaration: strip leading modifiers then grab first ident
    for kw in _BLUEPRINT_TOPLEVEL_KWS:
        if sig.startswith(kw):
            sig = sig[len(kw):]
    m = re.match(r'([A-Za-z_]\w*)', sig)
    name = m.group(1) if m else None
    return name if name and name not in _SYMBOL_NAME_STOPWORDS else None


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
            global _FILE_LIST, _FILE_LIST_TS, _ARCHITECTURE_MAP
            if event.is_directory:
                # Directory added/removed — force a file-list re-walk
                _FILE_LIST = []
                _FILE_LIST_TS = 0.0
                _ARCHITECTURE_MAP = ""
                return
            p = Path(getattr(event, 'dest_path', event.src_path))
            if p.suffix in EXT_LANG and not _is_blacklisted(p):
                deleted = event.event_type == 'deleted'
                _smart_invalidate(p)
                _ARCHITECTURE_MAP = ""
                # Update only this file's reverse-import contributions — O(1) per
                # target instead of _build_reverse_imports()'s full-corpus re-read.
                _update_reverse_imports_for_file(p, deleted)

    try:
        obs = _WatchdogObserver()
        obs.schedule(_Handler(), str(WORKSPACE_ROOT), recursive=True)
        obs.daemon = True
        obs.start()
        _FILE_WATCHER_ACTIVE = True
        return True
    except Exception:
        return False


def _flush_warmup_blueprints() -> None:
    """Batch-write all in-memory blueprints to disk in one transaction.

    Called after the parallel warmup parse completes. Writing N×8280 individual
    commits from worker threads causes SQLite write contention and N fsyncs.
    A single executemany + commit writes everything atomically with one fsync.
    """
    if _DISK_CACHE is None:
        return
    try:
        rows = [
            (path_str, mtime, size, blueprint)
            for path_str, (mtime, size, blueprint) in list(_CACHE.items())
        ]
        _DISK_CACHE.executemany(
            "INSERT OR REPLACE INTO blueprints VALUES (?,?,?,?)",
            rows,
        )
        _DISK_CACHE.commit()
    except Exception:
        pass


def _warm_cache() -> None:
    """Parse all source files in parallel so the first search is fast."""
    global _WARMUP_COMPLETE, _WARMUP_IN_PROGRESS
    files = _iter_source_files()
    workers = min(8, os.cpu_count() or 4)
    _WARMUP_IN_PROGRESS = True
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in as_completed(ex.submit(_build_blueprint, p) for p in files):
            pass
    _WARMUP_IN_PROGRESS = False
    _flush_warmup_blueprints()   # single batch write — no per-file fsyncs
    _build_cs_ns_index()
    _build_java_class_index()
    _build_symbol_index()
    _build_token_df()          # document frequencies for IDF-weighted ranking
    _build_fts_index()         # FTS5 BM25 table for semantic_search
    _build_path_strings()      # pre-compute (rel, stem_lower, dir_lower) for scope_task path loop
    _build_reverse_imports()
    _build_architecture_map()
    _WARMUP_COMPLETE = True
    _git_recency_scores()      # pre-warm git cache so first scope_task call isn't slow


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


# ---------------------------------------------------------------------------
# Exception / log message string-literal extraction
#
# Blueprints strip bodies by design, which makes error/log message text
# invisible to scope_task and semantic_search — a ticket quoting
# "Maximum retry attempts exceeded" can't find the file that throws it.
# We selectively surface string-literal arguments of throw/raise statements
# and known logging calls as an indented "#strings" section appended to the
# blueprint. Deliberately NOT all string literals: that would pollute the
# index with UI text, SQL, and format noise.
# ---------------------------------------------------------------------------
# AST node kinds that can carry an indexable message, per tree-sitter language.
# Creation kinds are included independently of throw statements because real
# codebases wrap exceptions instead of throwing them at the construction site
# (e.g. Result.ErrorResult(new Exception("Maximum retry attempts exceeded."))).
_LIT_NODE_KINDS: dict[str, frozenset[str]] = {
    "csharp":     frozenset({"throw_statement", "object_creation_expression",
                             "invocation_expression"}),
    "python":     frozenset({"raise_statement", "call"}),
    "javascript": frozenset({"throw_statement", "new_expression", "call_expression"}),
    "typescript": frozenset({"throw_statement", "new_expression", "call_expression"}),
    "tsx":        frozenset({"throw_statement", "new_expression", "call_expression"}),
    "java":       frozenset({"throw_statement", "object_creation_expression",
                             "method_invocation"}),
}
_LIT_THROW_KINDS = frozenset({"throw_statement", "raise_statement"})
_LIT_NEW_KINDS = frozenset({"object_creation_expression", "new_expression"})
# The expression inside a throw/raise that carries the constructor + message
_LIT_CREATION_KINDS = frozenset({
    "object_creation_expression", "new_expression", "call",
    "invocation_expression", "call_expression",
})
# Constructed-type / called-function suffixes treated as exception constructors
_LIT_EXC_SUFFIXES = ("Exception", "Error", "Fault", "Failure")
_LIT_STRING_KINDS = frozenset({
    "string_literal", "string", "template_string",
    "interpolated_string_expression", "verbatim_string_literal",
    "raw_string_literal",
})
# Text-content children of a string node. Joining only these skips
# interpolation holes ({x} / ${x}) in C# $-strings and JS template strings.
_LIT_CONTENT_KINDS = frozenset({
    "string_literal_content", "string_content", "string_fragment",
})
_LIT_ARG_KINDS = frozenset({"argument_list", "arguments"})
# Simple callee names (lowercased) recognised as logging calls. Matched
# against the last identifier of the callee chain (logger.LogWarning → logwarning).
_LOG_METHOD_NAMES = frozenset({
    "logwarning", "logerror", "loginformation", "logcritical", "logdebug",
    "logtrace", "logfatal", "logexception",
    "warn", "warning", "error", "exception", "critical", "fatal",
    "info", "information", "debug", "trace",
    "log_warning", "log_error", "log_info", "log_debug", "log_exception",
    "log_critical",
})
_LIT_MIN_LEN = 10        # message must be at least this long...
_LIT_MAX_LEN = 120       # ...and is truncated to this
_LIT_MAX_PER_FILE = 40   # bound blueprint growth on log-heavy files


def _lit_first_string(node) -> object | None:
    """Depth-first search for the first string-typed descendant (document order)."""
    for i in range(node.child_count()):
        c = node.child(i)
        if c.kind() in _LIT_STRING_KINDS:
            return c
        r = _lit_first_string(c)
        if r is not None:
            return r
    return None


def _lit_string_text(string_node, src: bytes) -> str:
    """Literal text of a string node, interpolation holes skipped, whitespace collapsed."""
    parts: list[str] = []

    def collect(n) -> None:
        if n.kind() in _LIT_CONTENT_KINDS:
            parts.append(src[n.start_byte():n.end_byte()].decode("utf-8", "replace"))
            return
        for i in range(n.child_count()):
            collect(n.child(i))

    collect(string_node)
    return " ".join(" ".join(parts).split())


def _lit_args_child(node) -> object | None:
    for i in range(node.child_count()):
        if node.child(i).kind() in _LIT_ARG_KINDS:
            return node.child(i)
    return None


def _lit_callee_name(node, args, src: bytes) -> str:
    """Last identifier before the argument list: logger.LogWarning → LogWarning."""
    head = src[node.start_byte():args.start_byte()].decode("utf-8", "replace")
    toks = re.findall(r'\w+', head)
    return toks[-1] if toks else ""


def _literal_row(node, src: bytes) -> Optional[str]:
    """Signature-style row for a throw/raise/log node, or None if not indexable."""
    try:
        kind = node.kind()
        if kind in _LIT_THROW_KINDS:
            inner = None
            for i in range(node.child_count()):
                if node.child(i).kind() in _LIT_CREATION_KINDS:
                    inner = node.child(i)
                    break
            if inner is None:
                return None
            args = _lit_args_child(inner)
            if args is None:
                return None
            callee = _lit_callee_name(inner, args, src)
            word = "raise" if kind == "raise_statement" else "throw"
        elif kind in _LIT_NEW_KINDS:
            # new SomeException("...") anywhere — thrown, returned, or wrapped
            args = _lit_args_child(node)
            if args is None:
                return None
            callee = _lit_callee_name(node, args, src)
            if not callee.endswith(_LIT_EXC_SUFFIXES):
                return None
            word = "new"
        else:
            args = _lit_args_child(node)
            if args is None:
                return None
            callee = _lit_callee_name(node, args, src)
            if not callee:
                return None
            # Logging call (logger.LogWarning / log.warn / console.error) or an
            # exception constructed via plain call syntax (Python ValueError(...)).
            if callee.lower() in _LOG_METHOD_NAMES:
                word = ""
            elif callee.endswith(_LIT_EXC_SUFFIXES):
                word = "new"
            else:
                return None
        string_node = _lit_first_string(args)
        if string_node is None:
            return None
        text = _lit_string_text(string_node, src)
        # Multi-word natural-language messages only — a single-word literal is
        # either an identifier (indexed elsewhere) or noise.
        if len(text) < _LIT_MIN_LEN or " " not in text:
            return None
        text = text[:_LIT_MAX_LEN]
        return f'{word} {callee}("{text}")'.lstrip() if callee else None
    except Exception:
        return None


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
        parser = _get_ts_parser(ts_lang)
        tree = parser.parse(src.decode("utf-8", "replace"))
    except Exception:  # grammar missing/broken -> let caller use regex
        return None

    lines: list[str] = []
    seen_line_nos: set[int] = set()
    path_key = str(path)
    offsets: dict[str, tuple[int, int]] = {}
    lit_kinds = _LIT_NODE_KINDS.get(ts_lang)
    lit_lines: list[str] = []
    seen_lit_line_nos: set[int] = set()

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
                    # Capture byte offsets so get_symbol can skip re-parsing
                    offsets[sig] = (node.start_byte(), node.end_byte())
                child_depth = depth + 1
        elif (lit_kinds is not None
              and len(lit_lines) < _LIT_MAX_PER_FILE
              and node.kind() in lit_kinds):
            row = _literal_row(node, src)
            if row:
                line_no = node.start_position().row + 1
                if line_no not in seen_lit_line_nos:
                    seen_lit_line_nos.add(line_no)
                    # Indented so _toplevel_names_from_blueprint never treats a
                    # literal as a top-level symbol in get_architecture output.
                    lit_lines.append(f"L{line_no:<5}  {row}")
        for i in range(node.child_count()):
            walk(node.child(i), child_depth)

    walk(tree.root_node(), 0)
    _OFFSET_CACHE[path_key] = offsets
    body = "\n".join(lines)
    if lit_lines:
        body = (body + "\n" if body else "") + "#strings\n" + "\n".join(lit_lines)
    return body


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


_NOTES_MAX_SHOWN = 3
_NOTE_MAX_CHARS = 200


def _notes_for_path(rel_path: str, weights: dict[str, list[str]] | None = None) -> list[str]:
    """Display-ready note lines for a workspace-relative path.

    Substring match, longest-prefix-first — same convention as _FOCUS_WEIGHTS.
    Unlike focus weights (which break at the first match since multipliers
    can't meaningfully combine), notes are additive context: ALL matching
    prefixes' notes are shown, just capped at _NOTES_MAX_SHOWN so one
    broad-prefix note doesn't drown every query in text.

    `weights` defaults to the module-level _MIMIRNOTES; the parameter exists
    purely so tests can pass a fabricated dict without monkeypatching.
    """
    source = weights if weights is not None else _MIMIRNOTES
    if not source:
        return []
    rel_lc = rel_path.replace("\\", "/").lower()
    matched = [(p, n) for p in sorted(source, key=len, reverse=True)
               if p in rel_lc for n in source[p]]
    if not matched:
        return []
    shown = matched[:_NOTES_MAX_SHOWN]
    lines = [f"note: {n if len(n) <= _NOTE_MAX_CHARS else n[:_NOTE_MAX_CHARS] + '…'}"
             for _, n in shown]
    if len(matched) > len(shown):
        lines.append(f"note: (+{len(matched) - len(shown)} more — see .mimirnotes)")
    return lines


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
            name_lc = name.lower()
            hits = [
                (f, l, c) for f, l, c in rows
                if (word_re.search(c) or name_lc in c.lower()) and _in_scope(f)
            ]
            return hits[:max_results]
        except Exception:
            pass  # fall through to linear scan

    word_def = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
    name_lc = name.lower()
    hits: list[tuple[str, str, str]] = []
    for path in _iter_source_files():
        if len(hits) >= max_results:
            break
        if not _in_scope(str(path.relative_to(WORKSPACE_ROOT))):
            continue
        cached = _cache_get(path)
        if cached is not None:
            if name not in cached and name_lc not in cached.lower():
                continue
            blueprint = cached
        else:
            try:
                raw = path.read_bytes()
                if name.encode() not in raw and name_lc.encode() not in raw.lower():
                    continue
            except OSError:
                continue
            blueprint = _build_blueprint(path)
        rel = str(path.relative_to(WORKSPACE_ROOT))
        for bl in blueprint.splitlines():
            if bl.startswith("#"):
                continue
            if not (word_def.search(bl) or name_lc in bl.lower()):
                continue
            m = re.match(r"L(\d+)\s*(.*)", bl.strip())
            if m:
                hits.append((rel, m.group(1), m.group(2).strip()))
                if len(hits) >= max_results:
                    break
    return hits


def _symbol_hits_multi(
    names: list[str], max_per_kw: int | dict[str, int] = 10
) -> dict[str, list[tuple[str, str, str]]]:
    """Multi-keyword search across blueprints.

    Uses the SQLite inverted index when available (fast path), otherwise
    falls back to a single-pass linear scan through the blueprint cache.
    Returns {keyword: [(rel_path, line_no, sig), ...]} for every keyword.

    max_per_kw may be a single int (uniform cap) or a {keyword: cap} dict for
    a per-keyword cap — callers use this to fetch deep on rare/high-IDF
    keywords and shallow on common ones, since common keywords saturate the
    scope_task scoring cap after a handful of hits regardless of how many
    more rows are fetched.
    """
    def _cap(name: str) -> int:
        return max_per_kw.get(name, 10) if isinstance(max_per_kw, dict) else max_per_kw

    if _FTS_READY and _DISK_CACHE is not None:
        try:
            hits: dict[str, list[tuple[str, str, str]]] = {n: [] for n in names}
            for name in names:
                cap = _cap(name)
                word_re = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
                rows = _DISK_CACHE.execute(
                    "SELECT l.file, l.lineno, l.context"
                    " FROM symbols s JOIN lines l ON s.file=l.file AND s.lineno=l.lineno"
                    " WHERE s.token=? LIMIT ?",
                    (name, cap * 4),
                ).fetchall()
                name_lc = name.lower()
                for f, l, c in rows:
                    if (word_re.search(c) or name_lc in c.lower()) and _in_scope(f):
                        hits[name].append((f, l, c))
                        if len(hits[name]) >= cap:
                            break
            return hits
        except Exception:
            pass  # fall through to linear scan

    patterns = {n: re.compile(rf"(?<![\w]){re.escape(n)}(?![\w])") for n in names}
    names_lc = {n: n.lower() for n in names}
    caps = {n: _cap(n) for n in names}
    hits = {n: [] for n in names}
    saturated: set[str] = set()

    for path in _iter_source_files():
        if len(saturated) == len(names):
            break
        rel = str(path.relative_to(WORKSPACE_ROOT))
        if not _in_scope(rel):
            continue
        cached = _cache_get(path)
        blueprint = cached if cached is not None else _build_blueprint(path)
        blueprint_lc = blueprint.lower()

        for name in names:
            if name in saturated:
                continue
            nlc = names_lc[name]
            if name not in blueprint and nlc not in blueprint_lc:
                continue
            pat = patterns[name]
            for bl in blueprint.splitlines():
                if bl.startswith("#"):
                    continue
                bl_lc = bl.lower()
                if not (pat.search(bl) or nlc in bl_lc):
                    continue
                m = re.match(r"L(\d+)\s*(.*)", bl.strip())
                if m:
                    hits[name].append((rel, m.group(1), m.group(2).strip()))
                    if len(hits[name]) >= caps[name]:
                        saturated.add(name)
                        break

    return hits


def _byte_keyword_freq(path: Path, keywords: list[str]) -> int:
    """Count total raw occurrences of all keywords in a file's bytes.

    Used as a tiebreaker when files share the same scope_task score: a file
    where search terms appear many times in implementation code ranks above one
    where they only appear in a definition line. Byte scan only — no content
    returned, no extra tokens.
    """
    try:
        raw = path.read_bytes().lower()
        return sum(raw.count(kw.lower().encode()) for kw in keywords)
    except OSError:
        return 0


def _git_recency_scores() -> dict[str, int]:
    """Return a recency score per workspace-relative file from recent git log.

    Most recently touched file = 40, next = 39, etc. Cached for 60 s.
    Returns {} silently on any failure (non-git dir, git not installed, etc.).
    """
    now = time.monotonic()
    if now - _GIT_RECENCY_CACHE["ts"] < 60:
        return _GIT_RECENCY_CACHE["scores"]
    try:
        out = subprocess.check_output(
            ["git", "log", "--format=", "--name-only", "-n", "40"],
            cwd=str(WORKSPACE_ROOT), text=True, timeout=3,
            stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        seen: dict[str, int] = {}
        rank = 40
        for line in out.splitlines():
            line = line.strip()
            if line and line not in seen:
                seen[line] = rank
                rank -= 1
                if rank <= 0:
                    break
        _GIT_RECENCY_CACHE.update({"ts": now, "scores": seen})
        return seen
    except Exception:
        _GIT_RECENCY_CACHE["ts"] = now  # avoid hammering on repeated failure
        return {}


def _extract_scope_keywords(task: str) -> list[str]:
    """Pull candidate symbol names out of a plain-English task description."""
    seen: set[str] = set()
    out: list[str] = []

    # Multi-hump identifiers (PushNotificationSenderServiceWorker, doWorkAsync)
    # collected FIRST, ahead of plain capitalized prose words. Tickets/task
    # descriptions often lead with several sentences of prose before an
    # embedded code snippet or stack trace — without this priority pass, plain
    # sentence-initial capitalized words ("Investigation", "The", "Observation")
    # fill the keyword cap below before the scan ever reaches the real
    # identifiers later in the text. Requires a lowercase/digit run between
    # capitals (not just 2+ capitals) so ALL-CAPS acronyms like "DB", "AWS",
    # "APK", "SDP" — common in ticket metadata headers ("DB 8.24, AWS 8.2.1,
    # APK 8.0.28...") — don't get mistaken for real identifiers; those have no
    # lowercase letters at all, unlike genuine CamelCase.
    for m in re.finditer(r'\b[A-Za-z][a-z0-9]+[A-Z][a-zA-Z0-9]*\b', task):
        w = m.group()
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)

    # CamelCase (single hump) — almost certainly code symbols. Skips short
    # ALL-CAPS-only tokens (DB, AWS, APK, SDP...) — these are virtually always
    # version/client codes from a ticket's metadata header ("DB 8.24, AWS
    # 8.2.1, APK 8.0.28..."), never real identifiers, and since truncation to
    # the cap only happens once at the very end (not per-pass), a header full
    # of them would otherwise consume the entire keyword budget before the
    # scan ever reaches real content words later in this same pass.
    for m in re.finditer(r'\b[A-Z][a-zA-Z0-9]+\b', task):
        w = m.group()
        if w.isupper() and len(w) <= 4:
            continue
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
        # Common English connectives/negation — near-zero path-search value,
        # but short enough to coincidentally substring-match real identifiers
        # (e.g. "not" inside "Notification"). Excluding them costs nothing
        # under substring matching (they were never real signal) and avoids
        # spurious candidates now that path matching is token-indexed.
        "not", "are", "was", "has", "had", "can", "may", "but", "all", "any",
        "out", "off", "yet", "too", "own",
    })
    words = re.findall(r'[a-zA-Z]{3,}', task.lower())
    # Split CamelCase terms into sub-components (min 4 chars to avoid noise)
    for camel in re.findall(r'\b[A-Z][a-zA-Z]{3,}\b', task):
        for part in re.findall(r'[A-Z][a-z0-9]+', camel):
            if len(part) >= 6:  # 6-char floor keeps "filter"/"refresh" but drops "stop"/"auto"/"start"
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

    elif suffix == '.java':
        for m in re.finditer(r'^import\s+(?:static\s+)?([\w.]+)\s*;', text, re.MULTILINE):
            spec = m.group(1)
            # Strip static member suffix: com.foo.Bar.METHOD → com.foo.Bar
            # Keep only if the last component starts with uppercase (class name)
            parts = spec.split('.')
            # Walk back to find the class boundary (first uppercase component)
            for i, part in enumerate(parts):
                if part and part[0].isupper():
                    entries.append(('.'.join(parts[:i + 1]), ''))
                    break
            else:
                entries.append((spec, ''))

    elif suffix in ('.kt', '.kts'):
        for m in re.finditer(r'^import\s+([\w.*]+)', text, re.MULTILINE):
            spec = m.group(1)
            # Same class-boundary trimming for Kotlin
            parts = spec.split('.')
            for i, part in enumerate(parts):
                if part and part[0].isupper():
                    entries.append(('.'.join(parts[:i + 1]), ''))
                    break
            else:
                entries.append((spec, ''))

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

_JAVA_CLASS_INDEX: "dict[str, list[str]]" = {}  # ClassName → [rel_path, ...]  (multiple files may share a name)
_JAVA_CLASS_INDEX_READY = False


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


def _build_java_class_index() -> None:
    """Build ClassName → [rel_path] index for all .java and .kt/.kts files.

    Called at the end of _warm_cache(). The file stem is the primary class name
    in Java/Kotlin convention, so DialogBuilder.java → {'DialogBuilder': [rel]}.
    Multiple files may share a name (rare but possible), so we store a list.
    """
    global _JAVA_CLASS_INDEX, _JAVA_CLASS_INDEX_READY
    index: dict[str, list[str]] = {}
    for path in _iter_source_files():
        if path.suffix not in ('.java', '.kt', '.kts'):
            continue
        rel = str(path.relative_to(WORKSPACE_ROOT))
        index.setdefault(path.stem, []).append(rel)
    _JAVA_CLASS_INDEX = index
    _JAVA_CLASS_INDEX_READY = True


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


@functools.lru_cache(maxsize=8192)
def _decompose_identifier(token: str) -> tuple[str, ...]:
    """Split a compound identifier into lowercase sub-tokens of ≥ 4 chars.

    Handles PascalCase, camelCase, snake_case, and mixed forms.
    Examples:
        StartOrStopAutoRefresh  → ('start', 'stop', 'auto', 'refresh')
        _refreshIntervalInSeconds → ('refresh', 'interval', 'seconds')
        MyCurrentViewModel      → ('current', 'view', 'model')

    Returns a tuple (not list) so the result is hashable and the lru_cache
    can store it — a single decomposed call per unique identifier per process.
    """
    parts = [p for p in token.strip('_').split('_') if p]
    sub: list[str] = []
    for part in parts:
        for component in (_CAMEL_SPLIT_RE.findall(part) or [part]):
            if len(component) >= 4:
                sub.append(component.lower())
    return tuple(sub)


def _fts_rows_for_blueprint(
    rel: str, blueprint: str
) -> list[tuple[str, str, str, str, str]]:
    """Produce (file, lineno, symbol_name, signature, decomposed) rows for symbol_fts.

    One row per unique definition line. symbol_name is the longest non-stopword
    identifier on the line. decomposed is a space-joined set of all sub-tokens
    derived from CamelCase/snake_case decomposition of every identifier — this
    is the primary FTS5 search surface for natural-language queries.
    """
    rows: list[tuple[str, str, str, str, str]] = []
    seen_lines: set[str] = set()
    for line in blueprint.splitlines():
        if not line or line.startswith('#'):
            continue
        m = re.match(r'L(\d+)\s*(.*)', line.strip())
        if not m:
            continue
        lineno = m.group(1)
        if lineno in seen_lines:
            continue
        seen_lines.add(lineno)
        signature = m.group(2).strip()
        if not signature:
            continue
        toks = re.findall(r'\w+', signature)
        candidates = [t for t in toks if t not in _SYMBOL_STOPWORDS and len(t) >= 2]
        if not candidates:
            continue
        symbol_name = max(candidates, key=len)
        decomp: set[str] = set()
        for tok in candidates:
            decomp.add(tok.lower())
            for sub in _decompose_identifier(tok):
                decomp.add(sub)

        # Explicit enrichment: return type, parameter type hints, decorator names.
        # The generic re.findall above already captures these tokens, but explicit
        # extraction ensures compound type names are always decomposed and that
        # future changes to the candidate filter don't accidentally drop them.
        ret_match = re.search(r'->\s*([\w\[\], |]+)', signature)
        if ret_match:
            for tok in re.findall(r'\w+', ret_match.group(1)):
                if len(tok) >= 4 and tok not in _SYMBOL_STOPWORDS:
                    decomp.add(tok.lower())
                    for sub in _decompose_identifier(tok):
                        decomp.add(sub)

        for tok in re.findall(r':\s*([A-Za-z_]\w+)', signature):
            if len(tok) >= 4 and tok not in _SYMBOL_STOPWORDS:
                decomp.add(tok.lower())
                for sub in _decompose_identifier(tok):
                    decomp.add(sub)

        if signature.lstrip().startswith('@'):
            deco_m = re.match(r'@([\w.]+)', signature.lstrip())
            if deco_m:
                for tok in deco_m.group(1).split('.'):
                    if len(tok) >= 4 and tok not in _SYMBOL_STOPWORDS:
                        decomp.add(tok.lower())
                        for sub in _decompose_identifier(tok):
                            decomp.add(sub)

        decomposed = ' '.join(sorted(decomp))
        rows.append((rel, lineno, symbol_name, signature, decomposed))
    return rows


def _index_blueprint_rows(rel: str, blueprint: str) -> list[tuple[str, str, str]]:
    """Extract (token, file, lineno) rows for the symbols table from a blueprint.

    Indexes both whole identifiers and their sub-components (from CamelCase /
    snake_case decomposition) so that a search for 'refresh' can find a file
    containing 'StartOrStopAutoRefresh' or '_refreshIntervalInSeconds'.
    Stopwords and tokens shorter than 2 chars are excluded.
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
            # Whole token
            if len(tok) >= 2 and tok not in _SYMBOL_STOPWORDS and tok not in seen:
                seen.add(tok)
                rows.append((tok, rel, lineno))
            # Sub-tokens from compound identifiers (≥ 4 chars each)
            for sub in _decompose_identifier(tok):
                if sub not in _SYMBOL_STOPWORDS and sub not in seen:
                    seen.add(sub)
                    rows.append((sub, rel, lineno))
    return rows


def _path_component_tokens(original: str) -> set[str]:
    """Tokenize a filename stem or directory path into lowercase search tokens.

    Splits on non-alphanumeric boundaries (path separators, dashes, dots,
    underscores) and CamelCase humps — same splitting logic as
    _decompose_identifier, but with a 2-char floor instead of 4, since path
    components are often short (vm, ui, api) and still meaningful for
    path matching. Must run on the ORIGINAL-case string; CamelCase boundaries
    are invisible once lowercased.
    """
    tokens: set[str] = set()
    for word in _PATH_WORD_RE.findall(original):
        tokens.add(word.lower())
        for part in (_CAMEL_SPLIT_RE.findall(word) or ()):
            if len(part) >= 2:
                tokens.add(part.lower())
    return tokens


def _build_path_strings() -> None:
    """Pre-compute (rel, stem_lower, dir_lower) for every source file, plus
    an inverted token index (_PATH_TOKEN_INDEX) for candidate narrowing.

    Avoids repeated Path.relative_to() / .stem calls in the scope_task path loop,
    reducing that loop from ~47ms to ~3ms on 2k-file repos. The token index
    additionally lets that loop skip files with no chance of matching at all
    on large repos, rather than substring-checking every file.
    """
    global _PATH_STRINGS, _PATH_STRINGS_TS, _PATH_TOKEN_INDEX
    result: list[tuple[str, str, str]] = []
    token_index: dict[str, list[int]] = {}
    for idx, src_path in enumerate(_iter_source_files()):
        rel = str(src_path.relative_to(WORKSPACE_ROOT))
        stem_lc = src_path.stem.lower()
        rel_dir = src_path.parent.relative_to(WORKSPACE_ROOT)
        dir_lc = str(rel_dir).replace("\\", "/").lower()
        result.append((rel, stem_lc, dir_lc))
        tokens = _path_component_tokens(src_path.stem) | _path_component_tokens(str(rel_dir))
        for tok in tokens:
            token_index.setdefault(tok, []).append(idx)
    _PATH_STRINGS = result
    _PATH_TOKEN_INDEX = token_index
    _PATH_STRINGS_TS = time.monotonic()


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


def _build_token_df() -> None:
    """Build the token document-frequency cache for IDF-weighted ranking.

    One GROUP BY over the symbols table (off idx_symbols_token) at warmup.
    Tokens are merged case-insensitively taking the max df — the table stores
    whole tokens verbatim plus lowercase sub-tokens, and for discounting a
    common word the larger count is the honest one.
    """
    global _TOKEN_DF, _TOKEN_DF_N, _TOKEN_DF_FLOOR, _IDF_REF, _DF_DIRTY
    if _DISK_CACHE is None:
        return
    try:
        n = _DISK_CACHE.execute(
            "SELECT COUNT(DISTINCT file) FROM symbols"
        ).fetchone()[0]
        if not n:
            return
        floor = max(4, n // 1000)
        rows = _DISK_CACHE.execute(
            "SELECT token, COUNT(DISTINCT file) AS fc FROM symbols"
            " GROUP BY token HAVING fc >= ?",
            (floor,),
        ).fetchall()
        df: dict[str, int] = {}
        for tok, fc in rows:
            k = tok.lower()
            if fc > df.get(k, 0):
                df[k] = fc
        # Reference point: a token present in 2% of files weighs 1.0
        ref_df = max(1.0, n / 50.0)
        idf_ref = math.log(1 + (n - ref_df + 0.5) / (ref_df + 0.5))
        _TOKEN_DF, _TOKEN_DF_N = df, n
        _TOKEN_DF_FLOOR, _IDF_REF = floor, max(idf_ref, 1e-9)
        # Blueprint line counts for BM25 document-length normalization: a
        # god-file (EF DbContext, generated registry) mentions every entity
        # and racks up weak matches on any query — its score is divided down;
        # a small file specifically about the queried concept is boosted.
        global _FILE_NLINES, _AVG_NLINES
        try:
            nl = dict(_DISK_CACHE.execute(
                "SELECT file, COUNT(*) FROM lines GROUP BY file"
            ).fetchall())
            if nl:
                _FILE_NLINES = nl
                _AVG_NLINES = sum(nl.values()) / len(nl)
        except Exception:
            pass  # length norm is optional; _doc_len_norm returns 1.0 without it
        _DF_DIRTY = 0
    except Exception:
        pass


def _doc_len_norm(rel: str) -> float:
    """BM25-style length normalization factor for a file, clamped to [0.25, 1.3].

    norm = 1 / (1 - b + b * len/avglen) with b = 0.4 — gentle enough that a
    legitimately large ViewModel keeps its rank, strong enough that a DbContext
    with 10× the average blueprint lines loses its everything-matches edge.
    """
    if not _AVG_NLINES:
        return 1.0
    n = _FILE_NLINES.get(rel)
    if not n:
        return 1.0
    w = 1.0 / (0.6 + 0.4 * (n / _AVG_NLINES))
    return 0.25 if w < 0.25 else (1.3 if w > 1.3 else w)


def _idf_weight(kw: str) -> float:
    """Normalized BM25 IDF for a query keyword, clamped to [0.05, 2.0].

    Rare identifiers (df below the storage floor) weigh ~2.0; a word present
    in 2% of files weighs 1.0; ubiquitous vocabulary ("Types", "Get", project
    prefixes) bottoms out at 0.05. Returns 1.0 before the DF cache is built,
    which is exactly the pre-IDF scoring behavior.
    """
    n = _TOKEN_DF_N
    if not n:
        return 1.0
    df = _TOKEN_DF.get(kw.lower(), max(1, _TOKEN_DF_FLOOR // 2))
    idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
    w = idf / _IDF_REF
    return 0.05 if w < 0.05 else (2.0 if w > 2.0 else w)


def _df_note_change() -> None:
    """Track index churn; refresh the DF cache on a daemon thread when stale.

    DF is a corpus-wide statistic — a few dozen changed files can't move it
    meaningfully, so queries never block on a rebuild.
    """
    global _DF_DIRTY, _DF_REBUILDING
    _DF_DIRTY += 1
    if (_TOKEN_DF_N and not _DF_REBUILDING
            and _DF_DIRTY > max(50, _TOKEN_DF_N // 50)):
        _DF_REBUILDING = True

        def _run() -> None:
            global _DF_REBUILDING
            try:
                _build_token_df()
            finally:
                _DF_REBUILDING = False

        threading.Thread(target=_run, daemon=True, name="mimir-df-rebuild").start()


def _build_fts_index() -> None:
    """Populate symbol_fts FTS5 table from all cached blueprints.

    Called from _warm_cache() after _build_symbol_index(). Skips rebuild if
    the table already covers all known source files (warm-restart optimisation).
    Incremental updates happen via _cache_put/_cache_evict for individual files.
    """
    global _SEMANTIC_READY
    if _DISK_CACHE is None:
        return
    try:
        indexed = _DISK_CACHE.execute(
            "SELECT COUNT(DISTINCT file) FROM symbol_fts"
        ).fetchone()[0]
        total = len(_iter_source_files())
        if indexed > 0 and indexed >= total:
            _SEMANTIC_READY = True
            return
    except Exception:
        return  # FTS5 table not present; skip silently

    fts_rows: list[tuple[str, str, str, str, str]] = []
    for path in _iter_source_files():
        cached = _cache_get(path)
        if cached is None:
            continue
        rel = str(path.relative_to(WORKSPACE_ROOT))
        fts_rows.extend(_fts_rows_for_blueprint(rel, cached))

    try:
        _DISK_CACHE.execute("DELETE FROM symbol_fts")
        _DISK_CACHE.executemany(
            "INSERT INTO symbol_fts(file, lineno, symbol_name, signature, decomposed)"
            " VALUES (?,?,?,?,?)",
            fts_rows,
        )
        _DISK_CACHE.commit()
        _SEMANTIC_READY = True
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

    if suffix in ('.java', '.kt', '.kts'):
        # specifier is like "com.foo.bar.ClassName" (class boundary already trimmed by parser)
        # Strategy 1: convert to path suffix com/foo/bar/ClassName.java and match any workspace file
        class_name = specifier.rsplit('.', 1)[-1]
        ext = '.java' if suffix == '.java' else '.kt'
        path_suffix = specifier.replace('.', '/') + ext
        # Walk workspace files looking for any path that ends with the package suffix
        if _JAVA_CLASS_INDEX_READY:
            candidates = _JAVA_CLASS_INDEX.get(class_name, [])
            if candidates:
                # Prefer the file whose path most closely matches the full package path
                best = min(candidates, key=lambda r: (
                    0 if r.replace('\\', '/').endswith(path_suffix) else 1,
                    len(r)
                ))
                return ('workspace', best)
        # Strategy 2: direct filesystem lookup (pre-index fallback)
        for p in WORKSPACE_ROOT.rglob(f'{class_name}{ext}'):
            if p.is_file():
                try:
                    return ('workspace', str(p.relative_to(WORKSPACE_ROOT)))
                except ValueError:
                    pass
        return ('external', specifier)

    if suffix in ('.cs', '.swift'):
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
#
# Functions below are decorated with @_tool (a plain list-collector, see top
# of file) instead of @mcp.tool() — the real FastMCP instance is created and
# these get registered onto it only in main()'s server-start branch, so a CLI
# invocation never imports fastmcp at all.
# --------------------------------------------------------------------------- #


@_tool
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
        blueprint = _build_blueprint(resolved)
    except Exception as e:  # last-resort guard: never break the stdio stream
        return f"Error mapping '{path}': {type(e).__name__}: {e}. Try a smaller file or a line range."
    notes = _notes_for_path(path)
    if notes:
        blueprint += "\n" + "\n".join(f"  {n}" for n in notes)
    return blueprint


@_tool
def verify_symbol_existence(symbol_name: str, max_results: int = 25, path: str = "") -> str:
    """Confirm whether a symbol (class / function / method / struct / etc.) is
    actually DEFINED anywhere in the workspace, and report exactly where, with
    its signature.

    WHEN TO USE: any time you are about to assume a function or type exists -
    before you call it, import it, or claim in an answer that it's defined. This
    returns ground truth from the source tree, so prefer it over guessing or
    grepping raw files yourself.

    Args:
        symbol_name: the identifier to look for. Matched case-insensitively
                     (e.g. "signalr" finds a symbol actually named "SignalR"),
                     but results show the symbol's real casing as defined.
        max_results: cap on matches returned (default 25).
        path: optional workspace-relative path (or path suffix) to restrict
              results to one file, e.g. "src/services/auth.py". Leave blank to
              search the whole workspace.

    Returns 'FOUND' lines with file:line and signature (in the codebase's
    actual casing), or a clear 'NOT FOUND'.
    """
    name = symbol_name.strip()
    if not name or not re.match(r"^\w[\w]*$", name):
        return "Error: pass a single bare identifier, e.g. 'createSession' (no parens, no dots)."
    target = path.strip().replace('\\', '/').lstrip('/')
    try:
        # Over-fetch before filtering so a match in the target file isn't
        # dropped by the global cap when other files also define the name.
        scan_cap = max(max_results, 200) if target else max_results
        raw_hits = _symbol_hits(name, scan_cap)
    except Exception as e:
        return f"Error during symbol scan: {type(e).__name__}: {e}."
    if target:
        raw_hits = [h for h in raw_hits if h[0].replace('\\', '/').endswith(target)]
    raw_hits = raw_hits[:max_results]
    if not raw_hits:
        scope_msg = f" in '{target}'" if target else " in the workspace"
        return (f"NOT FOUND: no definition of '{name}'{scope_msg}. "
                f"It may be undefined, external, or spelled differently.")
    out = "\n".join(f"FOUND  {r}:{l}  ->  {s}" for r, l, s in raw_hits)
    if len(raw_hits) >= max_results:
        out += f"\n... (truncated at {max_results}; refine the name to narrow down)"
    return out


@_tool
def scope_hint(terms: str) -> str:
    """Cheap first-pass symbol lookup — returns what actually exists and suggests a
    focused scope_task query.  Call this when you have rough keywords but are unsure
    of the exact symbol names; the output tells you what the codebase calls things so
    the follow-up scope_task query is precise instead of broad.

    Returns only names, file paths, and a suggested query — no blueprints — so it
    costs very few tokens.

    Args:
        terms: Space-or-comma-separated rough search terms, e.g. "timer refresh current jobs"
    """
    # Extract meaningful keywords — applies the same stopword + length filtering
    # that scope_task uses, so passing a full sentence like "are you sure you want
    # to exit" doesn't flood results with matches on "are", "you", "to".
    meaningful = _extract_scope_keywords(terms)
    # Also keep any raw words ≥ 4 chars not caught by the CamelCase/snake extractor
    # (e.g. plain lowercase words from a sentence like "exit").
    extra = [
        w for w in re.findall(r'[a-zA-Z]{4,}', terms)
        if w.lower() not in _SYMBOL_STOPWORDS
        and w.lower() not in {m.lower() for m in meaningful}
    ]
    raw = list(dict.fromkeys(meaningful + extra))  # deduplicated, order preserved

    if not raw:
        return (
            f"No searchable terms found in: '{terms}'\n"
            "All words were too short or too common. Try using class/method names or "
            "feature-specific terms (e.g. 'exit dialog confirmation')."
        )

    # Expand with sub-tokens so compound identifiers (StartOrStopAutoRefresh → refresh)
    # are also searched.
    expanded: list[str] = []
    seen_exp: set[str] = set()
    for t in raw:
        tl = t.lower()
        if tl not in seen_exp:
            seen_exp.add(tl)
            expanded.append(t)
        for sub in _decompose_identifier(t):
            if sub not in seen_exp:
                seen_exp.add(sub)
                expanded.append(sub)
    expanded = expanded[:24]

    try:
        hits = _symbol_hits_multi(expanded, max_per_kw=8)
    except Exception as e:
        return f"Error during symbol lookup: {e}"

    # Group results by file; track how many distinct search terms hit each file
    # so the ranking favours files matched by multiple terms over single-term hits.
    file_syms: dict[str, list[str]] = {}
    file_term_hits: dict[str, set[str]] = {}   # file → set of terms that hit it
    term_files: dict[str, set[str]] = {t: set() for t in raw}
    # sym → set of original terms that produced it (used for suggestion ranking)
    sym_terms: dict[str, set[str]] = {}

    _name_re = re.compile(r'\b([A-Z][A-Za-z0-9]+|[a-z_][a-zA-Z0-9_]{3,})\s*[({<:\[]')

    for kw, matches in hits.items():
        # Which original terms does this keyword relate to?
        related_orig = {
            orig for orig in raw
            if orig.lower() == kw or orig.lower() in kw or kw in orig.lower()
        }
        for rel, _lineno, sig in matches:
            if rel not in file_syms:
                file_syms[rel] = []
                file_term_hits[rel] = set()
            file_term_hits[rel].update(related_orig)
            m = _name_re.search(sig)
            if m:
                sym = m.group(1)
                if sym not in file_syms[rel]:
                    file_syms[rel].append(sym)
                if sym not in sym_terms:
                    sym_terms[sym] = set()
                sym_terms[sym].update(related_orig)
            for orig in related_orig:
                term_files[orig].add(rel)

    if not file_syms:
        return (
            f"No symbols found for: {', '.join(raw)}\n"
            "Try different terms or check spelling. "
            "Use verify_symbol_existence to confirm a specific name exists."
        )

    lines = [f"# Scope Hint: '{terms}'\n"]
    lines.append(f"Keywords searched: {', '.join(raw)}\n")

    # Per-term breakdown (shows which term hit which file)
    lines.append("## Term matches")
    for orig in raw:
        files = sorted(term_files.get(orig, set()))[:4]
        if files:
            lines.append(f"  '{orig}' → {', '.join(files)}")
        else:
            lines.append(f"  '{orig}' → (no matches)")

    # Top files ranked by IDF-weighted sum of distinct terms that hit them
    # (rare terms count more than ubiquitous ones), then symbol count.
    lines.append("\n## Top files")
    ranked = sorted(
        file_syms,
        key=lambda f: (-sum(_idf_weight(t) for t in file_term_hits.get(f, ())),
                       -len(file_syms[f]))
    )[:8]
    for rel in ranked:
        syms = ", ".join(file_syms[rel][:6])
        n_terms = len(file_term_hits.get(rel, set()))
        lines.append(f"  {rel}  ({n_terms} term{'s' if n_terms != 1 else ''} matched)")
        lines.append(f"    → {syms}")

    # Suggested query: prefer symbols whose name contains a search term as a CamelCase
    # component ("showExitDialog" → ["show","exit","dialog"] → "exit" matches).
    # Using components rather than raw substring prevents "UserWantsToContinue..."
    # from matching "want" just because "Wants" contains it as a substring.
    # Among component-matched symbols, prefer shorter names (more specific).
    # Fall back to any symbol from a multi-term file if no component match found.
    raw_lc = {t.lower() for t in raw}

    def _component_hits(s: str) -> int:
        components = set(_decompose_identifier(s)) | {s.lower()}
        return sum(1 for t in raw_lc if t in components)

    def _sym_score(s: str) -> tuple:
        comp_hits = _component_hits(s)
        n_terms = len(sym_terms.get(s, set()))
        return (0 if comp_hits > 0 else 1, -comp_hits, -n_terms, len(s))

    candidates = [
        s for s in sym_terms
        if len(s) >= 6 and s not in _SYMBOL_STOPWORDS
    ]
    best = sorted(candidates, key=_sym_score)[:4]

    lines.append(f"\n## Suggested scope_task query")
    if best:
        lines.append(f'  scope_task("{" ".join(best)}")')
    else:
        lines.append(f'  scope_task("{" ".join(raw)}")')

    return "\n".join(lines)


def _score_task_files(task: str, focus: str = "") -> tuple[dict[str, float], list[str], list[tuple[str, str, str, str]], str, bool]:
    """Shared scoring core for scope_task and scope_area.

    Expands aliases, extracts keywords, and returns EVERY matched file with its
    relevance score — unranked and untruncated (scope_task sorts and slices to
    max_files; scope_area rolls the full set up into a directory tree).

    Returns (file_hit_count, keywords, all_hits, expanded_task, used_fts).
    """
    expanded = _expand_task_with_aliases(task)
    keywords = _extract_scope_keywords(expanded)
    if not keywords:
        return {}, keywords, [], expanded, False

    file_hit_count: dict[str, float] = {}
    all_hits: list[tuple[str, str, str, str]] = []  # (keyword, rel, line, sig)

    valid_kws = [kw for kw in keywords if re.match(r"^\w[\w]*$", kw)]

    # Compound bigrams: tickets name domain concepts in prose ("Unavailable
    # Types are set against a category...") that the code spells as ONE
    # compound identifier (UnavailableType / UnavailableTypeCategory). For
    # each pair of adjacent capitalized words, also search their concatenation
    # and its singular form. Nonexistent compounds simply return no hits;
    # existing ones are rare tokens (near-max IDF) that anchor the ranking to
    # the files actually about that concept.
    _seen_kw_lc = {k.lower() for k in valid_kws}
    _compounds: list[str] = []
    for m in re.finditer(r'\b([A-Z][a-zA-Z0-9]+)\s+([A-Z][a-zA-Z0-9]+)\b', expanded):
        a, b = m.group(1), m.group(2)
        cands = [a + b]
        if b.endswith('s') and len(b) > 3:
            cands.append(a + b[:-1])
        for cand in cands:
            cl = cand.lower()
            if cl not in _seen_kw_lc:
                _seen_kw_lc.add(cl)
                _compounds.append(cand)
    valid_kws = valid_kws + _compounds[:4]

    # Expand with sub-tokens from compound identifiers (e.g. StartOrStopAutoRefresh
    # → refresh) so files with compound method names surface for plain-word queries.
    # Sub-token hits are scored at 0.3× so a direct whole-token definition match
    # always outranks files that merely contain the generic pieces elsewhere.
    # Path scoring below uses only `keywords` — "start" never boosts Startup* files.
    _sub_kws: list[str] = []
    _seen_sub: set[str] = {k.lower() for k in valid_kws}
    for kw in valid_kws:
        for sub in _decompose_identifier(kw):
            if sub not in _seen_sub:
                _seen_sub.add(sub)
                _sub_kws.append(sub)
    _sub_kws = _sub_kws[:16]
    _sub_kw_set = set(_sub_kws)

    # Adaptive per-keyword sample depth: only truly ubiquitous (idf < 0.25 —
    # the same threshold the diversity multiplier already uses to mean
    # "not meaningfully distinctive") keywords get a shallow sample; they
    # saturate the _KW_CAP scoring cap after ~3 definition hits regardless of
    # how many more rows are fetched, so fetching 40 for them is wasted SQL +
    # Python work. Everything else keeps the deep cap — SQL rows aren't
    # relevance-ordered, so a moderately-common-but-still-specific keyword
    # (e.g. a domain identifier used in 10% of files) genuinely needs the
    # deeper sample to have a chance of surfacing the one row that matters;
    # a mid-tier cap here was tried and cost a real ticket its #1 rank.
    # Pre-warmup, _idf_weight returns 1.0 for everything, so every keyword
    # gets the deep cap — identical to the prior uniform-40 behavior.
    _kw_caps = {kw: (10 if _idf_weight(kw) < 0.25 else 40) for kw in valid_kws + _sub_kws}
    try:
        multi_hits = _symbol_hits_multi(valid_kws + _sub_kws, max_per_kw=_kw_caps)
    except Exception:
        multi_hits = {}

    # BM25-style scoring: capped term frequency × IDF.
    # - The per-keyword cap is TF saturation: repeated hits of one word give
    #   diminishing returns (at most _KW_CAP per keyword per file).
    # - IDF makes rare keywords count more than common ones. "audit" appearing
    #   in 900 of 2040 files gets a heavy discount; a compound identifier in
    #   3 files gets a boost. This is what stops generic business vocabulary
    #   ("Types", "All", "Operative") from burying the specific file.
    _KW_CAP = 9.0
    _file_kw_scores: dict[str, dict[str, float]] = {}   # {rel: {kw: capped_score}}
    _file_kw_primary: dict[str, set[str]] = {}           # distinct primary kws per file

    for kw in valid_kws + _sub_kws:
        sub_weight_factor = 0.3 if kw in _sub_kw_set else 1.0
        is_primary = kw not in _sub_kw_set
        cap = _KW_CAP * sub_weight_factor
        for rel, line, sig in multi_hits.get(kw, []):
            all_hits.append((kw, rel, line, sig))
            weight = (3 if _DEF_LINE_PAT.search(sig) else 1) * sub_weight_factor
            kw_scores = _file_kw_scores.setdefault(rel, {})
            prev = kw_scores.get(kw, 0.0)
            if prev < cap:
                kw_scores[kw] = prev + min(weight, cap - prev)
            if is_primary:
                _file_kw_primary.setdefault(rel, set()).add(kw)

    # Apply IDF post-cap (equivalent to scaling both weight and cap): a
    # ubiquitous keyword can now contribute at most 9 × 0.05, a rare
    # identifier up to 9 × 2.0. Sub-tokens keep their 0.3 factor and get
    # their own (usually low) IDF on top — the correct double discount.
    # Document-length normalization then divides down god-files (DbContext,
    # generated registries) that weakly match everything.
    idf_w = {kw: _idf_weight(kw) for kw in valid_kws + _sub_kws}
    for rel, kws in _file_kw_scores.items():
        raw = sum(s * idf_w[kw] for kw, s in kws.items())
        file_hit_count[rel] = raw * _doc_len_norm(rel)
    _used_fts = _SEMANTIC_READY  # path loop uses FTS5; byte-scan tiebreaker skipped

    # Path-based supplement: files whose name/directory contains task keywords.
    # Filename stem matches score 3×, directory matches 1×.
    # _PATH_TOKEN_INDEX (pre-computed at warmup) narrows the scan to files
    # whose stem/dir contains at least one path keyword as a token — turning
    # this from O(all files) to O(candidate files) on every scope_task call,
    # which matters most on 30k+ file repos where the old full scan dominated
    # per-query latency. A keyword can still be a genuine mid-word substring
    # match that isn't at a token boundary (e.g. "man" inside "Human...");
    # for the (typically rare) query keyword with ZERO index hits at all, we
    # fall back to a bounded substring scan for just that keyword — recovers
    # the coincidental-but-real matches without paying full-scan cost when
    # (as usual) every keyword already hit the index via its whole-word token.
    # Falls back to a full linear scan if the index isn't built yet.
    path_kws = _extract_path_keywords(expanded)
    if path_kws:
        # IDF-scaled with a 0.3 floor: a filename-stem match stays high-signal
        # even for a common word ("email" → EmailService.cs must keep working).
        _path_idf = {kw: max(0.3, min(_idf_weight(kw), 1.0)) for kw in path_kws}
        try:
            if _PATH_STRINGS and _PATH_TOKEN_INDEX:
                # Fast path: narrow to candidates via the inverted index.
                candidates: set[int] = set()
                missed_kws = []
                for kw in path_kws:
                    hits = _PATH_TOKEN_INDEX.get(kw)
                    if hits:
                        candidates.update(hits)
                    else:
                        missed_kws.append(kw)
                if missed_kws:
                    for i, (_rel, stem_lc, dir_lc) in enumerate(_PATH_STRINGS):
                        if any(kw in stem_lc or kw in dir_lc for kw in missed_kws):
                            candidates.add(i)
                path_source = (_PATH_STRINGS[i] for i in candidates)
            elif _PATH_STRINGS:
                # Index not built yet but the precomputed list is — scan it
                # directly rather than falling all the way to a live disk
                # walk (matches the tool's pre-index behavior exactly).
                path_source = iter(_PATH_STRINGS)
            else:
                path_source = (
                    (
                        str(src_path.relative_to(WORKSPACE_ROOT)),
                        src_path.stem.lower(),
                        str(src_path.parent.relative_to(WORKSPACE_ROOT)).lower(),
                    )
                    for src_path in _iter_source_files()
                )
            for rel, stem_lc, dir_lc in path_source:
                if stem_lc in _GENERIC_ROUTE_STEMS:
                    # Framework route-convention filenames (Next.js/Nuxt "page.tsx",
                    # "index.ts", "route.ts", "layout.tsx") carry no signal of their
                    # own — the meaningful name lives in the parent directory. Promote
                    # a match there to the same 3x weight a real filename-stem match
                    # gets, so e.g. "translator" reaching app/translator/page.tsx scores
                    # like a stem hit instead of being capped at the weaker dir-match 1x.
                    last_seg = dir_lc.rsplit('/', 1)[-1]
                    score = sum((3 if kw in last_seg else (1 if kw in dir_lc else 0))
                                * _path_idf[kw]
                                for kw in path_kws)
                else:
                    score = sum((3 if kw in stem_lc else (1 if kw in dir_lc else 0))
                                * _path_idf[kw]
                                for kw in path_kws)
                if score > 0:
                    file_hit_count[rel] = file_hit_count.get(rel, 0.0) + score
                    # Path keyword matches count as primary for diversity tracking
                    for kw in path_kws:
                        if kw in stem_lc or kw in dir_lc:
                            _file_kw_primary.setdefault(rel, set()).add(kw)
        except Exception:
            pass

    # Keyword diversity multiplier: reward files that match many distinct query
    # keywords over files that repeat one keyword many times. Each additional
    # primary keyword matched beyond the first multiplies the score by 1.5×.
    # Only keywords with meaningful IDF (>= 0.25) count toward the exponent —
    # matching three ubiquitous words ("Types", "All", "Operative") is not
    # evidence of relevance and must not earn a ×2.25.
    if _file_kw_primary:
        for rel in list(file_hit_count):
            matched = sum(1 for kw in _file_kw_primary.get(rel, ())
                          if _idf_weight(kw) >= 0.25)
            if matched > 1:
                file_hit_count[rel] *= 1.5 ** (matched - 1)

    # Forward import expansion: for the top 3 keyword/path-matched files, parse
    # their imports and add workspace-resolved targets at 40% of the parent's score.
    # This surfaces files one hop deeper in the call graph — where bugs often live.
    if file_hit_count:
        top3_exp = sorted(file_hit_count, key=lambda f: -file_hit_count[f])[:3]
        for rel in top3_exp:
            exp_path = WORKSPACE_ROOT / rel
            try:
                exp_text = exp_path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            try:
                for specifier, _ in _parse_import_entries(exp_path, exp_text):
                    kind, display = _resolve_import(specifier, exp_path)
                    if kind == 'workspace' and display not in file_hit_count:
                        file_hit_count[display] = file_hit_count[rel] * 0.4
            except Exception:
                pass

    # Reverse dependent expansion: for the top 3 files, add their dependents
    # (files that import them) at 20% of the parent's score.  The bug is often
    # in the caller of the matched utility/builder, not the utility itself —
    # e.g. scope_task finds DialogBuilder but the bug is in BaseActivity which
    # calls it.  Lower weight than forward expansion (0.2 vs 0.4) because callers
    # are more numerous and less specifically related than direct dependencies.
    if file_hit_count and _REVERSE_IMPORTS:
        top3_rev = sorted(file_hit_count, key=lambda f: -file_hit_count[f])[:3]
        for rel in top3_rev:
            for dependent in _REVERSE_IMPORTS.get(rel, []):
                if dependent not in file_hit_count:
                    file_hit_count[dependent] = file_hit_count[rel] * 0.2

    # Project focus weights: multiply each file's score by the first matching prefix
    # weight.  Boost (>1) surfaces sub-projects you're working in; reduce (<1)
    # suppresses sibling projects you're not touching.  Entries checked longest-
    # prefix-first so more-specific rules win over broader ones.
    # Special key '*' applies to any file that matched no named prefix — use it
    # to suppress everything outside your focus area, e.g. "*:0.2".
    # Per-call `focus` overrides the session-wide _FOCUS_WEIGHTS without mutating it.
    _eff_weights = _parse_focus_str(focus) if focus.strip() else _FOCUS_WEIGHTS
    if _eff_weights:
        _default_weight = _eff_weights.get('*')
        _sorted_focus = sorted(
            ((k, v) for k, v in _eff_weights.items() if k != '*'),
            key=lambda kv: -len(kv[0])
        )
        for rel in list(file_hit_count):
            rel_lc = rel.replace("\\", "/").lower()
            for prefix_lc, multiplier in _sorted_focus:
                if prefix_lc in rel_lc:
                    file_hit_count[rel] *= multiplier
                    break
            else:
                if _default_weight is not None:
                    file_hit_count[rel] *= _default_weight

    # Git recency boost: files modified recently in git rise in the ranking when
    # they're already keyword-matched — high-signal for regression bug tickets.
    # Bounded to at most double the match score: IDF shrinks common-word scores,
    # and an unbounded additive boost would let recency swamp weak matches.
    try:
        recency = _git_recency_scores()
        for rel, rscore in recency.items():
            if rel in file_hit_count:
                file_hit_count[rel] += min(rscore * 0.5, file_hit_count[rel])
    except Exception:
        pass

    # Hard-scope filter: catches every path regardless of how it entered
    # (symbol hits, path/filename matches, forward-import expansion, reverse-
    # dependent expansion all funnel into file_hit_count) — one filter here
    # instead of patching every section above.
    if _ACTIVE_SCOPE:
        file_hit_count = {r: s for r, s in file_hit_count.items() if _in_scope(r)}
        all_hits = [h for h in all_hits if _in_scope(h[1])]

    return file_hit_count, keywords, all_hits, expanded, _used_fts


@_tool
def scope_task(task: str, max_files: int = 5, include_blueprints: bool = False, focus: str = "") -> str:
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
        focus: optional comma-separated "prefix:weight" pairs applied only for
               this call — e.g. "src/auth:3.0,src/payments:2.0".  Does not
               modify .mimir-focus or the session-wide weights.  Overrides any
               persistent focus weights when non-empty.

    Returns a compact context block: keywords searched, matched symbols with
    file:line locations, and ranked files by relevance score.
    """
    file_hit_count, keywords, all_hits, expanded, _used_fts = _score_task_files(task, focus)
    if not keywords:
        return (
            "No searchable terms found in task description. "
            "Try including class names (e.g. 'TutorSession') or function names."
        )
    if not file_hit_count:
        return (
            f"No matches found for: {', '.join(keywords)}.\n"
            "Try more specific terms — class names, function names, or file path segments."
        )

    # Rank by total score. When FTS5 was used, BM25 already encodes term frequency
    # so no byte scan is needed. Fall back to byte scan only for the symbols-table path.
    if _used_fts:
        top_files = sorted(file_hit_count, key=lambda f: -file_hit_count[f])[:max_files]
    else:
        _byte_freq: dict[str, int] = {
            f: _byte_keyword_freq(WORKSPACE_ROOT / f, keywords)
            for f in file_hit_count
        }
        top_files = sorted(file_hit_count, key=lambda f: (-file_hit_count[f], -_byte_freq[f]))[:max_files]
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
        n = int(round(file_hit_count[rel]))
        parts.append(f"  {i}. {rel}  ({n} {'match' if n == 1 else 'matches'})")
        for note in _notes_for_path(rel):
            parts.append(f"       {note}")

    # Suggest targeted get_symbol calls for definition hits — faster than reading a whole file
    sym_suggestions: list[tuple[str, str]] = []
    seen_syms: set[tuple[str, str]] = set()
    for r, l, s in unique:
        if _DEF_LINE_PAT.search(s):
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
            n = int(round(file_hit_count[rel]))
            try:
                bp_path = _resolve_in_workspace(rel)
                blueprint = _build_blueprint(bp_path)
            except Exception as e:
                parts.append(f"### {rel}\n  (error reading: {e})\n")
                continue
            parts.append(f"### {rel}  ({n} {'match' if n == 1 else 'matches'})\n{blueprint}\n")

    return "\n".join(parts)


@_tool
def scope_area(task: str, max_depth: int = 4, focus: str = "") -> str:
    """Like scope_task, but rolls matches up into a directory tree instead of a
    flat file list — shows WHERE in a large monorepo a task's matches cluster,
    so you can `cd`/pin MCP_WORKSPACE_ROOT into that sub-project first.

    WHEN TO USE: in a multi-project repo (several apps/services under one root)
    when you don't yet know which sub-project a task lives in, or a flat
    scope_task result is spread thin across unrelated areas. Once you know the
    sub-project, scope_task/scope_hint run cleaner scoped to just that folder.
    Skip this for repos that are already a single project.

    Args:
        task: plain-English description of what you want to do.
        max_depth: how many directory levels deep to roll scores up (default 4).
        focus: optional comma-separated "prefix:weight" pairs for this call only.

    Returns an indented directory tree annotated with match scores/file counts,
    plus a suggested `cd` target — the most specific directory that still
    captures a real cluster (2+ files) of the top matches.
    """
    file_hit_count, keywords, all_hits, expanded, _used_fts = _score_task_files(task, focus)
    if not keywords:
        return (
            "No searchable terms found in task description. "
            "Try including class names (e.g. 'TutorSession') or function names."
        )
    if not file_hit_count:
        return (
            f"No matches found for: {', '.join(keywords)}.\n"
            "Try more specific terms — class names, function names, or file path segments."
        )

    # Roll each file's score up into every ancestor directory (bounded to
    # max_depth levels) so a folder's number reflects everything matched
    # beneath it, not just files directly inside it.
    dir_score: dict[str, float] = {}
    dir_files: dict[str, int] = {}
    for rel, score in file_hit_count.items():
        parts = Path(rel).parts[:-1]
        for depth in range(1, min(len(parts), max_depth) + 1):
            d = "/".join(parts[:depth])
            dir_score[d] = dir_score.get(d, 0.0) + score
            dir_files[d] = dir_files.get(d, 0) + 1

    if not dir_score:
        top = sorted(file_hit_count, key=lambda f: -file_hit_count[f])[:10]
        lines = [f"# Scope area: {task!r}\n", "All matches sit directly at the workspace root:"]
        for rel in top:
            lines.append(f"  {rel}  ({int(round(file_hit_count[rel]))} matches)")
        return "\n".join(lines)

    # Parent -> children edges; "" is the synthetic root holding depth-1 dirs.
    children: dict[str, list[str]] = {}
    for d in dir_score:
        parent = "/".join(d.split("/")[:-1])
        children.setdefault(parent, []).append(d)

    tree_lines: list[str] = []

    def render(node: str, prefix: str, top_n: int = 5) -> None:
        kids = sorted(children.get(node, []), key=lambda d: -dir_score[d])
        shown, hidden = kids[:top_n], kids[top_n:]
        for i, d in enumerate(shown):
            is_last = (i == len(shown) - 1) and not hidden
            branch = "└─ " if is_last else "├─ "
            name = d.split("/")[-1]
            n = dir_files[d]
            tree_lines.append(
                f"{prefix}{branch}{name}/  (score {dir_score[d]:.0f}, {n} file{'s' if n != 1 else ''} matched)"
            )
            render(d, prefix + ("   " if is_last else "│  "), top_n=top_n)
        if hidden:
            tree_lines.append(f"{prefix}└─ … {len(hidden)} more folder{'s' if len(hidden) != 1 else ''} with weaker matches")

    render("", "")

    parts_out = [
        f"# Scope area: {task!r}\n",
        f"Keywords searched: {', '.join(keywords)}\n",
        "## Folders ranked by match concentration\n",
        *tree_lines,
    ]

    # Suggested cd target: walk the highest-scoring directories and take the
    # first one that's a real cluster (2+ files) narrower than the full match
    # set — skips shallow directories that just aggregate everything beneath them.
    total_files = len(file_hit_count)
    cd_target = None
    for d in sorted(dir_score, key=lambda d: -dir_score[d]):
        if 2 <= dir_files[d] < total_files:
            cd_target = d
            break
    if cd_target is None:
        cd_target = max(dir_score, key=lambda d: dir_score[d])

    parts_out.append(
        f"\n## Suggested scope\n"
        f"  set_scope(\"{cd_target}\")   (CLI: mimir scope --set {cd_target})\n"
        f"  ({dir_files[cd_target]} of {total_files} matched files live here — hard-narrows every "
        f"tool call to this folder until set_scope(\"\"), no reindex needed)"
    )

    return "\n".join(parts_out)


# ---------------------------------------------------------------------------
# semantic_search helpers
# ---------------------------------------------------------------------------

# English filler words that carry no search signal in a natural-language query.
# Intentionally small — code keywords (export, import, …) must NOT be here because
# they are meaningful query terms even though they are code syntax stopwords.
_QUERY_STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "are", "was", "were",
    "has", "have", "been", "how", "what", "where", "when", "which",
    "its", "their", "from", "into", "onto", "upon", "over", "under",
    "not", "but", "can", "will", "all", "any", "some", "each",
})


def _decompose_query_for_fts(query: str) -> list[str]:
    """Convert a natural-language query to FTS5 search terms.

    Extracts word tokens, decomposes CamelCase/snake_case identifiers,
    lowercases everything, deduplicates, filters English filler words, and
    returns up to 16 terms of 3+ characters.

    Uses _QUERY_STOPWORDS (not _SYMBOL_STOPWORDS) so that code keywords that
    appear in the query (e.g. "export", "import") are kept as search terms.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for tok in re.findall(r'[a-zA-Z_]\w*', query):
        parts_list = (tok.lower(),) + _decompose_identifier(tok)
        for p in parts_list:
            pl = p.lower()
            if len(pl) >= 3 and pl not in _QUERY_STOPWORDS and pl not in seen:
                seen.add(pl)
                terms.append(pl)
    return terms[:16]


def _fts_search(
    terms: list[str], limit: int = 50
) -> list[tuple]:
    """Run an FTS5 BM25 search over symbol_fts.

    Returns list of (file, lineno, symbol_name, signature, score) tuples.
    BM25 scores in SQLite FTS5 are negative (more negative = better match).
    Falls back to a bare OR query if the quoted form fails.
    """
    if not terms or _DISK_CACHE is None:
        return []
    safe_terms = [t for t in terms if re.match(r'^\w+$', t)]
    if not safe_terms:
        return []
    fts_query = ' OR '.join(f'"{t}"' for t in safe_terms)
    # Column weights: symbol_name=10, signature=5, decomposed=1.
    # Heavily penalises matches that only appear in the broad decomposed token
    # bag — reduces noise from common sub-tokens like 'file', 'get', 'error'.
    sql = (
        "SELECT file, lineno, symbol_name, signature, bm25(symbol_fts, 0, 0, 10, 5, 1) AS score"
        " FROM symbol_fts WHERE symbol_fts MATCH ? ORDER BY score LIMIT ?"
    )
    try:
        return _DISK_CACHE.execute(sql, (fts_query, limit)).fetchall()
    except Exception:
        try:
            bare = ' OR '.join(safe_terms)
            return _DISK_CACHE.execute(sql, (bare, limit)).fetchall()
        except Exception:
            return []


@_tool
def semantic_search(query: str, max_results: int = 10, focus: str = "") -> str:
    """Search the workspace by MEANING rather than exact symbol names.

    WHEN TO USE: when scope_task returns poor results because you know the
    CONCEPT but not the CODE NAME — e.g. "authentication token refresh"
    instead of "refreshAuthToken", "connection pool exhaustion" instead of
    "PoolExhaustedException". Also useful for cross-cutting concerns that
    live in several differently-named files.

    How it works:
      1. FTS5 BM25 full-text search over decomposed identifier tokens
      2. RRF fusion with symbol-index keyword hits
      3. Reverse-import graph expansion to surface structurally adjacent files

    TIP: use scope_task when you know the exact class/function name.
         Use semantic_search when you know what the code DOES but not what
         it is CALLED.

    Args:
        query:       natural-language description of what the code does, e.g.
                     "database connection retry logic"
                     "user authentication token expiry handler"
        max_results: how many ranked files to return (default 10, max 25).
        focus:       optional comma-separated "prefix:weight" pairs applied only
                     for this call — does not modify .mimir-focus or the
                     session-wide weights.  Overrides persistent focus when set.
    """
    try:
        max_results = max(1, min(int(max_results), 25))

        if not _SEMANTIC_READY or _DISK_CACHE is None:
            fallback = scope_task(query, max_files=max_results)
            return (
                "[semantic_search: FTS5 index not yet ready — using scope_task fallback]\n"
                + fallback
            )

        query_terms = _decompose_query_for_fts(query)
        if not query_terms:
            return (
                f"No searchable terms extracted from {query!r}. "
                "Try more specific language with concrete nouns or identifier-like words."
            )

        # Layer 1: FTS5 BM25 retrieval (top 50 candidates). Deliberately NOT an
        # early-return when empty — a query with zero lexical/token overlap
        # (BM25's blind spot) can still be answered by the symbol-index layer
        # below via exact-token matches. We only give up once every layer has
        # had a chance to contribute (see the file_scores check after Layer 2).
        fts_hits = _fts_search(query_terms, limit=50)

        # Group by file: keep the BEST (most negative = highest BM25) score per file.
        # Summing rewards high-symbol-count files for volume, not relevance — a file
        # with 100 weak matches beats a file with 1 perfect match. Max-score fixes that.
        file_scores: dict[str, float] = {}
        file_symbols: dict[str, list[tuple[str, str]]] = {}
        file_symbols_seen: dict[str, set[tuple[str, str]]] = {}  # O(1) dedup
        for file, lineno, sym_name, sig, score in fts_hits:
            abs_score = abs(score)
            if abs_score > file_scores.get(file, 0.0):
                file_scores[file] = abs_score
            key = (lineno, sig)
            if key not in file_symbols_seen.setdefault(file, set()):
                file_symbols_seen[file].add(key)
                file_symbols.setdefault(file, []).append(key)

        # Path boost: if any query term appears in the file path, multiply the score.
        # scope_task wins on class-name queries (e.g. "excel" → TableToExcel.cs) because
        # it directly matches file names. Replicate that signal here: a 3× boost when the
        # path contains a term, stronger (5×) when the *filename stem* contains it.
        for file in list(file_scores):
            path_lc = file.lower()
            stem_lc = os.path.splitext(os.path.basename(path_lc))[0]
            for term in query_terms:
                if term in stem_lc:
                    file_scores[file] *= 5.0
                    break
                if term in path_lc:
                    file_scores[file] *= 3.0
                    break

        # Layer 2: RRF merge with scope_task's symbol-index lookup.
        # FTS5 BM25 finds code by decomposed tokens; the symbol index finds class/method
        # names by exact token (e.g. "email" → CRectificationEmail via its token index).
        # Reciprocal Rank Fusion (k=60) combines both ranked lists without needing to
        # normalise their incompatible score scales.
        try:
            scope_kws = _extract_scope_keywords(query)
            valid_scope_kws = [kw for kw in scope_kws if re.match(r'^\w[\w]*$', kw)]
            if valid_scope_kws and _FTS_READY:
                sym_hits = _symbol_hits_multi(valid_scope_kws, max_per_kw=8)
                sym_scores: dict[str, float] = {}
                for kw in valid_scope_kws:
                    kw_idf = _idf_weight(kw)   # discount common-word hits pre-RRF
                    for rel, _line, sig in sym_hits.get(kw, []):
                        w = (3.0 if _DEF_LINE_PAT.search(sig) else 1.0) * kw_idf
                        sym_scores[rel] = sym_scores.get(rel, 0.0) + w

                K = 60
                fts_ranked = sorted(file_scores, key=lambda f: -file_scores[f])
                sym_ranked = sorted(sym_scores, key=lambda f: -sym_scores[f])

                rrf: dict[str, float] = {}
                for rank, f in enumerate(fts_ranked, 1):
                    rrf[f] = rrf.get(f, 0.0) + 1.0 / (K + rank)
                for rank, f in enumerate(sym_ranked, 1):
                    rrf[f] = rrf.get(f, 0.0) + 1.0 / (K + rank)
                # populate file_symbols for files found only by the symbol index
                for kw in valid_scope_kws:
                    for rel, line, sig in sym_hits.get(kw, []):
                        key = (line, sig)
                        if key not in file_symbols_seen.setdefault(rel, set()):
                            file_symbols_seen[rel].add(key)
                            file_symbols.setdefault(rel, []).append(key)

                file_scores = rrf
        except Exception:
            pass  # RRF is best-effort; fall back to FTS5-only ranking

        if not file_scores:
            return (
                f"No matches found for terms: {', '.join(query_terms)}.\n"
                "Try broader terms or use scope_task for exact symbol names."
            )

        # Layer 3: reverse-import graph expansion — add files that import the top 3 hits
        if _REVERSE_IMPORTS and file_scores:
            top3 = sorted(file_scores, key=lambda f: -file_scores[f])[:3]
            for rel in top3:
                for dependent in _REVERSE_IMPORTS.get(rel, []):
                    if dependent not in file_scores:
                        file_scores[dependent] = file_scores[rel] * 0.2

        # Apply focus weights (same logic as scope_task).
        # Per-call `focus` overrides session-wide _FOCUS_WEIGHTS without mutating it.
        _eff_weights = _parse_focus_str(focus) if focus.strip() else _FOCUS_WEIGHTS
        if _eff_weights:
            _default_weight = _eff_weights.get('*')
            _sorted_focus = sorted(
                ((k, v) for k, v in _eff_weights.items() if k != '*'),
                key=lambda kv: -len(kv[0]),
            )
            for rel in list(file_scores):
                rel_lc = rel.replace("\\", "/").lower()
                for prefix_lc, multiplier in _sorted_focus:
                    if prefix_lc in rel_lc:
                        file_scores[rel] *= multiplier
                        break
                else:
                    if _default_weight is not None:
                        file_scores[rel] *= _default_weight

        top_files = sorted(file_scores, key=lambda f: -file_scores[f])[:max_results]

        out: list[str] = [
            f"# semantic_search: {query!r}",
            f"Terms:     {', '.join(query_terms)}",
            "Retrieval: FTS5+RRF + graph expansion",
            f"Results:   {len(top_files)} files\n",
        ]
        for rank, rel in enumerate(top_files, 1):
            syms = file_symbols.get(rel, [])
            out.append(f"{rank}. {rel}")
            for lineno, sig in syms[:3]:
                out.append(f"   L{lineno}  {sig}")
            if len(syms) > 3:
                out.append(f"   ... and {len(syms) - 3} more symbols")

        return '\n'.join(out)

    except Exception as e:
        return f"Error in semantic_search: {type(e).__name__}: {e}"


@_tool
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
            # Try to resolve by extracting the type name (last dot segment) and
            # searching the symbol index — common for C# namespace imports like
            # "InControl.Core.Services.IAuthService" where only "IAuthService" is indexable.
            type_name = spec.rsplit('.', 1)[-1] if '.' in spec else spec
            hint = ""
            if type_name and type_name != spec and _FTS_READY:
                try:
                    hits = _symbol_hits(type_name, max_results=1)
                    if hits:
                        hint = f"\n             → found: {hits[0][0]}:{hits[0][1]}"
                except Exception:
                    pass
            workspace_lines.append(f"  [workspace?] {spec}{hint}  (not found on disk){suffix}")
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


@_tool
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


@_tool
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
    docstrings, decorators on the definition line, and the complete body. Each
    line is prefixed with its real line number in the file (e.g. "L42  ..."),
    matching get_file_structure's blueprint format, so the output can be used
    directly as an edit anchor without a follow-up grep.
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
    start_line = None
    try:
        full_text = resolved.read_bytes().decode('utf-8', 'replace')
        offset = full_text.find(body)
        if offset != -1:
            start_line = full_text.count('\n', 0, offset) + 1
    except OSError:
        pass
    if start_line is not None:
        numbered = "\n".join(
            f"L{start_line + i}  {line}"
            for i, line in enumerate(body.splitlines())
        )
        header = f"# {path}  symbol={symbol_name}  ({line_count} lines, starting at L{start_line})"
    else:
        numbered = body
        header = f"# {path}  symbol={symbol_name}  ({line_count} lines)"
    return f"{header}\n\n{numbered}"


@_tool
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
                stdin=subprocess.DEVNULL,
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

        # Collect line diff counts: working-tree vs base covers committed + uncommitted
        numstat: dict[str, tuple[int, int]] = {}
        r_numstat = _git("diff", "--numstat", base)
        if r_numstat.returncode == 0:
            for line in r_numstat.stdout.splitlines():
                parts_ns = line.split("\t", 2)
                if len(parts_ns) == 3:
                    added_s, removed_s, fname = parts_ns
                    try:
                        numstat[fname.strip()] = (int(added_s), int(removed_s))
                    except ValueError:
                        pass  # binary files show '-' — skip

        def _diff_tag(rel: str, p: Path) -> str:
            if rel in numstat:
                added, removed = numstat[rel]
                return f"+{added} -{removed}"
            if not p.exists():
                return "deleted"
            # Untracked new file — count its lines
            try:
                lines = len(p.read_text(errors="replace").splitlines())
                return f"+{lines} -0"
            except OSError:
                return "new"

        label = 'file' if len(source_files) == 1 else 'files'
        header = (
            f"# Changed files vs '{base}'  ({len(source_files)} source {label})\n"
        )
        summary_rows = []
        for rel in source_files:
            tag = _diff_tag(rel, WORKSPACE_ROOT / rel)
            summary_rows.append(f"  {rel:<55} {tag}")
        summary = "\n".join(summary_rows)

        parts = [header + summary + "\n"]
        for rel in source_files:
            p = WORKSPACE_ROOT / rel
            if not p.exists():
                parts.append(f"# {rel}  [deleted]\n")
            else:
                parts.append(_build_blueprint(p) + "\n")
        return "\n".join(parts)
    except Exception as e:
        return f"Error: {e}"


@_tool
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


# --------------------------------------------------------------------------- #
# find_callers: in-process tree-sitter Query engine.
#
# tree_sitter_language_pack.get_parser() (used everywhere else in this file
# for blueprint extraction) returns nodes with a method-based API (.kind(),
# .child_count()) that is NOT compatible with tree_sitter.Query/QueryCursor,
# which require the standard tree_sitter.Node (property-based: .type,
# .children). So this section builds its own parser via
# tree_sitter.Parser(get_language(lang)) instead of reusing _get_ts_parser.
#
# Design: one query per language, built once and cached, that captures every
# identifier-like leaf kind actually present in that language's grammar
# (probed individually since referencing an unknown kind raises QueryError).
# The expensive O(all-AST-nodes) traversal + kind filtering happens natively
# in the query engine; Python only touches the resulting (much smaller) list
# of identifier occurrences, filtering by text equality and walking a short
# ancestor chain to exclude a definition's own name.
#
# A fixed-pattern structural matcher (enumerating "callable name" shapes like
# foo(...), x.foo(...)) has a systemic blind spot: it can never match the
# symbol as the object before a dot (Foo.StaticMember), a generic type
# argument (Get<Foo>()), or a bare reference through `this`/`self`
# (this.Foo()) - all real, common call sites an impact-analysis tool needs
# to find. Scanning every identifier-kind leaf instead (this approach) finds
# all of those, verified on a 7000+ file C# monorepo, while resolving via
# the query engine (not a hand-rolled Python recursion) so it doesn't
# regress on the rare case of very common symbol names matching hundreds of
# files.
# --------------------------------------------------------------------------- #
_CS_PARAM_LIST_KINDS = {
    "parameters", "parameter_list", "formal_parameters", "function_value_parameters",
    "value_parameters",
}
_CS_CANDIDATE_IDENTIFIER_KINDS = (
    "identifier", "property_identifier", "field_identifier", "type_identifier",
    "simple_identifier", "package_identifier", "shorthand_property_identifier",
    "constant",
)
_CS_IMPORT_LIKE_EXACT = {"using_directive", "use_declaration", "use_list"}

_CS_STD_PARSER_LOCAL = threading.local()  # thread-local: .cache = {lang: Parser}
_CS_LANGUAGE_CACHE: dict[str, object] = {}
_CS_QUERY_CACHE: dict[str, object] = {}  # value is a Query, or None if unbuildable


def _cs_get_language(lang: str):
    if lang not in _CS_LANGUAGE_CACHE:
        _CS_LANGUAGE_CACHE[lang] = _ts_get_language(lang)  # type: ignore[misc]
    return _CS_LANGUAGE_CACHE[lang]


def _cs_get_parser(lang: str):
    """Thread-local standard tree_sitter.Parser (parsers are not thread-safe)."""
    cache = _CS_STD_PARSER_LOCAL.__dict__.setdefault('cache', {})
    if lang not in cache:
        cache[lang] = _tree_sitter.Parser(_cs_get_language(lang))  # type: ignore[misc]
    return cache[lang]


def _cs_identifier_query(lang: str):
    """Build (once, cached) a query matching every identifier-like leaf kind
    that actually exists in this language's grammar."""
    if lang in _CS_QUERY_CACHE:
        return _CS_QUERY_CACHE[lang]
    language = _cs_get_language(lang)
    valid = []
    for kind in _CS_CANDIDATE_IDENTIFIER_KINDS:
        try:
            _tree_sitter.Query(language, f"({kind}) @id")
            valid.append(kind)
        except Exception:
            continue
    query = None
    if valid:
        pattern = "[" + " ".join(f"({k})" for k in valid) + "] @id"
        query = _tree_sitter.Query(language, pattern)
    _CS_QUERY_CACHE[lang] = query
    return query


def _cs_is_def_node(node) -> bool:
    """Same classification as _is_def_node, adapted to the standard
    tree_sitter.Node property API instead of the method-based one."""
    parent = node.parent
    if parent is None:
        return False
    t = node.type
    if t in DEF_EXCLUDE:
        return False
    if t in ("lexical_declaration", "variable_declaration"):
        for child in node.children:
            if child.type == "variable_declarator":
                for gc in child.children:
                    if gc.type in _FN_NODE_KINDS:
                        return True
        return False
    if t.endswith(DEF_SUFFIXES):
        return True
    if t in DEF_EXACT and node.is_named and len(node.children) > 0:
        return True
    return False


def _cs_def_name_span(node):
    """BFS for tree-sitter's semantic 'name' field, not descending into
    body-like children. Handles the vast majority of grammars precisely -
    including when a typed return value precedes the actual name (Java/C#)."""
    queue = [node]
    head = 0
    while head < len(queue):
        n = queue[head]
        head += 1
        name_node = n.child_by_field_name("name")
        if name_node is not None:
            return (name_node.start_byte, name_node.end_byte)
        for child in n.children:
            k = child.type
            if k in BODY_TYPES or k.endswith("_body"):
                continue
            queue.append(child)
    return None


def _cs_def_name_span_tier2(node):
    """Fallback for grammars with no 'name' field annotation (Kotlin) or
    where the name is nested in a declarator wrapper (C/C++): take the last
    identifier-like direct child before any parameter-list/body boundary,
    recursing into non-leaf wrapper children if nothing is found here."""
    last = None
    for child in node.children:
        k = child.type
        if k in BODY_TYPES or k.endswith("_body") or k in _CS_PARAM_LIST_KINDS:
            break
        if len(child.children) == 0 and (k.endswith("identifier") or k == "constant"):
            last = child
    if last is not None:
        return (last.start_byte, last.end_byte)
    for child in node.children:
        k = child.type
        if k in BODY_TYPES or k.endswith("_body"):
            continue
        if len(child.children) > 0:
            result = _cs_def_name_span_tier2(child)
            if result is not None:
                return result
    return None


def _cs_is_inside_import(node) -> bool:
    n = node.parent
    depth = 0
    while n is not None and depth < 8:
        k = n.type
        if "import" in k or k in _CS_IMPORT_LIKE_EXACT:
            return True
        if k in BODY_TYPES:
            return False
        n = n.parent
        depth += 1
    return False


def _cs_find_call_sites(lang: str, raw: bytes, symbol_name: str) -> list[tuple[int, int]]:
    """Return (start_byte, end_byte) spans where symbol_name appears as a
    genuine code identifier reference - not the definition's own name, not
    inside an import/using statement."""
    parser = _cs_get_parser(lang)
    tree = parser.parse(raw)
    root = tree.root_node
    query = _cs_identifier_query(lang)
    if query is None:
        return []

    cursor = _tree_sitter.QueryCursor(query)
    caps = cursor.captures(root)
    candidates = caps.get("id", [])

    needle = symbol_name.encode("utf-8")
    results = []
    # Keyed by byte-range, not id(ancestor): tree-sitter's Python bindings
    # mint a fresh wrapper object on every .parent/.children access, so id()
    # is not stable - a GC'd wrapper's address can be reused by a later,
    # unrelated node and silently alias onto a stale cache entry.
    excluded_cache: dict[tuple[int, int], object] = {}

    for node in candidates:
        start, end = node.start_byte, node.end_byte
        if raw[start:end] != needle:
            continue
        if _cs_is_inside_import(node):
            continue
        # Walk up looking for an enclosing definition node whose "name" is
        # exactly this candidate - e.g. Go nests the name two levels up
        # (type_declaration -> type_spec -> type_identifier), not as a
        # direct parent. Stop early at a body boundary: once inside a
        # function/class body, this can only be a real usage.
        is_def_name = False
        ancestor = node.parent
        depth = 0
        while ancestor is not None and depth < 6:
            k = ancestor.type
            if k in BODY_TYPES or k.endswith("_body"):
                break
            key = (ancestor.start_byte, ancestor.end_byte)
            if key in excluded_cache:
                span = excluded_cache[key]
            else:
                span = None
                if _cs_is_def_node(ancestor):
                    span = _cs_def_name_span(ancestor) or _cs_def_name_span_tier2(ancestor)
                excluded_cache[key] = span
            if span == (start, end):
                is_def_name = True
                break
            ancestor = ancestor.parent
            depth += 1
        if not is_def_name:
            results.append((start, end))
    return results


@_tool
def find_callers(symbol_name: str, max_results: int = 20) -> str:
    """Find every call site of a symbol across the workspace.

    Uses an in-process tree-sitter query (when the grammar loads cleanly) to
    match every identifier reference to the symbol — calls (`foo(...)`,
    `x.foo(...)`), bare/delegate references (`x.foo`, `this.foo`), and type
    references (`Foo.Member`, `Get<Foo>()`) — while excluding the
    definition's own name, comments, docstrings, and import/using
    statements. Falls back to a ripgrep/regex text scan for a language whose
    grammar isn't available, or entirely when tree-sitter isn't installed —
    that fallback matches the identifier as a whole word anywhere in the
    file, including non-reference text.

    Unlike verify_symbol_existence (which searches only definition blueprints),
    this searches implementation code to find where the symbol is called.

    WHEN TO USE: after verify_symbol_existence tells you WHERE something is
    defined, use find_callers to trace WHO calls it — for impact analysis,
    understanding data flow, or finding all consumers of an interface.

    Args:
        symbol_name: exact identifier to search for (case-sensitive).
        max_results: cap on results returned (default 20).
    """
    if not re.match(r'^\w+$', symbol_name):
        return "Error: symbol_name must be a bare identifier (letters, digits, underscore)."

    cutoff = max_results * 4
    results: list[tuple[str, int, str]] = []

    if _PATH_STRINGS:
        path_pairs = [(WORKSPACE_ROOT / rel, rel) for rel, _, _ in _PATH_STRINGS]
    else:
        path_pairs = [(p, str(p.relative_to(WORKSPACE_ROOT))) for p in _iter_source_files()]
    if _ACTIVE_SCOPE:
        path_pairs = [(p, r) for p, r in path_pairs if _in_scope(r)]
    indexed_rels = {r for _, r in path_pairs}

    # Whole-repo textual prefilter: a single `rg -l` pass (near-free even on
    # 10k+ files) tells us which files even *mention* the symbol at all, so
    # the tree-sitter pass below only has to parse that handful of files
    # instead of every file of a given language in the repo. Without this, a
    # multi-language monorepo pays a full-tree parse per language even when
    # the symbol appears nowhere in most of those languages. candidate_rels
    # is None when rg is unavailable or fails — callers below then fall back
    # to scanning everything.
    candidate_rels: set[str] | None = None
    if _RG_BIN:
        try:
            proc = subprocess.run(
                [_RG_BIN, "-l", "--no-heading", "--no-ignore", "--word-regexp",
                 "--", symbol_name, str(WORKSPACE_ROOT)],
                capture_output=True, timeout=15,
                stdin=subprocess.DEVNULL,
            )
            if proc.returncode in (0, 1):
                ws_str = str(WORKSPACE_ROOT).replace("\\", "/")
                # rg's output is forward-slash even on Windows, but
                # indexed_rels (built from Path.relative_to) is native-
                # separator - normalize both sides for the lookup, keeping
                # the ORIGINAL native-separator rel for candidate_rels so
                # every downstream comparison against indexed_rels/path_pairs
                # still lines up.
                indexed_rels_fwd = {r.replace("\\", "/"): r for r in indexed_rels}
                candidate_rels = set()
                for raw_line in proc.stdout.splitlines():
                    decoded = raw_line.decode('utf-8', 'replace').replace("\\", "/")
                    if decoded.startswith(ws_str):
                        rel = decoded[len(ws_str):].lstrip("/")
                    else:
                        rel = decoded
                    native_rel = indexed_rels_fwd.get(rel)
                    if native_rel is not None:
                        candidate_rels.add(native_rel)
        except Exception:
            candidate_rels = None

    # The symbol doesn't appear as a whole word anywhere in the indexed
    # workspace — no amount of tree-sitter/text scanning will find a call site.
    if candidate_rels is not None and not candidate_rels:
        return f"No usages of '{symbol_name}' found in the workspace."

    scan_pairs = path_pairs if candidate_rels is None else [
        (p, r) for p, r in path_pairs if r in candidate_rels
    ]

    # Split into files this in-process tree-sitter pass can structurally
    # parse vs everything else (unmapped extension, or a language whose
    # grammar tree-sitter-language-pack doesn't ship / fails to load).
    # _cs_identifier_query results are cached per language, so this is only
    # ever a real probe once per language per process.
    cs_lang_files: dict[str, list[tuple[Path, str]]] = {}
    text_scan_pairs: list[tuple[Path, str]] = []
    if TREE_SITTER_OK:
        for path, rel in scan_pairs:
            lang = EXT_LANG.get(path.suffix, (None, None))[0]
            query_ok = False
            if lang:
                try:
                    query_ok = _cs_identifier_query(lang) is not None
                except Exception:
                    query_ok = False
            if query_ok:
                cs_lang_files.setdefault(lang, []).append((path, rel))
            else:
                text_scan_pairs.append((path, rel))
    else:
        text_scan_pairs = scan_pairs

    # In-process tree-sitter pass: parse each candidate file once and query
    # for every identifier reference to symbol_name in one native pass,
    # excluding the definition's own name (see _cs_find_call_sites). Cost
    # scales with textual hits (from the rg prefilter above), not repo size.
    if cs_lang_files:
        seen: set[tuple[str, int]] = set()
        for lang in sorted(cs_lang_files):
            for path, rel in cs_lang_files[lang]:
                if len(results) >= cutoff:
                    break
                try:
                    raw = path.read_bytes()
                    spans = _cs_find_call_sites(lang, raw, symbol_name)
                except Exception:
                    continue
                for start, end in spans:
                    if len(results) >= cutoff:
                        break
                    lineno = raw.count(b"\n", 0, start) + 1
                    key = (rel, lineno)
                    if key in seen:
                        continue
                    seen.add(key)
                    line_start = raw.rfind(b"\n", 0, start) + 1
                    line_end = raw.find(b"\n", start)
                    if line_end == -1:
                        line_end = len(raw)
                    snippet = raw[line_start:line_end].decode("utf-8", "replace").strip()[:120]
                    results.append((rel, lineno, snippet))
            if len(results) >= cutoff:
                break

    # Text-scan fallback: files the tree-sitter pass can't parse, plus (when
    # tree-sitter is missing entirely) every file the prefilter flagged (or
    # the whole workspace if the prefilter didn't run either).
    if text_scan_pairs and len(results) < cutoff:
        needle = symbol_name.encode('utf-8')
        word_re = re.compile(rf'(?<!\w){re.escape(symbol_name)}(?!\w)')

        def _scan(path: Path, rel: str) -> None:
            if len(results) >= cutoff:
                return
            try:
                raw = path.read_bytes()
            except OSError:
                return
            if needle not in raw:
                return
            for i, line in enumerate(raw.decode('utf-8', 'replace').splitlines(), 1):
                if word_re.search(line):
                    results.append((rel, i, line.strip()[:120]))

        if _RG_BIN and candidate_rels is None:
            # rg -n: emit path:line:content directly for every match — avoids
            # reopening and re-splitting every matched file in Python (rg -l
            # would only give filenames, requiring a second Python read pass).
            # --no-ignore: search same files as mimir (don't skip .gitignored files).
            # Post-filter to just the files this fallback is responsible for.
            # Only needed when the prefilter above didn't already run (it
            # already covers this ground when candidate_rels is known).
            try:
                text_scan_rels = {r for _, r in text_scan_pairs}
                proc = subprocess.run(
                    [_RG_BIN, "-n", "--no-heading", "--no-ignore", "--word-regexp",
                     "--", symbol_name, str(WORKSPACE_ROOT)],
                    capture_output=True, timeout=15,
                    stdin=subprocess.DEVNULL,
                )
                if proc.returncode in (0, 1):
                    ws_str = str(WORKSPACE_ROOT).replace("\\", "/")
                    for raw_line in proc.stdout.splitlines():
                        if len(results) >= cutoff:
                            break
                        decoded = raw_line.decode('utf-8', 'replace').replace("\\", "/")
                        path_text, _, rest = decoded.partition(':')
                        lineno_text, _, content = rest.partition(':')
                        if path_text.startswith(ws_str):
                            rel = path_text[len(ws_str):].lstrip("/")
                        else:
                            rel = path_text
                        if rel not in text_scan_rels or not lineno_text.isdigit():
                            continue
                        results.append((rel, int(lineno_text), content.strip()[:120]))
            except Exception:
                # rg failed — fall back to Python scan
                for path, rel in text_scan_pairs:
                    if len(results) >= cutoff:
                        break
                    _scan(path, rel)
        else:
            # Either candidate_rels already narrowed text_scan_pairs down to
            # the handful of files known to contain the symbol (prefilter
            # ran), or rg isn't installed at all — either way a direct
            # Python read of just these files is cheap.
            for path, rel in text_scan_pairs:
                if len(results) >= cutoff:
                    break
                _scan(path, rel)

    if not results:
        return f"No usages of '{symbol_name}' found in the workspace."

    results = results[:max_results]
    lines = [f"# Usages of '{symbol_name}'  ({len(results)} shown)\n"]
    for rel, lineno, ctx in results:
        lines.append(f"  {rel}:{lineno}  {ctx}")
    if len(results) >= max_results:
        lines.append(f"\n... capped at {max_results}; use a more specific name to narrow down")
    return '\n'.join(lines)


@_tool
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
            notes = _notes_for_path(str(path.relative_to(WORKSPACE_ROOT)))
            if notes:
                parts.extend(f"  {n}" for n in notes)
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


@_tool
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


@_tool
def record_note(path_prefix: str, note: str) -> str:
    """Attach a free-text contextual note to files/paths matching a prefix, so
    future scope_task/get_file_structure/get_directory_structure calls surface
    it as prose alongside those files.

    DIFFERENT FROM record_alias: record_alias teaches mimir SEARCH VOCABULARY
    (a domain term expands into a code name, silently feeding scope_task's
    keyword extraction). record_note attaches CONTEXT (shown verbatim as a
    "note:" line, never used for ranking or search). Use record_note for things
    like non-obvious architecture ("this uses platform-native timers, not the
    shared service"), gotchas, or pointers to the real implementation file when
    it differs from what the name suggests. Use record_alias when a plain-English
    term and a code identifier are just two names for the same thing.

    WHEN TO USE: after discovering something about a path/feature that a future
    reader would NOT be able to infer purely from the file/symbol names — e.g.
    you found out the "real" logic lives elsewhere, or that a method looks
    unused but is called reflectively. Prefer narrow, specific prefixes — a
    note on a broad prefix like "" or "src" will surface on every file.

    Args:
        path_prefix: a workspace-relative path or substring identifying the
                     files this note applies to (e.g. "Features/Playback" or
                     "SyncService.cs"). Matches by substring, same as .mimir-focus.
        note: the free-text note. Kept short (multi-line input is flattened
              to one line — the storage format is one note per line).

    Returns a confirmation showing the saved note, or "Already recorded" if
    this exact (prefix, note) pair already exists.
    """
    prefix = path_prefix.strip().replace("\\", "/").lower()
    if "=" in prefix:
        return "Error: path_prefix must not contain '='."
    text = " ".join(note.split())  # flatten multi-line input to one line
    if not prefix or not text:
        return "Error: both path_prefix and note must be non-empty strings."

    notes_path = WORKSPACE_ROOT / ".mimirnotes"

    # Load existing content
    existing: dict[str, list[str]] = {}
    lines_raw: list[str] = []
    if notes_path.exists():
        lines_raw = notes_path.read_text(encoding="utf-8").splitlines()
        for line in lines_raw:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                p, _, n = stripped.partition("=")
                existing.setdefault(p.strip().lower(), []).append(n.strip())

    if text in existing.get(prefix, []):
        return f"Already recorded: '{prefix}' → {text}"

    # Append-only log — no merge/rewrite needed, just add a new line.
    out_lines = list(lines_raw)
    if not notes_path.exists():
        out_lines = [
            "# mimir contextual notes — free-text context attached to a path prefix",
            "# Format:  path/prefix = note text",
            "# Shown as prose alongside matching files; never used for search ranking.",
            "",
        ] + out_lines
    out_lines.append(f"{prefix} = {text}")

    notes_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    # Update in-memory cache
    _MIMIRNOTES.setdefault(prefix, []).append(text)

    return f"Saved note for '{prefix}': {text}  (.mimirnotes updated)"


@_tool
def add_ignore(pattern: str, reason: str = "") -> str:
    """Add a gitignore-style pattern to .mimirignore to exclude noisy files from the index.

    Call this when you encounter files or directories that add noise without useful
    symbols — vendor libraries, generated code, test fixtures, build artefacts,
    minified bundles, resource XML, etc.

    IMPORTANT: Always tell the user what you are about to add and why BEFORE calling
    this tool — they may want to adjust the pattern or skip it entirely. A pattern
    that is too broad can silently exclude real source files.

    The pattern is appended immediately and takes effect on the next file walk
    (within 30 seconds). The architecture map cache is also cleared so the next
    get_architecture call reflects the updated exclusions.

    Args:
        pattern: gitignore-style glob, e.g. "**/vendor/**" or "**/*.generated.cs"
        reason:  short human-readable note saved as a comment above the pattern
                 so the team knows why it was added (e.g. "vendored jQuery bundles")

    Returns confirmation showing the added pattern.
    """
    global _MIMIRIGNORE_PATTERNS, _FILE_LIST, _FILE_LIST_TS, _ARCHITECTURE_MAP

    pattern = pattern.strip()
    if not pattern or pattern.startswith('#'):
        return "Error: pattern must be a non-empty, non-comment string."
    if '..' in pattern:
        return "Error: pattern must not escape the workspace root."

    ignore_path = WORKSPACE_ROOT / ".mimirignore"

    # Guard against duplicates
    if ignore_path.exists():
        for line in ignore_path.read_text(encoding='utf-8').splitlines():
            if line.strip() == pattern:
                return f"Pattern already in .mimirignore: {pattern}"

    lines: list[str] = []
    if reason:
        lines.append(f"# {reason}")
    lines.append(pattern)
    block = '\n'.join(lines) + '\n'

    with open(ignore_path, 'a', encoding='utf-8') as f:
        if ignore_path.stat().st_size > 0 if ignore_path.exists() else False:
            f.write('\n')
        f.write(block)

    # Reload patterns and invalidate file list + architecture cache
    _MIMIRIGNORE_PATTERNS = _load_mimirignore()
    _FILE_LIST = []
    _FILE_LIST_TS = 0.0
    _ARCHITECTURE_MAP = ''

    # Prune already-indexed files that now match the new pattern — otherwise
    # they keep surfacing in scope_task/semantic_search/find_callers until
    # some unrelated event happens to evict them.
    evicted = 0
    for path_str in list(_CACHE.keys()):
        path = Path(path_str)
        if _is_blacklisted(path):
            _cache_evict(path)
            evicted += 1

    suffix = f"  # {reason}" if reason else ""
    evicted_note = f"  ({evicted} already-indexed file(s) removed from the index)" if evicted else ""
    return f"Added to .mimirignore: {pattern}{suffix}{evicted_note}"


def audit_index_health(max_findings: int = 5) -> str:
    """Proactively scan the current index for noise that will degrade search
    quality, and suggest add_ignore patterns to fix it — before you stumble
    into the noise via a bad scope_task/semantic_search result.

    CLI-only (`mimir audit`), deliberately not an MCP tool: it's a human
    diagnostic for tuning .mimirignore, and every registered tool's schema
    costs tokens on every agent turn.

    WHEN TO USE: the first call when connecting to an unfamiliar or very large
    repo, especially a monorepo — run this before scope_task/semantic_search
    so you can exclude vendor/generated noise up front instead of discovering
    it result-by-result. Also useful any time search results seem oddly
    diluted (e.g. every query returns the same handful of unrelated files).

    Checks two things, both computed from data already in memory/SQLite — no
    extra disk scan beyond what warmup already did:
      1. Bloated files: large on disk but almost no extracted symbols (vendor
         bundles, generated migrations, minified assets that slipped past
         .mimirignore) — these cost cache memory and token budget for ~zero
         structural value.
      2. Over-saturated search terms: identifiers so common across the
         codebase that they dilute scope_task/semantic_search ranking for
         everyone (e.g. a generic 'Data' or 'Handler' suffix used in hundreds
         of unrelated files).

    Args:
        max_findings: cap on findings shown per section (default 5).

    IMPORTANT: like add_ignore itself, review suggested patterns before
    applying them — a pattern that's too broad can exclude real source files.
    """
    try:
        report: list[str] = ["# Mimir Index Health Audit\n"]

        # ── Bloat: large file size, almost no extracted symbols ─────────────
        bloat: list[tuple[str, int, int]] = []  # (rel, size_bytes, symbol_count)
        for path_str, (_mtime, size, blueprint) in _CACHE.items():
            if size < 20_000:  # not worth flagging small files regardless of symbol count
                continue
            symbol_count = sum(1 for line in blueprint.splitlines() if re.match(r'L\d+\s', line))
            if symbol_count <= 2:
                try:
                    rel = str(Path(path_str).relative_to(WORKSPACE_ROOT))
                except ValueError:
                    rel = path_str
                bloat.append((rel, size, symbol_count))

        if bloat:
            bloat.sort(key=lambda x: -x[1])
            report.append("## Bloated files (large, almost no structure)")
            report.append("Candidates for .mimirignore — vendor bundles, generated code, or")
            report.append("assets that parsed but yielded little to no useful structure:\n")
            for rel, size, symbol_count in bloat[:max_findings]:
                report.append(f"* `{rel}`  ({size // 1024:,}KB, {symbol_count} symbol(s) extracted)")
            report.append("")

        # ── Lexical pollution: tokens so common they dilute ranking ─────────
        pollution: list[tuple[str, int, int]] = []  # (token, file_count, total_count)
        if _FTS_READY and _DISK_CACHE is not None:
            try:
                total_files = max(len(_iter_source_files()), 1)
                floor = max(150, int(total_files * 0.05))
                rows = _DISK_CACHE.execute(
                    "SELECT token, COUNT(DISTINCT file) AS fc, COUNT(*) AS tc"
                    " FROM symbols GROUP BY token HAVING fc >= ? ORDER BY fc DESC LIMIT ?",
                    (floor, max_findings * 2),
                ).fetchall()
                pollution = [(t, fc, tc) for t, fc, tc in rows]
            except Exception:
                pass

        if pollution:
            report.append("## Over-saturated search terms")
            report.append("These identifiers appear across so many files that they dilute")
            report.append("scope_task/semantic_search ranking — not necessarily a problem to")
            report.append("fix, but worth knowing when a query returns oddly generic results:\n")
            for token, file_count, total_count in pollution[:max_findings]:
                try:
                    sample = _DISK_CACHE.execute(
                        "SELECT file FROM symbols WHERE token = ? LIMIT 200", (token,)
                    ).fetchall()
                    dirs = [str(Path(f).parent) for (f,) in sample]
                    top_dir = max(set(dirs), key=dirs.count) if dirs else "?"
                except Exception:
                    top_dir = "?"
                report.append(
                    f"* `{token}` — {total_count:,} hits across {file_count:,} files "
                    f"(top source: `{top_dir}/`)"
                )
            report.append("")

        if len(report) == 1:
            return "Index looks healthy — no significant bloat or search-term pollution detected."
        return "\n".join(report).rstrip()
    except Exception as e:
        return f"Error in audit_index_health: {type(e).__name__}: {e}"


@_tool
def set_focus(entries: str, persist: bool = True) -> str:
    """Set (or clear) per-project score weights so scope_task biases results toward
    the projects you're actively working in and away from those you're not.

    Each entry is a path-prefix substring optionally followed by :weight.
    Default weight when omitted is 3.0 (3× boost).  Use a weight below 1.0 to
    suppress a project (e.g. 0.3 reduces its scores to 30%).  Multiple entries are
    comma-separated.  Pass an empty string to clear all weights and restore equal
    scoring across every project.

    By default the weights are saved to .mimir-focus in the workspace root and take
    effect immediately — no restart needed.  Pass persist=False to apply weights for
    this session only without touching the file (useful when two AI assistants share
    the same workspace and you don't want them overwriting each other's focus state).
    Call set_focus("") to reset at any time.

    The special prefix '*' acts as a default weight for every file that does NOT
    match any named prefix — useful in mixed mono-repos to suppress noisy sibling
    directories without listing each one explicitly.

    For a single-call override without any persistent state change, pass the focus
    string directly to scope_task or semantic_search via their focus= parameter.

    Args:
        entries: comma-separated list of prefix[:weight] pairs, e.g.:
            "Carps.Mobile"                            → boost Mobile 3×, others unchanged
            "Carps.Mobile, Carps.Keypad:0.3"          → boost Mobile, suppress Keypad
            "Carps.Mobile:3, Carps.Keypad:0.3"        → explicit weights for both
            "app/src/main/java:5, *:0.2"              → boost Java 5×, suppress everything else 0.2×
            ""                                         → clear all, equal scoring
        persist: if True (default), save weights to .mimir-focus so they survive
                 server restart and are visible to other sessions.  If False, apply
                 weights in memory for this session only.

    Examples:
        set_focus("InControl.Carps.Mobile")
        set_focus("InControl.Carps.Mobile, InControl.Carps.Keypad:0.3")
        set_focus("app/src/main/java:5, *:0.2")
        set_focus("src/auth:3.0", persist=False)   # session-local, no file write
        set_focus("")
    """
    global _FOCUS_WEIGHTS
    raw = entries.strip()
    focus_file = WORKSPACE_ROOT / ".mimir-focus"
    try:
        if not raw:
            if persist and focus_file.exists():
                focus_file.unlink()
            _FOCUS_WEIGHTS = {}
            return "Focus cleared. All projects now score equally."

        parsed = _parse_focus_str(raw)
        if not parsed:
            return "Error: no valid entries found. Format: 'Prefix.Name' or 'Prefix.Name:0.3'"

        if persist:
            lines_out = [f"{prefix} = {weight}" for prefix, weight in parsed.items()]
            focus_file.write_text(
                "# mimir project focus weights  (prefix = multiplier)\n"
                + "\n".join(lines_out) + "\n",
                encoding="utf-8"
            )
        _FOCUS_WEIGHTS = parsed

        summary = ", ".join(
            f"'{k}' {'×' if v >= 1 else '×'}{v:.1f}" for k, v in parsed.items()
        )
        suffix = " Saved to .mimir-focus (call set_focus(\"\") to reset)." if persist else " (session only — not persisted to .mimir-focus)."
        return f"Focus set: {summary}.{suffix}"
    except OSError as e:
        return f"Error saving focus: {e}"


@_tool
def set_scope(path: str) -> str:
    """Hard-narrow every search tool (scope_task, scope_area, scope_hint,
    verify_symbol_existence, find_callers) to only files under one directory —
    the same effect as `cd`-ing into a sub-project before running mimir, but
    without losing the whole-repo index or paying a reindex cost.

    WHEN TO USE: in a large monorepo (multiple apps/services under one root),
    once scope_area or a first scope_task pass tells you which sub-project a
    task lives in, call set_scope on that directory so every subsequent call
    in this session only sees that sub-project. Persists until cleared with
    set_scope("") — it survives across separate CLI invocations and MCP calls,
    unlike a per-call scope_task `focus=` override.

    Unlike set_focus (a soft ranking bias — the rest of the repo still shows
    up, just lower-scored), this is a hard filter: files outside the scoped
    directory are excluded from results entirely. The two are independent and
    can be combined (set_focus re-ranks within whatever set_scope allows through).

    Args:
        path: workspace-relative directory path, e.g. "src/carps-web". Must
              exist under the workspace root and contain at least one
              indexed source file.

    Returns confirmation with the number of files now in scope, or an error
    if the path doesn't exist or contains no source files.
    """
    global _ACTIVE_SCOPE
    norm = path.strip().replace("\\", "/").strip("/")
    if not norm or norm == ".":
        # Empty path clears an active scope — same convention as set_focus("").
        p = WORKSPACE_ROOT / ".mimir-scope"
        try:
            if p.exists():
                p.unlink()
        except OSError as e:
            return f"Error clearing scope: {e}"
        was, _ACTIVE_SCOPE = _ACTIVE_SCOPE, None
        if was is None:
            return "No active scope to clear — already searching the full repo."
        return f"Scope cleared (was '{was}'). Now searching the full repo."
    if norm.startswith("..") or Path(norm).is_absolute():
        return "Error: path must be a relative subdirectory inside the workspace, not '..' or absolute."
    target = WORKSPACE_ROOT / norm
    if not target.is_dir():
        return f"Error: '{norm}' does not exist under the workspace root ({WORKSPACE_ROOT})."

    prefix = norm + "/"
    rels = (str(p.relative_to(WORKSPACE_ROOT)).replace("\\", "/") for p in _iter_source_files())
    count = sum(1 for r in rels if r == norm or r.startswith(prefix))
    if count == 0:
        return f"Error: no source files found under '{norm}' — nothing would match. Scope not changed."

    try:
        (WORKSPACE_ROOT / ".mimir-scope").write_text(norm + "\n", encoding="utf-8")
    except OSError as e:
        return f"Error saving scope: {e}"
    _ACTIVE_SCOPE = norm
    return f"Scope set: {norm} ({count} files). Call set_scope(\"\") to search the full repo again."


@_tool
def get_status() -> str:
    """Report the current state of the mimir index for this workspace.

    Call this at the start of a session to understand what mimir knows about
    the workspace before using other tools. Key things to check:

    - symbol_index=warm means scope_task and verify_symbol_existence use a fast
      SQL index; symbol_index=building means they fall back to a slower linear
      scan and may miss recently added symbols
    - blueprints_cached shows how many files are already parsed
    - ignored_patterns reports how many .mimirignore rules are active (not
      the rules themselves — a file missing from search results is worth
      checking against .mimirignore directly)

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
                # COUNT(*) on the symbols table (total rows, not unique tokens) is a
                # table scan but uses the index; DISTINCT would be more expensive.
                # The row count is a good-enough proxy for index size.
                sym_count = _DISK_CACHE.execute(
                    "SELECT COUNT(*) FROM symbols"
                ).fetchone()[0]
            except Exception:
                pass

        index_state = "warm" if _FTS_READY else "building (tools still work, slower until complete)"
        warmup_state = (
            "complete" if _WARMUP_COMPLETE
            else "in progress — wait ~30s then call get_status again before using scope_task"
        )

        # Presence/count only, not the actual patterns/mappings/notes: this
        # tool is called at the start of every session and its output stays
        # in context for the rest of it, so it's re-paying full detail every
        # time is pure token bloat. Usage instructions for these tools live
        # in their own docstrings; get_status only needs to answer "is
        # anything active" for a quick readiness check (e.g. "why isn't this
        # file showing up" -> check ignored_patterns first).
        ignore_path = WORKSPACE_ROOT / ".mimirignore"
        if _MIMIRIGNORE_PATTERNS:
            ignore_section = f"ignored_patterns:   {len(_MIMIRIGNORE_PATTERNS)} active"
        elif ignore_path.exists():
            ignore_section = "ignored_patterns:   .mimirignore exists but contains no active patterns"
        else:
            ignore_section = "ignored_patterns:   none  (.mimirignore not found)"

        alias_path = WORKSPACE_ROOT / ".mimiraliases"
        if _MIMIRALIASES:
            alias_section = f"domain_aliases:     {len(_MIMIRALIASES)} active"
        elif alias_path.exists():
            alias_section = "domain_aliases:     .mimiraliases exists but contains no active mappings"
        else:
            alias_section = "domain_aliases:     none  (.mimiraliases not found)"

        notes_path = WORKSPACE_ROOT / ".mimirnotes"
        if _MIMIRNOTES:
            total_notes = sum(len(v) for v in _MIMIRNOTES.values())
            notes_section = (
                f"context_notes:      {len(_MIMIRNOTES)} prefix(es), {total_notes} note(s)"
            )
        elif notes_path.exists():
            notes_section = "context_notes:      .mimirnotes exists but contains no active notes"
        else:
            notes_section = "context_notes:      none  (.mimirnotes not found)"

        rev_count = len(_REVERSE_IMPORTS)
        watcher_line = (
            "file_watcher:       on (changes invalidate cache instantly)"
            if _FILE_WATCHER_ACTIVE
            else "file_watcher:       off  (pip install watchdog to enable)"
        )
        if rev_count > 0:
            rev_line = f"reverse_imports:    {rev_count:,} files mapped"
        elif _WARMUP_COMPLETE:
            rev_line = "reverse_imports:    not loaded this run  (built on demand by scope_task, or by the MCP server)"
        else:
            rev_line = "reverse_imports:    building..."

        if _FOCUS_WEIGHTS:
            focus_entries = "  ".join(
                f"'{k}' ×{v:.1f}" for k, v in _FOCUS_WEIGHTS.items()
            )
            focus_line = f"project_focus:      {focus_entries}"
        else:
            focus_line = "project_focus:      none"

        if _SEMANTIC_READY:
            sem_line = "semantic_search:    warm (FTS5+RRF)"
        else:
            sem_line = "semantic_search:    building (FTS5 index pending)"

        if _ACTIVE_SCOPE:
            scope_line = f"active_scope:       {_ACTIVE_SCOPE}"
        else:
            scope_line = "active_scope:       none  (searching entire repo)"

        lines = [
            f"workspace:          {WORKSPACE_ROOT}",
            f"source_files:       {total_files}",
            f"blueprints_cached:  {cached} in memory, {disk_count} on disk",
            f"symbol_index:       {index_state}",
            f"  indexed_tokens:   {sym_count:,}" if _FTS_READY else "  indexed_tokens:   (not yet built)",
            sem_line,
            f"warmup:             {warmup_state}",
            f"tree_sitter:        {'on' if TREE_SITTER_OK else 'off (regex fallback)'}",
            watcher_line,
            rev_line,
            scope_line,
            focus_line,
            ignore_section,
            alias_section,
            notes_section,
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"EXCEPTION in get_status: {e}"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _write_servers_json(path: Path, entry: dict, key: str = "servers") -> str:
    """Merge {key: {"mimir": entry}} into the mcp.json at path, preserving any
    other servers/keys already there — this file may be hand-maintained, so a
    blind overwrite would silently drop those. `key` is "mcpServers" for
    GitHub Copilot CLI. Returns 'created', 'updated', or 'unchanged' (mimir
    entry already present and identical)."""
    import json

    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raise ValueError(f"{path} exists but isn't valid JSON — not touching it")
    servers = existing.setdefault(key, {})
    if servers.get("mimir") == entry:
        return "unchanged"
    action = "updated" if "mimir" in servers else "created"
    servers["mimir"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return action


def _register_mcp_server(arg: str, is_global: bool, cwd: Path) -> None:
    """Register mimir as an MCP server with the named client — appending/
    merging the mimir entry alongside any servers already configured, never
    overwriting. claude goes through its own CLI (`claude mcp add`), verified
    to merge correctly on its own. copilot-cli has no CLI subcommand for this
    (only an interactive `/mcp add` form), so it merges the JSON file
    directly instead. Never raises: a missing CLI, failed subprocess call, or
    unparseable existing file prints a clear fallback message and setup()
    continues on to the instruction file regardless, since that half of the
    job is independent."""
    if arg == "copilot-cli":
        # GitHub Copilot CLI (the standalone `copilot` terminal tool) — its
        # own config file and schema ("mcpServers" + "type": "local" + a
        # required "tools" allowlist). Confirmed (GitHub's own docs) to have
        # NO real project/repo scope — only $COPILOT_HOME/mcp-config.json
        # (~/.copilot by default), always. So is_global doesn't change the
        # registration target here, only whether the (still project-level)
        # instructions file gets written below.
        home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))
        path = home / "mcp-config.json"
        entry = {"type": "local", "command": "mimir", "args": [], "env": {}, "tools": ["*"]}
        try:
            result = _write_servers_json(path, entry, key="mcpServers")
            if result == "unchanged":
                print(f"skipped  MCP registration  (mimir already registered in {path})")
            else:
                print(f"{result}  {path}  (mimir server entry — Copilot CLI has no project "
                      "scope, this always applies to every repo)")
        except Exception as e:
            print(f"WARNING: could not write {path}: {e}")
        return

    if arg == "claude":
        cli = shutil.which("claude")
        if not cli:
            print("skipped  MCP registration  ('claude' CLI not found on PATH — "
                  "install Claude Code, then re-run mimir-setup)")
            return
        scope = "user" if is_global else "project"
        try:
            proc = subprocess.run(
                [cli, "mcp", "add", "mimir", "--scope", scope, "--", "mimir"],
                capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
            )
            out = (proc.stdout or proc.stderr or "").strip()
            if proc.returncode == 0:
                print(f"registered  mimir with Claude Code ({scope} scope)")
            elif "already exists" in out.lower():
                print(f"skipped  MCP registration  (mimir already registered at {scope} scope)")
            else:
                print(f"WARNING: 'claude mcp add' failed: {out or 'unknown error'}")
        except Exception as e:
            print(f"WARNING: could not run 'claude mcp add': {e}")


def setup() -> None:
    """Console-script entry point: register mimir as an MCP server with one
    client and drop its workflow-instruction file into the current project.

    Usage: `mimir-setup [claude|copilot-cli] [--global]` — client defaults to
    claude when no arg is given ("cli" is accepted as a shorthand alias for
    "copilot-cli"). Both are CLI/terminal tools: claude means Claude Code,
    copilot-cli means the standalone `copilot` GitHub Copilot CLI.

    Without --global (default): registers mimir at PROJECT scope — a committed
    .mcp.json for claude (via `claude mcp add --scope project`) — and writes
    CLAUDE.md or copilot-instructions.md plus a starter .mimirignore into the
    current repo. Use this once you're ready to share mimir with a team on
    this project — everything it creates is meant to be committed.

    copilot-cli has no real project/repo scope (confirmed in GitHub's own
    docs — only a home-directory mcp-config.json), so its MCP registration is
    always at that one location regardless of --global; only the instructions
    file changes based on the flag, same as for claude.

    --global: registers mimir at USER scope instead (available in every
    project on this machine, nothing in any repo) and writes the instructions
    to your user profile instead — ~/.claude/CLAUDE.md for claude,
    $HOME/.copilot/copilot-instructions.md for copilot-cli (confirmed in
    GitHub's docs; overridable via COPILOT_CUSTOM_INSTRUCTIONS_DIRS). Skips
    .mimirignore, which is inherently project-specific. Use this while mimir
    is still your personal tool — drop --global later, in a given project,
    once you're ready to make it a shared team setup there; the two scopes
    layer without conflict.

    Registration falls back to a printed warning (never aborts the rest of
    setup) if a required CLI isn't on PATH, a subprocess call fails, or an
    existing mcp-config.json isn't valid JSON — see HowTo.md for the manual
    .mcp.json snippet in that case.
    """
    argv = sys.argv[1:]
    is_global = "--global" in argv
    positional = [a for a in argv if a != "--global"]
    arg = (positional[0].strip().lower() if positional else "claude")
    if arg == "cli":
        arg = "copilot-cli"
    if arg not in ("claude", "copilot-cli"):
        print(f"ERROR: unknown client {arg!r} — expected 'claude' or 'copilot-cli'.")
        return

    cwd = Path.cwd()
    _register_mcp_server(arg, is_global, cwd)
    mimir_marker = "## Code exploration — use mimir tools"
    availability_line = (
        "You have mimir MCP tools available in every project on this machine."
        if is_global else
        "This project has mimir MCP tools available."
    )

    if arg == "claude":
        claude_md = (Path.home() / ".claude" / "CLAUDE.md") if is_global else (cwd / "CLAUDE.md")
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        if claude_md.exists() and mimir_marker in claude_md.read_text(encoding="utf-8"):
            print(f"skipped  {claude_md}  (mimir section already present)")
        else:
            snippet = (
                f"\n{mimir_marker}\n\n"
                f"{availability_line} Use them before reading raw files.\n\n"
                "At the start of any coding session:\n"
                "1. Call `get_status` to check the index is ready and see active exclusions\n"
                "2. If the user names or clearly implies a specific sub-project/app/API area (e.g.\n"
                "   \"the mobile app\", \"the API\"), call `set_focus(\"matching-prefix:3\")` immediately —\n"
                "   or pass `focus=\"prefix:3\"` directly on individual `scope_task`/`semantic_search`\n"
                "   calls. In a multi-sub-project repo, unscoped ranking silently\n"
                "   defaults to whichever sub-project has the most indexed symbols; it will NOT\n"
                "   reliably surface the right area on its own\n"
                "3. Call `get_architecture()` for a high-level map of the whole codebase (cheap) —\n"
                "   once per session if you don't already have this context warm; skip it for a\n"
                "   narrow follow-up task later in the same session\n"
                "4. Call `get_changed_files()` to see what is currently in flight vs main — same,\n"
                "   once per session, not per task\n"
                "5. Call `scope_task(\"describe what you want to do\")` to find relevant files\n\n"
                "For any task involving existing code:\n"
                "- Use `scope_task` before opening files — it finds the right files in one call\n"
                "- Use `get_symbol(path, name)` to read ONE function or class body instead of the whole file\n"
                "- Use `get_file_structure` to see a file's full symbol map before reading it line by line\n"
                "- Skip `verify_symbol_existence` if the symbol already showed up in scope_task's\n"
                "  \"Matched symbols\" with a file:line — that's already confirmation. Use it only for a\n"
                "  symbol scope_task didn't surface, before assuming it exists\n"
                "- Use `find_callers` after `verify_symbol_existence` to trace impact\n"
                "- Use `get_dependents(path)` to find what else imports a file before changing it\n"
                "- Use `get_imports` when an unfamiliar symbol appears and you need to trace its origin\n"
                "- Call `record_alias(domain_term, code_name)` when you discover a feature name maps\n"
                "  to a different code name — future scope_task searches will expand it automatically\n"
                "- Call `add_ignore(pattern, reason)` when you encounter vendor/generated/test files\n"
                "  that add noise — always tell the user what you are adding and why first\n"
            )
            with open(claude_md, "a", encoding="utf-8") as f:
                f.write(snippet)
            action = "updated" if claude_md.exists() else "created"
            print(f"{action}  {claude_md}  (mimir section appended)")

    if arg == "copilot-cli":
        # Global personal instructions: $HOME/.copilot/copilot-instructions.md
        # (confirmed in GitHub's docs, overridable via COPILOT_CUSTOM_INSTRUCTIONS_DIRS).
        # Project scope: .github/copilot-instructions.md, same file/convention
        # as GitHub Copilot everywhere else.
        if is_global:
            copilot_home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))
            copilot_home.mkdir(parents=True, exist_ok=True)
            copilot_instructions = copilot_home / "copilot-instructions.md"
        else:
            github_dir = cwd / ".github"
            github_dir.mkdir(exist_ok=True)
            copilot_instructions = github_dir / "copilot-instructions.md"
        if copilot_instructions.exists() and mimir_marker in copilot_instructions.read_text(encoding="utf-8"):
            print(f"skipped  {copilot_instructions}  (mimir section already present)")
        else:
            copilot_snippet = (
                f"{mimir_marker}\n\n"
                f"{availability_line} Always use them before using built-in search or reading files.\n\n"
                "Workflow for any coding session:\n"
                "1. Call `get_status` to confirm the index is ready\n"
                "2. If the user names or clearly implies a specific sub-project/app/API area (e.g. "
                "\"the mobile app\", \"the API\"), call `set_focus(\"matching-prefix:3\")` immediately — "
                "or pass `focus=\"prefix:3\"` directly on individual `scope_task`/`semantic_search` "
                "calls. In a multi-sub-project repo, unscoped ranking silently "
                "defaults to whichever sub-project has the most indexed symbols; it will NOT "
                "reliably surface the right area on its own\n"
                "3. Call `get_architecture()` for a high-level map of the whole codebase (one cheap "
                "call) — once per session if you don't already have this context warm; skip it for a "
                "narrow follow-up task later in the same session\n"
                "4. Call `get_changed_files()` to see what is currently in flight vs main — same, "
                "once per session, not per task\n"
                "5. Call `scope_task` with a description of the task — use its ranked file list and "
                "suggested get_symbol calls, do NOT fall back to glob or grep\n"
                "   - Tip: use technical/class names when known (e.g. 'RectificationFilter') "
                "rather than feature names (e.g. 'corrective actions filter')\n"
                "6. Call `get_symbol(path, name)` to read ONE function or class body — much cheaper "
                "than reading the whole file\n"
                "7. Use `get_dependents(path)` before changing a shared file to see blast radius\n"
                "8. When you discover a domain/feature term maps to a code name, call `record_alias` "
                "to save it — future scope_task searches will expand it automatically\n"
                "9. When you encounter vendor/generated/noisy files, tell the user what you found, "
                "ask for confirmation, then call `add_ignore(pattern, reason)` to exclude them\n\n"
                "Do not use built-in file search, glob, or grep if mimir tools are available.\n"
            )
            with open(copilot_instructions, "a", encoding="utf-8") as f:
                f.write(copilot_snippet)
            action = "updated" if copilot_instructions.exists() else "created"
            print(f"{action}  {copilot_instructions}  (mimir section appended)")

    if is_global:
        print("skipped  .mimirignore  (project-specific — run mimir-setup again without --global in a project to add it)")
        client_name = {"claude": "Claude Code", "copilot-cli": "Copilot CLI"}[arg]
        print(f"\nDone. Restart {client_name} to pick up the global registration and instructions.")
        return

    mimirignore = cwd / ".mimirignore"
    if mimirignore.exists():
        print(f"skipped  {mimirignore}  (already exists)")
    else:
        mimirignore.write_text(
            "# .mimirignore — gitignore-style patterns for files mimir should skip.\n"
            "# Generated by mimir-setup. Edit freely; mimir reloads this automatically.\n"
            "# Use add_ignore(pattern, reason) in Claude/Copilot to add patterns on the fly.\n"
            "\n"
            "# Build and compiler output\n"
            "**/obj/**\n"
            "**/bin/**\n"
            "**/build/**\n"
            "**/dist/**\n"
            "**/__pycache__/**\n"
            "**/.next/**\n"
            "**/.nuxt/**\n"
            "\n"
            "# Package managers and vendored dependencies\n"
            "**/node_modules/**\n"
            "**/vendor/**\n"
            "**/Pods/**\n"
            "**/wwwroot/lib/**\n"
            "**/Packages/**\n"
            "\n"
            "# Generated and migration files\n"
            "**/Migrations/**\n"
            "**/*.generated.*\n"
            "**/*.Designer.cs\n"
            "**/*.g.cs\n"
            "**/*.g.i.cs\n"
            "**/Generated/**\n"
            "\n"
            "# Minified / bundled JS and CSS\n"
            "**/*.min.js\n"
            "**/*.min.css\n"
            "**/*.bundle.js\n"
            "**/*.map\n"
            "\n"
            "# Android resource XML (layout, drawables — not code symbols)\n"
            "**/res/layout/**\n"
            "**/res/drawable/**\n"
            "**/res/menu/**\n"
            "**/res/values/**\n"
            "**/androidTest/**\n"
            "\n"
            "# iOS / macOS frameworks and CocoaPods output\n"
            "**/Pods/**\n"
            "**/DerivedData/**\n"
            "\n"
            "# Test projects (uncomment if test noise outweighs the benefit)\n"
            "# **/*.Tests/**\n"
            "# **/*.Specs/**\n"
            "# **/*Tests.Integration/**\n"
            "# **/*Tests.Unit/**\n",
            encoding="utf-8",
        )
        print(f"created  {mimirignore}")

    client_name = {"claude": "Claude Code", "copilot-cli": "Copilot CLI"}[arg]
    print(f"\nDone. Restart {client_name} to pick up the registration and instructions.")


_CLI_HELP = """\
mimir — structural code index for Claude Code and GitHub Copilot

SETUP
  mimir-setup [claude|copilot-cli]            Registers the MCP server at PROJECT scope
                                               (committed .mcp.json for claude; copilot-cli
                                               has no project scope, always registers at
                                               ~/.copilot/mcp-config.json) and writes CLAUDE.md
                                               or copilot-instructions.md (default: claude)
                                               + .mimirignore — for sharing mimir with a team
  mimir-setup [claude|copilot-cli] --global   Same, but at USER scope and written to your
                                               user profile instead — applies to every
                                               project, nothing added to this repo (use while
                                               mimir is still your personal tool)

  "cli" is a shorthand alias for "copilot-cli" (the standalone `copilot` terminal tool).

TERMINAL COMMANDS
  mimir hint   "<rough terms>"  Cheap first pass: discover what the codebase calls
                                things before writing a scope/AI prompt
  mimir scope  "<task>"    Find files relevant to a plain-English task description
  mimir area   "<task>"    Directory tree of where a task's matches cluster —
                            useful in a monorepo to find which sub-project to scope into
  mimir scope --set <path>  Hard-narrow every command to one directory until reset
                            (same effect as cd, but no reindex — keeps the whole-repo
                            index warm and just filters results)
  mimir scope --reset       Clear an active scope, search the whole repo again
  mimir find   <Symbol>    Locate a symbol definition across the workspace
  mimir callers <Symbol>   Find every call site and usage of a symbol
  mimir status             Show index state, file count, active scope, and exclusions
  mimir audit              Scan the index for noise (bloated files, over-saturated
                            terms) and suggest .mimirignore patterns to fix it
  mimir --help             Show this help

EXAMPLES
  mimir hint  "quiet zone notification volume"
  mimir scope "change how jobs are retried on failure"
  mimir area  "SignalR notifications"
  mimir scope --set src/carps-web
  mimir find   JobScheduler
  mimir callers authenticate
  mimir scope --reset
  mimir status

MCP TOOLS (available to Claude Code and GitHub Copilot)
  get_status               Index state, file count, ignore patterns, domain aliases — call first
  get_architecture         High-level workspace map: directories, files, top-level symbols
  scope_task               Find relevant files from a task description
  scope_area               Directory-tree view of where a task's matches cluster (monorepos)
  set_scope                Hard-narrow every tool to one directory; set_scope("") clears
  get_file_structure       Compact symbol map of a single file (classes, methods, line nos)
  get_symbol               Full source of ONE named symbol — efficient middle ground
  get_directory_structure  Symbol maps for every file under a directory
  get_changed_files        Blueprints of files changed vs a git branch (session orientation)
  get_imports              Resolve imports to workspace files or external packages
  get_dependents           Find all files that directly import a given file (blast radius)
  verify_symbol_existence  Confirm a symbol is defined and find its location
  find_callers             Find every call site and usage of a symbol
  record_alias             Save a domain/feature name → code name mapping for future searches
  add_ignore               Add a glob pattern to .mimirignore to exclude noisy files

EXCLUDING FILES
  mimir-setup creates a starter .mimirignore covering common noise (build output,
  node_modules, vendored assets, minified JS). To refine it:
  - Edit .mimirignore directly with gitignore-style patterns
  - Ask Claude/Copilot to call add_ignore(pattern, reason) when it spots noisy files
  Run `mimir status` to confirm active patterns.

DOMAIN ALIASES
  Create .mimiraliases in the project root to map feature names to code names:
    corrective actions = RectificationFilter
    live tutor = LiveTutor, GeminiLive
  Or let Claude/Copilot call record_alias() to build it automatically.
  Mimir applies aliases before every scope_task search.

ENVIRONMENT VARIABLES
  MCP_WORKSPACE_ROOT       Root of the repo mimir maps (default: current dir)
  MCP_MAX_FILE_BYTES       Skip files larger than this in bytes (default: 2000000)

Without arguments, mimir starts as an MCP server on stdio — this is what your
AI client launches. You never need to run this manually.
"""


def _cli_run(subcommand: str, arg: str) -> None:
    """Run a single tool query, print the result, and exit.

    Each CLI invocation is a fresh process — nothing persists in memory between
    runs except what's on disk (_DISK_CACHE). _load_disk_cache() restores the
    blueprint cache and symbol/FTS tables from the last warm run almost
    instantly. Only run the expensive parts of _warm_cache() that a given
    subcommand actually needs, and only do a full rebuild when the disk cache
    isn't warm yet (first run, or repo changed enough to invalidate it).
    """
    global _WARMUP_COMPLETE
    _load_disk_cache()
    if not _FTS_READY:
        print(f"[mimir] no warm index found — building one ({len(_iter_source_files())} files, "
              f"first run only)...", file=sys.stderr, flush=True)
        _warm_cache()
    else:
        if subcommand in ("scope", "area"):
            # scope_task/scope_area's ranking needs these in-memory structures.
            # _build_token_df/_build_path_strings aren't disk-persisted so they
            # rebuild every process regardless (cheap — SQL query + blueprint
            # scan). _REVERSE_IMPORTS IS disk-persisted (reverse_imports table)
            # once a previous full _warm_cache() has run — try that first to
            # avoid re-reading the first 8KB of every JS/TS/Python/Java/Kotlin
            # file in the workspace on every single CLI invocation.
            print("[mimir] loading rankings...", file=sys.stderr, flush=True)
            _build_token_df()
            _build_path_strings()
            if not _load_reverse_imports_from_disk():
                _build_reverse_imports()
        # status/hint/find/callers read straight off the disk-backed symbol
        # and FTS tables loaded above — no further warmup needed. Either way,
        # everything this invocation needs is ready: mark warmup complete so
        # get_status doesn't report "in progress" for a run that's already done.
        _WARMUP_COMPLETE = True

    if subcommand == "hint":
        print(scope_hint(arg))
    elif subcommand == "scope":
        print(scope_task(arg))
    elif subcommand == "area":
        print(scope_area(arg))
    elif subcommand == "find":
        print(verify_symbol_existence(arg))
    elif subcommand == "callers":
        print(find_callers(arg))
    elif subcommand == "status":
        print(get_status())
    elif subcommand == "audit":
        print(audit_index_health())
    else:
        print(f"Unknown subcommand '{subcommand}'. Run `mimir` with no arguments for help.")
        sys.exit(1)


def main() -> None:
    """Console-script / module entry point.

    Without arguments: starts the MCP stdio server (used by AI clients).
    With a subcommand:  runs a single query and prints the result.
    """
    try:
        _main()
    except KeyboardInterrupt:
        print("\n[mimir] cancelled", file=sys.stderr)
        sys.exit(130)


def _main() -> None:
    args = sys.argv[1:]

    # `mimir scope --set <path>` / `mimir scope --reset` are scope-state
    # management, distinct from `mimir scope "<task>"` (ranking query) — the
    # leading "--" makes them unambiguous since a real task description never
    # starts with it. Handled before the generic dispatch below.
    if args[:1] == ["scope"] and len(args) > 1 and args[1] in ("--set", "--reset"):
        if args[1] == "--reset":
            print(set_scope(""))
        else:
            if len(args) < 3:
                print("Usage: mimir scope --set <path>")
                sys.exit(1)
            print(set_scope(" ".join(args[2:])))
        return

    _CLI_ARG_HINT = {
        "hint": "rough terms",
        "scope": "task description",
        "area": "task description",
        "find": "SymbolName",
        "callers": "SymbolName",
    }
    if args and args[0] in (*_CLI_ARG_HINT, "status", "audit"):
        subcommand = args[0]
        arg = " ".join(args[1:]) if len(args) > 1 else ""
        if subcommand not in ("status", "audit") and not arg:
            print(f"Usage: mimir {subcommand} <{_CLI_ARG_HINT[subcommand]}>")
            sys.exit(1)
        _cli_run(subcommand, arg)
        return

    if args and args[0] in ("-h", "--help", "help"):
        print(_CLI_HELP)
        return

    # No subcommand — start as MCP server. fastmcp is imported here, not at
    # module level, so CLI subcommands (handled above, already returned by
    # this point) never pay its ~150-250ms import cost.
    from fastmcp import FastMCP
    mcp = FastMCP("mimir")
    for fn in _TOOLS:
        mcp.tool()(fn)

    disk_loaded = _load_disk_cache()
    total_files = len(_iter_source_files())

    disk_status = f"disk_cache={disk_loaded}/{total_files} loaded" if _DISK_CACHE else "disk_cache=off"
    fts_status = f"symbol_index={'warm' if _FTS_READY else 'building'}"
    print(
        f"[mimir] root={WORKSPACE_ROOT} "
        f"tree_sitter={'on' if TREE_SITTER_OK else 'off (regex fallback)'}  {disk_status}  {fts_status}",
        file=sys.stderr,
    )
    import threading
    threading.Thread(target=_warm_cache, daemon=True, name="mimir-warmup").start()
    watcher_state = "on" if _start_file_watcher() else "off (install watchdog for auto-invalidation)"
    print(f"[mimir] file_watcher={watcher_state}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
