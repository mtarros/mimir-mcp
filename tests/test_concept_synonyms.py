"""
Tests for concept synonym expansion (Phase 2 of the 2026-07-09 search-
quality work -- see docs/plan-behavioral-tags-chrono-fts.md).

A ticket saying "save the record" should also search "persist"/"store"/
"write" -- the code may spell the concept differently than the ticket does.
A naive first version of this idea (each synonym added as an independent
keyword) has a real scoring flaw: _KW_CAP/IDF in _score_task_files are
applied per-keyword-string, so a file weakly matching several synonyms
could out-score a file precisely matching the literal term once, by
stacking N independent caps instead of sharing one. These tests confirm
the fix: every synonym in a matched group routes to one shared scoring_key,
so the group shares one _KW_CAP budget and one (best-synonym) IDF weight,
never more than a single keyword would get.
"""
import tempfile
from pathlib import Path

import pytest

import mimir


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(mimir, "_FILE_LIST", [])
    monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
    # Must reset _DISK_CACHE too -- otherwise this test inherits whatever
    # SQLite connection a PRECEDING test file left it pointing at, and
    # _warm_cache()'s symbol-index build can leak that other workspace's
    # rows into this test's file_hit_count (caught via a real full-suite
    # run: a stray "tmp0z0v4eiu.cs" from an unrelated test file showed up
    # in this test's results when run after test_behavioral_tags.py).
    monkeypatch.setattr(mimir, "_DISK_CACHE", None)
    monkeypatch.setattr(mimir, "_CACHE", mimir._CACHE.__class__())
    return tmp_path


class TestConceptLookupAndKeywordExtraction:
    def test_all_synonyms_map_to_a_concept(self):
        for concept_id, syns in mimir._CONCEPT_SYNONYMS.items():
            for syn in syns:
                assert mimir._CONCEPT_LOOKUP[syn.lower()] == concept_id

    def test_short_synonym_word_is_extracted_as_a_keyword(self):
        # "save"/"load"/"add"/"edit" are all <5 chars and would otherwise be
        # filtered by the plain-word length rule before synonym expansion
        # ever sees them.
        kws = mimir._extract_scope_keywords("save the record to disk")
        assert "save" in [k.lower() for k in kws]

    def test_unrelated_short_word_is_not_extracted(self):
        # The short-word allowlist is narrow (only _CONCEPT_LOOKUP members)
        # -- must not become a general relaxation of the length rule.
        kws = mimir._extract_scope_keywords("the fox ran and hid")
        assert "fox" not in [k.lower() for k in kws]
        assert "ran" not in [k.lower() for k in kws]


class TestSynonymExpansionFindsHiddenMatches:
    def test_literal_query_term_finds_file_using_only_a_synonym(self, workspace):
        (workspace / "persistence.py").write_text(
            "class RecordPersister:\n    def persist(self, record):\n        return record\n"
        )
        mimir._warm_cache()
        fhc, kw, hits, exp, fts = mimir._score_task_files("save the record")
        assert "persistence.py" in fhc
        assert fhc["persistence.py"] > 0

    def test_no_match_when_neither_literal_term_nor_any_synonym_present(self, workspace):
        (workspace / "unrelated.py").write_text(
            "class Widget:\n    def render(self):\n        return 'ok'\n"
        )
        mimir._warm_cache()
        fhc, kw, hits, exp, fts = mimir._score_task_files("save the record")
        assert "unrelated.py" not in fhc


class TestAntiStackingInvariant:
    """The core fix: multiple weak synonym hits in one file must not
    out-score a single precise literal match -- confirms the shared
    scoring_key/cap actually holds, not just that synonyms are found."""

    def test_precise_literal_match_beats_weak_multi_synonym_match(self, workspace):
        (workspace / "precise_match.py").write_text(
            "class RecordSaver:\n    def save(self, record):\n        return record\n"
        )
        (workspace / "weak_multi_match.py").write_text(
            "class Unrelated:\n"
            "    def persist(self): pass\n"
            "    def store(self): pass\n"
            "    def write(self): pass\n"
        )
        mimir._warm_cache()
        fhc, kw, hits, exp, fts = mimir._score_task_files("save the record")
        assert fhc["precise_match.py"] >= fhc["weak_multi_match.py"], fhc

    def test_diversity_multiplier_does_not_double_count_synonyms_of_one_concept(self, workspace):
        """A file matching "save" and its synonym "persist" (one concept,
        two spellings) must count as ONE distinct primary keyword for the
        1.5x-per-extra-keyword diversity multiplier, not two -- otherwise a
        file gets rewarded for matching a single concept twice."""
        (workspace / "both_synonyms.py").write_text(
            "class Combo:\n"
            "    def save(self, x): pass\n"
            "    def persist(self, x): pass\n"
        )
        mimir._warm_cache()
        fhc, kw, hits, exp, fts = mimir._score_task_files("save the widget")
        # Compare against a file matching "save" the same number of times
        # under one literal spelling only -- diversity multiplier should
        # treat both cases identically since it's still one concept.
        (workspace / "one_synonym_twice.py").write_text(
            "class Combo2:\n"
            "    def save(self, x): pass\n"
            "    def saveAgain(self, x): pass\n"
        )
        mimir._warm_cache()
        fhc2, _kw2, _hits2, _exp2, _fts2 = mimir._score_task_files("save the widget")
        # Neither file should get a 1.5x diversity boost from the single
        # "save" concept alone (diversity requires >1 DISTINCT concept).
        # This is a soft check: scores should be in the same rough order of
        # magnitude, not one 1.5x+ inflated purely from synonym duplication.
        if "both_synonyms.py" in fhc and "one_synonym_twice.py" in fhc2:
            ratio = fhc["both_synonyms.py"] / fhc2["one_synonym_twice.py"]
            assert ratio < 1.4, (fhc["both_synonyms.py"], fhc2["one_synonym_twice.py"])


class TestConceptGroupSizeBounded:
    def test_max_added_synonyms_is_bounded(self, workspace):
        # A task naming several concept trigger words at once must not
        # explode the keyword list unboundedly.
        task = "save load delete create cancel notify"
        fhc, kw, hits, exp, fts = mimir._score_task_files(task)
        # All 6 concepts triggered; each adds at most len(group)-1 new terms,
        # bounded overall by _CONCEPT_MAX_ADDED.
        assert len(kw) <= 12  # _extract_scope_keywords' own hard cap
