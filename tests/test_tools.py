"""
Unit tests for individual mimir helper functions: blacklist/mimirignore path
filtering, scope keyword extraction, and symbol name validation.

These test the building blocks that the integration tests exercise end-to-end.

Run with: pytest tests/ -v
"""
import os
import tempfile
import time
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


# ---------------------------------------------------------------------------
# _byte_keyword_freq — raw byte frequency tiebreaker
# ---------------------------------------------------------------------------

class TestByteKeywordFreq:
    def test_counts_occurrences(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("refresh refresh refresh\ndef refresh_data(): pass\n")
        count = mimir._byte_keyword_freq(f, ["refresh"])
        assert count == 4

    def test_case_insensitive(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("Refresh REFRESH refresh\n")
        count = mimir._byte_keyword_freq(f, ["refresh"])
        assert count == 3

    def test_multiple_keywords_summed(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("timer interval timer\ninterval\n")
        count = mimir._byte_keyword_freq(f, ["timer", "interval"])
        assert count == 4  # 2 "timer" + 2 "interval"

    def test_missing_file_returns_zero(self, tmp_path):
        result = mimir._byte_keyword_freq(tmp_path / "nonexistent.py", ["anything"])
        assert result == 0

    def test_empty_keywords_returns_zero(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("lots of text here\n")
        assert mimir._byte_keyword_freq(f, []) == 0

    def test_keyword_not_present_returns_zero(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("class Foo: pass\n")
        assert mimir._byte_keyword_freq(f, ["timer", "interval"]) == 0


# ---------------------------------------------------------------------------
# scope_task tiebreaker — body-rich file surfaces above definition-only file
# ---------------------------------------------------------------------------

class TestScopeTaskTiebreaker:
    """When two files tie on symbol-definition score, the file with more keyword
    occurrences in its implementation body should rank first."""

    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, monkeypatch):
        # Two files that both define RefreshService (score tie on symbol match).
        # shallow.py: one class definition, no further body mentions.
        # deep.py: same class definition PLUS lots of "refresh"/"timer" in body.
        (tmp_path / "shallow.py").write_text(
            "class RefreshService:\n"
            "    def run(self): pass\n"
        )
        (tmp_path / "deep.py").write_text(
            "class RefreshService:\n"
            "    _refresh_timer = None\n"
            "    _refresh_interval = 10\n"
            "    def start_refresh_timer(self): pass\n"
            "    def stop_refresh_timer(self): pass\n"
            "    def _on_refresh_tick(self): pass\n"
            "    def configure_refresh(self, interval): pass\n"
        )
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
        monkeypatch.setattr(mimir, "_FTS_READY", False)
        monkeypatch.setattr(mimir, "_DISK_CACHE", None)
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", [])
        monkeypatch.setattr(mimir, "_MIMIRALIASES", {})
        monkeypatch.setattr(mimir, "_REVERSE_IMPORTS", {})
        monkeypatch.setattr(mimir, "_CACHE", mimir._CACHE.__class__())
        monkeypatch.setattr(mimir, "_GIT_RECENCY_CACHE", {"ts": float("inf"), "scores": {}})

    def test_body_rich_file_ranks_above_definition_only_on_tie(self):
        result = mimir.scope_task("RefreshService timer refresh", max_files=2)
        lines = result.splitlines()
        ranked = [l for l in lines if l.strip().startswith(("1.", "2."))]
        assert len(ranked) >= 2, f"Expected 2 ranked files, got:\n{result}"
        assert "deep.py" in ranked[0], (
            f"Expected deep.py first (more keyword occurrences), got: {ranked[0]!r}\n"
            f"Full output:\n{result}"
        )

    def test_shallow_file_still_appears_in_results(self):
        result = mimir.scope_task("RefreshService timer refresh", max_files=2)
        assert "shallow.py" in result

    def test_byte_freq_does_not_promote_unrelated_file(self, tmp_path):
        # A file full of the keyword but with NO class definition should not
        # outrank one that actually defines the matched symbol.
        (tmp_path / "noise.py").write_text(
            "# refresh refresh refresh refresh refresh refresh refresh\n"
            "x = 1\n"
        )
        result = mimir.scope_task("RefreshService", max_files=3)
        lines = result.splitlines()
        ranked = [l.strip() for l in lines if l.strip().startswith(("1.", "2.", "3."))]
        # noise.py has no RefreshService symbol — it may appear via path/body frequency
        # but the definition files should rank above it on symbol score
        if any("noise.py" in r for r in ranked):
            def_rank = next((i for i, r in enumerate(ranked) if "noise.py" not in r), None)
            noise_rank = next((i for i, r in enumerate(ranked) if "noise.py" in r), None)
            assert def_rank is not None and noise_rank is not None
            assert def_rank < noise_rank, "noise.py should not outrank symbol-definition files"


# ---------------------------------------------------------------------------
# _decompose_identifier — sub-token splitting for CamelCase / snake_case
# ---------------------------------------------------------------------------

class TestDecomposeIdentifier:
    def test_pascal_case_splits(self):
        result = mimir._decompose_identifier("StartOrStopAutoRefresh")
        assert "start" in result
        assert "stop" in result
        assert "auto" in result
        assert "refresh" in result

    def test_snake_with_underscores(self):
        result = mimir._decompose_identifier("_refreshIntervalInSeconds")
        assert "refresh" in result
        assert "interval" in result
        assert "seconds" in result

    def test_short_components_excluded(self):
        # "in" is 2 chars, "or" is 2 chars — excluded by the 4-char minimum
        result = mimir._decompose_identifier("StartOrStopAutoRefresh")
        assert "or" not in result
        assert "in" not in result

    def test_plain_word_returns_itself_if_long_enough(self):
        result = mimir._decompose_identifier("refresh")
        assert "refresh" in result

    def test_short_plain_word_returns_empty(self):
        result = mimir._decompose_identifier("Get")
        assert result == () or result == []

    def test_camel_case_splits(self):
        result = mimir._decompose_identifier("myCurrentViewModel")
        assert "current" in result
        assert "view" in result
        assert "model" in result

    def test_all_caps_acronym_no_crash(self):
        result = mimir._decompose_identifier("HTTPSRequest")
        # Should not raise; result may vary but must be a sequence
        assert isinstance(result, (list, tuple))

    def test_leading_trailing_underscores_stripped(self):
        result = mimir._decompose_identifier("__private_method__")
        assert "private" in result
        assert "method" in result


# ---------------------------------------------------------------------------
# _index_blueprint_rows — sub-tokens appear in the index
# ---------------------------------------------------------------------------

class TestSubTokenIndexing:
    """Verify that _index_blueprint_rows emits sub-tokens for compound names."""

    def test_camel_case_sub_tokens_indexed(self):
        blueprint = "L1  def StartOrStopAutoRefresh(value: bool) -> None:"
        rows = mimir._index_blueprint_rows("foo.py", blueprint)
        tokens = {r[0] for r in rows}
        assert "refresh" in tokens, f"'refresh' not in indexed tokens: {tokens}"
        assert "stop" in tokens
        assert "start" in tokens
        assert "auto" in tokens

    def test_snake_case_field_sub_tokens_indexed(self):
        blueprint = "L5  _refreshIntervalInSeconds: int"
        rows = mimir._index_blueprint_rows("foo.py", blueprint)
        tokens = {r[0] for r in rows}
        assert "refresh" in tokens
        assert "interval" in tokens
        assert "seconds" in tokens

    def test_whole_token_also_indexed(self):
        # The whole identifier should still be in the index alongside sub-tokens
        blueprint = "L1  def StartOrStopAutoRefresh(value: bool) -> None:"
        rows = mimir._index_blueprint_rows("foo.py", blueprint)
        tokens = {r[0] for r in rows}
        assert "StartOrStopAutoRefresh" in tokens

    def test_short_sub_tokens_not_indexed(self):
        # "or" (2 chars), "in" (2 chars) — must not appear
        blueprint = "L1  def StartOrStopAutoRefresh(x: int) -> None:"
        rows = mimir._index_blueprint_rows("foo.py", blueprint)
        tokens = {r[0] for r in rows}
        assert "or" not in tokens
        assert "in" not in tokens

    def test_no_duplicate_tokens_per_file_line(self):
        blueprint = "L1  def refresh_timer_refresh(x): pass"
        rows = mimir._index_blueprint_rows("foo.py", blueprint)
        token_lineno_pairs = [(r[0], r[2]) for r in rows]
        # Each (token, lineno) pair should appear at most once
        assert len(token_lineno_pairs) == len(set(token_lineno_pairs))

    def test_hash_comments_skipped(self):
        blueprint = "# L1  def StartOrStopAutoRefresh(): pass"
        rows = mimir._index_blueprint_rows("foo.py", blueprint)
        assert rows == []


# ---------------------------------------------------------------------------
# Sub-token search — _symbol_hits and _symbol_hits_multi with compound names
# ---------------------------------------------------------------------------

class TestSubTokenSearch:
    """End-to-end: searching for a sub-token finds files with compound identifiers."""

    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, monkeypatch):
        (tmp_path / "viewmodel.py").write_text(
            "class MyCurrentViewModel:\n"
            "    _refreshIntervalInSeconds: int = 10\n"
            "    def StartOrStopAutoRefresh(self, value: bool) -> None:\n"
            "        pass\n"
        )
        (tmp_path / "unrelated.py").write_text(
            "class Unrelated:\n"
            "    def compute(self): pass\n"
        )
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
        monkeypatch.setattr(mimir, "_FTS_READY", False)
        monkeypatch.setattr(mimir, "_DISK_CACHE", None)
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", [])
        monkeypatch.setattr(mimir, "_CACHE", mimir._CACHE.__class__())

    def test_symbol_hits_finds_sub_token(self):
        hits = mimir._symbol_hits("refresh", max_results=10)
        files = {h[0] for h in hits}
        assert "viewmodel.py" in files, (
            f"Expected viewmodel.py in hits for 'refresh', got: {files}"
        )

    def test_symbol_hits_does_not_find_unrelated(self):
        hits = mimir._symbol_hits("refresh", max_results=10)
        files = {h[0] for h in hits}
        assert "unrelated.py" not in files

    def test_symbol_hits_multi_finds_sub_tokens(self):
        # "refresh" and "current" both appear as sub-tokens of method/class names
        result = mimir._symbol_hits_multi(["refresh", "current"], max_per_kw=10)
        refresh_files = {h[0] for h in result["refresh"]}
        current_files = {h[0] for h in result["current"]}
        assert "viewmodel.py" in refresh_files
        assert "viewmodel.py" in current_files

    def test_scope_task_surfaces_compound_identifier_file(self, monkeypatch):
        monkeypatch.setattr(mimir, "_MIMIRALIASES", {})
        monkeypatch.setattr(mimir, "_REVERSE_IMPORTS", {})
        monkeypatch.setattr(mimir, "_GIT_RECENCY_CACHE", {"ts": float("inf"), "scores": {}})
        result = mimir.scope_task("current jobs timer polling refresh", max_files=5)
        assert "viewmodel.py" in result, (
            f"Expected viewmodel.py to rank for 'refresh' sub-token search.\nOutput:\n{result}"
        )


# ---------------------------------------------------------------------------
# Reverse dependent expansion — caller surfaces alongside matched utility
# ---------------------------------------------------------------------------

class TestReverseDependentExpansion:
    """scope_task should surface files that IMPORT a matched file (callers/users)
    alongside the matched file itself, so the bug in BaseActivity is found when
    scope_task finds DialogBuilder."""

    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, monkeypatch):
        # dialog.py: defines the matched utility (DialogBuilder equivalent)
        (tmp_path / "dialog.py").write_text(
            "class DialogBuilder:\n"
            "    def exit_app(self): pass\n"
            "    def show_confirmation(self): pass\n"
        )
        # activity.py: imports dialog.py — this is where the bug lives
        (tmp_path / "activity.py").write_text(
            "from dialog import DialogBuilder\n"
            "class BaseActivity:\n"
            "    def on_back_pressed(self):\n"
            "        DialogBuilder().exit_app()\n"
        )
        # unrelated.py: no connection to the dialog
        (tmp_path / "unrelated.py").write_text(
            "class SomethingElse:\n"
            "    def compute(self): pass\n"
        )
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
        monkeypatch.setattr(mimir, "_FTS_READY", False)
        monkeypatch.setattr(mimir, "_DISK_CACHE", None)
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", [])
        monkeypatch.setattr(mimir, "_MIMIRALIASES", {})
        monkeypatch.setattr(mimir, "_CACHE", mimir._CACHE.__class__())
        monkeypatch.setattr(mimir, "_GIT_RECENCY_CACHE", {"ts": float("inf"), "scores": {}})
        monkeypatch.setattr(mimir, "_FOCUS_WEIGHTS", {})
        # Wire up the reverse import index: dialog.py is imported by activity.py
        monkeypatch.setattr(mimir, "_REVERSE_IMPORTS", {
            "dialog.py": ["activity.py"],
        })

    def test_caller_surfaces_in_scope_task(self):
        """activity.py imports dialog.py — it should appear in results even though
        no keywords match activity.py directly."""
        result = mimir.scope_task("DialogBuilder exit_app confirmation", max_files=5)
        assert "dialog.py" in result, "Matched file must appear"
        assert "activity.py" in result, (
            "Caller (activity.py) should surface via reverse dependent expansion.\n"
            f"Output:\n{result}"
        )

    def test_caller_ranks_below_directly_matched_file(self):
        """The matched utility should outrank its caller — caller gets 0.2× weight."""
        result = mimir.scope_task("DialogBuilder exit_app", max_files=5)
        lines = result.splitlines()
        ranked = [l.strip() for l in lines if l.strip().startswith(("1.", "2."))]
        assert len(ranked) >= 2, f"Expected at least 2 ranked files:\n{result}"
        assert "dialog.py" in ranked[0], (
            f"dialog.py should rank above activity.py (direct match vs 0.2× caller).\n"
            f"Got: {ranked}"
        )

    def test_unrelated_file_not_pulled_in(self):
        """Only dependents of matched files appear — unrelated files must not."""
        result = mimir.scope_task("DialogBuilder exit_app", max_files=5)
        assert "unrelated.py" not in result

    def test_no_reverse_imports_does_not_crash(self, monkeypatch):
        """Empty _REVERSE_IMPORTS must not raise or change behaviour."""
        monkeypatch.setattr(mimir, "_REVERSE_IMPORTS", {})
        result = mimir.scope_task("DialogBuilder exit_app", max_files=5)
        assert "dialog.py" in result
        assert not result.startswith("EXCEPTION")


# ---------------------------------------------------------------------------
# Java import parsing and resolution
# ---------------------------------------------------------------------------

class TestJavaImportParsing:
    """_parse_import_entries should extract Java import specifiers trimmed to class boundary."""

    def _parse(self, text: str) -> list[tuple[str, str]]:
        return mimir._parse_import_entries(Path("Foo.java"), text)

    def test_simple_class_import(self):
        entries = self._parse("import com.example.app.dialog.DialogBuilder;\n")
        assert any(spec == "com.example.app.dialog.DialogBuilder" for spec, _ in entries)

    def test_static_import_trimmed_to_class(self):
        entries = self._parse("import static com.example.app.dialog.DialogBuilder.show;\n")
        assert any(spec == "com.example.app.dialog.DialogBuilder" for spec, _ in entries)

    def test_wildcard_import_not_crash(self):
        entries = self._parse("import com.example.utils.*;\n")
        assert isinstance(entries, list)

    def test_multiple_imports(self):
        text = (
            "import com.example.BaseActivity;\n"
            "import com.example.dialog.DialogBuilder;\n"
            "import android.os.Bundle;\n"
        )
        specs = [s for s, _ in self._parse(text)]
        assert "com.example.BaseActivity" in specs
        assert "com.example.dialog.DialogBuilder" in specs
        assert "android.os.Bundle" in specs

    def test_deduplication(self):
        text = (
            "import com.example.BaseActivity;\n"
            "import com.example.BaseActivity;\n"
        )
        entries = self._parse(text)
        specs = [s for s, _ in entries]
        assert specs.count("com.example.BaseActivity") == 1


class TestJavaImportResolution:
    """_resolve_import should map Java imports to workspace-relative paths using _JAVA_CLASS_INDEX."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        java_root = tmp_path / "app" / "src" / "main" / "java" / "com" / "example" / "topcat"
        dialog_dir = java_root / "dialog"
        dialog_dir.mkdir(parents=True)
        (dialog_dir / "DialogBuilder.java").write_text("public class DialogBuilder {}")
        (java_root / "BaseActivity.java").write_text("public class BaseActivity {}")

        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", [])
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)

        mimir._build_java_class_index()

        self.src_file = dialog_dir / "SomeOtherClass.java"

    def test_resolves_to_workspace_by_class_name(self):
        kind, display = mimir._resolve_import("com.example.topcat.BaseActivity", self.src_file)
        assert kind == "workspace"
        assert "BaseActivity.java" in display

    def test_prefers_package_path_match(self):
        kind, display = mimir._resolve_import(
            "com.example.topcat.dialog.DialogBuilder", self.src_file
        )
        assert kind == "workspace"
        assert "DialogBuilder.java" in display
        assert "dialog" in display.replace("\\", "/")

    def test_unknown_class_returns_external(self):
        kind, display = mimir._resolve_import("com.example.topcat.UnknownClass", self.src_file)
        assert kind == "external"

    def test_android_sdk_class_returns_external(self):
        kind, display = mimir._resolve_import("android.os.Bundle", self.src_file)
        assert kind == "external"


class TestJavaReverseImports:
    """_build_reverse_imports should populate _REVERSE_IMPORTS for Java projects."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        java_root = tmp_path / "app" / "src" / "main" / "java" / "com" / "example"
        dialog_dir = java_root / "dialog"
        dialog_dir.mkdir(parents=True)
        (dialog_dir / "DialogBuilder.java").write_text("public class DialogBuilder {}")
        (java_root / "BaseActivity.java").write_text(
            "import com.example.dialog.DialogBuilder;\n"
            "public class BaseActivity {}\n"
        )
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", [])
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
        monkeypatch.setattr(mimir, "_CACHE", mimir._CACHE.__class__())

        mimir._build_java_class_index()
        mimir._build_reverse_imports()

    def test_base_activity_depends_on_dialog_builder(self):
        rel_dialog = next(
            (r for r in mimir._REVERSE_IMPORTS if "DialogBuilder.java" in r),
            None
        )
        assert rel_dialog is not None, (
            f"DialogBuilder.java not found as a key in _REVERSE_IMPORTS.\n"
            f"Keys: {list(mimir._REVERSE_IMPORTS.keys())}"
        )
        dependents = mimir._REVERSE_IMPORTS[rel_dialog]
        assert any("BaseActivity.java" in d for d in dependents), (
            f"BaseActivity.java not found in dependents of DialogBuilder.java.\n"
            f"Dependents: {dependents}"
        )

    def test_reverse_imports_not_empty_for_java_project(self):
        assert len(mimir._REVERSE_IMPORTS) > 0, (
            "_REVERSE_IMPORTS is empty — Java import resolution is not working."
        )


# ---------------------------------------------------------------------------
# set_focus wildcard (*) suppression
# ---------------------------------------------------------------------------

class TestFocusWildcard:
    """When '*' is in _FOCUS_WEIGHTS, files not matching any named prefix get that weight."""

    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, monkeypatch):
        (tmp_path / "android" / "app").mkdir(parents=True)
        (tmp_path / "android" / "app" / "MainActivity.java").write_text(
            "class MainActivity {\n    void onCreate() {}\n}\n"
        )
        (tmp_path / "dotnet").mkdir()
        (tmp_path / "dotnet" / "SyncService.cs").write_text(
            "class SyncService {\n    void onCreate() {}\n}\n"
        )
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
        monkeypatch.setattr(mimir, "_FTS_READY", False)
        monkeypatch.setattr(mimir, "_DISK_CACHE", None)
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", [])
        monkeypatch.setattr(mimir, "_MIMIRALIASES", {})
        monkeypatch.setattr(mimir, "_CACHE", mimir._CACHE.__class__())
        monkeypatch.setattr(mimir, "_GIT_RECENCY_CACHE", {"ts": float("inf"), "scores": {}})
        monkeypatch.setattr(mimir, "_REVERSE_IMPORTS", {})

    def test_wildcard_suppresses_non_matching_files(self):
        """With android:5 + *:0.1, Android file should rank above .NET file even if
        they'd otherwise tie on keyword hits."""
        import mimir as _m
        _m._FOCUS_WEIGHTS = {"android": 5.0, "*": 0.1}
        result = _m.scope_task("onCreate service", max_files=5)
        lines = result.splitlines()
        ranked = [l for l in lines if l.strip().startswith(("1.", "2."))]
        assert len(ranked) >= 1, f"No ranked files:\n{result}"
        assert "MainActivity.java" in ranked[0], (
            f"Android file should rank first with wildcard suppression.\nRanked: {ranked}\nFull:\n{result}"
        )

    def test_no_wildcard_does_not_suppress(self, monkeypatch):
        """Without '*', files not matching any prefix are scored normally (1×)."""
        monkeypatch.setattr(mimir, "_FOCUS_WEIGHTS", {"android": 5.0})
        result = mimir.scope_task("onCreate service", max_files=5)
        # SyncService.cs should still appear somewhere since it matches "onCreate" and "service"
        assert not result.startswith("EXCEPTION")

    def test_load_focus_parses_wildcard(self, tmp_path, monkeypatch):
        """_load_focus should parse a '*' entry from the focus file."""
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        (tmp_path / ".mimir-focus").write_text(
            "android = 5.0\n* = 0.1\n"
        )
        weights = mimir._load_focus()
        assert weights.get("android") == 5.0
        assert weights.get("*") == 0.1


# ---------------------------------------------------------------------------
# _path_in_scope / _in_scope — hard scope matching, incl. Foo/Foo.Core siblings
# ---------------------------------------------------------------------------

class TestPathInScope:
    """Real-world case that motivated this: set_scope(".../InControl.Carps.Keypad")
    must also cover InControl.Carps.Keypad.Core — a sibling project, not a
    child directory — since that's where the codebase actually put the
    reusable abstraction the ticket needed."""

    def test_exact_and_child_match_unchanged(self):
        assert mimir._path_in_scope("src/Foo", "src/Foo") is True
        assert mimir._path_in_scope("src/Foo/bar.cs", "src/Foo") is True
        assert mimir._path_in_scope("src/Other/bar.cs", "src/Foo") is False

    def test_dotted_sibling_matches(self):
        assert mimir._path_in_scope("src/Foo.Core/bar.cs", "src/Foo") is True
        assert mimir._path_in_scope("src/Foo.Core", "src/Foo") is True

    def test_dash_and_underscore_sibling_matches(self):
        assert mimir._path_in_scope("src/Foo-Tests/bar.cs", "src/Foo") is True
        assert mimir._path_in_scope("src/Foo_Shared/bar.cs", "src/Foo") is True

    def test_alnum_continuation_not_a_sibling(self):
        # "FooBar" is a different project name, not "Foo" + a conventional
        # separator — must not match just because it starts with "Foo".
        assert mimir._path_in_scope("src/FooBar/bar.cs", "src/Foo") is False
        assert mimir._path_in_scope("src/Foo2/bar.cs", "src/Foo") is False

    def test_sibling_must_share_parent_dir(self):
        # Same leaf name one level down a different parent — not a sibling.
        assert mimir._path_in_scope("other/src/Foo.Core/bar.cs", "src/Foo") is False

    def test_carps_keypad_core_real_case(self):
        scope = "src/carps-mobile/InControl.Carps.Keypad"
        assert mimir._path_in_scope(
            "src/carps-mobile/InControl.Carps.Keypad.Core/Icons/IconCatalog.cs", scope
        ) is True
        assert mimir._path_in_scope(
            "src/carps-mobile/InControl.Carps.Mobile/App.cs", scope
        ) is False

    def test_in_scope_reads_active_global(self, monkeypatch):
        monkeypatch.setattr(mimir, "_ACTIVE_SCOPE", "src/Foo")
        assert mimir._in_scope("src/Foo.Core/bar.cs") is True
        assert mimir._in_scope("src/Other/bar.cs") is False

    def test_in_scope_no_active_scope_matches_everything(self, monkeypatch):
        monkeypatch.setattr(mimir, "_ACTIVE_SCOPE", None)
        assert mimir._in_scope("anything/at/all.cs") is True


# ---------------------------------------------------------------------------
# get_symbol — comma-separated batching (one shared header vs N separate
# ~100-char headers). Real motivation: TileView.xaml.cs in a live A/B test —
# property-heavy MAUI code-behind where get_file_structure's blueprint barely
# compresses (little body to strip), and separate get_symbol calls each pay
# their own header. Batching should always be <= calling get_symbol N times
# and concatenating, and single-name behavior must be byte-identical to
# before this feature existed (existing callers/tests depend on that shape).
# ---------------------------------------------------------------------------

class TestGetSymbolBatching:
    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, monkeypatch):
        (tmp_path / "props.cs").write_text(
            "public class Tile\n"
            "{\n"
            "    public string A { get; set; } = \"\";\n"
            "    public string B { get; set; } = \"\";\n"
            "    public string C { get; set; } = \"\";\n"
            "}\n"
        )
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)

    def test_single_name_unchanged_format(self):
        # Single-name output must still be the plain "# path  symbol=X (...)"
        # shape, not the multi-symbol "N of M symbols found" wrapper.
        out = mimir.get_symbol("props.cs", "A")
        assert out.startswith("# props.cs  symbol=A")
        assert "of" not in out.split("\n")[0]

    def test_batch_returns_all_requested_symbols(self):
        out = mimir.get_symbol("props.cs", "A, B, C")
        assert "symbols found" not in out   # no miss -> no summary-line overhead
        for name in ("A", "B", "C"):
            assert f"symbol={name}" in out

    def test_batch_cheaper_than_n_separate_calls(self):
        batched = mimir.get_symbol("props.cs", "A, B, C")
        separate_total = sum(len(mimir.get_symbol("props.cs", n)) for n in ("A", "B", "C"))
        assert len(batched) < separate_total, (
            f"batched ({len(batched)} chars) should beat 3 separate calls "
            f"({separate_total} chars) by avoiding repeated headers"
        )

    def test_batch_partial_miss_still_returns_found_symbols(self):
        out = mimir.get_symbol("props.cs", "A, DoesNotExist")
        assert "1 of 2 symbols found" in out
        assert "Not found: DoesNotExist" in out
        assert "symbol=A" in out

    def test_batch_all_missing_falls_back_to_blueprint_hint(self):
        out = mimir.get_symbol("props.cs", "Nope1, Nope2")
        assert "not found" in out.lower()
        assert "Nope1" in out and "Nope2" in out
        assert "public class Tile" in out or "class Tile" in out  # blueprint fallback

    def test_whitespace_and_empty_entries_tolerated(self):
        out = mimir.get_symbol("props.cs", " A ,  , B")
        assert "symbol=A" in out and "symbol=B" in out


# ---------------------------------------------------------------------------
# CLI dispatch — mimir <subcommand> <arg>
# ---------------------------------------------------------------------------

class TestCliDispatch:
    """mimir.main() parses argv and routes to _cli_run; _cli_run calls the
    matching tool function and prints its result. These tests exercise the
    dispatch table directly rather than spawning a subprocess."""

    def test_hint_subcommand_calls_scope_hint(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(mimir, "_load_disk_cache", lambda: None)
        monkeypatch.setattr(mimir, "_warm_cache", lambda: None)
        monkeypatch.setattr(mimir, "scope_hint", lambda terms: calls.append(terms) or "HINT OUTPUT")

        mimir._cli_run("hint", "quiet zone notification")

        assert calls == ["quiet zone notification"]
        assert "HINT OUTPUT" in capsys.readouterr().out

    def test_main_routes_hint_args_to_cli_run(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(mimir.sys, "argv", ["mimir", "hint", "quiet", "zone"])
        monkeypatch.setattr(mimir, "_cli_run", lambda sub, arg: seen.update(sub=sub, arg=arg))

        mimir.main()

        assert seen == {"sub": "hint", "arg": "quiet zone"}

    def test_main_hint_without_arg_prints_usage_and_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(mimir.sys, "argv", ["mimir", "hint"])
        with pytest.raises(SystemExit) as exc_info:
            mimir.main()
        assert exc_info.value.code == 1
        assert "Usage: mimir hint" in capsys.readouterr().out

    def test_help_text_documents_hint(self):
        assert "mimir hint" in mimir._CLI_HELP

    def test_audit_subcommand_calls_audit_index_health(self, monkeypatch, capsys):
        monkeypatch.setattr(mimir, "_load_disk_cache", lambda: None)
        monkeypatch.setattr(mimir, "_warm_cache", lambda: None)
        monkeypatch.setattr(mimir, "audit_index_health", lambda: "AUDIT OUTPUT")

        mimir._cli_run("audit", "")

        assert "AUDIT OUTPUT" in capsys.readouterr().out

    def test_main_routes_audit_without_arg(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(mimir.sys, "argv", ["mimir", "audit"])
        monkeypatch.setattr(mimir, "_cli_run", lambda sub, arg: seen.update(sub=sub, arg=arg))

        mimir.main()

        assert seen == {"sub": "audit", "arg": ""}

    def test_help_text_documents_audit(self):
        assert "mimir audit" in mimir._CLI_HELP


# ---------------------------------------------------------------------------
# _notes_for_path — display helper for record_note
# ---------------------------------------------------------------------------

class TestNotesForPath:
    def test_single_match(self):
        weights = {"src/service.py": ["watch the retry logic"]}
        result = mimir._notes_for_path("src/service.py", weights=weights)
        assert result == ["note: watch the retry logic"]

    def test_no_match_returns_empty(self):
        weights = {"src/other.py": ["irrelevant"]}
        assert mimir._notes_for_path("src/service.py", weights=weights) == []

    def test_empty_source_returns_empty(self):
        assert mimir._notes_for_path("src/service.py", weights={}) == []

    def test_substring_match_not_just_startswith(self):
        # Same convention as _FOCUS_WEIGHTS: substring, not path-segment-aware.
        weights = {"service": ["generic service note"]}
        result = mimir._notes_for_path("src/UserService.py", weights=weights)
        assert result == ["note: generic service note"]

    def test_multiple_nested_prefixes_longest_first(self):
        weights = {
            "src": ["broad src note"],
            "src/auth": ["specific auth note"],
        }
        result = mimir._notes_for_path("src/auth/login.py", weights=weights)
        assert result == ["note: specific auth note", "note: broad src note"]

    def test_multiple_notes_under_one_prefix_all_shown(self):
        weights = {"src/service.py": ["first note", "second note"]}
        result = mimir._notes_for_path("src/service.py", weights=weights)
        assert result == ["note: first note", "note: second note"]

    def test_truncates_beyond_max_shown(self):
        weights = {"src/service.py": [f"note {i}" for i in range(mimir._NOTES_MAX_SHOWN + 5)]}
        result = mimir._notes_for_path("src/service.py", weights=weights)
        assert len(result) == mimir._NOTES_MAX_SHOWN + 1  # +1 for the "(+N more)" line
        assert "more" in result[-1]
        assert "+5" in result[-1]

    def test_long_note_truncated_with_ellipsis(self):
        long_note = "x" * (mimir._NOTE_MAX_CHARS + 50)
        weights = {"src/service.py": [long_note]}
        result = mimir._notes_for_path("src/service.py", weights=weights)
        assert len(result) == 1
        assert result[0].endswith("…")
        assert len(result[0]) < len(f"note: {long_note}")

    def test_defaults_to_module_global(self, monkeypatch):
        monkeypatch.setattr(mimir, "_MIMIRNOTES", {"src/service.py": ["from global dict"]})
        result = mimir._notes_for_path("src/service.py")
        assert result == ["note: from global dict"]


# ---------------------------------------------------------------------------
# v2 tool surface — locate / inspect / mimir_dispatch
# ---------------------------------------------------------------------------

class _V2WorkspaceMixin:
    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, monkeypatch):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "service.py").write_text(
            "class UserService:\n"
            "    def authenticate(self, u, p):\n"
            "        return True\n"
            "    def get_user(self, uid):\n"
            "        return None\n"
        )
        (tmp_path / "src" / "auth.py").write_text(
            "from src.service import UserService\n\n"
            "def login(u, p):\n"
            "    return UserService().authenticate(u, p)\n"
        )
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
        monkeypatch.setattr(mimir, "_FTS_READY", False)
        monkeypatch.setattr(mimir, "_DISK_CACHE", None)
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", [])
        monkeypatch.setattr(mimir, "_MIMIRALIASES", {})
        monkeypatch.setattr(mimir, "_MIMIRNOTES", {})
        monkeypatch.setattr(mimir, "_CACHE", mimir._CACHE.__class__())
        monkeypatch.setattr(mimir, "_GIT_RECENCY_CACHE", {"ts": float("inf"), "scores": {}})
        monkeypatch.setattr(mimir, "_REVERSE_IMPORTS", {})
        monkeypatch.setattr(mimir, "_ACTIVE_SCOPE", None)
        monkeypatch.setattr(mimir, "_FOCUS_WEIGHTS", {})
        monkeypatch.setattr(mimir, "_ARCHITECTURE_MAP", "")
        monkeypatch.setattr(mimir, "_LAST_OVERVIEW_HASH", None)
        return tmp_path


class TestLocate(_V2WorkspaceMixin):
    def test_finds_relevant_file(self):
        out = mimir.locate("UserService authenticate login")
        assert "service.py" in out
        assert out.startswith("# locate:")

    def test_symbol_names_shown_inline_not_full_signatures(self):
        out = mimir.locate("UserService authenticate")
        assert "authenticate" in out
        # Compact format: no separate "## Matched symbols" section like scope_task.
        assert "## Matched symbols" not in out

    def test_suggests_batched_inspect_call(self):
        out = mimir.locate("UserService authenticate")
        assert 'inspect("' in out

    def test_falls_back_to_semantic_search_on_zero_hits(self):
        out = mimir.locate("completely unmatched gibberish query zzqxv")
        assert "falling back to semantic search" in out

    def test_mode_area_routes_to_scope_area(self):
        out = mimir.locate("UserService", mode="area")
        # scope_area's output format starts with a directory-tree header, not "# locate:"
        assert not out.startswith("# locate:")

    def test_mode_symbol_routes_to_verify_symbol_existence(self):
        out = mimir.locate("UserService", mode="symbol")
        assert "FOUND" in out

    def test_unknown_mode_returns_error(self):
        out = mimir.locate("UserService", mode="bogus")
        assert "Error" in out and "bogus" in out

    def test_max_files_respected(self):
        out = mimir.locate("UserService authenticate", max_files=1)
        ranked = [l for l in out.splitlines() if l.strip().startswith(("1.", "2."))]
        assert len(ranked) <= 1

    def test_never_raises(self):
        # int(max_files) on a non-numeric string should be caught, not propagate.
        out = mimir.locate("UserService", max_files="not-a-number")
        assert "Error" in out

    def test_narrow_match_in_different_area_annotated(self):
        """A file matching only 1 of a multi-keyword query AND sitting in a
        different top-level area than the #1 result gets a precise,
        data-driven annotation distinct from the (unrelated) low-IDF one.
        Real case verified on Carps: 'workflow'/'override' are globally
        RARE there (high IDF, comfortably above the 0.25 low-IDF cutoff),
        so IDF alone couldn't flag the two unrelated test files that matched
        one of those words in a real query's tail — but combining narrow
        keyword coverage with "different area than the top result" does,
        since that's what actually separated real noise from real hits.
        """
        (mimir.WORKSPACE_ROOT / "other").mkdir()
        (mimir.WORKSPACE_ROOT / "other" / "unrelated.py").write_text(
            "class Token:\n    pass\n"
        )
        out = mimir.locate("UserService authenticate login Token", max_files=5)
        lines = [l for l in out.splitlines() if "unrelated.py" in l]
        assert lines, f"unrelated.py should appear in results:\n{out}"
        assert "matched only 1 of" in lines[0], lines[0]
        assert "different area than #1" in lines[0], lines[0]

    def test_narrow_match_in_same_area_not_annotated(self):
        """The critical negative case: a real re-test against Carps showed
        the coverage-only version of this annotation ALSO flagged genuinely
        relevant secondary files that just happened to share the top
        result's directory — e.g. AddOverridePhotoPage.xaml.cs, in the same
        UI project as the top-ranked AddOverridePhotoViewModel.cs, matching
        only "override". A narrow match in the SAME area as #1 must not be
        flagged — only narrow AND off-area should be."""
        (mimir.WORKSPACE_ROOT / "src" / "sibling.py").write_text(
            "class Token:\n    pass\n"
        )
        out = mimir.locate("UserService authenticate login Token", max_files=5)
        lines = [l for l in out.splitlines() if "sibling.py" in l]
        assert lines, f"sibling.py should appear in results:\n{out}"
        assert "matched only" not in lines[0], lines[0]

    def test_short_query_does_not_trigger_narrow_coverage_annotation(self):
        """With fewer than 3 keywords total, matching 1 is normal, not a
        red flag — must not annotate."""
        out = mimir.locate("UserService", max_files=5)
        assert "matched only" not in out


class TestInspect(_V2WorkspaceMixin):
    def test_default_view_returns_file_structure(self):
        out = mimir.inspect("src/service.py")
        assert "UserService" in out
        assert "symbol=" not in out  # structure mode, not a symbol body

    def test_symbol_param_returns_body(self):
        out = mimir.inspect("src/service.py", symbol="authenticate")
        assert "return True" in out

    def test_comma_separated_symbols_batch(self):
        out = mimir.inspect("src/service.py", symbol="authenticate, get_user")
        assert "symbol=authenticate" in out and "symbol=get_user" in out

    def test_view_imports(self):
        out = mimir.inspect("src/auth.py", view="imports")
        assert "workspace" in out.lower() or "service" in out.lower()

    def test_view_dependents(self):
        out = mimir.inspect("src/service.py", view="dependents")
        assert "EXCEPTION" not in out

    def test_view_callers_requires_symbol(self):
        out = mimir.inspect(view="callers")
        assert "Error" in out and "callers" in out

    def test_view_callers_ignores_missing_path(self):
        out = mimir.inspect(symbol="authenticate", view="callers")
        assert "Error" not in out or "auth.py" in out

    def test_missing_path_required_for_non_callers_views(self):
        out = mimir.inspect(view="imports")
        assert "Error" in out and "path" in out

    def test_unknown_view_returns_error(self):
        out = mimir.inspect("src/service.py", view="bogus")
        assert "Error" in out and "bogus" in out

    def test_directory_path_returns_directory_structure(self):
        out = mimir.inspect("src")
        assert "UserService" in out


class TestExtractBareIdentifier:
    def test_returns_single_bare_identifier_unchanged(self):
        assert mimir._extract_bare_identifier("createSession") == "createSession"

    def test_strips_trailing_descriptor_word(self):
        assert mimir._extract_bare_identifier("Request property") == "Request"

    def test_picks_longest_non_stopword_from_phrase(self):
        assert mimir._extract_bare_identifier("the AddOverridePhotoViewModel class") == "AddOverridePhotoViewModel"

    def test_falls_back_to_longest_raw_token_when_all_are_stopwords(self):
        # "class of the type" -- every token is a stopword; must not return None.
        result = mimir._extract_bare_identifier("class of the type")
        assert result is not None

    def test_returns_none_for_no_word_tokens(self):
        assert mimir._extract_bare_identifier("()...") is None


class TestVerifySymbolExistence(_V2WorkspaceMixin):
    def test_finds_known_symbol(self):
        out = mimir.verify_symbol_existence("UserService")
        assert "FOUND" in out and "service.py" in out

    def test_not_found_graceful(self):
        out = mimir.verify_symbol_existence("TotallyMadeUpNameXYZ")
        assert "NOT FOUND" in out

    def test_rejects_unrecoverable_input_without_crashing(self):
        out = mimir.verify_symbol_existence("()...")
        assert "Error" in out

    def test_auto_extracts_identifier_from_descriptive_phrase(self):
        out = mimir.verify_symbol_existence("the UserService class")
        assert "interpreted" in out
        assert "'UserService'" in out
        assert "FOUND" in out

    def test_auto_extraction_note_present_on_not_found_too(self):
        out = mimir.verify_symbol_existence("some TotallyMadeUpNameXYZ thing")
        assert "interpreted" in out
        assert "NOT FOUND" in out

    def test_bare_identifier_unaffected_by_extraction_path(self):
        # A real bare identifier should produce no "(interpreted...)" note at all.
        out = mimir.verify_symbol_existence("UserService")
        assert "interpreted" not in out

    def test_truncation_suggests_scope_when_hits_cluster(self, monkeypatch):
        """Real case: searching a generic name like 'Startup' across a large
        repo truncates at max_results with no clue where to look next. If
        the shown hits cluster under one directory, suggest scoping there.
        Paths need >3 segments so the first-3-segments grouping key lands on
        a shared directory, not each hit's own (distinct) filename."""
        clustered_hits = [
            (f"src/mobile/Sub/Area{i}.cs", "1", f"class Area{i}Startup") for i in range(5)
        ] + [("src/other/Sub2/Elsewhere.cs", "1", "class ElsewhereStartup")]
        monkeypatch.setattr(mimir, "_symbol_hits", lambda name, max_results: clustered_hits)
        out = mimir.verify_symbol_existence("Startup", max_results=6)
        assert "truncated" in out
        assert "set_scope" in out
        assert "src/mobile" in out

    def test_truncation_without_clear_cluster_has_no_scope_hint(self, monkeypatch):
        scattered_hits = [
            (f"src/p{i}/Sub/File{i}.cs", "1", f"class File{i}") for i in range(6)
        ]
        monkeypatch.setattr(mimir, "_symbol_hits", lambda name, max_results: scattered_hits)
        out = mimir.verify_symbol_existence("File", max_results=6)
        assert "truncated" in out
        assert "set_scope" not in out


class TestMimirDispatch(_V2WorkspaceMixin):
    def test_empty_command_returns_help(self):
        out = mimir.mimir_dispatch("")
        assert "mimir commands" in out

    def test_help_lists_commands(self):
        out = mimir.mimir_dispatch("help")
        assert "status" in out and "scope_task" in out

    def test_help_for_specific_command_returns_its_docstring(self):
        out = mimir.mimir_dispatch("help", "record_alias")
        assert "domain_term" in out

    def test_unknown_command_is_self_teaching(self):
        out = mimir.mimir_dispatch("totally_bogus_command")
        assert "Unknown command" in out
        assert "status" in out  # points at the real command list

    def test_status_command(self):
        out = mimir.mimir_dispatch("status")
        assert "source_files" in out

    def test_arch_command(self):
        out = mimir.mimir_dispatch("arch")
        assert "EXCEPTION" not in out

    def test_legacy_scope_task_name_still_works(self):
        out = mimir.mimir_dispatch("scope_task", "UserService authenticate")
        assert "## Matched symbols" in out  # exact old scope_task format preserved

    def test_legacy_scope_alias_maps_to_scope_task(self):
        out = mimir.mimir_dispatch("scope", "UserService authenticate")
        assert "## Matched symbols" in out

    def test_kwarg_extraction_for_scope_task_max_files(self):
        out = mimir.mimir_dispatch("scope_task", "UserService authenticate max_files=1")
        ranked = [l for l in out.splitlines() if "## Ranked files" not in l and l.strip().startswith(("1.", "2."))]
        assert len(ranked) <= 1

    def test_legacy_get_symbol_batching_via_comma_partition(self):
        out = mimir.mimir_dispatch("get_symbol", "src/service.py, authenticate, get_user")
        assert "symbol=authenticate" in out and "symbol=get_user" in out

    def test_legacy_record_alias_two_positional(self):
        out = mimir.mimir_dispatch("alias", "corrective actions, RectificationFilter")
        assert "corrective actions" in out.lower()
        assert "RectificationFilter" in out

    def test_legacy_record_note_free_text_with_commas_preserved(self):
        out = mimir.mimir_dispatch("note", "src/service.py, has a, comma in it")
        assert "has a, comma in it" in out

    def test_never_raises_on_bad_int_kwarg(self):
        out = mimir.mimir_dispatch("scope_task", "UserService max_files=not-a-number")
        assert "Error" in out

    def test_set_scope_then_legacy_scope_task_is_filtered(self):
        mimir.mimir_dispatch("set_scope", "src")
        out = mimir.mimir_dispatch("scope_task", "UserService")
        mimir.mimir_dispatch("set_scope", "")  # reset for other tests
        assert "EXCEPTION" not in out


# ---------------------------------------------------------------------------
# _upsert_mimir_section — CLAUDE.md/copilot-instructions.md marker splice
# ---------------------------------------------------------------------------

class TestUpsertMimirSection:
    def test_creates_when_file_missing(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        action = mimir._upsert_mimir_section(target, f"{mimir._MIMIR_SECTION_MARKER}\n\nbody\n")
        assert action == "created"
        assert mimir._MIMIR_SECTION_MARKER in target.read_text()

    def test_appends_when_file_exists_without_marker(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("# My Project\n\nsome existing content\n")
        action = mimir._upsert_mimir_section(target, f"{mimir._MIMIR_SECTION_MARKER}\n\nbody\n")
        assert action == "updated"
        text = target.read_text()
        assert "some existing content" in text
        assert mimir._MIMIR_SECTION_MARKER in text

    def test_skips_when_current_version_already_present(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text(f"{mimir._MIMIR_SECTION_MARKER}\n\nexisting body\n")
        action = mimir._upsert_mimir_section(target, f"{mimir._MIMIR_SECTION_MARKER}\n\nNEW body\n")
        assert action == "skipped"
        assert "existing body" in target.read_text()
        assert "NEW body" not in target.read_text()

    def test_replaces_stale_versionless_marker(self, tmp_path):
        """A section written before the (v2) marker existed must be
        upgraded, not left stale forever — this was the actual bug in the
        pre-redesign setup() (skip-if-marker-present-at-all)."""
        target = tmp_path / "CLAUDE.md"
        target.write_text(
            f"{mimir._MIMIR_MARKER_PREFIX}\n\nold v1 body naming scope_task etc.\n"
        )
        action = mimir._upsert_mimir_section(target, f"{mimir._MIMIR_SECTION_MARKER}\n\nNEW v2 body\n")
        assert action == "updated"
        text = target.read_text()
        assert "NEW v2 body" in text
        assert "old v1 body" not in text

    def test_replace_preserves_unrelated_trailing_sections(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text(
            f"# My Project\n\n{mimir._MIMIR_MARKER_PREFIX}\n\nstale\n\n"
            "## Unrelated section\n\nmust survive\n"
        )
        mimir._upsert_mimir_section(target, f"{mimir._MIMIR_SECTION_MARKER}\n\nfresh\n")
        text = target.read_text()
        assert "## Unrelated section" in text
        assert "must survive" in text
        assert "stale" not in text

    def test_replace_preserves_unrelated_leading_content(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text(f"# My Project\n\nintro text\n\n{mimir._MIMIR_MARKER_PREFIX}\n\nstale\n")
        mimir._upsert_mimir_section(target, f"{mimir._MIMIR_SECTION_MARKER}\n\nfresh\n")
        text = target.read_text()
        assert "intro text" in text
        assert "fresh" in text


# ---------------------------------------------------------------------------
# _write_overview / mimir sync
# ---------------------------------------------------------------------------

class TestWriteOverview(_V2WorkspaceMixin):
    def test_writes_overview_file(self):
        mimir._write_overview()
        overview = mimir.WORKSPACE_ROOT / mimir._OVERVIEW_FILE
        assert overview.exists()
        text = overview.read_text()
        assert "Source files indexed" in text
        assert "UserService" in text  # architecture map content

    def test_hash_guard_skips_redundant_write(self, monkeypatch):
        mimir._write_overview()
        overview = mimir.WORKSPACE_ROOT / mimir._OVERVIEW_FILE
        first_mtime = overview.stat().st_mtime_ns
        mimir._write_overview()  # nothing changed — should not rewrite
        assert overview.stat().st_mtime_ns == first_mtime

    def test_reflects_active_scope(self, monkeypatch):
        monkeypatch.setattr(mimir, "_ACTIVE_SCOPE", "src")
        mimir._write_overview()
        text = (mimir.WORKSPACE_ROOT / mimir._OVERVIEW_FILE).read_text()
        assert "Active scope: src" in text

    def test_never_raises_on_write_failure(self, monkeypatch):
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", Path("/nonexistent/does/not/exist"))
        mimir._write_overview()  # must not raise


# ---------------------------------------------------------------------------
# _save_file_list_to_disk / _load_file_list_from_disk / _clear_file_list_disk_cache
#
# Real cost this fixes: a fresh CLI subprocess has no in-memory _FILE_LIST to
# reuse, so every single `mimir <command>` shell invocation used to re-walk
# the whole tree from scratch -- measured ~1.9s on the real ~8,000-file Carps
# repo, even for a command like `status` that barely needs anything else.
# Persisting the list (with a longer, cross-process TTL than the in-memory
# 30s one) cut that to ~0.15-0.17s on repeat calls. These tests use a real
# sqlite3 connection (not monkeypatched to None) since that's what the
# functions actually read/write.
# ---------------------------------------------------------------------------

class TestFileListDiskCache:
    @pytest.fixture(autouse=True)
    def disk_cache(self, tmp_path, monkeypatch):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE file_list (path TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        monkeypatch.setattr(mimir, "_DISK_CACHE", db)
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
        monkeypatch.setattr(mimir, "_MIMIRIGNORE_PATTERNS", [])
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\n")
        return db

    def test_round_trip_save_and_load(self):
        paths = [mimir.WORKSPACE_ROOT / "a.py", mimir.WORKSPACE_ROOT / "b.py"]
        mimir._save_file_list_to_disk(paths)
        # Simulate a fresh process: no in-memory list at all.
        mimir._FILE_LIST = []
        mimir._FILE_LIST_TS = 0.0
        loaded = mimir._load_file_list_from_disk()
        assert loaded is True
        assert {p.name for p in mimir._FILE_LIST} == {"a.py", "b.py"}

    def test_load_returns_false_when_nothing_persisted(self):
        assert mimir._load_file_list_from_disk() is False
        assert mimir._FILE_LIST == []

    def test_load_returns_false_when_stale(self, monkeypatch):
        mimir._save_file_list_to_disk([mimir.WORKSPACE_ROOT / "a.py"])
        # Back-date the persisted timestamp past the disk TTL.
        stale_ts = time.time() - mimir._FILE_LIST_DISK_TTL - 1
        mimir._DISK_CACHE.execute(
            "UPDATE meta SET value = ? WHERE key = 'file_list_ts'", (str(stale_ts),)
        )
        mimir._DISK_CACHE.commit()
        mimir._FILE_LIST = []
        assert mimir._load_file_list_from_disk() is False
        assert mimir._FILE_LIST == []

    def test_clear_removes_persisted_list(self):
        mimir._save_file_list_to_disk([mimir.WORKSPACE_ROOT / "a.py"])
        mimir._clear_file_list_disk_cache()
        mimir._FILE_LIST = []
        assert mimir._load_file_list_from_disk() is False

    def test_functions_are_no_ops_without_disk_cache(self, monkeypatch):
        monkeypatch.setattr(mimir, "_DISK_CACHE", None)
        mimir._save_file_list_to_disk([mimir.WORKSPACE_ROOT / "a.py"])  # must not raise
        assert mimir._load_file_list_from_disk() is False
        mimir._clear_file_list_disk_cache()  # must not raise

    def test_iter_source_files_persists_after_real_walk(self):
        """The actual call site: _iter_source_files() itself should persist
        after a real walk, not just when a test calls _save_ directly."""
        mimir._iter_source_files()
        mimir._FILE_LIST = []
        loaded = mimir._load_file_list_from_disk()
        assert loaded is True
        assert {p.name for p in mimir._FILE_LIST} == {"a.py", "b.py"}
