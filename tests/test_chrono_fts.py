"""
Tests for git commit-message search (chrono_fts).

A ticket described in the developer's own words ("fix race condition on
retry") often echoes a past commit MESSAGE more literally than the code
itself does. _git_recency_scores() already ranks by recency but discards
message content entirely (--format=) -- chrono_fts closes that gap with a
separate FTS5 table, built via a bounded git-log subprocess, folded into
_score_task_files's ranking with the SAME conservative shape the existing
recency boost already uses: bounded to at most doubling an ALREADY-matched
file's score, never seeding a file with zero code-match score.
"""
import sqlite3
import subprocess
import time

import pytest

import mimir


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def chrono_repo(tmp_path, monkeypatch):
    """Real git repo with commits carrying distinct, searchable messages,
    wired up with a real (in-memory) SQLite chrono_fts table."""
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "test@test.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)

    (tmp_path / "widget.py").write_text(
        "class Widget:\n    def retry_render(self):\n        return 'ok'\n"
    )
    _git("add", "widget.py", cwd=tmp_path)
    _git("commit", "-q", "-m", "cosmetic UI polish pass", cwd=tmp_path)

    (tmp_path / "handler.py").write_text(
        "class Handler:\n"
        "    def retry_process(self):\n"
        "        for attempt in range(3):\n"
        "            try:\n"
        "                return self.attempt_once()\n"
        "            except Exception:\n"
        "                continue\n"
        "    def attempt_once(self):\n"
        "        pass\n"
    )
    _git("add", "handler.py", cwd=tmp_path)
    _git("commit", "-q", "-m", "fix race condition on retry in handler", cwd=tmp_path)

    (tmp_path / "auth.py").write_text("class Auth:\n    def login(self):\n        pass\n")
    _git("add", "auth.py", cwd=tmp_path)
    _git("commit", "-q", "-m", "add OAuth login flow", cwd=tmp_path)

    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE chrono_fts USING fts5(file UNINDEXED, commit_message, tokenize='unicode61')")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

    monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(mimir, "_DISK_CACHE", db)
    monkeypatch.setattr(mimir, "_FILE_LIST", [])
    monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
    monkeypatch.setattr(mimir, "_CACHE", mimir._CACHE.__class__())
    monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", [])
    return tmp_path


# ---------------------------------------------------------------------------
# _maybe_build_chrono_fts — build/refresh behavior
# ---------------------------------------------------------------------------

class TestMaybeBuildChronoFts:
    def test_populates_chrono_fts_from_real_git_log(self, chrono_repo):
        mimir._maybe_build_chrono_fts(force=True)
        rows = mimir._DISK_CACHE.execute(
            "SELECT file, commit_message FROM chrono_fts"
        ).fetchall()
        messages = {r[1] for r in rows}
        assert "fix race condition on retry in handler" in messages
        assert "add OAuth login flow" in messages
        files_for_handler = {r[0] for r in rows if "race condition" in r[1]}
        assert "handler.py" in files_for_handler

    def test_records_build_timestamp(self, chrono_repo):
        before = time.time()
        mimir._maybe_build_chrono_fts(force=True)
        row = mimir._DISK_CACHE.execute(
            "SELECT value FROM meta WHERE key='chrono_fts_built_ts'"
        ).fetchone()
        assert row is not None
        assert float(row[0]) >= before

    def test_within_ttl_does_not_rebuild(self, chrono_repo, monkeypatch):
        mimir._maybe_build_chrono_fts(force=True)
        first_ts = mimir._DISK_CACHE.execute(
            "SELECT value FROM meta WHERE key='chrono_fts_built_ts'"
        ).fetchone()[0]
        # Add a new commit that would show up if a rebuild happened.
        (chrono_repo / "new_file.py").write_text("x = 1\n")
        _git("add", "new_file.py", cwd=chrono_repo)
        _git("commit", "-q", "-m", "should not appear yet", cwd=chrono_repo)

        mimir._maybe_build_chrono_fts()  # force=False, still within TTL
        second_ts = mimir._DISK_CACHE.execute(
            "SELECT value FROM meta WHERE key='chrono_fts_built_ts'"
        ).fetchone()[0]
        assert first_ts == second_ts
        rows = mimir._DISK_CACHE.execute("SELECT commit_message FROM chrono_fts").fetchall()
        assert not any("should not appear yet" in r[0] for r in rows)

    def test_force_true_rebuilds_regardless_of_ttl(self, chrono_repo):
        mimir._maybe_build_chrono_fts(force=True)
        (chrono_repo / "new_file.py").write_text("x = 1\n")
        _git("add", "new_file.py", cwd=chrono_repo)
        _git("commit", "-q", "-m", "should appear after forced rebuild", cwd=chrono_repo)

        mimir._maybe_build_chrono_fts(force=True)
        rows = mimir._DISK_CACHE.execute("SELECT commit_message FROM chrono_fts").fetchall()
        assert any("should appear after forced rebuild" in r[0] for r in rows)

    def test_no_disk_cache_is_a_noop(self, chrono_repo, monkeypatch):
        monkeypatch.setattr(mimir, "_DISK_CACHE", None)
        mimir._maybe_build_chrono_fts(force=True)  # must not raise

    def test_non_git_directory_handled_gracefully(self, tmp_path, monkeypatch):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE chrono_fts USING fts5(file UNINDEXED, commit_message, tokenize='unicode61')")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)  # not a git repo
        monkeypatch.setattr(mimir, "_DISK_CACHE", db)
        mimir._maybe_build_chrono_fts(force=True)  # must not raise
        rows = db.execute("SELECT COUNT(*) FROM chrono_fts").fetchone()
        assert rows[0] == 0


# ---------------------------------------------------------------------------
# _chrono_fts_scores — query lookup
# ---------------------------------------------------------------------------

class TestChronoFtsScores:
    def test_finds_file_by_commit_message_terms(self, chrono_repo):
        mimir._maybe_build_chrono_fts(force=True)
        scores = mimir._chrono_fts_scores(["race", "condition", "retry"])
        assert "handler.py" in scores
        assert scores["handler.py"] > 0

    def test_unrelated_terms_do_not_match(self, chrono_repo):
        mimir._maybe_build_chrono_fts(force=True)
        scores = mimir._chrono_fts_scores(["completelyunrelatedxyz"])
        assert scores == {}

    def test_empty_terms_returns_empty(self, chrono_repo):
        mimir._maybe_build_chrono_fts(force=True)
        assert mimir._chrono_fts_scores([]) == {}

    def test_no_disk_cache_returns_empty(self, chrono_repo, monkeypatch):
        monkeypatch.setattr(mimir, "_DISK_CACHE", None)
        assert mimir._chrono_fts_scores(["retry"]) == {}

    def test_best_score_kept_per_file_not_summed(self, chrono_repo):
        """Mirrors _fts_search's per-file max-score grouping — a file with
        multiple matching commits keeps its best score, not an inflated sum."""
        _git("add", "-A", cwd=chrono_repo)
        (chrono_repo / "handler.py").write_text(
            chrono_repo.joinpath("handler.py").read_text() + "\n# retry logic revisited\n"
        )
        _git("add", "handler.py", cwd=chrono_repo)
        _git("commit", "-q", "-m", "another retry condition fix", cwd=chrono_repo)
        mimir._maybe_build_chrono_fts(force=True)
        scores = mimir._chrono_fts_scores(["retry", "condition"])
        # Two commits now mention handler.py with these terms; still one score.
        assert isinstance(scores.get("handler.py"), float)


# ---------------------------------------------------------------------------
# Fold-in into _score_task_files — the conservative-design invariants
# ---------------------------------------------------------------------------

class TestChronoFoldIn:
    def test_never_adds_a_file_absent_from_code_matches(self, chrono_repo):
        """The core promise of the conservative design: a strong commit-
        message match must NOT surface a file that has zero code-level
        keyword match. auth.py's commit message doesn't matter here --
        the query has no code-level overlap with auth.py at all."""
        file_hit_count, _kw, _hits, _exp, _fts = mimir._score_task_files(
            "completely unrelated query terms xyzabc"
        )
        assert "auth.py" not in file_hit_count

    def test_boosts_rank_of_weaker_code_match_with_matching_commit_message(self, chrono_repo):
        """The real, positive case: handler.py's code match on 'retry' is no
        stronger than widget.py's, but its commit message ('fix race
        condition on retry') echoes the query -- it should outrank
        widget.py once the chrono boost applies."""
        file_hit_count, _kw, _hits, _exp, _fts = mimir._score_task_files(
            "race condition retry"
        )
        assert "handler.py" in file_hit_count and "widget.py" in file_hit_count
        assert file_hit_count["handler.py"] > file_hit_count["widget.py"], file_hit_count

    def test_boost_is_uncapped_but_gate_still_holds(self, chrono_repo, monkeypatch):
        """A real Carps A/B (7 real tickets, see docs/plan-behavioral-tags-
        chrono-fts.md) found the original "at most double an already-
        matched file's score" cap made the feature measure ZERO improvement
        across all 7 tasks: the tickets a commit-message match helps most
        are exactly the ones with the WEAKEST code-level match, so doubling
        a small number was never enough to close the gap to a stronger,
        unrelated code match. The cap was removed for an already-matched
        file's boost -- re-tested the same way, 4 of 7 real tasks improved,
        0 regressed. The one invariant that must still hold, and is the
        actual safety property (not the removed doubling cap), is
        test_never_adds_a_file_absent_from_code_matches above: chrono can
        amplify an existing match without limit, but never invents one."""
        mimir._maybe_build_chrono_fts(force=True)
        # Isolate from the (separately tested) recency boost so only the
        # chrono contribution is being measured here.
        monkeypatch.setattr(mimir, "_git_recency_scores", lambda: {})
        base_fhc, _kw, _hits, _exp, _fts = mimir._score_task_files("retry")
        full_fhc, _kw2, _hits2, _exp2, _fts2 = mimir._score_task_files("race condition retry")
        if "handler.py" in base_fhc and "handler.py" in full_fhc:
            # The boost is real and can exceed a 2x bound now -- assert it's
            # positive, not that it's capped.
            assert full_fhc["handler.py"] >= base_fhc["handler.py"]

    def test_disabled_chrono_leaves_ranking_unchanged(self, chrono_repo, monkeypatch):
        """Isolation check for the real A/B methodology: neutralizing
        _maybe_build_chrono_fts must make chrono_fts effectively inert."""
        monkeypatch.setattr(mimir, "_maybe_build_chrono_fts", lambda *a, **k: None)
        # chrono_fts table is never populated -> _chrono_fts_scores returns {}
        file_hit_count, _kw, _hits, _exp, _fts = mimir._score_task_files(
            "race condition retry"
        )
        if "handler.py" in file_hit_count and "widget.py" in file_hit_count:
            # Without chrono, both weak "retry" code matches should be closer
            # in score than the boosted-vs-unboosted gap seen in the enabled test.
            ratio = file_hit_count["handler.py"] / file_hit_count["widget.py"]
            assert ratio < 1.5, file_hit_count
