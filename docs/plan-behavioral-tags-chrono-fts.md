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
- Fold-in, immediately after the existing git-recency boost: queries `chrono_fts` with the already-computed `valid_kws` (no extra keyword-extraction pass), bounded identically to the recency boost — only boosts a file *already present* in `file_hit_count`, at most doubling its score, never seeds a file with zero code-match score.

### Results (real, measured — not estimated; scope narrower than planned, see below)

**Blocker found during verification**: the plan called for a Carps file-rank A/B mirroring Feature 1's. Neither `/Users/mtarros/Projects/Carps` nor `/Users/mtarros/Projects/Topcat` has a `.git` directory locally — no commit history exists to search, so `_maybe_build_chrono_fts` correctly no-ops on both (verified this is graceful, not a crash). A survey of every directory under `~/Projects` found exactly one real git repo: `mimir-mcp` itself (87 commits) — but mimir-mcp is architecturally a single dominant source file (`mimir.py`), so file-level rank is already saturated at "mimir.py wins" for nearly any query regardless of this feature, making a file-rank A/B there structurally uninformative. User was asked how to proceed and chose to have another repo located; none was found, so verification fell back to what's actually measurable:

1. **Unit-level, real-git-fixture verification** (`tests/test_chrono_fts.py`, 15 tests) — a real git repo built via subprocess in the test fixture, not mocked: confirms `chrono_fts` populates correctly from real `git log` output, `_chrono_fts_scores()` retrieves the right file, the TTL/force-rebuild behavior is correct, and — the core invariant — `_score_task_files`'s fold-in demonstrably moves a weaker-code-match file (`handler.py`, matching commit message) above an equal-code-match file with no relevant commit message (`widget.py`), bounded to at most doubling, and is fully inert when disabled.
2. **Real-commit-history retrieval check** against mimir-mcp's own actual git log (not synthetic): 7 real past commits, each queried with a phrasing close to its actual commit subject, checked against the files that commit *actually* touched (`git show --stat`, not invented ground truth). **`_chrono_fts_scores()` correctly surfaced at least one actually-changed file for 7/7 commits.**

**What this does and doesn't prove**: (1) and (2) together confirm the mechanism is correct — it retrieves the right files from real git history and folds them into ranking with the documented, bounded, conservative shape. What's *not* yet verified is the thing the user explicitly asked for — whether this measurably improves `locate()`'s file-rank / reduces round-trips on a large, realistic, multi-file codebase — because no such repo with real git history is currently available locally. This is flagged as an open verification gap, not a claimed win: re-run the methodology in `tests/test_chrono_fts.py`'s fixture pattern against a real multi-file repo with git history as soon as one is available, using the same rank-before/rank-after methodology as Feature 1's Carps A/B.

## Deferred: Virtual Token Expansion redesign (Phase 2)

Not built in this pass — sequenced after Features 1+2 so its own A/B isn't confounded by simultaneous ranking changes. Corrected design direction: a small, conservative `_CONCEPT_SYNONYMS` map (generic English verb groups — save/persist/store, load/fetch/get, delete/remove/clear) where every synonym hit in a file counts toward **one shared cap** (the group scores as if it were a single keyword) and the group's IDF weight is the best among matched synonyms, not summed — fixing the cap-stacking flaw found in the original proposal.

## Commit breakdown

1. Feature 1: `_BEHAVIORAL_ANCHORS` + `walk()` branch + `BLUEPRINT_VERSION` bump + 15 unit tests
2. Feature 2: `chrono_fts` schema + `_maybe_build_chrono_fts`/`_chrono_fts_scores` + two call sites + fold-in + 15 unit tests
3. Real A/B / retrieval-quality verification + this write-up
