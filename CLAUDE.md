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
| `MCP_ENABLE_SANDBOX` | `1` | Enable `execute_local_sandbox` tool |
| `MCP_SANDBOX_TIMEOUT` | `10` | Hard ceiling (seconds) for sandbox runs |

## Architecture

Everything lives in `mimir.py`. There is no package structure. Key sections in order:

1. **Config constants** — `BLACKLIST_DIRS`, `EXT_LANG` (extension → tree-sitter lang + regex profile key), `DEF_SUFFIXES`/`DEF_EXACT`/`BODY_TYPES` (AST node classification).
2. **LRU cache** — `_CACHE` (OrderedDict, max 2048 entries), keyed by absolute path, validated by `(mtime, size)`. Bounded to keep memory light on large repos.
3. **Structure extraction** — `_extract_tree_sitter()` (preferred) and `_extract_regex()` (fallback). Both return the same dense line format `L{lineno}  {indent}{signature}`. Tree-sitter blueprints additionally end with a `#strings` section listing exception/log message literals (`_literal_row`) so error text from tickets is searchable. `_build_blueprint()` orchestrates cache → tree-sitter → regex.
4. **Warm-up** — `_warm_cache()` runs at startup: walks all source files, builds blueprints, populates `_SYMBOL_INDEX`, builds the token document-frequency cache (`_build_token_df`, backs IDF ranking + length norm), builds the FTS5 table, `_REVERSE_IMPORTS` map, and `_ARCHITECTURE_MAP`, then starts the file watcher (`watchdog`).
5. **File watcher** — `_start_file_watcher()` invalidates `_CACHE` and `_REVERSE_IMPORTS` entries on file change/create/delete events within the workspace.
6. **MCP tools** (20 total):
   - `get_status` — index health, file count, exclusion patterns, domain aliases, active focus weights
   - `set_focus` — save per-prefix score multipliers to `.mimir-focus`; takes effect immediately; `persist=False` for session-only weights
   - `get_architecture` — high-level directory/symbol map of the whole workspace
   - `get_changed_files` — blueprints of files changed vs a git base branch
   - `scope_hint` — cheap symbol lookup that returns what the codebase calls things + suggested query
   - `scope_task` — ranked files + suggested `get_symbol` calls for a plain-English task description; BM25-style scoring (capped TF × IDF × doc-length norm) over the exact-token symbol index, plus compound-bigram search ("Unavailable Types" → `UnavailableType`); accepts optional `focus="prefix:weight"` for a per-call weight override that does not modify `.mimir-focus`
   - `semantic_search` — FTS5 BM25 over decomposed identifier sub-tokens, RRF-fused with symbol-index hits; for when you know the concept but not the code name
   - `get_context` — one-shot: ranked files + blueprints + top symbol bodies for a task description; replaces the scope_task → get_file_structure → get_symbol chain in a single call
   - `get_symbol` — full body of one named function/class/method
   - `get_file_structure` — blueprint (signatures + line numbers, bodies stripped) for one file
   - `get_directory_structure` — blueprints for all source files under a directory
   - `get_imports` — resolved import map for one file; `[workspace?]` entries auto-suggest definition file via symbol index; for C#/Kotlin/Swift unresolved namespace imports, the tool now auto-resolves by type name
   - `verify_symbol_existence` — search the symbol index for a definition
   - `find_callers` — text search for every call/usage site of a symbol; auto-uses ripgrep (`rg`) if on PATH for ~10× speedup on large repos
   - `get_dependents` — reverse import index: which files import a given file
   - `record_alias` — save a domain-term → code-name mapping for `scope_task` expansion
   - `record_note` — attach a free-text contextual note to a path prefix; surfaced as prose in `get_file_structure`/`get_directory_structure`/`scope_task`, never used for ranking
   - `add_ignore` — append a pattern to `.mimirignore` and reload immediately
   - `audit_index_health` — reports bloated files and over-saturated search terms in the index
   - `execute_local_sandbox` — run python/bash snippets with timeout + process-group kill
7. **Entry point** — `main()` prints a status banner to stderr, then calls `mcp.run()`.

## Adding language support

Add an entry to `EXT_LANG` mapping file extension → `(tree_sitter_language_name, regex_profile_key)`, and optionally add a regex profile to `REGEX_PROFILES` for the no-tree-sitter fallback. The tree-sitter extraction is language-agnostic (driven by `DEF_SUFFIXES`/`DEF_EXACT`); regex profiles only matter as fallback.

## Key design constraints

- **stdout is sacred** — every tool catches all exceptions and returns a string; nothing may raise out of a tool handler.
- **stderr for diagnostics** — startup banner and any debug output go to `sys.stderr`, never `sys.stdout`.
- **Memory bounded** — the `_CACHE` FIFO eviction and `MAX_FILE_BYTES` guard are load-bearing for large repos; don't break them.
- Python snippet sandbox uses `-I` (isolated mode) to ignore user site-packages; bash snippets have no equivalent isolation — note this in tool doc changes.
