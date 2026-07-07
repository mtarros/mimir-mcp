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

### After installing — connect mimir to your AI client

`mimir-setup [claude|copilot] [--global]` does everything: registers mimir as
an MCP server with the named client (via `claude mcp add` / `code --add-mcp`
under the hood) and writes the workflow instructions telling the AI when and
how to use it. Client defaults to `claude` when no arg is given.

**Personal use, nothing shared yet** — add `--global`. Registers at USER scope
(available in every project on this machine) and writes the instructions to
your user profile instead of this repo — nothing touches the project at all:

```bash
mimir-setup claude --global     # registers with Claude Code (user scope) + ~/.claude/CLAUDE.md
mimir-setup copilot --global    # registers with Copilot (user scope) + a VS Code user instructions file
```

**Ready to share with a team on this project** — drop `--global`. Registers at
PROJECT scope and writes the instructions into the repo, meant to be committed:

```bash
cd /path/to/your-project
mimir-setup            # .mcp.json + CLAUDE.md (defaults to claude)
mimir-setup copilot    # .vscode/mcp.json + copilot-instructions.md
```

| Mode | MCP registration | Instructions | .mimirignore |
|---|---|---|---|
| `--global` | user scope (every project) | `~/.claude/CLAUDE.md` or a VS Code user instructions file | skipped — inherently project-specific |
| default (no `--global`) | project scope (`.mcp.json` / `.vscode/mcp.json`, committed) | `CLAUDE.md` or `.github/copilot-instructions.md` | starter file created |

The two scopes don't conflict — global instructions/registration apply
everywhere, a project's own `mimir-setup` (no `--global`) layers on top for
that one repo when you're ready to move it from personal to shared.

MCP registration needs the client's own CLI (`claude` / `code`) on PATH; if
it's missing or the command fails, `mimir-setup` prints a warning and still
writes the instruction files — see the manual `.mcp.json` / `.vscode/mcp.json`
snippets further down as a fallback.

The command is safe to re-run — it skips any file or registration that already exists, and only appends to `CLAUDE.md`/`copilot-instructions.md` if the mimir section isn't already there.

---

## Using mimir from the command line

You don't need an AI client to query the index. Run these directly in your terminal from inside any project that has mimir set up:

```bash
mimir hint   "quiet zone notification volume"          # discover what the codebase calls things
mimir scope  "change how jobs are retried on failure"   # find relevant files
mimir find   JobScheduler                              # locate a symbol definition
mimir callers authenticate                             # find every call site
mimir status                                           # check index state + exclusions
```

These are the same tools Claude uses — you can explore a codebase, verify a symbol exists, or trace call chains without opening a chat session. `mimir hint` is worth running yourself before writing an AI prompt from a vague ticket: it shows you the actual symbol/class names the codebase uses for the terms you typed, so you can spot right away if your search vocabulary doesn't overlap the code's (e.g. business/support language like "quiet zone" that has no lexical match anywhere) before spending a prompt on it.

---

## How Claude knows to use mimir

`mimir-setup` writes a `CLAUDE.md` section that instructs Claude to follow the mimir workflow. Without it, Claude uses mimir when it judges the task warrants it but won't follow the workflow consistently.

The section looks like this (you can edit it to suit your project):

```markdown
## Code exploration — use mimir tools

This project has mimir MCP tools available. Use them before reading raw files.

At the start of any coding session:
1. Call `get_status` to check the index is ready, see active exclusions, and note
   any project_focus already set.
2. If the user mentions a specific sub-project or area (e.g. "I'm working on the
   mobile app"), call `set_focus("matching-prefix")` immediately — this boosts files
   in that area for all subsequent searches. If no focus is set and the user has not
   indicated one, leave it unset.
3. Call `get_architecture()` for a high-level map of the whole codebase (cheap).
4. Call `get_changed_files()` to see what is currently in flight vs main.
5. Call `scope_task("describe what you want to do")` to find relevant files.

For any task involving existing code:
- Use `scope_hint("rough terms")` BEFORE `scope_task` when you have vague keywords
  and are unsure of the exact symbol names. It returns what the codebase calls things
  so the follow-up `scope_task` query is precise. Costs very few tokens.
- Use `scope_task` with specific class/method names once you know them — it finds
  the right files in one call.
- Use `semantic_search("what the code does")` when `scope_task` returns poor results
  because you know the concept but not the code name. It decomposes identifiers and
  uses BM25 ranking to find files by meaning rather than exact token match.
- Use `get_symbol(path, name)` to read ONE function or class body instead of the
  whole file.
- Use `get_file_structure` to see a file's full symbol map before reading it line
  by line.
- Use `verify_symbol_existence` before assuming a function or type exists.
- Use `find_callers` after `verify_symbol_existence` to trace impact.
- Use `get_dependents(path)` to find what else imports a file before changing it.
- Use `get_imports` when an unfamiliar symbol appears and you need to trace its
  origin.
- Call `record_alias(domain_term, code_name)` when you discover a feature name maps
  to a different code name — future scope_task searches will expand it automatically.
- Call `add_ignore(pattern, reason)` when you encounter vendor/generated/test files
  that add noise — always tell the user what you are adding and why first.
```

---

## Connecting to Claude Code (manual setup)

`mimir-setup` handles this automatically (`--global` for user scope, no arg for project scope — see above). If you'd rather configure it by hand, drop a `.mcp.json` file in the project root:

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

`mimir-setup copilot` handles this automatically (`--global` for user-profile scope, no arg for project scope — see above). If you'd rather configure it by hand, drop a `.vscode/mcp.json` file in the project root:

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

`mimir-setup copilot` creates `.github/copilot-instructions.md`, which Copilot reads automatically. If you prefer to create it manually:

```markdown
## Code exploration — use mimir tools

This project has mimir MCP tools. Always use them before using built-in search or reading files.

Workflow for any coding session:
1. Call `get_status` to confirm the index is ready and note any project_focus set.
2. If the user mentions a specific sub-project, call `set_focus("prefix")` to bias
   searches toward that area. Call `set_focus("")` when switching to another area.
3. Call `get_architecture()` for a high-level map of the whole codebase (one cheap call).
4. Call `get_changed_files()` to see what is currently in flight vs main.
5. For vague queries, call `scope_hint("rough terms")` first to find the right symbol
   names, then call `scope_task` with those names.
6. Call `get_symbol(path, name)` to read ONE function or class body.
7. Use `get_dependents(path)` before changing a shared file to see blast radius.
8. When you discover a domain/feature term maps to a code name, call `record_alias`.
9. When you encounter vendor/generated/noisy files, tell the user what you found,
   ask for confirmation, then call `add_ignore(pattern, reason)` to exclude them.

Do not use built-in file search, glob, or grep if mimir tools are available.
```

Without this file, Copilot may fall back to its own glob/grep search when mimir output is large.

### Both on the same project

If you use Claude Code on your Mac and Copilot at work, all config files can coexist in the same project:

```
your-project/
  .mcp.json                        ← picked up by Claude Code
  .mimirignore                     ← exclusion patterns (commit this to share with the team)
  .mimir-focus                     ← active project focus weights (commit if team works in same sub-project)
  .mimiraliases                    ← domain-term → code-name mappings (commit to share vocabulary)
  .mimirnotes                      ← free-text context notes tied to path prefixes (commit to share tribal knowledge)
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

Call this at the start of a session to see how much of the workspace is indexed, whether the symbol index is ready, and what focus and exclusion settings are active.

**Example output:**
```
workspace:          /your/project
source_files:       8605
blueprints_cached:  8605 in memory, 8605 on disk
symbol_index:       warm
  indexed_tokens:   267,034
semantic_search:    warm (FTS5+RRF)
warmup:             complete
tree_sitter:        on
file_watcher:       on (changes invalidate cache instantly)
reverse_imports:    1,204 files mapped
sandbox:            on

project_focus:      'incontrol.carps.mobile' ×3.0  'incontrol.carps.keypad' ×0.3
  → call set_focus("") to clear, or set_focus("prefix:weight, ...") to change

ignored_patterns (8 active):
  **/obj/**
  **/bin/**
  ...

domain_aliases (2 active):
  corrective actions → RectificationFilter
  live tutor → LiveTutor, GeminiLive
```

If `symbol_index: building`, the other tools still work but `scope_task` and `verify_symbol_existence` use a slower fallback until indexing completes (typically under 60s for large repos).

**Large monorepo note:** During the initial warm-up, mimir builds blueprints in parallel (up to 8 threads) and loads tree-sitter grammars for each language it encounters. On repos with 10k+ source files or many languages, memory can spike briefly before the LRU cache caps it. If startup is unusually slow, set `MCP_MAX_FILE_BYTES` lower to skip very large generated files, or add patterns to `.mimirignore` to exclude build output and vendor directories.

---

### 1. `set_focus` — bias searches toward the project you're working in

In a repo with multiple sibling projects (common in mobile mono-repos, microservices, etc.), generic keyword searches can surface files from projects you're not touching. `set_focus` tells mimir to multiply scores for files matching a path prefix, so the right project rises to the top.

**This tool can be called by both you and the AI.** When you tell Claude "I'm working on the mobile app", it should call `set_focus` immediately so every subsequent `scope_task` call is already biased correctly. You can also call it yourself via the CLI.

Each entry is a path-prefix substring with an optional `:weight`. Default weight is `3.0` (3× boost). Use a weight below `1.0` to suppress a project rather than boost it.

**Examples:**

```
# Boost one project
set_focus("InControl.Carps.Mobile")

# Boost Mobile AND suppress Keypad
set_focus("InControl.Carps.Mobile, InControl.Carps.Keypad:0.3")

# Explicit weights for both
set_focus("Carps.Mobile:3, Carps.Keypad:0.2, Carps.Shared:1.5")

# Clear all weights — equal scoring across all projects
set_focus("")

# Session-only weights — do not write .mimir-focus
set_focus("src/auth:3.0", persist=False)
```

By default the focus is saved to `.mimir-focus` in the workspace root and persists across sessions. Pass `persist=False` to apply weights in memory only for the current session, without touching the file. This is useful when multiple AI assistants (e.g. Claude Code and GitHub Copilot) are running against the same workspace simultaneously and you don't want them overwriting each other's focus state.

**Per-call focus:** If you only need focus for a single query, pass the `focus=` parameter directly to `scope_task` or `semantic_search` instead — it applies weights for that call only and does not modify any state:

```
scope_task("add retry logic", focus="src/payments:3.0")
semantic_search("authentication flow", focus="src/auth:2.0")
```

**Effect on scoring:** A file in the focused area with a score of 5 becomes 15 (5 × 3). A suppressed project file with a score of 15 becomes 4.5 (15 × 0.3). Files with no keyword hits are not affected — focus only amplifies existing matches, it does not invent them.

**When to reset:** Call `set_focus("")` when switching to a different part of the codebase, or if results look wrong (too many files from one area). `get_status` always shows the active weights.

---

### 2. `get_architecture` — orient yourself in one call

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

### 3. `get_changed_files` — see what's in flight

Returns structural blueprints of every source file changed vs a git base branch — covering committed branch changes, uncommitted edits, and untracked new files.

**Example:**
> `get_changed_files()` or `get_changed_files(base="develop")`

Returns blueprints (symbols + line numbers, bodies stripped) for each changed file plus a diff summary table so you get the full structural picture in one call.

WHEN TO USE: at the start of a session to orient yourself on what is actively being worked on before deciding where to focus.

---

### 4. `scope_hint` — cheap first pass when you have rough keywords

Call this **before** `scope_task` when you have vague descriptions and are unsure of the exact symbol or class names used in the codebase. It performs a quick symbol lookup across all terms, returns what it found per term, and suggests a refined `scope_task` query using the actual names it discovered.

Because it returns only names and file paths — no blueprints — it costs very few tokens.

**Example:**
> `scope_hint("timer refresh current jobs")`

```
# Scope Hint: 'timer refresh current jobs'

## Term matches
  'timer' → Services/Realtime/GroupProfileRealtimeRefreshServiceTests.cs, ...
  'refresh' → Services/Realtime/GroupProfileRealtimeRefreshService.cs, ...
  'current' → ViewModels/CurrentJobs/CurrentJobsViewModel.cs, ...

## Top files
  InControl.Carps.Mobile/UI/ViewModels/MyCurrentViewModel.cs
    → StartOrStopAutoRefresh, _refreshIntervalInSeconds

## Suggested scope_task query
  scope_task("MyCurrentViewModel StartOrStopAutoRefresh")
```

The suggested query uses the actual symbol names found — copy it directly into `scope_task` for a precise, high-confidence result.

**Sub-token matching:** `scope_hint` and `scope_task` both understand compound identifiers. Searching for `"refresh"` will find files containing `StartOrStopAutoRefresh` or `_refreshIntervalInSeconds` — you don't need to know the exact compound name to find the right file.

---

### 5. `scope_task` — find the right files

Call this on any task involving existing code. Give it a plain-English description — or ideally specific class/method names once you know them from `scope_hint`. It extracts keywords, searches the workspace (symbol index + path matching + import graph + git recency), and returns a ranked list of the most relevant files with matched symbol locations and suggested `get_symbol` calls.

**Example — using exact names (most precise):**
> `scope_task("MyCurrentViewModel StartOrStopAutoRefresh refresh interval")`

**Example — using feature description (less precise, use scope_hint first if uncertain):**
> `scope_task("timer that polls refresh in My Current Jobs")`

Returns: keywords searched, matched symbols with file:line locations, ranked files by relevance, and suggested next calls:
```
## Ranked files
  1. InControl.Carps.Mobile/UI/ViewModels/MyCurrentViewModel.cs  (18 matches)
  2. ...

## Suggested next calls (read just the symbols you need)
  get_symbol("InControl.Carps.Mobile/UI/ViewModels/MyCurrentViewModel.cs", "MyCurrentViewModel")
  get_symbol("InControl.Carps.Mobile/UI/ViewModels/MyCurrentViewModel.cs", "StartOrStopAutoRefresh")
```

**Tips for better results:**
- Use technical/class names when known rather than feature descriptions. `"RectificationFilter section order"` finds the right file faster than `"corrective actions filter UI"`.
- If you're in a mono-repo with multiple projects, call `set_focus` first so the right project's files rank above sibling projects with similar naming.
- If the results look wrong, try `scope_hint` to discover the actual names and re-run.
- Pass `include_blueprints=True` to get full symbol maps inline (useful for small repos or when you want the structure in one call, without needing `get_file_structure` as a follow-up).

**How scores work:** BM25-style. Symbol definition hits score 3×, usage hits 1×, capped per keyword (repeated hits of one word give diminishing returns), then weighted by IDF — rare identifiers count up to 2×, ubiquitous words as little as 0.05×. Scores are length-normalized so god-files that weakly match everything don't dominate. Path/filename matches score 3×. Adjacent capitalized words ("Unavailable Types") are also searched as their compound identifier (`UnavailableType`). Files inside the active `set_focus` prefix get a multiplier on top, and git recency gives a bounded boost to already-matched files.

---

### 5b. `get_context` — one-shot context loader

Combines `scope_task`, `get_file_structure`, and `get_symbol` into a single call. Use this when you know you'll need to read file structures and key symbols right away, and you want to avoid 3–4 serial round trips.

**Example:**
> `get_context("UserService authenticate login", max_files=3)`

Returns: ranked files section (same as `scope_task`), blueprints for each top file, and the full bodies of the 2 most relevant definition symbols.

**When to prefer `get_context` over `scope_task`:**
- You're starting fresh on a task and need to orient quickly
- `max_files` is 2–3 (output stays manageable)
- You expect to call `get_file_structure` immediately after `scope_task` anyway

**When to prefer `scope_task` instead:**
- You only want the ranked file list to decide what to look at next
- `max_files` > 3 (blueprints for many files produce very large output)
- You're in a mono-repo and want to scan the ranked list before committing to reading anything

---

### 6. `semantic_search` — find code by what it does, not what it's called

Use this when `scope_task` returns poor results because you know the **concept** but not the **code name**. Good cases:
- "authentication token refresh" when the method is called `renewCredentials`
- "connection pool exhaustion" when the class is `DBSessionLimiter`
- Cross-cutting concerns spread across several differently-named files

**How it works:** Decomposes identifiers into sub-tokens at index time (`handleAuthFailure` → `"handle auth fail"`) and runs FTS5 BM25 ranked retrieval against those tokens, fused with symbol-index keyword hits via Reciprocal Rank Fusion. The reverse-import graph then adds structurally adjacent files.

The index also explicitly extracts and decomposes **parameter type names**, **return type names**, and **decorator/annotation names** from each signature — so searching for `"UserProfile"` finds functions that *return* a `UserProfile` even if the function name doesn't mention it, and searching for `"Authenticated"` finds classes decorated with `@Authenticated`.

**Key difference from `scope_task`:** `scope_task` does exact-token lookup (~1ms). `semantic_search` does ranked full-text search over decomposed tokens (~3ms). Use `scope_task` first — fall back to `semantic_search` when you know what the code *does* but the name is different from your mental model.

`set_focus` weights apply to `semantic_search` the same way as `scope_task`. You can also pass `focus=` inline for a single call (see `set_focus` docs).

**Example:**
> `semantic_search("database connection pool exhaustion")`

Returns ranked files with matched symbol signatures showing why each file was ranked.

---

### 7. `get_symbol` — read just the code you need

Returns the complete source of ONE named function, class, or method — bodies included. The efficient middle ground between `get_file_structure` (signatures only) and reading the whole raw file.

**Example:**
> `get_symbol("src/services/auth.py", "authenticate")`

Returns just the `authenticate` method — 20 lines instead of the 400-line file.

WHEN TO USE: after `scope_task` or `get_file_structure` identifies the symbol you need. Typically 10–50× fewer tokens than reading the whole file.

If the symbol is not found, the response includes the file's full blueprint so you can see what IS available.

---

### 8. `get_file_structure` — understand a file

Returns a compact map of a single file: every class, function, method, and their signatures — with line numbers, bodies stripped.

**Example:**
> `get_file_structure("lib/lessonProgress.ts")`

Use this when you want the full symbol map of a file before deciding which symbols to read. Use `get_symbol` when you already know which symbol you need.

---

### 9. `get_imports` — trace where symbols come from

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

> **Tip for namespace imports (C#, Kotlin, Swift):** When `get_imports` returns `[workspace?] SomeNamespace.TypeName`, mimir now automatically searches the symbol index for the type name and shows a `→ found:` hint with the definition file and line number when it can resolve it. If the hint doesn't appear, call `verify_symbol_existence("TypeName")` manually.

---

### 10. `get_directory_structure` — browse a module

Returns structural blueprints for every source file under a directory. Use this when you know *where* to look but not *which* file.

**Example:**
> `get_directory_structure("src/api/controllers", max_files=10)`

Use `scope_task` when you don't know where to look. Use `get_directory_structure` when you already know the directory.

---

### 11. `verify_symbol_existence` — confirm a symbol is real

Searches the entire workspace for a symbol definition and returns its exact location and signature.

**Example:**
> `verify_symbol_existence("StartOrStopAutoRefresh")`

Use this before assuming a function or type exists, before importing it, or when a symbol appears in a blueprint and you want its definition location.

---

### 12. `find_callers` — trace who calls a symbol

Searches raw source text across the entire workspace for every call site and usage of a symbol. Unlike `verify_symbol_existence` (which only finds definitions), this finds where a symbol is called, passed, or referenced in implementation code.

**Example:**
> `find_callers("AuthenticationService", max_results=20)`

WHEN TO USE: after `verify_symbol_existence` tells you where something is defined, use `find_callers` to trace who calls it — for impact analysis, understanding data flow, or finding all consumers of an interface.

> **Performance tip:** If [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) is installed, mimir uses it automatically for ~10× faster scanning on large repos (macOS: `brew install ripgrep`, Windows: `winget install BurntSushi.ripgrep.MSVC`). Falls back to Python scan transparently when unavailable.

---

### 13. `get_dependents` — blast-radius analysis

Returns every workspace file that directly imports a given file. Built from the reverse import index constructed at startup — no extra configuration needed.

**Example:**
> `get_dependents("src/services/auth.py")`

Returns a sorted list of files that import `auth.py`. If no workspace files import it, mimir says so and suggests `find_callers` for languages (Swift, Kotlin, Java, Go, Rust) where import resolution is not supported.

WHEN TO USE: before modifying a widely-used utility, service, or model — get the full blast radius in one call instead of grepping for the filename manually.

---

### 14. `record_alias` — teach mimir your project's vocabulary

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

### 14b. `record_note` — attach context to a path

Records a free-text note tied to a path prefix. **Different from `record_alias`**: aliases expand search vocabulary (silently fed into `scope_task`'s keyword matching); notes attach context — shown as prose next to matching files, never used for ranking or search.

**Example:**
> `record_note("Features/Playback", "background sync uses platform-native timers, not the shared cross-platform service — check MainActivity.java/AppDelegate.swift, not SyncService.cs")`

**How it works:**
- Writes to `.mimirnotes` in the workspace root (human-editable, commit to git)
- Shown as `note: ...` lines under matching files in `get_file_structure`, `get_directory_structure`, and `scope_task`'s ranked-files list
- Multiple notes can accumulate under the same prefix — it's an append-only log, unlike `.mimiraliases` which merges into one line per domain term
- Shown longest-matching-prefix first, capped at 3 notes per file (with a "+N more" line if truncated) so one broad-prefix note doesn't drown every query in text
- `get_status` shows a count summary only — full notes appear in the file-scoped output above, not in `get_status` itself

**Maintaining `.mimirnotes` manually:**

```
# mimir contextual notes — free-text context attached to a path prefix
# Format:  path/prefix = note text

Features/Playback = background sync uses platform-native timers, not SyncService.cs
Features/Playback = check MainActivity.java/AppDelegate.swift for the real logic
```

---

### 15. `add_ignore` — exclude noisy files on the fly

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

### 16. `execute_local_sandbox` — run a quick snippet

Runs a Python or bash snippet locally with a timeout, captures output, and returns it.

**Example use cases:**
- Run a test to verify a fix
- List files matching a pattern
- Check a computed value

Not for long-running jobs. Not a security sandbox — code runs as your user.

---

## Recommended workflow

### General session

```
Session start
  1. get_status()                           ← confirm index ready; check if focus is set
  2. set_focus("your-sub-project")          ← if in a mono-repo — boost your project, suppress others
                                               (AI calls this automatically when you name a sub-project)
  3. get_architecture()                     ← understand the project layout
  4. get_changed_files()                    ← see what's currently in flight

Finding and reading code
  5. scope_hint("rough description")        ← cheap first pass: discover the right symbol names
  6. scope_task("ClassName MethodName")     ← precise search using names from scope_hint
     — or —
     semantic_search("what the code does")  ← when you know the concept but not the name
  7. get_symbol("file.cs", "SymbolName")   ← read just the function/class you need
     — or —
     get_file_structure("file.cs")          ← full symbol map if you need the overview

Tracing dependencies
  8. get_imports("file.cs")                ← where do its symbols come from?
  9. get_dependents("file.cs")             ← what breaks if I change this?
  10. find_callers("SymbolName")           ← who calls this?

Keeping the index clean
  11. add_ignore("**/pattern/**", "reason")  ← exclude noise when you find it
      record_alias("feature", "CodeName")    ← teach mimir your project's language
```

### Jira ticket workflow

This is the highest-value pattern when working from a ticket or bug report.

**Step 1 — Set context once per ticket:**
```
set_focus("InControl.Carps.Mobile")
```
Tell the AI what project/area the ticket is about. It should call `set_focus` for you, but you can also call it yourself.

**Step 2 — Use scope_hint when the ticket is written in feature language:**
```
scope_hint("timer polling refresh my current jobs")
```
The hint tells you what the codebase calls things:
```
Top files:
  InControl.Carps.Mobile/UI/ViewModels/MyCurrentViewModel.cs
    → StartOrStopAutoRefresh, _refreshIntervalInSeconds
```

**Step 3 — Use those exact names in scope_task:**
```
scope_task("MyCurrentViewModel StartOrStopAutoRefresh refresh interval")
```
This finds the right file at #1 with high confidence, shows matched symbol locations, and gives you exact `get_symbol` calls to read just the code you need.

**Step 4 — Read only what you need:**
```
get_symbol("InControl.Carps.Mobile/UI/ViewModels/MyCurrentViewModel.cs", "StartOrStopAutoRefresh")
get_symbol("InControl.Carps.Mobile/UI/ViewModels/MyCurrentViewModel.cs", "MyCurrentViewModel")
```

This three-step pattern (hint → task → symbol) uses a fraction of the tokens that reading files directly would cost, and lands on the right code much faster than grepping.

### When scope_hint isn't needed

Skip `scope_hint` when you already know the class or method name — go straight to `scope_task`:
```
scope_task("JobCancelled JobPaused IRecipient MyCurrentViewModel")
```
Technical names in the query give `scope_task` enough signal to find the right file in one call. `scope_hint` is the warm-up round when you only have feature/domain language to start from.

### Fixed limitations (found testing real tickets on Carps, ~8,000 files)

Four distinct problems surfaced across the pipeline while testing real tickets. All four are now fixed and guarded by `tests/bench_rank_eval.py`, a ground-truth ranking eval that runs the actual failing tickets against Carps and Topcat.

**Fixed — query understanding (`_extract_scope_keywords`):**
1. Plain sentence-initial capitalized words ("Investigation", "The", "Observation") were crowding out real identifiers embedded later in a ticket (e.g. in a code snippet or stack trace) — the keyword cap filled before the scan ever reached them. Fixed by giving multi-hump identifiers (`PushNotificationSenderServiceWorker`, `DoWorkAsync`) priority over single-capital prose words.
2. Short ALL-CAPS version/client codes from a ticket's metadata header (`DB 8.24, AWS 8.2.1, APK 8.0.28..., Client: SCGH, SDP: 20631`) were being mistaken for real identifiers for the same reason. Fixed by excluding ALL-CAPS tokens ≤4 chars from that pass.

**Fixed — index coverage (`#strings` blueprint section):** exception/log message strings (`"Maximum retry attempts exceeded"`) used to be structurally invisible to `scope_task`/`semantic_search` because blueprints strip bodies. Blueprints now end with a `#strings` section listing string-literal arguments of throw/raise statements, exception constructors (`new *Exception(...)` matched anywhere — Carps wraps them in `Result` objects rather than throwing at the construction site), and known logging calls (`LogWarning`, `logger.error`, `console.error`, ...). Interpolated (`$"..."`) and template strings are supported with the interpolation holes skipped. Deliberately NOT all string literals: messages must be ≥10 chars and multi-word, are truncated at 120 chars, and cap at 40 per file — so UI text, SQL, and format noise stay out of the index. The real Rollbar ticket that motivated this went from a complete miss to **#1** in both `scope_task` and `semantic_search`.

**Fixed — ranking (BM25 on the fast path):** `scope_task` ranked by capped raw match-count, so generic business-vocabulary tickets ("Unavailable Types are set against a category... Categories/Teams that they have access to") buried the right file under files racking up matches on common words like "Types"/"All"/"Operative". Ranking is now the full BM25 recipe on the existing exact-token path (kept separate from FTS5 on purpose — exact-token retrieval semantics and ~ms latency are the tool's core promise):
- **IDF weighting** — document frequencies computed at warmup from the symbol index; ubiquitous words are discounted down to 0.05×, rare identifiers boosted up to 2×. Refreshed in the background as files change.
- **Document-length normalization** — god-files (EF `DbContext`, generated registries) that weakly match every entity are divided down; small files specifically about the queried concept are boosted.
- **Compound bigrams** — adjacent capitalized prose words ("Unavailable Types") are also searched as the code's compound identifier (`UnavailableType(s)`), anchoring the ranking to the concept's actual files.
- The keyword-diversity multiplier only counts keywords with meaningful IDF, and the git-recency boost is bounded to at most double a match score.

Eval results: ticket MRR 0.394 → 0.800, all four failing ticket cases now top-5 (three at #1), zero regressions on precise-identifier queries (5/5 still #1), p50 latency 16ms on 8k files. Re-run anytime with `python tests/bench_rank_eval.py`.

**Still open (deliberately):** `.resx`/localized UI-text search — a separate, bigger feature to consider only if it comes up as a real need later.

---

## Connecting the AI to your project context — tips

**For the AI to get the best results automatically:**

1. **Name your sub-project early.** Say "I'm working on the mobile app" or "this ticket is for the Keypad project" at the start of a session. The AI will call `set_focus` for you based on what it finds in `get_status` and `get_architecture`.

2. **Use ticket language freely.** `scope_hint` bridges the gap between feature descriptions and code names. You don't need to know the class names — tell the AI what the feature does and let it discover the names first.

3. **Record aliases for your team's vocabulary.** When you notice `scope_task` consistently needing the same bridging — e.g., "corrective actions" always means `RectificationFilter` — run `record_alias` once and it applies to every future session.

4. **Record notes for non-obvious tribal knowledge.** When you discover something a future reader couldn't infer from names alone — e.g. "this uses platform-native timers, not the shared service" — run `record_note` once and it surfaces automatically whenever that path comes up again, without affecting search ranking the way aliases do.

5. **Commit `.mimirignore`, `.mimiraliases`, and `.mimirnotes`.** These are the institutional memory of your codebase. The whole team benefits from shared exclusions, vocabulary mappings, and tribal-knowledge notes.

6. **Commit `.mimir-focus` if your team works in the same sub-project.** If everyone on the team is working in `InControl.Carps.Mobile`, checking in `.mimir-focus` means the right focus is active from the first session with no manual setup.

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

Expected: **272 passed** (~35s — smoke tests spawn real subprocesses).

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
