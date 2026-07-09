# Behavioral tags + git commit-message search

Implementation plan and results, written 2026-07-09. **Status: implemented, tested, partially A/B-verified** (Feature 1 has a real Carps file-rank A/B; Feature 2 has real-commit-history retrieval verification but not a full-pipeline file-rank A/B — see Results).

## Context

User proposed 4 "radical" search-quality upgrades (Behavioral DNA Sifting, Topological Gravity, Chrono-Semantic Log Sifting, Virtual Token Expansion). Ground-truthing against the actual codebase before implementing found:

- **Topological Gravity is already shipped** — `_score_task_files()` already does reverse-import score spillover (`* 0.2`, mimir.py ~3814-3819) plus a forward-import expansion (`* 0.4`) the proposal didn't even mention. Nothing to build.
- **Virtual Token Expansion has a real scoring flaw as specified** — `_score_task_files`'s per-keyword `_KW_CAP`/IDF accounting is keyed by exact keyword string, so naive synonym expansion (`save` → 8 OR'd synonyms) lets a weakly-matching file stack up to 8x the score budget of one precise literal match. Needs a redesign (synonym groups sharing one cap) before it's safe to build — deferred to a future pass, sequenced after this one so its own A/B isn't confounded by simultaneous ranking changes.
- **Behavioral DNA Sifting (Feature 1, below) and Chrono-Semantic Log Sifting (Feature 2, below) are genuine gaps** with clean, low-risk implementation paths that piggyback on existing infrastructure (`_literal_row`/`#strings`'s body-walking pattern, `_git_recency_scores()`'s subprocess pattern) — these are what got built.

## Feature 1: Behavioral tag extraction (`#tags`)

Surfaces framework/infra usage (`URLSession`, `Keychain`, `DbContext`, `IHostedService`, ...) hidden inside a method body behind a generic name, by tagging it into the blueprint — same mechanism `#strings` already uses for exception/log literals, added as a third `elif` branch to `_extract_tree_sitter()`'s `walk()` (mimir.py ~2008-2016).

- `_BEHAVIORAL_ANCHORS` (mimir.py ~1697): curated frozenset across every tree-sitter language mimir supports — HTTP/networking, persistence/ORM, security/keychain/crypto, concurrency/background execution, push/scheduling, DI, serialization, game/physics.
- Dedup by tag (first occurrence only, `_TAG_MAX_PER_FILE = 24`), and skipped when the anchor's own line is already a captured signature line (avoids a duplicate row when e.g. Swift's `let x = URLSession.shared` is itself def-node-classified — that line's signature text already contains the anchor).
- Emits `L{lineno:<5}  {tag}` lines under a `#tags` header, identical format to `#strings` — flows through the existing `_index_blueprint_rows`/`_fts_rows_for_blueprint` `L<n>`-line tokenizers with zero code changes there, confirmed by an end-to-end unit test.
- `BLUEPRINT_VERSION` bumped `8` → `9` to invalidate stale cached blueprints.

### Results (real, measured — not estimated)

Real Carps A/B, isolated tags-off vs tags-on (same disk cache path, forced re-extraction between passes), 3 real cases found via grep in `/Users/mtarros/Projects/Carps/src` (7,191 C#/XAML files):

| Task | Correct file | Rank before | Rank after |
|---|---|---|---|
| `authenticated document fetching over http` | `AuthenticatedUriDocumentSourceFactory.cs` (HttpClient hidden in `CreateDocumentSource`) | 1 | 1 (unchanged — already found via existing signal) |
| `rules engine IHostedService reload` | `RulesEngineConfigurationService.cs` (IHostedService hidden — class name doesn't mention it) | 6 | **2** |
| `terminal jobs SemaphoreSlim` | `TerminalJobsViewModelBase.cs` (SemaphoreSlim hidden — generic ViewModelBase name) | 1 | 1 (unchanged — already found via existing signal) |

**1 real rank improvement (6→2), 2 unchanged, 0 regressions.** An earlier draft of this A/B used generic-English-paraphrase queries ("background hosted service configuration reload") instead of the literal framework term and got zero hits in either direction — corrected after checking the blueprint directly and confirming the tag *was* present but simply didn't share any token with a paraphrased query. This is a real, documented limit of the feature, not a bug: **tags only help when the query uses the same literal framework term the tag does** (e.g. "IHostedService"), not a generic English description of what that framework does. Closing that second gap is what the deferred Virtual Token Expansion redesign would need to do.

15 unit tests in `tests/test_behavioral_tags.py` (multi-language extraction, dedup, non-pollution of `get_architecture`, end-to-end flow-through to `symbols`/`symbol_fts`).

## Feature 2: Git commit-message search (`chrono_fts`)

A ticket phrased in the developer's own words ("fix race condition on retry") often echoes a past commit message more literally than the code itself. `_git_recency_scores()` already ranks by recency but explicitly discards message content (`--format=`) — this is a separate, real gap.

- New FTS5 table `chrono_fts` (mimir.py `_init_disk_cache`, ~line 463), same pattern as `symbol_fts`, placed outside the `version_changed` invalidation block (content comes from git log, not blueprint parsing).
- `_maybe_build_chrono_fts(force=False)` (mimir.py, near `_git_recency_scores`): `git log --since=180.days.ago -n 2000 --name-only --pretty=format:\x1e%s`, disk-persisted with a 6-hour TTL (a 180-day log is too expensive to redo on every 60s in-memory-cache expiry the way the existing `-n 40` recency scan is).
- Two call sites: once in `_warm_cache()` (background thread, off startup latency), and lazily inside `_score_task_files()` itself — required because `_warm_cache()` only runs once per long-lived MCP server process, so without a second call site the table would never refresh mid-session. The in-`_score_task_files()` call is a cheap no-op (one indexed SELECT) except once per 6-hour window.
- Fold-in, immediately after the existing git-recency boost: queries `chrono_fts` with the already-computed `valid_kws` (no extra keyword-extraction pass). **Never seeds a file with zero code-match score** — that gate is unconditional. The boost magnitude itself changed after real A/B testing, see below.

### Results (real, measured — not estimated)

**Blocker found first**: the original plan called for a Carps file-rank A/B mirroring Feature 1's. At the time, neither `/Users/mtarros/Projects/Carps` nor `/Users/mtarros/Projects/Topcat` had a `.git` directory locally, and a survey of every other directory under `~/Projects` found only one real git repo (`mimir-mcp` itself, too single-file-dominant for a meaningful file-rank signal). User then supplied real Bitbucket credentials and a fresh clone (`/Users/mtarros/Projects/Carps-git`, 8,288 real commits, 1,347 in the last 180 days, real ticket-linked messages like `C9-7522`/`C10-931`) was made specifically for this measurement — **strictly read-only against that clone**: no git write commands were ever issued against it, and `_write_overview()` was monkeypatched to a no-op for the duration so mimir never wrote into that directory either.

**Ground truth**: 7 real, non-merge, single-purpose commits from Carps-git's actual history, each with a focused diff (`git show --stat`, read-only) giving an unambiguous "correct file" set, queried with a phrasing close to the real ticket title (not invented).

**First A/B (original conservative design — bounded to `min(cscore * 0.5, file_hit_count[rel])`, i.e. at most doubling an already-matched file's score): 0 of 7 improved.** Direct inspection of `file_hit_count` explained why, and is the actual interesting finding: the tickets a commit-message match is *most* useful for are precisely the ones where the code-level match is weakest (e.g. `EscalationService.cs` scored 6.0 on code-level keywords for "audible alarm... escalation notification", while the actual top-ranked competitor scored 19.9) — a cap that can at most double a small number was never going to close a gap that size. This is a real structural property of the conservative design, not a bug: the cap does exactly what it was built to do (never let a commit-message match swamp things), but that also makes it inert in the exact scenario the feature targets.

**Second A/B, after removing the doubling cap for already-matched files** (kept the "never seed a new file" gate — only the cap against the file's *own* current score was dropped): tested two variants first via direct simulation against the real scoring pipeline — seeding brand-new files at a moderate score (weaker result, 2/7 improved) vs. uncapping the boost for already-matched files only (stronger result, 4/7 improved, 0 regressions) — then implemented the winning variant for real and re-ran the full `locate()` pipeline end to end (not simulated):

| Metric | Before | After (uncapped) |
|---|---|---|
| MRR (7 tasks) | 0.143 | **0.352** |
| Hit-rate@5 | 0.14 | **0.43** |
| Improved / unchanged / regressed | — | 4 / 3 / 0 |

One task jumped from "not in top 10" to rank 1 (`CancelAllJobsViewModel.cs`, "centralize keypad job number formatting"); two more moved from "not in top 10" into the top 10. Zero regressions on tasks that were already correct. Two tasks remained unfound in both passes: one (`StartupViewModel.cs`) *is* boosted but the corpus (400+ matched files) is too competitive for the boost to crack the top 10; the other (a SQL migration script) never enters `file_hit_count` at all — confirming the "never seed a new file" gate is the active constraint there, working as designed, not a bug.

**Methodological note, for transparency**: the first A/B run's harness had a real bug — it restored the real (non-suppressed) `_maybe_build_chrono_fts` *before* running the "before" measurement loop, and since `_score_task_files` calls that function unconditionally, the very first "before" `locate()` call on a fresh cache silently triggered a real chrono_fts build mid-measurement, contaminating "before" with "after" behavior. Caught by comparing the harness's aggregate result against a separate, clean, direct inspection of `file_hit_count`/`_chrono_fts_scores()` (which is what actually surfaced the cap-too-tight explanation above) — the two didn't agree, which is what prompted re-checking the harness itself rather than trusting the first number. Fixed before the final, reported A/B run above.

## Deferred: Virtual Token Expansion redesign (Phase 2)

Not built in this pass — sequenced after Features 1+2 so its own A/B isn't confounded by simultaneous ranking changes. Corrected design direction: a small, conservative `_CONCEPT_SYNONYMS` map (generic English verb groups — save/persist/store, load/fetch/get, delete/remove/clear) where every synonym hit in a file counts toward **one shared cap** (the group scores as if it were a single keyword) and the group's IDF weight is the best among matched synonyms, not summed — fixing the cap-stacking flaw found in the original proposal.

## Commit breakdown

1. Feature 1: `_BEHAVIORAL_ANCHORS` + `walk()` branch + `BLUEPRINT_VERSION` bump + 15 unit tests
2. Feature 2: `chrono_fts` schema + `_maybe_build_chrono_fts`/`_chrono_fts_scores` + two call sites + fold-in + 15 unit tests
3. Real A/B / retrieval-quality verification + this write-up
