# Mimir — Installation & Usage Guide

## What it does

Mimir is a local MCP server that gives Claude a set of tools to explore a codebase efficiently without reading raw files. Instead of opening files line by line, Claude calls mimir tools to get structural maps, resolve imports, and scope tasks to the right files — using far fewer tokens.

---

## Installation

### Option A — Automated install (recommended)

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/mtarros/mimir-mcp/main/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/mtarros/mimir-mcp/main/install.ps1 | iex
```

The script installs pipx if needed, then installs mimir. To update, run it again.

### Option B — Manual install

```bash
pipx install git+https://github.com/mtarros/mimir-mcp.git
```

To update later:
```bash
pipx install --force git+https://github.com/mtarros/mimir-mcp.git
```

### Option C — Local development clone

```bash
git clone https://github.com/mtarros/mimir-mcp.git
cd mimir-mcp
pipx install -e .
```

Changes to `mimir.py` take effect immediately without reinstalling.

> **Requires:** Python 3.10+ and [pipx](https://pipx.pypa.io). Install pipx with `pip install pipx` if you don't have it.

### After installing — set up a project

Run this once in the root of any project you want to use mimir with:

```bash
cd /path/to/your-project
mimir-setup
```

This creates four files:

| File | Purpose |
|---|---|
| `.mcp.json` | Registers mimir with Claude Code |
| `.vscode/mcp.json` | Registers mimir with GitHub Copilot in VS Code |
| `CLAUDE.md` | Tells Claude Code when and how to use each tool consistently |
| `.github/copilot-instructions.md` | Tells GitHub Copilot to use mimir tools and not fall back to built-in search |

That's it. Restart Claude Code (or reload VS Code) and mimir is active.

The command is safe to re-run — it skips any file that already exists, and only appends to `CLAUDE.md` if the mimir section isn't already there.

---

## Using mimir from the command line

You don't need an AI client to query the index. Run these directly in your terminal from inside any project that has mimir set up:

```bash
mimir scope "change how jobs are retried on failure"   # find relevant files
mimir find   JobScheduler                              # locate a symbol definition
mimir callers authenticate                             # find every call site
mimir status                                           # check index state + exclusions
```

These are the same tools Claude uses — you can explore a codebase, verify a symbol exists, or trace call chains without opening a chat session.

---

## How Claude knows to use mimir

`mimir-setup` writes a `CLAUDE.md` section that instructs Claude to call `get_status` first and `scope_task` before reading files. Without it, Claude uses mimir when it judges the task warrants it but won't follow the workflow consistently.

The section looks like this (you can edit it to suit your project):

```markdown
## Code exploration — use mimir tools

This project has mimir MCP tools available. Use them before reading raw files.

At the start of any coding session:
1. Call `get_status` to check the index is ready and see any active exclusions
2. Call `scope_task("describe what you want to do")` to find relevant files

For any task involving existing code:
- Use `scope_task` before opening files — it finds the right files in one call
- Use `get_file_structure` to see a file's symbol map before reading it line by line
- Use `verify_symbol_existence` before assuming a function or type exists
- Use `find_callers` after `verify_symbol_existence` to trace impact — who calls this?
- Use `get_directory_structure` when you know the directory but not which file
- Use `get_imports` when an unfamiliar symbol appears and you need to trace its origin
```

---

## Connecting to Claude Code (manual setup)

`mimir-setup` handles this automatically. If you prefer to configure manually, drop a `.mcp.json` file in the project root:

```json
{
  "mcpServers": {
    "mimir": {
      "command": "mimir",
      "env": {
        "MCP_WORKSPACE_ROOT": "."
      }
    }
  }
}
```

Claude Code picks this up automatically when you open that folder. The `"."` resolves to the project root so you never need to change it per-machine.

---

## Connecting to GitHub Copilot (VS Code) (manual setup)

`mimir-setup` handles this automatically. If you prefer to configure manually, or need to understand what it created, drop a `.vscode/mcp.json` file in the project root:

```json
{
  "servers": {
    "mimir": {
      "type": "stdio",
      "command": "mimir",
      "env": {
        "MCP_WORKSPACE_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```

`${workspaceFolder}` is a VS Code variable that resolves to the project root automatically.

To use the tools in Copilot Chat, open the chat panel, switch to agent mode, and the mimir tools will be available alongside the built-in Copilot tools.

> **Note:** Copilot only uses MCP tools in agent mode — not in regular chat or inline completions.

### Giving Copilot persistent instructions (manual setup)

`mimir-setup` also creates `.github/copilot-instructions.md`, which Copilot reads automatically to know how to use mimir. If you prefer to create it manually:

```markdown
## Code exploration — use mimir tools

This project has mimir MCP tools. Always use them before using built-in search or reading files.

Workflow for any task involving existing code:
1. Call `get_status` to confirm the index is ready
2. Call `scope_task` with a description of the task — use its ranked file list,
   do NOT fall back to glob or grep even if the output is large
   - Tip: use technical/class names when known rather than feature names
3. Call `get_file_structure` on the files scope_task returns before reading raw lines
4. Use `find_callers` or `verify_symbol_existence` to trace symbols

Do not use built-in file search, glob, or grep if mimir tools are available.
Read the full scope_task output even if it is saved to a temp file — it contains the answer.
```

Without this file, Copilot may fall back to its own glob/grep search when mimir output is large.

### Both on the same project

If you use Claude Code on your Mac and Copilot at work, all config files can coexist in the same project:

```
your-project/
  .mcp.json                        ← picked up by Claude Code
  .vscode/
    mcp.json                       ← picked up by VS Code Copilot (MCP connection)
  .github/
    copilot-instructions.md        ← tells Copilot to trust mimir results
  CLAUDE.md                        ← tells Claude Code when and how to use mimir
```

Same mimir install, same tools, no conflicts.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MCP_WORKSPACE_ROOT` | current directory | Root of the repo mimir maps |
| `MCP_MAX_FILE_BYTES` | `2000000` | Skip files larger than this |
| `MCP_ENABLE_SANDBOX` | `1` | Enable `execute_local_sandbox` (`0` to disable) |
| `MCP_SANDBOX_TIMEOUT` | `10` | Max seconds a sandbox snippet can run |

### Windows note

All tools work on Windows. The one exception: `execute_local_sandbox` with `language="bash"` requires bash (WSL or Git Bash). Python snippets work fine without it.

---

## Tools and when to use them

### 0. `get_status` — check the index before you start

Call this at the start of a session to see how much of the workspace is indexed and whether the symbol index is ready.

**Example output:**
```
workspace:          /your/project
source_files:       8605
blueprints_cached:  8605 in memory, 8605 on disk
symbol_index:       warm
  indexed_tokens:   267,034
warmup:             complete
tree_sitter:        on
sandbox:            on

ignored_patterns: none  (.mimirignore not found)
  → create .mimirignore in the workspace root to exclude noisy directories
    e.g. '**/obj/**', '**/bin/**', '**/*.generated.cs', '**/vendor/**'
```

If `symbol_index: building`, the other tools still work but `scope_task` and `verify_symbol_existence` use a slower fallback until indexing completes (typically under 60s for large repos).

**Excluding noisy files:** Create a `.mimirignore` file in the workspace root with gitignore-style patterns. Mimir reloads it automatically. For example:
```
**/obj/**
**/bin/**
**/*.generated.cs
**/node_modules/**
**/vendor/**
```

**Domain aliases:** When `.mimiraliases` exists, `get_status` lists the active mappings so you can see what's been learned:
```
domain_aliases (2 active):
  corrective actions → RectificationFilter
  live tutor → LiveTutor, GeminiLive
```

---

### 1. `scope_task` — start here

Call this **first** on any task involving existing code. Give it a plain-English description of what you want to do. It extracts symbol names, searches the workspace, and returns a ranked list of the most relevant files with matched symbol locations.

**Example:**
> "change how the live tutor handles pronunciation errors"

Returns: ranked files, matched symbols with file:line locations. Then call `get_file_structure` on the files you need.

**Tip:** Use technical/class names when known rather than feature names — the symbol index matches code identifiers, not domain language. `"RectificationFilter section order"` finds the right file faster than `"corrective actions filter UI"`.

Pass `include_blueprints=True` to get full symbol maps inline (useful for small repos, but can produce large output on large codebases).

Use this before reading any files. It replaces open-ended exploration.

---

### 2. `get_file_structure` — understand a file

Returns a compact map of a single file: every class, function, method, and their signatures — with line numbers, bodies stripped.

**Example:**
> `get_file_structure("lib/lessonProgress.ts")`

Use this when `scope_task` points you at a file and you want to see its full symbol map before deciding which lines to read.

---

### 3. `get_imports` — trace where symbols come from

Lists every import in a file. Resolves relative paths and the `@/` alias (Next.js) to actual workspace files. Distinguishes workspace files from external packages.

**Example:**
> `get_imports("app/live-tutor/page.tsx")`

Returns:
```
[workspace]  lib/geminiLive.ts          ← { GEMINI_LIVE_MODEL, AudioPlayer }
[workspace]  lib/lessonProgress.ts      ← { loadCompletedIds, advanceProgress }
[external]   react                      ← { useState, useEffect }
```

Use this after `get_file_structure` reveals an unfamiliar symbol — find which file it comes from without opening anything.

Works for: TypeScript, JavaScript, Python, Kotlin, Swift, C#, Go, Rust.

> **Note:** Kotlin, C#, and Swift use module/namespace imports rather than file paths. Mimir identifies whether they belong to the project or an external SDK, but cannot resolve them to a specific file.

---

### 4. `get_directory_structure` — browse a module

Returns structural blueprints for every source file under a directory. Use this when you know *where* to look but not *which* file — for example to see all controllers in a layer, or to understand what a namespace contains.

**Example:**
> `get_directory_structure("src/api/controllers", max_files=10)`

Returns: one blueprint per file in that directory, with a file count summary.

Use `scope_task` when you don't know where to look. Use `get_directory_structure` when you already know the directory and want to see everything in it.

---

### 5. `verify_symbol_existence` — confirm a symbol is real

Searches the entire workspace for a symbol definition and returns its exact location and signature.

**Example:**
> `verify_symbol_existence("advanceProgress")`

Use this before assuming a function or type exists, before importing it, or when a symbol appears in a blueprint and you want its definition location.

---

### 6. `find_callers` — trace who calls a symbol

Searches raw source text across the entire workspace for every call site and usage of a symbol. Unlike `verify_symbol_existence` (which only finds definitions), this finds where a symbol is called, passed, or referenced in implementation code.

**Example:**
> `find_callers("AuthenticationService", max_results=20)`

WHEN TO USE: after `verify_symbol_existence` tells you where something is defined, use `find_callers` to trace who calls it — for impact analysis, understanding data flow, or finding all consumers of an interface.

---

### 7. `record_alias` — teach mimir your project's vocabulary

Records a mapping from a domain/feature name to the code name used in the codebase. Once saved, `scope_task` automatically expands matching phrases before searching — so plain-English task descriptions find the right files even when the code uses completely different terminology.

**Example:**
> `record_alias("corrective actions", "RectificationFilter")`

After this, searching `"corrective actions filter iOS"` automatically also searches for `RectificationFilter`, surfacing `RectificationFilterVC.swift` and `RectificationFilterDialogFragment.java` on the first call.

**How it works:**
- Writes to `.mimiraliases` in the workspace root (human-editable, checked into git)
- Applied automatically to every future `scope_task` call in this project
- `scope_task` output shows `Aliases expanded: RectificationFilter` when a mapping fires
- `get_status` lists all active aliases

**When to call it:**
Call `record_alias` whenever you discover that a feature name in a task description maps to a different name in the code. Claude and Copilot are both instructed to do this automatically, but you can also call it manually or edit `.mimiraliases` directly.

**Maintaining `.mimiraliases` manually:**

The file lives in the project root and has a simple format:

```
# mimir domain aliases — maps feature/domain names to code names
# Format:  domain phrase = CodeName1, CodeName2
# scope_task expands these automatically before searching.

corrective actions = RectificationFilter
live tutor = LiveTutor, GeminiLive
audit filter = AuditFilter, FilterVC
push notifications = PushNotificationService, PushManager
```

Rules:
- One mapping per line
- Left side: the plain-English phrase (case-insensitive, matched anywhere in the task)
- Right side: one or more code names, comma-separated
- Lines starting with `#` are comments
- Longer phrases are matched first, so `"corrective actions filter"` takes precedence over `"filter"`

To add entries manually: open `.mimiraliases` in any editor and add lines. Mimir picks them up immediately — no restart needed.

To remove an entry: delete the line or comment it out with `#`.

---

### 8. `execute_local_sandbox` — run a quick snippet

Runs a Python or bash snippet locally with a timeout, captures output, and returns it.

**Example use cases:**
- Run a test to verify a fix
- List files matching a pattern
- Check a computed value

Not for long-running jobs. Not a security sandbox — code runs as your user.

---

## Recommended workflow

```
1. scope_task("describe what you want to do")
        ↓
   Returns: relevant files + matched symbols + blueprints

2. get_file_structure("path/to/file")   ← for any file that needs deeper inspection
        ↓
   Returns: full symbol map with line numbers

3. get_imports("path/to/file")          ← when an unfamiliar symbol appears
        ↓
   Returns: resolved import paths

4. Read specific line ranges            ← only now, only what you need
```

Following this order means Claude reads raw file contents as little as possible, keeping context usage low and responses faster.

---

## Running the tests

The test suite lives in `tests/` and covers the symbol index: stopword filtering, normalized schema correctness, SQL vs linear-scan equivalence, and performance benchmarks.

### Prerequisites

Requires the mimir development install (Option C) and pytest:

```bash
# Install pytest into the mimir venv
/Users/mtarros/.local/pipx/venvs/mimir-mcp/bin/python3 -m pip install pytest
```

Or if you installed with `pip install -e .` into an active virtualenv:

```bash
pip install pytest
```

### Run unit tests (fast)

```bash
# From the mimir-mcp project root
pytest tests/ -v --ignore=tests/test_smoke.py
```

Expected output: **81 passed** in under 1 second.

### Run smoke tests (wire protocol)

```bash
pytest tests/test_smoke.py -v
```

Smoke tests spawn a real mimir subprocess and exercise every tool over the MCP
stdio JSON-RPC protocol. They take ~12s total (21 tests, ~0.5s each).

### Run everything

```bash
pytest tests/ tests/test_smoke.py -v
```

### Run only the performance benchmarks

```bash
pytest tests/ -v -k Performance -s
```

This prints SQL vs linear-scan timing and speedup numbers.

### What the tests cover

| File | Class | What it tests |
|---|---|---|
| `test_blueprints.py` | `TestBlueprintHeader` | Line count in header; C# array params produce no duplicate line numbers |
| `test_blueprints.py` | `TestGetDirectoryStructure` | Returns correct files; respects max_files; rejects paths outside workspace |
| `test_blueprints.py` | `TestBlueprintVersion` | Stale cache cleared on version mismatch; valid cache preserved |
| `test_tools.py` | `TestIsBlacklisted` | BLACKLIST_DIRS and `.mimirignore` pattern matching including `**` globs |
| `test_tools.py` | `TestExtractScopeKeywords` | CamelCase/snake_case/plain-word extraction; stopwords and deduplication |
| `test_tools.py` | `TestFindCallersValidation` | Rejects dotted names/parens; accepts valid bare identifiers |
| `test_tools.py` | `TestGeneratedFileExclusion` | `.g.cs`, `.generated.cs`, `AssemblyInfo.cs` excluded from index |
| `test_symbol_index.py` | `TestExtractBlueprintLines` | Blueprint lines parsed into `(file, lineno, context)`; deduplicates linenos |
| `test_symbol_index.py` | `TestIndexBlueprintRows` | Stopwords excluded; symbol names indexed; no context in rows |
| `test_symbol_index.py` | `TestNormalizedSchema` | `lines` holds context; `symbols` holds only tokens; no duplication |
| `test_symbol_index.py` | `TestSearchCorrectness` | SQL JOIN results match linear-scan results for 7 symbol types |
| `test_symbol_index.py` | `TestPerformance` | SQL lookup is <1ms and ≥10× faster than scanning blueprints |
| `test_smoke.py` | `TestToolRegistration` | All 8 tools registered with descriptions over MCP wire protocol |
| `test_smoke.py` | `TestGetFileStructureWire` | Blueprint returned; missing file and path traversal handled gracefully |
| `test_smoke.py` | `TestScopeTaskWire` | Finds relevant files; returns non-empty response |
| `test_smoke.py` | `TestVerifySymbolExistenceWire` | Finds known symbol; graceful "not found" message |
| `test_smoke.py` | `TestGetImportsWire` | Resolves workspace imports; handles files with no imports |
| `test_smoke.py` | `TestFindCallersWire` | Finds call sites; rejects dotted names end-to-end |
| `test_smoke.py` | `TestGetDirectoryStructureWire` | Returns blueprints; path traversal rejected |
| `test_smoke.py` | `TestGetStatusWire` | Reports workspace, file count, index state, and `.mimirignore` patterns |
| `test_smoke.py` | `TestSandboxWire` | Python snippet executes; disabled sandbox returns error; runtime errors captured |

---

## Supported languages

| Language | Blueprints | Imports |
|---|---|---|
| TypeScript / TSX | tree-sitter | full path resolution |
| JavaScript / JSX | tree-sitter | full path resolution |
| Python | tree-sitter | relative import resolution |
| Kotlin | tree-sitter | workspace vs external |
| Swift | tree-sitter | external only (no file resolution) |
| C# | tree-sitter | workspace vs external |
| Go | tree-sitter | listed |
| Rust | tree-sitter | listed |
| Ruby | tree-sitter | — |
| Java | tree-sitter | — |
| C / C++ | tree-sitter | — |
