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
4. **Warm-up** — `_warm_cache()` runs at startup: walks all source files, builds blueprints, populates `_SYMBOL_INDEX`, builds the token document-frequency cache (`_build_token_df`, backs IDF ranking + length norm), builds the FTS5 table, `_REVERSE_IMPORTS` map, `_ARCHITECTURE_MAP`, and `.mimir-overview.md` (`_write_overview()`, see point 6a), then starts the file watcher (`watchdog`).
5. **File watcher** — `_start_file_watcher()` invalidates `_CACHE` and `_REVERSE_IMPORTS` entries on file change/create/delete events within the workspace, and debounces a `.mimir-overview.md` refresh (`_schedule_overview_write()`, 5s quiet window via `threading.Timer` — one architecture rebuild per burst of saves, not one per file).

### 6a. Registered MCP surface (v3, 2026-07-09): 2 tools, not 18 — not even 3

Only `locate` and `inspect` are registered via `mcp.tool()` in `_main()`'s server-start branch (`_MCP_TOOLS = [locate, inspect]`). Every registered tool's schema is transmitted to the AI client on every turn — cut from ~15,100 (18 tools) to ~3,800 (an interim 3-tool design that also registered a `mimir` dispatcher) to ~2,622 wire chars (~655 tokens) by this second cut; `TestToolRegistration::test_wire_budget_under_3000_chars` in `tests/test_smoke.py` guards the budget going forward. `mimir_dispatch` (below) was pulled from MCP registration too: its commands are occasional/session-level actions (status, alias, set_scope, ...), not needed every task the way `locate`/`inspect` are, so the adoption-reliability tradeoff of moving them off the wire schema was judged worth it — unlike `locate`/`inspect` themselves, which stay registered specifically because tool presence is what drives the AI reaching for them over native grep/Read on every task. The 18 pre-v2 tools (below, section 6b) are all still plain, directly-callable Python functions (decorated with `@_tool`, which only appends to the now-otherwise-unused `_TOOLS` list) — reachable via `mimir_dispatch`'s legacy-name routing (`_COMMANDS` dict) and via the CLI, just not directly MCP-registered.

- `locate(task, max_files=5, focus="", mode="files")` — replaces `scope_task`/`scope_hint`/`scope_area`/`semantic_search`/`verify_symbol_existence`. `_render_locate()` builds a compact format (ranked files with matched symbol NAMES inline, no full signatures, one batched `inspect()` suggestion per file) from the same `_score_task_files()` core `scope_task` uses. Falls through to `semantic_search()` automatically when the exact-token symbol index returns zero hits. `mode="area"`/`"symbol"` route to `scope_area`/`verify_symbol_existence` directly. Ranked-file entries get a `(matched only K of N search terms, different area than #1)` annotation when BOTH signals hold — narrow keyword coverage alone isn't enough (verified on Carps: most real results, including genuinely relevant ones, only match 1 of a multi-keyword query; the discriminator that actually separated two real noise files from real hits was that they ALSO sat in a different `_path_area()` — top-2-directory-segment, filename excluded — than the #1 result). This is a distinct signal from the existing `(common terms only)` low-IDF annotation: "workflow"/"override" were both globally RARE (high IDF) on Carps, so IDF alone couldn't have caught that case.
- `inspect(path="", symbol="", view="auto")` — replaces `get_symbol`/`get_file_structure`/`get_imports`/`get_dependents`/`find_callers`. Dispatches on `view` (`"imports"`/`"dependents"`/`"callers"`) or, in `"auto"` mode, on whether `symbol` is set (→ `get_symbol`, which already supports comma-separated batching) vs not (→ `get_file_structure`, file or directory).
- `mimir_dispatch(command, args="")` — NOT MCP-registered as of 2026-07-09; CLI-only (`mimir <command> "<args>"`, run via the shell/Bash tool). Handles everything else: `status`/`arch`/`changed`/`set_focus`/`set_scope`/`alias`/`note`/`ignore`/`audit`/`help`, PLUS every pre-v2 tool name for backward compatibility with instructions generated before this redesign (`"scope"` is also aliased to `scope_task`, matching the CLI's long-standing meaning). `args` is free text; optional typed params are trailing `key=value` tokens (extracted by `_extract_kwargs`, a regex over `\S+`-valued tokens so a `focus="prefix:weight,prefix2:weight2"` value with an internal comma survives intact). Two-positional legacy commands (`get_symbol`, `record_alias`, `record_note`, `add_ignore`) split on the FIRST comma only (`str.partition`), so a second free-text arg that itself contains commas (a batched symbol list, a multi-code alias, note prose) is preserved. `mimir help <command>` returns the target function's real docstring — full docs stay free at runtime, only the wire-registered schemas are trimmed. `_cli_run`'s final `else` branch is the CLI entry point (`_main()`'s dispatch gate routes any subcommand not otherwise recognized there); it also decides which subcommands need the ranking structures (`_build_token_df`/`_build_path_strings`/reverse-imports) rebuilt, generously rather than precisely enumerated, since that rebuild is already cheap.
- `.mimir-overview.md` — written by `_write_overview()` (architecture map + index stats + active scope/focus + timestamp), hash-guarded on stable content so it's not rewritten on every warmup/watcher firing. The generated CLAUDE.md snippet `@`-imports it (project-scoped setup only, not `--global`) for zero-tool-call session orientation. `mimir sync` (CLI) forces a regen; `mimir status` is the staleness escape hatch mentioned in the file's own header comment.

### 6b. Underlying functions (all still directly callable; not MCP-registered as of v2)
   - `get_status` — index health, file count, exclusion patterns, domain aliases, active focus weights, active scope
   - `set_focus` — save per-prefix score *multipliers* to `.mimir-focus` (soft ranking bias — out-of-focus files still appear, just lower-scored); takes effect immediately; `persist=False` for session-only weights
   - `set_scope` — hard-narrow every search tool to one directory via `.mimir-scope` (files outside are excluded entirely, not just down-weighted); persists until cleared with `set_scope("")` (mirrors `set_focus("")`). Distinct from `set_focus`: scope filters the result set, focus re-ranks within whatever scope allows through — the two compose. Filtering happens at the read layer (`_in_scope()`/`_path_in_scope()`, checked in `_symbol_hits`/`_symbol_hits_multi`, the end of `_score_task_files`, and `find_callers`'s `path_pairs`), not the index layer — the full-repo index stays warm, so setting/resetting scope is instant, no reindex. `_path_in_scope` also matches sibling `Foo.Core`/`Foo-Tests`/`Foo_Shared` projects at the same directory level as a scope ending in `Foo` (verified on Carps: `InControl.Carps.Keypad` and `InControl.Carps.Keypad.Core` are siblings, not parent/child — a plain prefix check would silently exclude the second one). Ranking also needs to know a keyword matching the scope's own name (e.g. "keypad") carries no signal once already scoped there, even if that keyword is globally rare — `_idf_weight_in_scope()` forces it to the ubiquitous-term floor; every `_idf_weight()` call inside `_score_task_files`/`scope_task`/`semantic_search` uses this scope-aware wrapper, not the raw function.
   - `get_architecture` — high-level directory/symbol map of the whole workspace
   - `get_changed_files` — blueprints of files changed vs a git base branch
   - `scope_hint` — cheap symbol lookup that returns what the codebase calls things + suggested query
   - `scope_task` — ranked files + suggested `get_symbol` calls for a plain-English task description; BM25-style scoring (capped TF × IDF × doc-length norm) over the exact-token symbol index, plus compound-bigram search ("Unavailable Types" → `UnavailableType`); accepts optional `focus="prefix:weight"` for a per-call weight override that does not modify `.mimir-focus`; pass `include_blueprints=True` for ranked files + full blueprints in one call. Its scoring core lives in `_score_task_files()`, shared with `scope_area`.
   - `scope_area` — like `scope_task` but rolls matches up into an indented directory tree instead of a flat file list, for finding which sub-project a task lives in in a large monorepo; suggests a `set_scope(...)` call for the highest-concentration folder
   - `semantic_search` — FTS5 BM25 over decomposed identifier sub-tokens, RRF-fused with symbol-index hits; for when you know the concept but not the code name
   - `get_symbol` — full body of one named function/class/method, or several comma-separated names in one call (one shared header instead of N repeated ones — cheaper than either N separate get_symbol calls or get_file_structure's whole blueprint on a property-heavy file where there's little body to strip)
   - `get_file_structure` — blueprint (signatures + line numbers, bodies stripped) for one file, or for every source file under a directory (merged with the former `get_directory_structure`, dispatched on `is_dir()`)
   - `get_imports` — resolved import map for one file; `[workspace?]` entries auto-suggest definition file via symbol index; for C#/Kotlin/Swift unresolved namespace imports, the tool now auto-resolves by type name
   - `verify_symbol_existence` — search the symbol index for a definition; tolerant of a short descriptive phrase instead of a bare identifier (`_extract_bare_identifier()` strips common descriptor stopwords and picks the longest remaining token, echoing the guess back as `(interpreted 'X' as identifier 'Y')` rather than silently guessing or hard-failing); when a generic name truncates at `max_results`, suggests `set_scope(...)` toward the sub-project the shown hits mostly cluster under (only when a real cluster exists — a `Counter` over each hit's `_path_area()`-equivalent 3-segment prefix, gated on a minimum share of the shown hits, so a genuinely scattered result set gets no hint at all rather than a misleading one)
   - `find_callers` — finds every call/usage site of a symbol; primary path is an in-process tree-sitter `Query`/`QueryCursor` pass (`_cs_find_call_sites`) that matches every identifier-kind leaf equal to the symbol and excludes only the definition's own name and import/using statements, so it catches call sites a fixed-pattern matcher would miss (bare `this.foo`/`x.foo` references, the symbol as a generic type argument, or as the object before a dot) since it doesn't need the symbol to be in a specific "callable" position; falls back to ripgrep (`rg`)/plain text-scan for a language whose grammar isn't available or when tree-sitter isn't installed. Note: this pass uses `tree_sitter.Parser(get_language(lang))` directly rather than `_get_ts_parser()` — `tree_sitter_language_pack.get_parser()`'s nodes use a different, `Query`-incompatible method-based API (`.kind()`, `.child_count()`) vs the standard property-based `tree_sitter.Node` (`.type`, `.children`) that `Query`/`QueryCursor` require
   - `get_dependents` — reverse import index: which files import a given file
   - `record_alias` — save a domain-term → code-name mapping for `scope_task` expansion
   - `record_note` — attach a free-text contextual note to a path prefix; surfaced as prose in `get_file_structure`/`scope_task`, never used for ranking
   - `add_ignore` — append a pattern to `.mimirignore` and reload immediately
7. **CLI** (`mimir <subcommand>`, see `_cli_run`/`_main`) — dedicated subcommands for terminal use outside an AI client: `hint`, `locate "<task>"` (CLI form of the `locate` MCP tool), `scope "<task>"` (same search, `scope_task`'s original verbose format), `scope --set <path>` / `scope --reset` (state management for `set_scope`, disambiguated from the task-string form by the leading `--`), `area "<task>"`, `find <Symbol>`, `callers <Symbol>`, `status`, `audit` (index-health diagnostic — CLI-only on purpose, it's a human tuning aid for `.mimirignore`, not an agent tool), `sync` (force-regenerate `.mimir-overview.md`). Any OTHER subcommand (`arch`, `changed`, `set_focus`, `set_scope`, `alias`, `note`, `ignore`, `help <command>`, or any pre-v2 tool name) falls through to `mimir_dispatch` via `_cli_run`'s final `else` branch — see 6a. Each invocation is a fresh process — `_load_disk_cache()` restores blueprints/symbol/FTS tables from the on-disk SQLite cache almost instantly; `.mimir-focus`/`.mimir-scope`/`.mimirignore`/`.mimiraliases`/`.mimirnotes` are re-read at module import every run. Most subcommands rebuild the in-memory-only ranking structures (`_build_token_df`, `_build_path_strings`, `_build_reverse_imports`) each run, since those aren't disk-cached — skipped only for a short list of subcommands that provably never touch ranking (`find`, `callers`, `status`, `audit`, `sync`, `help`, `set_focus`, `set_scope`, `alias`, `note`, `ignore`). `_FILE_LIST` is ALSO disk-persisted (`file_list` table, `_save_file_list_to_disk`/`_load_file_list_from_disk`, called from `_iter_source_files()` and `_cli_run` respectively) with its own longer cross-process TTL (`_FILE_LIST_DISK_TTL`, 300s vs the 30s in-memory `_FILE_LIST_TTL`) — a fresh CLI subprocess has no in-memory list to reuse the way a long-running MCP server does, and the `os.walk()` alone cost ~1.9s on the real ~8,000-file Carps repo (measured after v3 made routine commands like `status` hit this path far more often); reusing a persisted list cut repeat CLI calls to ~0.15-0.17s. Invalidated (`_clear_file_list_disk_cache()`) at every point the in-memory list itself resets (`add_ignore`, file-watcher directory events, `_cache_evict`), so a stale list can't survive past the next real change.
8. **Entry point** — `main()` prints a status banner to stderr, then calls `mcp.run()`.
9. **`mimir-setup`** (`setup()`, console-script entry point `mimir-setup`) — registers the MCP server (`_register_mcp_server`, unchanged since v2) and writes the generated CLAUDE.md/copilot-instructions.md workflow snippet via `_upsert_mimir_section()`. That helper matches on a version-less marker prefix (`_MIMIR_MARKER_PREFIX`) so re-running `mimir-setup` after a tool-surface change REPLACES a stale section (old tool names and all) instead of the old skip-if-marker-present-at-all behavior, which would have left a v1 CLAUDE.md permanently stale. `_MIMIR_SECTION_MARKER` is the version-suffixed marker (currently `... (v3)`, bumped from `(v2)` when `mimir_dispatch` was pulled from MCP registration) that gates the skip case once the current version is already present. The generated snippet's dispatcher commands (`status`, `alias`, `set_scope`, ...) are written in CLI shell-command syntax (`` `mimir alias "..."` ``), not tool-call syntax, since `mimir_dispatch` isn't a tool to call anymore — only `locate(...)`/`inspect(...)` keep function-call syntax in the generated instructions.

## Adding language support

Add an entry to `EXT_LANG` mapping file extension → `(tree_sitter_language_name, regex_profile_key)`, and optionally add a regex profile to `REGEX_PROFILES` for the no-tree-sitter fallback. The tree-sitter extraction is language-agnostic (driven by `DEF_SUFFIXES`/`DEF_EXACT`); regex profiles only matter as fallback.

## Key design constraints

- **stdout is sacred** — every tool catches all exceptions and returns a string; nothing may raise out of a tool handler.
- **stderr for diagnostics** — startup banner and any debug output go to `sys.stderr`, never `sys.stdout`.
- **Memory bounded** — the `_CACHE` FIFO eviction and `MAX_FILE_BYTES` guard are load-bearing for large repos; don't break them.
- **Lean MCP surface** — every registered tool's schema costs tokens on every agent turn; new tools need strong evidence of agent (not human) value, and human diagnostics go in the CLI instead.
