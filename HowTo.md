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
mimir-setup
```

This drops both config files automatically:
- `.mcp.json` — for Claude Code
- `.vscode/mcp.json` — for GitHub Copilot in VS Code

---

## Connecting to Claude Code

Drop a `.mcp.json` file in the root of each project you want to use mimir with:

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

## Connecting to GitHub Copilot (VS Code)

Requires VS Code 1.99+ and a Copilot Individual, Business, or Enterprise plan. Tools are available in Copilot Chat agent mode only — not in inline completions.

Drop a `.vscode/mcp.json` file in the project root:

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

### Both on the same project

If you use Claude Code on your Mac and Copilot at work, both config files can coexist in the same project:

```
your-project/
  .mcp.json            ← picked up by Claude Code
  .vscode/
    mcp.json           ← picked up by VS Code Copilot
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

### 1. `scope_task` — start here

Call this **first** on any task involving existing code. Give it a plain-English description of what you want to do. It extracts symbol names, searches the workspace, and returns blueprints for the most relevant files.

**Example:**
> "change how the live tutor handles pronunciation errors"

Returns: the files most likely involved, matched symbols, and their structural blueprints — all in one call.

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

### 7. `execute_local_sandbox` — run a quick snippet

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

### Run all tests

```bash
# From the mimir-mcp project root
pytest tests/ -v
```

Expected output: **79 passed** in under 1 second.

### Run only the performance benchmarks

```bash
pytest tests/ -v -k Performance -s
```

This prints SQL vs linear-scan timing and speedup numbers.

### What the tests cover

| Class | What it tests |
|---|---|
| `TestBlueprintHeader` | Line count appears in blueprint header; accurate for Python and C# |
| `TestGetDirectoryStructure` | Returns correct files; respects max_files; rejects paths outside workspace |
| `TestBlueprintVersion` | Stale cache is cleared on version mismatch; valid cache is preserved |
| `TestIsBlacklisted` | BLACKLIST_DIRS and `.mimirignore` pattern matching including `**` globs |
| `TestExtractScopeKeywords` | CamelCase/snake_case/plain-word extraction; stopwords and deduplication |
| `TestFindCallersValidation` | Rejects dotted names/parens; accepts valid bare identifiers |
| `TestGeneratedFileExclusion` | `.g.cs`, `.generated.cs`, `AssemblyInfo.cs` excluded from index |
| `TestExtractBlueprintLines` | Blueprint lines parsed into `(file, lineno, context)` correctly |
| `TestIndexBlueprintRows` | Stopwords excluded; symbol names indexed; no context in rows |
| `TestNormalizedSchema` | `lines` holds context; `symbols` holds only tokens; no duplication |
| `TestSearchCorrectness` | SQL JOIN results match linear-scan results for 7 symbol types |
| `TestPerformance` | SQL lookup is <1ms and ≥10× faster than scanning blueprints |

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
