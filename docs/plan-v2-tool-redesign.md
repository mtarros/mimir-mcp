# Mimir v2: 18 tools → 3, compact `locate` discovery, zero-round-trip session overview

Implementation plan, written 2026-07-09. **Status: implemented and verified** — all 5 commits landed, 375/375 tests passing.

## Results (measured, not estimated)

- **Wire size**: 18-tool schema was 15,112 chars (~3,778 tok) transmitted every turn. 3-tool schema (`locate`/`inspect`/`mimir`): **3,764 chars (~941 tok) — a 75% cut.** Guarded going forward by `TestToolRegistration::test_wire_budget_under_4000_chars`.
- **Carps re-run** (exact "Keypad font-icon rendering" task from the 2026-07-08 A/B session): 12,210 chars (v1, all 3 prior fixes applied) → **9,168 chars (v2, locate+inspect) — mimir now beats the native grep+Read baseline (11,556 chars) by 1.26x**, not just closes the gap. `locate()` alone replaced `scope_task`'s 4,793-char call with a 1,751-char one for the identical ranked result.
- **Topcat cross-check** (different repo, different task — "retry delay workflow configuration when device does not respond"): `scope_task` 4,051 chars vs `locate` 1,185 chars for **identical ranking** (same top-6 files, same scores, same order) — 3.42x smaller, confirming the win isn't Carps-specific and ranking quality is unchanged, only the presentation format shrank.

## Context

After the 2026-07-08 tool-surface audit, mimir still transmits 15,112 wire chars (~3,778 tokens) of tool schemas on EVERY agent turn, and a real A/B test on the Carps repo showed mimir (12,210 chars) barely beating native grep+Read (11,556) on a full investigation task — `scope_task`'s single 4,793-char call being the biggest line item. This plan is a more radical redesign: fewer, more compact tools, plus a compact discovery output format and a zero-round-trip session overview.

## Architecture: 3 MCP tools (not 1, not 18)

| Tool | Signature | Purpose | Est. wire |
|---|---|---|---|
| `locate` | `(task, max_files=5, focus="", mode="files")` | ALL discovery: scope_task core; `mode="area"`→tree, `mode="symbol"`→verify; auto-falls-through to FTS/semantic when symbol index has zero hits (kills the 2-round-trip miss pattern) | ~1,300 |
| `inspect` | `(path="", symbol="", view="auto")` | ALL reading: symbol batch (`get_symbol`), file/dir structure, `view="imports"/"dependents"/"callers"` | ~1,000 |
| `mimir` | `(command, args="")` | Dispatcher for plumbing: status, arch, changed, focus, scope, alias, note, ignore, help — PLUS all 18 legacy tool names mapped for stale-snippet compat. Self-teaching errors + `help <cmd>` returns full docs at runtime (free at registration) | ~1,000 |

Total ≈ 3,300 chars (~830 tok) vs 15,112 — **~78% per-turn cut**, keeping typed params on the two hot-path tools that compete with native grep/Read (the one place rich docstrings matter).

Rejected alternative: a single dispatcher tool for everything (locate/inspect included). Saves another ~1,500-1,800 chars but risks tool-selection degradation on the two calls that most need to out-compete native grep — gate that idea on how this 3-tool version performs first.

## Compact `locate` output (replaces scope_task's format)

```
# locate: 'Keypad font-icon rendering'  (12 matched, showing 5)
1. .../IconCatalog.cs        [IconCatalog: Providers, GetGlyph, GetDefaultGlyph +4]  119
2. .../TileView.xaml.cs      [TileView: TileIconGlyph, BuildTileIconSource +3]        15
...
Next: inspect(".../IconCatalog.cs", "GetGlyph,GetDefaultGlyph,Providers")
Dependents: .../KeypadView.cs, +2
```

Target <2,000 chars (from 4,793). Kills the 20-line Matched-symbols section; symbol names inline (name-only — `inspect` recovers signatures on demand); ONE batched follow-up suggestion per top file (batching already shipped). Keeps the low-IDF suppression feeding which names show inline.

## Zero-round-trip orientation

`_write_overview()` writes `.mimir-overview.md` (architecture map + index stats + active scope/focus + timestamp) at end of `_warm_cache()` and from the file watcher, hash-guarded so it only rewrites on real change. The generated CLAUDE.md snippet `@`-imports it → session orientation with zero tool calls. `mimir("status")` stays as the staleness escape hatch; `mimir sync` CLI alias force-regenerates.

## Implementation steps

1. `_render_locate()` — new compact renderer, consumes the existing `_score_task_files()` output; reuses `_symbol_name_from_sig` + the existing low-IDF filter.
2. `locate()` — plain function wrapping `_render_locate` + FTS fallback (calls `semantic_search`'s internals) when the symbol index returns zero hits + `mode` routing to scope_area/verify_symbol_existence.
3. `inspect()` — dispatches on args to get_symbol (already supports comma-separated batching), get_file_structure (file or dir), or `view=` for imports/dependents/callers.
4. `mimir_dispatch(command, args="")` — `_COMMANDS` dict mapping short names AND all pre-v2 tool names (`scope_task` → locate, etc.) for backward compat with stale generated instructions; tolerant `key=value` arg parsing; every failure returns a self-teaching usage string, never raises.
5. Registration: `_MCP_TOOLS = [locate, inspect, mimir_dispatch]`; `main()`'s server-start branch registers only these three (dispatcher exposed under the tool name `mimir`). The existing `_TOOLS` list, all current plain functions, and the CLI stay untouched — v2 is additive at the Python level, only the MCP *registration* surface shrinks.
6. CLI: add `mimir locate "<task>"`; consider routing `mimir scope` through the new compact renderer too (update any CLI-output tests accordingly).
7. `mimir-setup` snippet v2: rewrite the generated CLAUDE.md/copilot-instructions.md sections around the 3 tools; switch the marker-detection from skip-if-present to a versioned marker that splice-replaces an old-marker section, so re-running setup actually upgrades stale instructions; add the `@.mimir-overview.md` import line to the Claude variant.
8. `_write_overview()` + a watcher hook to regenerate on real file-tree changes + `mimir sync` CLI alias to force it.
9. Tests: `test_smoke.py`'s exact-tool-set test → `{locate, inspect, mimir}`; new tests for the dispatcher's legacy-name routing, unknown-command error text, `help`/`help <cmd>`, locate's compact format and its FTS fallback, inspect's dispatch matrix; a **wire-budget regression test** asserting the total `list_tools()` JSON stays under a fixed char budget so the per-turn tax can't silently creep back up.

## Commit breakdown

1. `locate` renderer + function + unit tests (additive, no registration change — safe to land standalone)
2. `inspect` + `mimir` dispatcher + legacy-name mapping + tests
3. Flip MCP registration to the 3-tool surface; update expected-tool-set test; add the wire-budget regression test
4. `mimir-setup` snippet v2 + overview writer + `mimir sync`
5. A/B results write-up + migration notes (existing users need to re-run `mimir-setup`)

## Verification

- `pytest tests/` green throughout (331 tests at plan time; `scope_task`-format-specific assertions need updating once the renderer changes).
- Wire measurement via the existing stdio `list_tools()` harness: baseline 15,112 chars → target under 4,000.
- **Carps A/B**: re-run the exact "understand how Keypad implements font-icon rendering" task (from the 2026-07-08 session) using `locate`/`inspect` against the real `/Users/mtarros/Projects/Carps` repo. Success: `locate` call under 2,000 chars, full task total under 8,000 chars (vs the 12,210 mimir-v1 / 11,556 native baselines), and no relevant file missing compared to that session's transcript.
- **Topcat A/B**: pick one representative cross-file investigation task against the real `/Users/mtarros/Projects/Topcat` repo, same measurement methodology.
- Legacy-compat check: with an old-format CLAUDE.md snippet still in place (referencing `scope_task` etc. by name), confirm a session still works via the dispatcher's legacy-name mapping rather than failing outright.

## Risks

- **Tool-selection degradation** (agent defaults to native grep because the surface is less richly documented): mitigated by keeping `locate`/`inspect` as real typed tools with strong docstrings, and gating the whole change on the Carps A/B result before considering it done. Rollback is one line (register the old `_TOOLS` list again).
- **Dispatcher string-arg parsing errors**: tolerant `key=value` parsing; every failure path returns a usage string naming the exact expected args, never an exception (stdout-sacred rule still applies).
- **Stale snippets breaking sessions**: versioned marker + splice-replace on `mimir-setup` re-run; `locate`/`inspect`/`mimir` docstrings cross-reference the legacy names they replace so an agent used to the old names self-corrects.
- **Overview staleness**: hash-guarded regeneration, a timestamp line in the file itself, and `mimir("status")` as an always-available escape hatch if the overview looks wrong.
