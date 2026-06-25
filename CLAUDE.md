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
3. **Structure extraction** — `_extract_tree_sitter()` (preferred) and `_extract_regex()` (fallback). Both return the same dense line format `L{lineno}  {indent}{signature}`. `_build_blueprint()` orchestrates cache → tree-sitter → regex.
4. **MCP tools** (three total):
   - `get_file_structure` — blueprint for one file
   - `verify_symbol_existence` — grep-style search across all source files using cached blueprints
   - `execute_local_sandbox` — run python/bash snippets with timeout + process-group kill
5. **Entry point** — `main()` prints a status banner to stderr, then calls `mcp.run()`.

## Adding language support

Add an entry to `EXT_LANG` mapping file extension → `(tree_sitter_language_name, regex_profile_key)`, and optionally add a regex profile to `REGEX_PROFILES` for the no-tree-sitter fallback. The tree-sitter extraction is language-agnostic (driven by `DEF_SUFFIXES`/`DEF_EXACT`); regex profiles only matter as fallback.

## Key design constraints

- **stdout is sacred** — every tool catches all exceptions and returns a string; nothing may raise out of a tool handler.
- **stderr for diagnostics** — startup banner and any debug output go to `sys.stderr`, never `sys.stdout`.
- **Memory bounded** — the `_CACHE` FIFO eviction and `MAX_FILE_BYTES` guard are load-bearing for large repos; don't break them.
- Python snippet sandbox uses `-I` (isolated mode) to ignore user site-packages; bash snippets have no equivalent isolation — note this in tool doc changes.
