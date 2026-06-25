# Mimir — Installation & Usage Guide

## What it does

Mimir is a local MCP server that gives Claude a set of tools to explore a codebase efficiently without reading raw files. Instead of opening files line by line, Claude calls mimir tools to get structural maps, resolve imports, and scope tasks to the right files — using far fewer tokens.

---

## Installation

### Option A — From GitHub (recommended for development)

```bash
pipx install git+https://github.com/yourusername/mimir-mcp.git
```

To update later:
```bash
pipx install --force git+https://github.com/yourusername/mimir-mcp.git
```

### Option B — From a local clone

```bash
git clone https://github.com/yourusername/mimir-mcp.git
cd mimir-mcp
pipx install .
```

For development (changes take effect immediately, no reinstall):
```bash
pipx install -e .
```

> **Requires:** Python 3.9+ and [pipx](https://pipx.pypa.io). Install pipx with `pip install pipx` if you don't have it.

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

### 4. `verify_symbol_existence` — confirm a symbol is real

Searches the entire workspace for a symbol definition and returns its exact location and signature.

**Example:**
> `verify_symbol_existence("advanceProgress")`

Use this before assuming a function or type exists, before importing it, or when a symbol appears in a blueprint and you want its definition location.

---

### 5. `execute_local_sandbox` — run a quick snippet

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
