"""
Unit tests for individual mimir helper functions: blacklist/mimirignore path
filtering, scope keyword extraction, and symbol name validation.

These test the building blocks that the integration tests exercise end-to-end.

Run with: pytest tests/ -v
"""
import os
import tempfile
from pathlib import Path

import pytest

import mimir


# ---------------------------------------------------------------------------
# _is_blacklisted — BLACKLIST_DIRS and .mimirignore pattern matching
# ---------------------------------------------------------------------------

class TestIsBlacklisted:
    @pytest.fixture(autouse=True)
    def patch_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", [])
        self.root = tmp_path

    def _path(self, rel):
        return self.root / rel

    # ── BLACKLIST_DIRS ────────────────────────────────────────────────────────

    def test_node_modules_blacklisted(self):
        assert mimir._is_blacklisted(self._path("src/node_modules/lodash/index.js"))

    def test_dotgit_blacklisted(self):
        assert mimir._is_blacklisted(self._path(".git/objects/abc"))

    def test_venv_blacklisted(self):
        assert mimir._is_blacklisted(self._path("backend/.venv/lib/python3.11/site.py"))

    def test_build_blacklisted(self):
        assert mimir._is_blacklisted(self._path("project/build/output.js"))

    def test_normal_src_not_blacklisted(self):
        assert not mimir._is_blacklisted(self._path("src/app/main.py"))

    def test_blacklist_anywhere_in_path(self):
        # node_modules nested deep — still blocked
        assert mimir._is_blacklisted(self._path("a/b/c/node_modules/d/e.js"))

    # ── .mimirignore pattern matching ─────────────────────────────────────────

    def test_simple_glob_match(self, monkeypatch):
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", ["*.min.js"])
        assert mimir._is_blacklisted(self._path("src/vendor/jquery.min.js"))

    def test_simple_glob_no_match(self, monkeypatch):
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", ["*.min.js"])
        assert not mimir._is_blacklisted(self._path("src/app/main.js"))

    def test_double_star_glob_matches_nested(self, monkeypatch):
        # The Topcat pattern: **/js/*.js excludes vendor JS in any js/ directory
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", ["**/js/*.js"])
        assert mimir._is_blacklisted(self._path("src/rectifications/js/moment.js"))
        assert mimir._is_blacklisted(self._path("deeply/nested/path/js/vendor.js"))

    def test_double_star_does_not_block_non_matching(self, monkeypatch):
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", ["**/js/*.js"])
        # File is not in a js/ directory — should not be blocked
        assert not mimir._is_blacklisted(self._path("src/app/utils.js"))

    def test_map_files_excluded(self, monkeypatch):
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", ["**/js/*.map"])
        assert mimir._is_blacklisted(self._path("static/js/bundle.map"))

    def test_multiple_patterns(self, monkeypatch):
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", ["**/js/*.js", "**/js/*.map"])
        assert mimir._is_blacklisted(self._path("src/js/vendor.js"))
        assert mimir._is_blacklisted(self._path("src/js/vendor.map"))
        assert not mimir._is_blacklisted(self._path("src/main.js"))

    def test_blacklist_and_mimirignore_both_apply(self, monkeypatch):
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", ["**/gen/*.py"])
        # Either rule can block
        assert mimir._is_blacklisted(self._path("src/node_modules/foo.py"))  # BLACKLIST_DIRS
        assert mimir._is_blacklisted(self._path("src/gen/schema.py"))        # mimirignore


# ---------------------------------------------------------------------------
# _extract_scope_keywords — keyword extraction from task descriptions
# ---------------------------------------------------------------------------

class TestExtractScopeKeywords:
    def test_camel_case_symbols_extracted(self):
        kws = mimir._extract_scope_keywords("Fix how JobScheduler handles retries")
        assert "JobScheduler" in kws

    def test_multiple_camel_case(self):
        kws = mimir._extract_scope_keywords("AuthService calls UserRepository on login")
        assert "AuthService" in kws
        assert "UserRepository" in kws

    def test_snake_case_extracted(self):
        kws = mimir._extract_scope_keywords("update the job_queue processing logic")
        assert "job_queue" in kws

    def test_long_plain_words_extracted(self):
        kws = mimir._extract_scope_keywords("Find where authentication middleware is registered")
        assert "authentication" in kws
        assert "middleware" in kws

    def test_stopwords_not_extracted(self):
        kws = mimir._extract_scope_keywords("Find where the code does this thing")
        # Short common words should be filtered
        assert "the" not in kws
        assert "does" not in kws
        assert "this" not in kws

    def test_short_words_filtered(self):
        kws = mimir._extract_scope_keywords("add it to get the job done")
        for kw in kws:
            assert len(kw) >= 4, f"Short word '{kw}' should be filtered"

    def test_capped_at_twelve(self):
        # Even a verbose description shouldn't produce more than 12 keywords
        task = " ".join(f"SomeClass{i}" for i in range(20))
        kws = mimir._extract_scope_keywords(task)
        assert len(kws) <= 12

    def test_empty_task_returns_empty(self):
        kws = mimir._extract_scope_keywords("")
        assert kws == []

    def test_deduplication(self):
        kws = mimir._extract_scope_keywords("UserService and UserService create a UserService")
        count = sum(1 for k in kws if k == "UserService")
        assert count == 1


# ---------------------------------------------------------------------------
# find_callers — symbol name validation
# ---------------------------------------------------------------------------

class TestFindCallersValidation:
    """find_callers must reject invalid identifiers without crashing."""

    def _call(self, name):
        return mimir.find_callers(name, max_results=5)

    def test_rejects_dotted_name(self):
        result = self._call("some.method")
        assert result.startswith("Error")

    def test_rejects_name_with_parens(self):
        result = self._call("foo()")
        assert result.startswith("Error")

    def test_rejects_empty_string(self):
        result = self._call("")
        assert result.startswith("Error")

    def test_accepts_camel_case(self):
        # Valid bare identifier — should not raise; may return "No usages" in test workspace
        result = self._call("UserService")
        assert not result.startswith("EXCEPTION")

    def test_accepts_snake_case(self):
        result = self._call("get_by_id")
        assert not result.startswith("EXCEPTION")

    def test_accepts_leading_underscore(self):
        result = self._call("_private_method")
        assert not result.startswith("EXCEPTION")


# ---------------------------------------------------------------------------
# Generated file exclusion
# ---------------------------------------------------------------------------

class TestGeneratedFileExclusion:
    def test_generated_cs_suffix_excluded(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)

        (tmp_path / "Foo.g.cs").write_text("public class Foo {}")
        (tmp_path / "Bar.generated.cs").write_text("public class Bar {}")
        (tmp_path / "Normal.cs").write_text("public class Normal {}")

        files = mimir._iter_source_files()
        names = [f.name for f in files]

        assert "Normal.cs" in names
        assert "Foo.g.cs" not in names
        assert "Bar.generated.cs" not in names

    def test_assemblyinfo_excluded(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)

        (tmp_path / "AssemblyInfo.cs").write_text("// assembly info")
        (tmp_path / "MyClass.cs").write_text("public class MyClass {}")

        files = mimir._iter_source_files()
        names = [f.name for f in files]

        assert "MyClass.cs" in names
        assert "AssemblyInfo.cs" not in names
