"""
Tests for IDF-weighted ranking (BM25 IDF) in scope_task.

The DF cache (_TOKEN_DF) discounts ubiquitous vocabulary and boosts rare
identifiers so that generic business words ("Types", "All", "Operative")
can no longer bury the specific file a ticket is about.
"""
import math
import re
import sqlite3

import pytest

import mimir


def _install_df(monkeypatch, df: dict[str, int], n: int, floor: int = 4):
    ref_df = max(1.0, n / 50.0)
    idf_ref = math.log(1 + (n - ref_df + 0.5) / (ref_df + 0.5))
    monkeypatch.setattr(mimir, "_TOKEN_DF", df)
    monkeypatch.setattr(mimir, "_TOKEN_DF_N", n)
    monkeypatch.setattr(mimir, "_TOKEN_DF_FLOOR", floor)
    monkeypatch.setattr(mimir, "_IDF_REF", max(idf_ref, 1e-9))


# ---------------------------------------------------------------------------
# _idf_weight
# ---------------------------------------------------------------------------

class TestIdfWeight:
    @pytest.fixture(autouse=True)
    def df_cache(self, monkeypatch):
        _install_df(monkeypatch, {
            "types": 900, "all": 1400, "operative": 800,
            "unavailable": 30, "categories": 60,
            "moderate": 40,   # exactly the reference df (N/50)
        }, n=2000)

    def test_rare_token_exceeds_common(self):
        assert mimir._idf_weight("unavailable") > 1.0
        assert mimir._idf_weight("types") < 0.5
        assert mimir._idf_weight("unavailable") > mimir._idf_weight("types")

    def test_reference_df_weighs_one(self):
        assert mimir._idf_weight("moderate") == pytest.approx(1.0, abs=0.05)

    def test_unknown_token_gets_near_max_boost(self):
        w = mimir._idf_weight("VeryRareCompoundIdentifier")
        assert w > mimir._idf_weight("unavailable")
        assert w <= 2.0

    def test_clamps_bounds(self, monkeypatch):
        _install_df(monkeypatch, {"everywhere": 1999}, n=2000)
        assert mimir._idf_weight("everywhere") == 0.05
        assert mimir._idf_weight("neverseen") <= 2.0

    def test_lookup_is_case_insensitive(self):
        assert mimir._idf_weight("Types") == mimir._idf_weight("types")

    def test_is_1_before_warmup(self, monkeypatch):
        monkeypatch.setattr(mimir, "_TOKEN_DF_N", 0)
        assert mimir._idf_weight("anything") == 1.0
        assert mimir._idf_weight("types") == 1.0


# ---------------------------------------------------------------------------
# _idf_weight_in_scope — a term globally rare (real high IDF) but ubiquitous
# WITHIN the active hard scope (e.g. a sub-project's own name) must still be
# treated as non-discriminating once scoped. Real case: "keypad" hits only
# 344/8043 files repo-wide on Carps (idf_weight ~0.8, comfortably "rare"),
# but is in nearly every filename once scoped into .../Keypad.
# ---------------------------------------------------------------------------

class TestIdfWeightInScope:
    @pytest.fixture(autouse=True)
    def df_cache(self, monkeypatch):
        # "keypad" mirrors the real Carps numbers: rare enough repo-wide to
        # look like a meaningful keyword (idf > 0.25) absent scope context.
        _install_df(monkeypatch, {"keypad": 344}, n=8043)

    def test_no_active_scope_falls_back_to_plain_idf(self, monkeypatch):
        monkeypatch.setattr(mimir, "_ACTIVE_SCOPE", None)
        assert mimir._idf_weight_in_scope("keypad") == mimir._idf_weight("keypad")
        assert mimir._idf_weight_in_scope("keypad") > 0.25   # confirms the setup is realistic

    def test_scope_matching_keyword_forced_to_floor(self, monkeypatch):
        monkeypatch.setattr(mimir, "_ACTIVE_SCOPE", "src/carps-mobile/InControl.Carps.Keypad")
        assert mimir._idf_weight_in_scope("keypad") == 0.05

    def test_non_matching_keyword_unaffected_by_scope(self, monkeypatch):
        monkeypatch.setattr(mimir, "_ACTIVE_SCOPE", "src/carps-mobile/InControl.Carps.Keypad")
        # "provider" isn't part of the scope path, so scoping shouldn't touch it.
        assert mimir._idf_weight_in_scope("provider") == mimir._idf_weight("provider")

    def test_case_insensitive_scope_match(self, monkeypatch):
        monkeypatch.setattr(mimir, "_ACTIVE_SCOPE", "src/Carps-Mobile/InControl.Carps.KEYPAD")
        assert mimir._idf_weight_in_scope("keypad") == 0.05


# ---------------------------------------------------------------------------
# _build_token_df
# ---------------------------------------------------------------------------

class TestBuildTokenDf:
    def test_floor_default_and_case_merge(self, monkeypatch):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute("CREATE TABLE symbols (token TEXT, file TEXT, lineno TEXT)")
        rows = []
        for i in range(10):
            rows.append(("common", f"f{i}.py", "1"))
        rows.append(("Rare", "f0.py", "2"))          # df=1 < floor → dropped
        for i in range(6):
            rows.append(("Refresh", f"f{i}.py", "3"))
        for i in range(4):
            rows.append(("refresh", f"g{i}.py", "4"))
        db.executemany("INSERT INTO symbols VALUES (?,?,?)", rows)
        monkeypatch.setattr(mimir, "_DISK_CACHE", db)
        monkeypatch.setattr(mimir, "_TOKEN_DF", {})
        monkeypatch.setattr(mimir, "_TOKEN_DF_N", 0)
        monkeypatch.setattr(mimir, "_DF_DIRTY", 99)

        mimir._build_token_df()

        assert mimir._TOKEN_DF_N == 14                 # distinct files
        assert mimir._TOKEN_DF["common"] == 10
        assert "rare" not in mimir._TOKEN_DF           # below floor
        assert mimir._TOKEN_DF["refresh"] == 6         # case merge takes max, not sum
        assert mimir._DF_DIRTY == 0                    # build resets churn counter
        # below-floor token defaults to near-max IDF, capped at 2.0
        assert mimir._idf_weight("rare") > mimir._idf_weight("common")

    def test_no_db_is_noop(self, monkeypatch):
        monkeypatch.setattr(mimir, "_DISK_CACHE", None)
        monkeypatch.setattr(mimir, "_TOKEN_DF_N", 0)
        mimir._build_token_df()   # must not raise
        assert mimir._TOKEN_DF_N == 0


# ---------------------------------------------------------------------------
# _doc_len_norm — BM25 document-length normalization
# ---------------------------------------------------------------------------

class TestDocLenNorm:
    def test_neutral_without_stats(self, monkeypatch):
        monkeypatch.setattr(mimir, "_AVG_NLINES", 0.0)
        assert mimir._doc_len_norm("any.cs") == 1.0

    def test_god_file_divided_small_file_boosted(self, monkeypatch):
        monkeypatch.setattr(mimir, "_AVG_NLINES", 20.0)
        monkeypatch.setattr(mimir, "_FILE_NLINES",
                            {"dbcontext.cs": 400, "model.cs": 8, "avg.cs": 20})
        assert mimir._doc_len_norm("avg.cs") == pytest.approx(1.0)
        assert mimir._doc_len_norm("dbcontext.cs") == 0.25       # clamped floor
        assert mimir._doc_len_norm("model.cs") > 1.0             # small → boost
        assert mimir._doc_len_norm("model.cs") <= 1.3            # clamped ceiling

    def test_unknown_file_neutral(self, monkeypatch):
        monkeypatch.setattr(mimir, "_AVG_NLINES", 20.0)
        monkeypatch.setattr(mimir, "_FILE_NLINES", {})
        assert mimir._doc_len_norm("unknown.cs") == 1.0


# ---------------------------------------------------------------------------
# scope_task end-to-end ranking behavior (symbol hits monkeypatched)
# ---------------------------------------------------------------------------

USAGE_SIG = "x = SomeUsage.Call()"                       # no def-pattern match
DEF_SIG = "public class {0} : BaseService"               # def-pattern match


def _cap_for(max_per_kw, kw: str, default: int = 10) -> int:
    """_symbol_hits_multi's max_per_kw may be a uniform int or a per-keyword
    dict (scope_task now sizes it by IDF) — mirror that contract in mocks."""
    return max_per_kw.get(kw, default) if isinstance(max_per_kw, dict) else max_per_kw


def _ranked(out: str) -> list[tuple[str, float]]:
    files = []
    in_section = False
    for line in out.splitlines():
        if "## Ranked files" in line:
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                break
            m = re.match(r'\s*\d+\.\s+(\S+)\s+\(score ([\d.]+)\)', line)
            if m:
                files.append((m.group(1), float(m.group(2))))
    return files


class TestScopeTaskIdfRanking:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        _install_df(monkeypatch, {
            "types": 900, "all": 1400, "operative": 800,
            "unavailable": 30, "categories": 60,
        }, n=2000)
        monkeypatch.setattr(mimir, "_SEMANTIC_READY", True)
        monkeypatch.setattr(mimir, "_PATH_STRINGS", [("x/none.cs", "none", "x")])
        monkeypatch.setattr(mimir, "_REVERSE_IMPORTS", {})
        monkeypatch.setattr(mimir, "_git_recency_scores", lambda: {})
        monkeypatch.setattr(mimir, "_FOCUS_WEIGHTS", {})
        monkeypatch.setattr(mimir, "_AVG_NLINES", 0.0)   # neutral length norm

        def fake_hits(kws, max_per_kw=10):
            hits = {}
            for kw in kws:
                k = kw.lower()
                if k in ("types", "operative"):
                    # decoy racks up many usage hits on common words
                    hits[kw] = [("decoy.cs", str(10 + i), USAGE_SIG)
                                for i in range(_cap_for(max_per_kw, kw))]
                elif k in ("unavailable", "categories"):
                    hits[kw] = [("target.cs", "5", DEF_SIG.format("UnavailableTypeService")),
                                ("target.cs", "40", USAGE_SIG)]
            return hits

        monkeypatch.setattr(mimir, "_symbol_hits_multi", fake_hits)

    def test_generic_words_dont_bury_specific_file(self):
        out = mimir.scope_task("unavailable categories operative types", max_files=5)
        ranked = _ranked(out)
        assert ranked, f"no ranked files parsed from:\n{out}"
        names = [f for f, _ in ranked]
        assert names[0] == "target.cs", f"expected target.cs first, got {names}"

    def test_diversity_ignores_low_idf_keywords(self):
        # decoy matches 2 distinct-but-ubiquitous keywords; without the IDF
        # gate it would earn a 1.5x diversity multiplier on top of raw volume.
        out = mimir.scope_task("unavailable categories operative types", max_files=5)
        scores = dict(_ranked(out))
        # target: caps 9*idf(unavail ~1.5+) + hits*idf(categories) with x1.5 diversity
        # decoy: 9*0.19 + 9*0.21 ~ 3.6 and NO diversity multiplier
        assert scores["target.cs"] > 2 * scores["decoy.cs"]

    def test_recency_boost_bounded_by_match_score(self, monkeypatch):
        out_plain = mimir.scope_task("operative types", max_files=5)
        base = dict(_ranked(out_plain)).get("decoy.cs", 0.0)
        assert base > 0
        monkeypatch.setattr(mimir, "_git_recency_scores",
                            lambda: {"decoy.cs": 1000.0})
        out_boosted = mimir.scope_task("operative types", max_files=5)
        boosted = dict(_ranked(out_boosted)).get("decoy.cs", 0.0)
        assert boosted <= base * 2 + 0.01, (
            f"recency should at most double a match score: {base} -> {boosted}"
        )

    def test_precise_identifier_query_still_ranks_first(self, monkeypatch):
        # Regression guard for the currently-working case: a rare compound
        # identifier (not in _TOKEN_DF → near-max IDF) must outrank decoys.
        def fake_hits(kws, max_per_kw=10):
            hits = {}
            for kw in kws:
                k = kw.lower()
                if k == "mycurrentviewmodel":
                    hits[kw] = [("vm.cs", "23", DEF_SIG.format("MyCurrentViewModel")),
                                ("vm.cs", "90", USAGE_SIG)]
                elif k in ("types", "operative"):
                    hits[kw] = [("decoy.cs", str(10 + i), USAGE_SIG)
                                for i in range(_cap_for(max_per_kw, kw))]
            return hits

        monkeypatch.setattr(mimir, "_symbol_hits_multi", fake_hits)
        out = mimir.scope_task("MyCurrentViewModel operative types", max_files=5)
        names = [f for f, _ in _ranked(out)]
        assert names and names[0] == "vm.cs", f"got {names}"

    def test_compound_bigram_anchors_ranking(self, monkeypatch):
        # "Unavailable Types" in prose → the code's compound identifier
        # UnavailableType(s) is searched too and anchors the right file.
        searched: list[str] = []

        def fake_hits(kws, max_per_kw=10):
            searched.extend(kws)
            hits = {}
            for kw in kws:
                if kw in ("UnavailableTypes", "UnavailableType"):
                    hits[kw] = [("compound.cs", "5",
                                 DEF_SIG.format("UnavailableTypeService"))]
                elif kw.lower() in ("types", "operative"):
                    hits[kw] = [("decoy.cs", str(10 + i), USAGE_SIG)
                                for i in range(_cap_for(max_per_kw, kw))]
            return hits

        monkeypatch.setattr(mimir, "_symbol_hits_multi", fake_hits)
        out = mimir.scope_task("Unavailable Types set against operative types",
                               max_files=5)
        assert "UnavailableTypes" in searched
        assert "UnavailableType" in searched            # singular variant too
        names = [f for f, _ in _ranked(out)]
        assert names and names[0] == "compound.cs", f"got {names}"

    def test_pre_warmup_scoring_unchanged(self, monkeypatch):
        # Before the DF cache exists every keyword weighs 1.0 — the decoy's
        # volume wins again, proving the fallback is exactly legacy behavior.
        monkeypatch.setattr(mimir, "_TOKEN_DF_N", 0)
        out = mimir.scope_task("unavailable categories operative types", max_files=5)
        names = [f for f, _ in _ranked(out)]
        assert names and names[0] == "decoy.cs", f"got {names}"

    def test_adaptive_max_per_kw_reaches_symbol_hits(self, monkeypatch):
        # Common (low-IDF) keywords should request a shallow sample; rare
        # (high-IDF) keywords a deep one — this is the whole point of sizing
        # max_per_kw by IDF instead of a uniform 40.
        seen_caps: dict[str, int] = {}

        def fake_hits(kws, max_per_kw=10):
            for kw in kws:
                seen_caps[kw] = _cap_for(max_per_kw, kw)
            return {}

        monkeypatch.setattr(mimir, "_symbol_hits_multi", fake_hits)
        mimir.scope_task("unavailable categories operative types", max_files=5)
        assert seen_caps["types"] < seen_caps["unavailable"], seen_caps
        assert seen_caps["types"] == 10       # idf < 0.25 -> shallow sample
        assert seen_caps["unavailable"] == 40  # everything else -> deep sample


# ---------------------------------------------------------------------------
# scope_task Matched-symbols/Ranked-files display filter — hides matches
# whose ONLY signal is a low-IDF (ubiquitous) keyword. Display-only: must
# never change which files rank, or their scores.
# ---------------------------------------------------------------------------

class TestScopeTaskDisplayFilter:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        _install_df(monkeypatch, {
            "worker": 1998,     # ubiquitous -> idf clamps to the 0.05 floor
            "service": 1997,    # ubiquitous
            "rarewidget": 3,    # rare -> idf near the 2.0 ceiling
        }, n=2000)
        monkeypatch.setattr(mimir, "_SEMANTIC_READY", True)
        monkeypatch.setattr(mimir, "_PATH_STRINGS", [("x/none.cs", "none", "x")])
        monkeypatch.setattr(mimir, "_REVERSE_IMPORTS", {})
        monkeypatch.setattr(mimir, "_git_recency_scores", lambda: {})
        monkeypatch.setattr(mimir, "_FOCUS_WEIGHTS", {})
        monkeypatch.setattr(mimir, "_AVG_NLINES", 0.0)

    @staticmethod
    def _mixed_hits(kws, max_per_kw=10):
        hits = {}
        for kw in kws:
            k = kw.lower()
            if k in ("worker", "service"):
                hits[kw] = [("decoy.cs", "10", DEF_SIG.format("SomeWorkerService"))]
            elif k == "rarewidget":
                hits[kw] = [("target.cs", "5", DEF_SIG.format("RareWidget"))]
        return hits

    def test_low_idf_only_match_suppressed_with_summary(self, monkeypatch):
        monkeypatch.setattr(mimir, "_symbol_hits_multi", self._mixed_hits)
        out = mimir.scope_task("worker service rarewidget", max_files=5)
        assert "decoy.cs:10" not in out
        assert "ubiquitous terms hidden" in out
        assert "target.cs:5" in out   # the rare-keyword hit still shown

    def test_ranking_order_unaffected_by_display_filter(self, monkeypatch):
        # decoy.cs's matched-symbol line is suppressed, but it must still
        # appear (with its real score) in Ranked files — filtering is
        # display-only and never touches file_hit_count/sorting.
        monkeypatch.setattr(mimir, "_symbol_hits_multi", self._mixed_hits)
        out = mimir.scope_task("worker service rarewidget", max_files=5)
        names = [f for f, _ in _ranked(out)]
        assert "decoy.cs" in names
        assert "target.cs" in names
        assert "(matched only common terms)" in out

    def test_all_low_idf_query_falls_back_to_unfiltered(self, monkeypatch):
        def fake_hits(kws, max_per_kw=10):
            hits = {}
            for kw in kws:
                if kw.lower() in ("worker", "service"):
                    hits[kw] = [("decoy.cs", "10", DEF_SIG.format("SomeWorkerService"))]
            return hits

        monkeypatch.setattr(mimir, "_symbol_hits_multi", fake_hits)
        out = mimir.scope_task("worker service", max_files=5)
        assert "## Matched symbols" in out
        assert "decoy.cs:10" in out
        assert "[common-term match]" in out

    def test_pre_warmup_filter_is_inert(self, monkeypatch):
        # Before the DF cache builds, _idf_weight returns 1.0 for everything
        # — nothing should be suppressed regardless of vocabulary, matching
        # legacy (pre-filter) behavior exactly.
        monkeypatch.setattr(mimir, "_TOKEN_DF_N", 0)
        monkeypatch.setattr(mimir, "_symbol_hits_multi", self._mixed_hits)
        out = mimir.scope_task("worker service rarewidget", max_files=5)
        assert "decoy.cs:10" in out
        assert "ubiquitous terms hidden" not in out


# ---------------------------------------------------------------------------
# Path-token index — inverted index for scope_task's path-matching loop
# ---------------------------------------------------------------------------

class TestPathTokenIndex:
    def test_build_indexes_camelcase_and_whole_stem(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        (tmp_path / "EmailNotificationService.cs").write_text("class A {}\n")
        (tmp_path / "Unrelated.cs").write_text("class B {}\n")
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)

        mimir._build_path_strings()
        try:
            idx = mimir._PATH_TOKEN_INDEX
            assert "email" in idx
            assert "notification" in idx
            assert "emailnotificationservice" in idx
            rel = mimir._PATH_STRINGS[idx["email"][0]][0]
            assert rel == "EmailNotificationService.cs"
            assert "unrelated" in idx
            assert idx["unrelated"] != idx.get("email")
        finally:
            mimir._PATH_STRINGS = []
            mimir._PATH_TOKEN_INDEX = {}

    def test_token_index_candidates_subset_of_linear_scan(self, monkeypatch):
        # The index narrows candidates conservatively — it must never surface
        # a file the full substring scan wouldn't also find. It CAN miss
        # matches the full scan finds via cross-boundary/non-token substrings
        # (e.g. "service" inside directory "services") — that's the accepted
        # precision/speed trade documented at the call site.
        _install_df(monkeypatch, {}, n=0)  # neutral IDF (pre-warmup)
        monkeypatch.setattr(mimir, "_SEMANTIC_READY", True)
        monkeypatch.setattr(mimir, "_REVERSE_IMPORTS", {})
        monkeypatch.setattr(mimir, "_git_recency_scores", lambda: {})
        monkeypatch.setattr(mimir, "_FOCUS_WEIGHTS", {})
        monkeypatch.setattr(mimir, "_AVG_NLINES", 0.0)
        monkeypatch.setattr(mimir, "_symbol_hits_multi", lambda kws, max_per_kw=10: {})

        # (rel, ORIGINAL-case stem, ORIGINAL-case dir) — tokenized like real
        # _build_path_strings does (before lowering); path_strings mirrors
        # the lowered tuple shape scope_task actually consumes.
        originals = [
            ("Services/EmailNotificationService.cs", "EmailNotificationService", "Services"),
            ("Services/Unrelated.cs", "Unrelated", "Services"),
            ("Models/Email.cs", "Email", "Models"),
        ]
        path_strings = [(rel, stem.lower(), dir_.lower()) for rel, stem, dir_ in originals]
        token_index: dict[str, list[int]] = {}
        for i, (_, stem, dir_) in enumerate(originals):
            for tok in mimir._path_component_tokens(stem) | mimir._path_component_tokens(dir_):
                token_index.setdefault(tok, []).append(i)

        def run(with_index: bool) -> list[str]:
            monkeypatch.setattr(mimir, "_PATH_STRINGS", path_strings)
            monkeypatch.setattr(mimir, "_PATH_TOKEN_INDEX", token_index if with_index else {})
            out = mimir.scope_task("email notification service", max_files=10)
            return set(f for f, _ in _ranked(out))

        indexed, full_scan = run(with_index=True), run(with_index=False)
        assert indexed <= full_scan, f"indexed found files the full scan didn't: {indexed - full_scan}"
        # Both must still find the token-aligned, unambiguous match.
        assert "Services/EmailNotificationService.cs" in indexed
        assert "Models/Email.cs" in indexed

    def test_token_aligned_query_matches_identically(self, monkeypatch):
        # For keywords that align to real path tokens (the common case —
        # queries drawn from ticket text rarely need cross-boundary
        # substrings), indexed and full-scan results must be identical.
        _install_df(monkeypatch, {}, n=0)
        monkeypatch.setattr(mimir, "_SEMANTIC_READY", True)
        monkeypatch.setattr(mimir, "_REVERSE_IMPORTS", {})
        monkeypatch.setattr(mimir, "_git_recency_scores", lambda: {})
        monkeypatch.setattr(mimir, "_FOCUS_WEIGHTS", {})
        monkeypatch.setattr(mimir, "_AVG_NLINES", 0.0)
        monkeypatch.setattr(mimir, "_symbol_hits_multi", lambda kws, max_per_kw=10: {})

        originals = [
            ("Services/EmailNotificationService.cs", "EmailNotificationService", "Services"),
            ("Models/JobModel.cs", "JobModel", "Models"),
        ]
        path_strings = [(rel, stem.lower(), dir_.lower()) for rel, stem, dir_ in originals]
        token_index: dict[str, list[int]] = {}
        for i, (_, stem, dir_) in enumerate(originals):
            for tok in mimir._path_component_tokens(stem) | mimir._path_component_tokens(dir_):
                token_index.setdefault(tok, []).append(i)

        def run(with_index: bool) -> list[str]:
            monkeypatch.setattr(mimir, "_PATH_STRINGS", path_strings)
            monkeypatch.setattr(mimir, "_PATH_TOKEN_INDEX", token_index if with_index else {})
            out = mimir.scope_task("email notification", max_files=10)
            return sorted(f for f, _ in _ranked(out))

        assert run(with_index=True) == run(with_index=False) == ["Services/EmailNotificationService.cs"]
