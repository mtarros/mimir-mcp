# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Mimir is a single-file local MCP server (`mimir.py`) that produces compact "structural blueprints" of source files — symbol maps with line numbers, bodies stripped — to reduce token usage when an LLM explores a codebase. It uses tree-sitter for AST-based extraction, falling back to regex patterns when tree-sitter is unavailable.

Transport: stdio (JSON-RPC piped via stdin/stdout). The `fastmcp` library handles the MCP protocol layer; `mimir.py` registers tools on the `FastMCP` instance.

## Setup and installation

```bash
# Recommended (isolated, adds `mimir` to PATH):
pipx install .

# Or for development (editable):
pip install -e .

# Dependencies only (without installing the package):
pip install fastmcp "tree-sitter>=0.23" "tree-sitter-language-pack>=1.0"
```

## Running the server

```bash
mimir                        # after install (stdio transport)
python mimir.py              # directly
MCP_WORKSPACE_ROOT=/path/to/repo mimir   # point at a specific repo
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MCP_WORKSPACE_ROOT` | `os.getcwd()` | Repo root the server maps |
| `MCP_MAX_FILE_BYTES` | `2000000` | Skip files larger than this |

## Testing

```bash
pip install pytest pytest-asyncio   # not in pyproject deps; install separately
pytest                               # full suite
pytest tests/test_tools.py           # one file
pytest tests/test_tools.py -k get_symbol   # one test by name
```

`tests/conftest.py` points `MCP_WORKSPACE_ROOT` at a fresh temp dir before `mimir` is imported, so tests never touch the real workspace cache. `test_smoke.py` spawns a real `mimir` subprocess per test (~1-2s each) — expect it to dominate wall-clock time vs the rest of the suite.

## Architecture

Everything lives in `mimir.py`. There is no package structure. Key sections in order:

1. **Config constants** — `BLACKLIST_DIRS`, `EXT_LANG` (extension → tree-sitter lang + regex profile key), `DEF_SUFFIXES`/`DEF_EXACT`/`BODY_TYPES` (AST node classification).
2. **LRU cache** — `_CACHE` (OrderedDict, max 2048 entries), keyed by absolute path, validated by `(mtime, size)`. Bounded to keep memory light on large repos.
3. **Structure extraction** — `_extract_tree_sitter()` (preferred) and `_extract_regex()` (fallback). Both return the same dense line format `L{lineno}  {indent}{signature}`. Tree-sitter blueprints additionally end with a `#strings` section listing exception/log message literals (`_literal_row`) so error text from tickets is searchable. `_build_blueprint()` orchestrates cache → tree-sitter → regex.
4. **Warm-up** — `_warm_cache()` runs at startup: walks all source files, builds blueprints, populates `_SYMBOL_INDEX`, builds the token document-frequency cache (`_build_token_df`, backs IDF ranking + length norm), builds the FTS5 table, `_REVERSE_IMPORTS` map, and `_ARCHITECTURE_MAP`, then starts the file watcher (`watchdog`).
5. **File watcher** — `_start_file_watcher()` invalidates `_CACHE` and `_REVERSE_IMPORTS` entries on file change/create/delete events within the workspace.
6. **MCP tools** (19 total, registered via `@_tool` — a plain list-collector — rather than `@mcp.tool()` directly; `main()`'s server-start branch loops over the collected list and registers each onto the real `FastMCP` instance, so a bare CLI invocation never imports `fastmcp`). Every registered tool's schema is transmitted to the AI client on every turn, so tools are deliberately few and docstrings deliberately lean — human diagnostics belong in the CLI, not the MCP surface:
   - `get_status` — index health, file count, exclusion patterns, domain aliases, active focus weights, active scope
   - `set_focus` — save per-prefix score *multipliers* to `.mimir-focus` (soft ranking bias — out-of-focus files still appear, just lower-scored); takes effect immediately; `persist=False` for session-only weights
   - `set_scope` — hard-narrow every search tool to one directory via `.mimir-scope` (files outside are excluded entirely, not just down-weighted); persists until cleared with `set_scope("")` (mirrors `set_focus("")`). Distinct from `set_focus`: scope filters the result set, focus re-ranks within whatever scope allows through — the two compose. Filtering happens at the read layer (`_in_scope()`, checked in `_symbol_hits`/`_symbol_hits_multi`, the end of `_score_task_files`, and `find_callers`'s `path_pairs`), not the index layer — the full-repo index stays warm, so setting/resetting scope is instant, no reindex.
   - `get_architecture` — high-level directory/symbol map of the whole workspace
   - `get_changed_files` — blueprints of files changed vs a git base branch
   - `scope_hint` — cheap symbol lookup that returns what the codebase calls things + suggested query
   - `scope_task` — ranked files + suggested `get_symbol` calls for a plain-English task description; BM25-style scoring (capped TF × IDF × doc-length norm) over the exact-token symbol index, plus compound-bigram search ("Unavailable Types" → `UnavailableType`); accepts optional `focus="prefix:weight"` for a per-call weight override that does not modify `.mimir-focus`; pass `include_blueprints=True` for ranked files + full blueprints in one call. Its scoring core lives in `_score_task_files()`, shared with `scope_area`.
   - `scope_area` — like `scope_task` but rolls matches up into an indented directory tree instead of a flat file list, for finding which sub-project a task lives in in a large monorepo; suggests a `set_scope(...)` call for the highest-concentration folder
   - `semantic_search` — FTS5 BM25 over decomposed identifier sub-tokens, RRF-fused with symbol-index hits; for when you know the concept but not the code name
   - `get_symbol` — full body of one named function/class/method
   - `get_file_structure` — blueprint (signatures + line numbers, bodies stripped) for one file
   - `get_directory_structure` — blueprints for all source files under a directory
   - `get_imports` — resolved import map for one file; `[workspace?]` entries auto-suggest definition file via symbol index; for C#/Kotlin/Swift unresolved namespace imports, the tool now auto-resolves by type name
   - `verify_symbol_existence` — search the symbol index for a definition
   - `find_callers` — finds every call/usage site of a symbol; primary path is an in-process tree-sitter `Query`/`QueryCursor` pass (`_cs_find_call_sites`) that matches every identifier-kind leaf equal to the symbol and excludes only the definition's own name and import/using statements, so it catches call sites a fixed-pattern matcher would miss (bare `this.foo`/`x.foo` references, the symbol as a generic type argument, or as the object before a dot) since it doesn't need the symbol to be in a specific "callable" position; falls back to ripgrep (`rg`)/plain text-scan for a language whose grammar isn't available or when tree-sitter isn't installed. Note: this pass uses `tree_sitter.Parser(get_language(lang))` directly rather than `_get_ts_parser()` — `tree_sitter_language_pack.get_parser()`'s nodes use a different, `Query`-incompatible method-based API (`.kind()`, `.child_count()`) vs the standard property-based `tree_sitter.Node` (`.type`, `.children`) that `Query`/`QueryCursor` require
   - `get_dependents` — reverse import index: which files import a given file
   - `record_alias` — save a domain-term → code-name mapping for `scope_task` expansion
   - `record_note` — attach a free-text contextual note to a path prefix; surfaced as prose in `get_file_structure`/`get_directory_structure`/`scope_task`, never used for ranking
   - `add_ignore` — append a pattern to `.mimirignore` and reload immediately
7. **CLI** (`mimir <subcommand>`, see `_cli_run`/`_main`) — thin wrappers around the same tool functions for terminal use outside an AI client: `hint`, `scope "<task>"`, `scope --set <path>` / `scope --reset` (state management for `set_scope`, disambiguated from the task-string form by the leading `--`), `area "<task>"`, `find <Symbol>`, `callers <Symbol>`, `status`, `audit` (index-health diagnostic — CLI-only on purpose, it's a human tuning aid for `.mimirignore`, not an agent tool). Each invocation is a fresh process — `_load_disk_cache()` restores blueprints/symbol/FTS tables from the on-disk SQLite cache almost instantly; `.mimir-focus`/`.mimir-scope`/`.mimirignore`/`.mimiraliases`/`.mimirnotes` are re-read at module import every run. Only `scope`/`area` rebuild the in-memory-only ranking structures (`_build_token_df`, `_build_path_strings`, `_build_reverse_imports`) each run, since those aren't disk-cached.
8. **Entry point** — `main()` prints a status banner to stderr, then calls `mcp.run()`.

## Adding language support

Add an entry to `EXT_LANG` mapping file extension → `(tree_sitter_language_name, regex_profile_key)`, and optionally add a regex profile to `REGEX_PROFILES` for the no-tree-sitter fallback. The tree-sitter extraction is language-agnostic (driven by `DEF_SUFFIXES`/`DEF_EXACT`); regex profiles only matter as fallback.

## Key design constraints

- **stdout is sacred** — every tool catches all exceptions and returns a string; nothing may raise out of a tool handler.
- **stderr for diagnostics** — startup banner and any debug output go to `sys.stderr`, never `sys.stdout`.
- **Memory bounded** — the `_CACHE` FIFO eviction and `MAX_FILE_BYTES` guard are load-bearing for large repos; don't break them.
- **Lean MCP surface** — every registered tool's schema costs tokens on every agent turn; new tools need strong evidence of agent (not human) value, and human diagnostics go in the CLI instead.
