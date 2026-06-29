# Mimir — Installation & Usage Guide

## What it does

Mimir is a local MCP server that gives Claude and Copilot a set of tools to explore a codebase efficiently without reading raw files. Instead of opening files line by line, the AI calls mimir tools to get structural maps, resolve imports, and scope tasks to the right files — using far fewer tokens.

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

This creates five files:

| File | Purpose |
|---|---|
| `.mcp.json` | Registers mimir with Claude Code |
| `.vscode/mcp.json` | Registers mimir with GitHub Copilot in VS Code |
| `CLAUDE.md` | Tells Claude Code when and how to use each tool |
| `.github/copilot-instructions.md` | Tells Copilot to use mimir and not fall back to built-in search |
| `.mimirignore` | Starter exclusion patterns (build output, vendor libs, generated files) |

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

`mimir-setup` writes a `CLAUDE.md` section that instructs Claude to follow the mimir workflow. Without it, Claude uses mimir when it judges the task warrants it but won't follow the workflow consistently.

The section looks like this (you can edit it to suit your project):

```markdown
## Code exploration — use mimir tools

This project has mimir MCP tools available. Use them before reading raw files.

At the start of any coding session:
1. Call `get_status` to check the index is ready and see active exclusions
2. Call `get_architecture()` for a high-level map of the whole codebase (cheap)
3. Call `get_changed_files()` to see what is currently in flight vs main
4. Call `scope_task("describe what you want to do")` to find relevant files

For any task involving existing code:
- Use `scope_task` before opening files — it finds the right files in one call
- Use `get_symbol(path, name)` to read ONE function or class body instead of the whole file
- Use `get_file_structure` to see a file's full symbol map before reading it line by line
- Use `verify_symbol_existence` before assuming a function or type exists
- Use `find_callers` after `verify_symbol_existence` to trace impact
- Use `get_dependents(path)` to find what else imports a file before changing it
- Use `get_imports` when an unfamiliar symbol appears and you need to trace its origin
- Call `record_alias(domain_term, code_name)` when you discover a feature name maps
  to a different code name — future scope_task searches will expand it automatically
- Call `add_ignore(pattern, reason)` when you encounter vendor/generated/test files
  that add noise — always tell the user what you are adding and why first
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

`mimir-setup` handles this automatically. If you prefer to configure manually, drop a `.vscode/mcp.json` file in the project root:

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

`mimir-setup` also creates `.github/copilot-instructions.md`, which Copilot reads automatically. If you prefer to create it manually:

```markdown
## Code exploration — use mimir tools

This project has mimir MCP tools. Always use them before using built-in search or reading files.

Workflow for any coding session:
1. Call `get_status` to confirm the index is ready
2. Call `get_architecture()` for a high-level map of the whole codebase (one cheap call)
3. Call `get_changed_files()` to see what is currently in flight vs main
4. Call `scope_task` with a description of the task — use its ranked file list and
   suggested get_symbol calls, do NOT fall back to glob or grep
5. Call `get_symbol(path, name)` to read ONE function or class body
6. Use `get_dependents(path)` before changing a shared file to see blast radius
7. When you discover a domain/feature term maps to a code name, call `record_alias`
8. When you encounter vendor/generated/noisy files, tell the user what you found,
   ask for confirmation, then call `add_ignore(pattern, reason)` to exclude them

Do not use built-in file search, glob, or grep if mimir tools are available.
```

Without this file, Copilot may fall back to its own glob/grep search when mimir output is large.

### Both on the same project

If you use Claude Code on your Mac and Copilot at work, all config files can coexist in the same project:

```
your-project/
  .mcp.json                        ← picked up by Claude Code
  .mimirignore                     ← exclusion patterns (commit this to share with the team)
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
file_watcher:       on (changes invalidate cache instantly)
reverse_imports:    1,204 files mapped
sandbox:            on

ignored_patterns: 8 active (.mimirignore)
  **/obj/**
  **/bin/**
  ...
```

If `symbol_index: building`, the other tools still work but `scope_task` and `verify_symbol_existence` use a slower fallback until indexing completes (typically under 60s for large repos).

**Domain aliases:** When `.mimiraliases` exists, `get_status` lists the active mappings:
```
domain_aliases (2 active):
  corrective actions → RectificationFilter
  live tutor → LiveTutor, GeminiLive
```

---

### 1. `get_architecture` — orient yourself in one call

Returns a high-level map of the entire workspace: directories grouped by file count, with top-level symbol names per file. Much cheaper than calling `get_file_structure` on every file to understand the project layout.

**Example output:**
```
# Workspace architecture: topcat

## src/android/app/src/main/java/com/incontrol/topcat/audit/  (22 files)
  AuditActivity, AuditFragment, AuditViewModel, AuditAdapter…

## src/ios/Topcat/audit/  (8 files)
  AuditVC.swift                            AuditVC
  AuditCell.swift                          AuditCell
  AuditViewModel.swift                     AuditViewModel
  ...
```

WHEN TO USE: at the very start of a session to understand the project layout before diving into specific files. For a specific directory use `get_directory_structure` instead.

Large repos (60+ directories) show the most file-dense modules first with a note pointing to `get_directory_structure` for the rest.

---

### 2. `get_changed_files` — see what's in flight

Returns structural blueprints of every source file changed vs a git base branch — covering committed branch changes, uncommitted edits, and untracked new files.

**Example:**
> `get_changed_files()` or `get_changed_files(base="develop")`

Returns blueprints (symbols + line numbers, bodies stripped) for each changed file so you get the full structural picture in one call.

WHEN TO USE: at the start of a session to orient yourself on what is actively being worked on before deciding where to focus.

---

### 3. `scope_task` — find the right files

Call this **first** on any task involving existing code. Give it a plain-English description of what you want to do. It extracts symbol names, searches the workspace, and returns a ranked list of the most relevant files with matched symbol locations and suggested `get_symbol` calls.

**Example:**
> `scope_task("change how the live tutor handles pronunciation errors")`

Returns: keywords searched, matched symbols with file:line locations, ranked files by relevance, and suggested next calls:
```
## Suggested next calls (read just the symbols you need)
  get_symbol("lib/lessonProgress.ts", "advanceProgress")
  get_symbol("lib/geminiLive.ts", "LiveTutorSession")
```

**Tip:** Use technical/class names when known rather than feature names — the symbol index matches code identifiers, not domain language. `"RectificationFilter section order"` finds the right file faster than `"corrective actions filter UI"`.

Pass `include_blueprints=True` to get full symbol maps inline (useful for small repos).

---

### 4. `get_symbol` — read just the code you need

Returns the complete source of ONE named function, class, or method — bodies included. The efficient middle ground between `get_file_structure` (signatures only) and reading the whole raw file.

**Example:**
> `get_symbol("src/services/auth.py", "authenticate")`

Returns just the `authenticate` method — 20 lines instead of the 400-line file.

WHEN TO USE: after `scope_task` or `get_file_structure` identifies the symbol you need. Typically 10–50× fewer tokens than reading the whole file.

If the symbol is not found, the response includes the file's full blueprint so you can see what IS available.

---

### 5. `get_file_structure` — understand a file

Returns a compact map of a single file: every class, function, method, and their signatures — with line numbers, bodies stripped.

**Example:**
> `get_file_structure("lib/lessonProgress.ts")`

Use this when you want the full symbol map of a file before deciding which symbols to read. Use `get_symbol` when you already know which symbol you need.

---

### 6. `get_imports` — trace where symbols come from

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

### 7. `get_directory_structure` — browse a module

Returns structural blueprints for every source file under a directory. Use this when you know *where* to look but not *which* file.

**Example:**
> `get_directory_structure("src/api/controllers", max_files=10)`

Use `scope_task` when you don't know where to look. Use `get_directory_structure` when you already know the directory.

---

### 8. `verify_symbol_existence` — confirm a symbol is real

Searches the entire workspace for a symbol definition and returns its exact location and signature.

**Example:**
> `verify_symbol_existence("advanceProgress")`

Use this before assuming a function or type exists, before importing it, or when a symbol appears in a blueprint and you want its definition location.

---

### 9. `find_callers` — trace who calls a symbol

Searches raw source text across the entire workspace for every call site and usage of a symbol. Unlike `verify_symbol_existence` (which only finds definitions), this finds where a symbol is called, passed, or referenced in implementation code.

**Example:**
> `find_callers("AuthenticationService", max_results=20)`

WHEN TO USE: after `verify_symbol_existence` tells you where something is defined, use `find_callers` to trace who calls it — for impact analysis, understanding data flow, or finding all consumers of an interface.

---

### 10. `get_dependents` — blast-radius analysis

Returns every workspace file that directly imports a given file. Built from the reverse import index constructed at startup — no extra configuration needed.

**Example:**
> `get_dependents("src/services/auth.py")`

Returns a sorted list of files that import `auth.py`. If no workspace files import it, mimir says so and suggests `find_callers` for languages (Swift, Kotlin, Java, Go, Rust) where import resolution is not supported.

WHEN TO USE: before modifying a widely-used utility, service, or model — get the full blast radius in one call instead of grepping for the filename manually.

---

### 11. `record_alias` — teach mimir your project's vocabulary

Records a mapping from a domain/feature name to the code name used in the codebase. Once saved, `scope_task` automatically expands matching phrases before searching.

**Example:**
> `record_alias("corrective actions", "RectificationFilter")`

After this, searching `"corrective actions filter iOS"` automatically also searches for `RectificationFilter`, surfacing `RectificationFilterVC.swift` and `RectificationFilterDialogFragment.java` on the first call.

**How it works:**
- Writes to `.mimiraliases` in the workspace root (human-editable, commit to git)
- Applied automatically to every future `scope_task` call in this project
- `scope_task` output shows `Aliases expanded: RectificationFilter` when a mapping fires
- `get_status` lists all active aliases

**Maintaining `.mimiraliases` manually:**

```
# mimir domain aliases — maps feature/domain names to code names
# Format:  domain phrase = CodeName1, CodeName2

corrective actions = RectificationFilter
live tutor = LiveTutor, GeminiLive
audit filter = AuditFilter, FilterVC
push notifications = PushNotificationService, PushManager
```

---

### 12. `add_ignore` — exclude noisy files on the fly

Adds a gitignore-style pattern to `.mimirignore` and takes effect immediately — no restart needed. The AI uses this when it encounters vendor libraries, generated code, test fixtures, or build artefacts that pollute blueprints and `get_architecture` output.

**Example:**
> `add_ignore("**/wwwroot/lib/**", "vendored jQuery and Bootstrap bundles")`

The AI is instructed to always tell you what it is adding and why before calling this — you can redirect or adjust the pattern before it takes effect.

**Maintaining `.mimirignore` manually:**

`mimir-setup` creates a starter file covering common noise. Edit it directly at any time — mimir reloads it automatically within 30 seconds. Commit it to the repo so the whole team shares the same exclusions.

```
# Build output
**/obj/**
**/bin/**
**/dist/**
**/__pycache__/**

# Package managers
**/node_modules/**
**/vendor/**

# Generated files
**/Migrations/**
**/*.generated.*

# Android resources (layout XML, drawables — not code symbols)
**/res/layout/**
**/res/drawable/**

# Test projects (uncomment if noise outweighs benefit)
# **/*.Tests/**
# **/*Tests.Integration/**
```

Run `mimir status` to confirm which patterns are active.

---

### 13. `execute_local_sandbox` — run a quick snippet

Runs a Python or bash snippet locally with a timeout, captures output, and returns it.

**Example use cases:**
- Run a test to verify a fix
- List files matching a pattern
- Check a computed value

Not for long-running jobs. Not a security sandbox — code runs as your user.

---

## Recommended workflow

```
Session start
  1. get_status()                           ← confirm index ready, see exclusions
  2. get_architecture()                     ← understand the project layout
  3. get_changed_files()                    ← see what's currently in flight

Finding and reading code
  4. scope_task("what you want to do")      ← find relevant files + suggested calls
  5. get_symbol("file.py", "SymbolName")    ← read just the function/class you need
     — or —
     get_file_structure("file.py")          ← full symbol map if you need the overview

Tracing dependencies
  6. get_imports("file.py")                 ← where do its symbols come from?
  7. get_dependents("file.py")              ← what breaks if I change this?
  8. find_callers("SymbolName")             ← who calls this?

Keeping the index clean
  9. add_ignore("**/pattern/**", "reason")  ← exclude noise when you find it
     record_alias("feature", "CodeName")    ← teach mimir your project's language
```

Following this order means the AI reads raw file contents as little as possible, keeping context usage low and responses faster.

---

## Running the tests

The test suite lives in `tests/` and covers the symbol index, blueprints, tool logic, and full MCP wire protocol.

### Prerequisites

Requires the mimir development install (Option C) and pytest:

```bash
~/.local/pipx/venvs/mimir-mcp/bin/python -m pip install pytest pytest-anyio anyio
```

### Run all tests

```bash
~/.local/pipx/venvs/mimir-mcp/bin/python -m pytest tests/ -q
```

Expected: **127 passed** (~25s — smoke tests spawn real subprocesses).

### Run unit tests only (fast)

```bash
~/.local/pipx/venvs/mimir-mcp/bin/python -m pytest tests/ -q --ignore=tests/test_smoke.py
```

### Run smoke tests only (wire protocol)

```bash
~/.local/pipx/venvs/mimir-mcp/bin/python -m pytest tests/test_smoke.py -v
```

Smoke tests spawn a real mimir subprocess and exercise every tool over the MCP stdio JSON-RPC protocol (~0.5s each).

---

## Supported languages

| Language | Blueprints | Imports |
|---|---|---|
| TypeScript / TSX | tree-sitter | full path resolution |
| JavaScript / JSX | tree-sitter | full path resolution |
| Python | tree-sitter | relative + absolute resolution |
| Kotlin | tree-sitter | workspace vs external |
| Swift | tree-sitter | external only (no file resolution) |
| C# | tree-sitter | workspace vs external |
| Go | tree-sitter | listed |
| Rust | tree-sitter | listed |
| Ruby | tree-sitter | — |
| Java | tree-sitter | — |
| C / C++ | tree-sitter | — |
| XML | custom extractor | — |

**XML blueprint format:** Layout files show the view hierarchy with `@id` attributes. Resource files show name→value pairs:

```
# res/layout/dialog_rectification_filter.xml  [xml · 184 lines]
L1   ScrollView
L4     LinearLayout
L12      TextView @id/labelDateRange text="Date Range"
L19      Spinner @id/filterStatus
L26      Spinner @id/filterAllocatedUser

# res/values/strings.xml  [xml · 312 lines]
L45    string "rectification_filter_section_date_range" = "Date Range"
L46    string "rectification_filter_section_status" = "Status"
```

To exclude noisy XML files (Maven pom.xml, IDE metadata), add patterns to `.mimirignore`:
```
**/pom.xml
**/.idea/**
**/*.generated.xml
```
