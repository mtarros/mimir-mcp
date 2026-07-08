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

`mimir-setup [claude|copilot-cli] [--global]` does everything: registers
mimir as an MCP server with the named client and writes the workflow
instructions telling the AI when and how to use it. Client defaults to
`claude` when no arg is given (`cli` is accepted as shorthand for
`copilot-cli`). Both are terminal/CLI tools — `claude` means the Claude Code
CLI, `copilot-cli` means the standalone `copilot` GitHub Copilot CLI (not
VS Code, not the full Visual Studio IDE — mimir doesn't target either of
those editor integrations).

**Personal use, nothing shared yet** — add `--global`. Registers at USER scope
(available in every project on this machine) and writes the instructions to
your user profile instead of this repo — nothing touches the project at all:

```bash
mimir-setup claude --global         # registers with Claude Code (user scope) + ~/.claude/CLAUDE.md
mimir-setup copilot-cli --global    # registers with Copilot CLI (~/.copilot/mcp-config.json) + $HOME/.copilot/copilot-instructions.md
```

**Ready to share with a team on this project** — drop `--global`. Registers at
PROJECT scope and writes the instructions into the repo, meant to be committed:

```bash
cd /path/to/your-project
mimir-setup             # .mcp.json + CLAUDE.md (defaults to claude)
mimir-setup copilot-cli # .github/copilot-instructions.md (MCP registration is always ~/.copilot/mcp-config.json — Copilot CLI has no project scope)
```

| Mode | MCP registration | Instructions | .mimirignore |
|---|---|---|---|
| `--global` | user scope (every project) | `~/.claude/CLAUDE.md`, or `$HOME/.copilot/copilot-instructions.md` | skipped — inherently project-specific |
| default (no `--global`) | project scope for claude (committed `.mcp.json`); always `~/.copilot/mcp-config.json` for copilot-cli, which has no project scope | `CLAUDE.md` or `.github/copilot-instructions.md` | starter file created |

The two scopes don't conflict — global instructions/registration apply
everywhere, a project's own `mimir-setup` (no `--global`) layers on top for
that one repo when you're ready to move it from personal to shared.

Every path appends/merges the mimir entry alongside whatever's already
registered — never overwrites. `claude` goes through its own CLI (`claude mcp
add`), verified to merge correctly on its own. `copilot-cli` has no CLI
subcommand for registration (only an interactive `/mcp add` form), so
mimir-setup merges its `~/.copilot/mcp-config.json` directly instead —
verified this preserves any other servers already configured there. Either
way, if a required CLI is missing, a command fails, or an existing
`.mcp.json`/`mcp-config.json` isn't valid JSON, `mimir-setup` prints a
warning and still writes the instruction file — see the manual `.mcp.json`
snippet further down as a fallback.

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
mimir audit                                            # scan the index for noise, get .mimirignore suggestions
```

These are the same tools Claude uses — you can explore a codebase, verify a symbol exists, or trace call chains without opening a chat session. `mimir hint` is worth running yourself before writing an AI prompt from a vague ticket: it shows you the actual symbol/class names the codebase uses for the terms you typed, so you can spot right away if your search vocabulary doesn't overlap the code's (e.g. business/support language like "quiet zone" that has no lexical match anywhere) before spending a prompt on it.

---

## How Claude knows to use mimir

`mimir-setup` writes a `CLAUDE.md` section that instructs Claude to follow the mimir workflow. Without it, Claude uses mimir when it judges the task warrants it but won't follow the workflow consistently.

The section looks like this (you can edit it to suit your project):

```markdown
## Code exploration — use mimir tools (v3)

This project has mimir MCP tools available. Use them before reading raw files.

@.mimir-overview.md

Two MCP tools are registered: `locate` (find relevant files/symbols) and `inspect`
(read code). Everything else — status, arch, changed, set_focus, set_scope, alias,
note, ignore, audit, help — is a `mimir` shell command instead (occasional/session-
level actions, not needed every task, so they're not separate registered tools):
run `mimir <command> "<args>"` via the shell, e.g. `mimir alias "domain term, CodeName"`.
`mimir --help` lists every command.

At the start of any coding session:
1. If the user names or clearly implies a specific sub-project/app/API area (e.g.
   "the mobile app", "the API"), run `mimir set_focus "matching-prefix:3"`
   immediately, or pass focus="prefix:3" directly on individual locate() calls. In a
   multi-sub-project repo, unscoped ranking silently defaults to whichever sub-project
   has the most indexed symbols; it will NOT reliably surface the right area on its own.
2. Call locate("describe what you want to do") to find relevant files/symbols.

For any task involving existing code:
- Call locate() before opening files or grepping — one call ranks files AND surfaces
  the specific symbol names in them.
- Call inspect(path, symbol="Name1,Name2") to read just the symbols you need — batch
  multiple names in one call rather than one inspect() call per name.
- Omit `symbol` on inspect() to get a file's (or directory's) full structural map first.
- Use inspect(path, view="imports") to trace an unfamiliar symbol's origin.
- Use inspect(path, view="dependents") before changing a shared file to see blast radius.
- Use inspect(symbol=Name, view="callers") after locate(mode="symbol") to trace impact.
- Run `mimir alias "domain term, CodeName"` when a feature name maps to a different
  code name — future locate() searches expand it automatically.
- Run `mimir ignore "pattern, reason"` when you encounter vendor/generated/test
  files that add noise — always tell the user what you are adding and why first.

In a monorepo, if locate() results span unrelated sub-projects, call locate(mode="area")
to see where matches cluster, then `mimir scope --set <dir>` to hard-narrow (excludes
other sub-projects entirely) or `mimir set_focus "prefix:weight"` to soft-bias (they
still appear, just lower-scored). Clear with `mimir scope --reset` / `mimir set_focus ""`.
```

(`@.mimir-overview.md` is Claude Code's file-import syntax — it pulls mimir's generated architecture snapshot into context automatically, no tool call needed. Only added for project-scoped setup, since the file is workspace-specific.)

> **Upgrading from a pre-2026-07-09 install?** Re-run `mimir-setup` — it now replaces a stale `## Code exploration` section (any version) instead of skipping it when one is already present, so your CLAUDE.md picks up the new tool names/syntax automatically. Nothing breaks in the meantime either way: every old tool name (`scope_task`, `get_symbol`, etc.) still works, just as a `mimir scope_task "..."` shell command instead of a directly-registered tool — see [Tools and when to use them](#tools-and-when-to-use-them) below. (An earlier same-day v2 also briefly registered a third `mimir` dispatcher tool — since replaced by v3's CLI-only approach, so `mimir("command", ...)` tool-call syntax from a v2 install is stale too and gets upgraded the same way.)

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

## Connecting to GitHub Copilot CLI (manual setup)

`mimir-setup copilot-cli` handles this automatically (registration always goes
to `~/.copilot/mcp-config.json` — Copilot CLI has no project scope, per
GitHub's own docs). If you'd rather configure it by hand, edit that file
directly (or override its location with the `COPILOT_HOME` env var):

```json
{
  "mcpServers": {
    "mimir": {
      "type": "local",
      "command": "mimir",
      "args": [],
      "env": {},
      "tools": ["*"]
    }
  }
}
```

Manage it interactively instead with `/mcp add` inside a `copilot` session, or check what's registered with the `copilot mcp` subcommands from the terminal.

### Giving Copilot CLI persistent instructions (manual setup)

`mimir-setup copilot-cli` creates `.github/copilot-instructions.md` in the current project (or `mimir-setup copilot-cli --global` for `$HOME/.copilot/copilot-instructions.md`, applied to every project), both of which Copilot CLI reads automatically. If you prefer to create either manually:

```markdown
## Code exploration — use mimir tools (v3)

This project has mimir MCP tools. Always use them before using built-in search or reading files.

Two MCP tools are registered: locate (find relevant files/symbols) and inspect (read
code). Everything else — status, arch, changed, set_focus, set_scope, alias, note,
ignore, audit, help — is a `mimir` shell command instead (occasional/session-level
actions, not needed every task, so they're not separate registered tools): run
`mimir <command> "<args>"` via the shell, e.g. `mimir alias "domain term, CodeName"`.
`mimir --help` lists every command.

Workflow for any coding session:
1. If the user names or clearly implies a specific sub-project/app/API area (e.g.
   "the mobile app", "the API"), run `mimir set_focus "matching-prefix:3"`
   immediately, or pass focus="prefix:3" directly on individual locate() calls. In a
   multi-sub-project repo, unscoped ranking silently defaults to whichever sub-project
   has the most indexed symbols; it will NOT reliably surface the right area on its own.
2. Call locate() with a description of the task — use its ranked file list and inline
   symbol names, do NOT fall back to glob or grep.
   - Tip: use technical/class names when known (e.g. 'RectificationFilter') rather
     than feature names (e.g. 'corrective actions filter').
3. Call inspect(path, symbol="Name1,Name2") to read just the symbols you need in one
   call — much cheaper than reading the whole file, and cheaper than one inspect()
   call per symbol.
4. Use inspect(path, view="dependents") before changing a shared file to see blast
   radius.
5. When you discover a domain/feature term maps to a code name, run
   `mimir alias "domain term, CodeName"` to save it — future locate() searches
   expand it automatically.
6. When you encounter vendor/generated/noisy files, tell the user what you found, ask
   for confirmation, then run `mimir ignore "pattern, reason"` to exclude them.

Do not use built-in file search, glob, or grep if mimir tools are available.

In a monorepo, if locate() results span unrelated sub-projects, call
locate(task, mode="area") to see where matches cluster, then `mimir scope --set <dir>`
to hard-narrow (excludes other sub-projects entirely) or
`mimir set_focus "prefix:weight"` to soft-bias (they still appear, just
lower-scored). Clear with `mimir scope --reset` / `mimir set_focus ""`.
```

Without this file, Copilot may fall back to its own glob/grep search when mimir output is large.

### Both on the same project

If you use Claude Code on your Mac and Copilot CLI at work, project-level
config files can coexist in the same project (Copilot CLI's own MCP
registration lives outside the repo, at `~/.copilot/mcp-config.json`,
regardless):

```
your-project/
  .mcp.json                        ← picked up by Claude Code
  .mimirignore                     ← exclusion patterns (commit this to share with the team)
  .mimir-focus                     ← active project focus weights (commit if team works in same sub-project)
  .mimiraliases                    ← domain-term → code-name mappings (commit to share vocabulary)
  .mimirnotes                      ← free-text context notes tied to path prefixes (commit to share tribal knowledge)
  .github/
    copilot-instructions.md        ← tells Copilot CLI to trust mimir results
  CLAUDE.md                        ← tells Claude Code when and how to use mimir
```

Same mimir install, same tools, no conflicts.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MCP_WORKSPACE_ROOT` | current directory | Root of the repo mimir maps |
| `MCP_MAX_FILE_BYTES` | `2000000` | Skip files larger than this |

### Windows note

All tools work on Windows.

---

## Tools and when to use them

**As of 2026-07-09, only 2 tools are registered with your AI client: `locate` and `inspect`** — a redesign that cut the per-turn tool-schema overhead from ~15,100 to ~2,622 characters (an interim design briefly also registered a third `mimir` dispatcher tool; that was pulled too, the same day, once we confirmed its commands are occasional/session-level rather than needed every task). Every tool below this line is still a real, fully working Python function (nothing was removed), but the AI reaches it two different ways instead of calling it by name directly:

- `locate()` and `inspect()` are themselves the new tools — see [locate](#5-scope_task--find-the-right-files) and [inspect](#7-get_symbol--read-just-the-code-you-need) below for what each one replaces.
- Everything else (`get_status`, `set_focus`, `record_alias`, `add_ignore`, `scope_hint`, `verify_symbol_existence`, `find_callers`, `get_dependents`, `get_imports`, `get_architecture`, `get_changed_files`, `semantic_search`, `record_note`, and the original `scope_task`/`get_symbol`/`get_file_structure` forms) is reached as a **shell command**, run via the Bash/terminal tool, not a tool call: `mimir status`, `mimir alias "domain term, CodeName"`, etc. Each subsection below is marked with a `→` line showing the exact command. Run `mimir help` for the full command list, or `mimir help <command>` for one command's original argument docs.

The explanations and examples below still describe exactly how each underlying function behaves — only the call syntax changed.

### 0. `get_status` — check the index before you start

→ `mimir status` (shell command)

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

→ `mimir set_focus "entries persist=true|false"` (shell command)

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

→ `mimir arch` (shell command) — or, cheaper still, zero calls: the generated
CLAUDE.md `@`-imports `.mimir-overview.md`, which already contains this map.

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

WHEN TO USE: at the very start of a session to understand the project layout before diving into specific files. For a specific directory use `get_file_structure(dir_path)` instead.

Large repos (60+ directories) show the most file-dense modules first with a note pointing to `get_file_structure` for the rest.

---

### 3. `get_changed_files` — see what's in flight

→ `mimir changed main` (shell command; base branch defaults to "main" if omitted)

Returns structural blueprints of every source file changed vs a git base branch — covering committed branch changes, uncommitted edits, and untracked new files.

**Example:**
> `get_changed_files()` or `get_changed_files(base="develop")`

Returns blueprints (symbols + line numbers, bodies stripped) for each changed file plus a diff summary table so you get the full structural picture in one call.

WHEN TO USE: at the start of a session to orient yourself on what is actively being worked on before deciding where to focus.

---

### 4. `scope_hint` — cheap first pass when you have rough keywords

→ `mimir scope_hint "rough terms"`, or the dedicated `mimir hint "rough terms"` (shell commands)

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

→ Superseded by the `locate` tool: `locate("describe what you want to do")`.
`locate` returns a more compact format (matched symbol names inline, not a
separate section) and falls through to a semantic search automatically on
zero hits. The exact original `scope_task` format below is still reachable
via `mimir scope_task "task max_files=5"` (shell command) if you want it specifically.

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

---

### 6. `semantic_search` — find code by what it does, not what it's called

→ `locate()` now falls through to this automatically when its exact-symbol
search finds nothing — a manual call is rarely needed. To force it directly:
`mimir semantic_search "what the code does max_results=10"` (shell command).

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

→ Superseded by the `inspect` tool: `inspect(path, symbol="Name1,Name2")`.

Returns the complete source of one or more named functions/classes/methods — bodies included. The efficient middle ground between `get_file_structure` (signatures only) and reading the whole raw file.

**Example:**
> `get_symbol("src/services/auth.py", "authenticate")`

Returns just the `authenticate` method — 20 lines instead of the 400-line file.

**Batching:** pass several comma-separated names in one call — `get_symbol("src/services/auth.py", "authenticate, refresh_token, revoke")` — to get all of them with one shared header instead of one call (and one header) per name. This matters most on property-heavy files (MAUI/XAML code-behind, DTOs with many one-line fields) where `get_file_structure`'s blueprint barely compresses — there's little body to strip when most "symbols" are already one line — so several targeted `get_symbol` calls beat the blueprint, and one batched call beats several separate ones.

WHEN TO USE: after `scope_task` or `get_file_structure` identifies the symbol(s) you need. Typically 10–50× fewer tokens than reading the whole file.

If a symbol isn't found, it's listed under "Not found" and the response still includes the file's full blueprint so you can see what IS available; other requested symbols that were found are still returned in full.

---

### 8. `get_file_structure` — understand a file or browse a module

→ Superseded by the `inspect` tool: `inspect(path)` (omit `symbol` for the
structural map, works on a file or a whole directory).

Pass a file for a compact map of it: every class, function, method, and their signatures — with line numbers, bodies stripped. Pass a directory instead and it returns the same blueprint for every source file under it (capped at `max_files`, default 10) — use this when you know *where* to look but not *which* file.

**Examples:**
> `get_file_structure("lib/lessonProgress.ts")`
> `get_file_structure("src/api/controllers", max_files=10)`

Use this when you want the full symbol map of a file before deciding which symbols to read — `get_symbol` when you already know which symbol you need, `scope_task` when you don't know where to look at all.

---

### 9. `get_imports` — trace where symbols come from

→ `inspect(path, view="imports")`

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

### 10. `verify_symbol_existence` — confirm a symbol is real

→ `locate(symbol_name, mode="symbol")`, or `mimir verify_symbol_existence "SymbolName"` (shell command)

Searches the entire workspace for a symbol definition and returns its exact location and signature.

**Example:**
> `verify_symbol_existence("StartOrStopAutoRefresh")`

Use this before assuming a function or type exists, before importing it, or when a symbol appears in a blueprint and you want its definition location.

---

### 11. `find_callers` — trace who calls a symbol

→ `inspect(symbol="SymbolName", view="callers")`

Searches raw source text across the entire workspace for every call site and usage of a symbol. Unlike `verify_symbol_existence` (which only finds definitions), this finds where a symbol is called, passed, or referenced in implementation code.

**Example:**
> `find_callers("AuthenticationService", max_results=20)`

WHEN TO USE: after `verify_symbol_existence` tells you where something is defined, use `find_callers` to trace who calls it — for impact analysis, understanding data flow, or finding all consumers of an interface.

> **Performance tip:** If [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) is installed, mimir uses it automatically for ~10× faster scanning on large repos (macOS: `brew install ripgrep`, Windows: `winget install BurntSushi.ripgrep.MSVC`). Falls back to Python scan transparently when unavailable.

---

### 12. `get_dependents` — blast-radius analysis

→ `inspect(path, view="dependents")`

Returns every workspace file that directly imports a given file. Built from the reverse import index constructed at startup — no extra configuration needed.

**Example:**
> `get_dependents("src/services/auth.py")`

Returns a sorted list of files that import `auth.py`. If no workspace files import it, mimir says so and suggests `find_callers` for languages (Swift, Kotlin, Java, Go, Rust) where import resolution is not supported.

WHEN TO USE: before modifying a widely-used utility, service, or model — get the full blast radius in one call instead of grepping for the filename manually.

---

### 13. `record_alias` — teach mimir your project's vocabulary

→ `mimir alias "domain term, CodeName"` (shell command)

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

### 13b. `record_note` — attach context to a path

→ `mimir note "path/prefix, note text"` (shell command)

Records a free-text note tied to a path prefix. **Different from `record_alias`**: aliases expand search vocabulary (silently fed into `scope_task`'s keyword matching); notes attach context — shown as prose next to matching files, never used for ranking or search.

**Example:**
> `record_note("Features/Playback", "background sync uses platform-native timers, not the shared cross-platform service — check MainActivity.java/AppDelegate.swift, not SyncService.cs")`

**How it works:**
- Writes to `.mimirnotes` in the workspace root (human-editable, commit to git)
- Shown as `note: ...` lines under matching files in `get_file_structure` (file or directory mode) and `scope_task`'s ranked-files list
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

### 14. `add_ignore` — exclude noisy files on the fly

→ `mimir ignore "pattern, reason"` (shell command)

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
