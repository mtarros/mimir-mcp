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
        assert result == []

    def test_camel_case_splits(self):
        result = mimir._decompose_identifier("myCurrentViewModel")
        assert "current" in result
        assert "view" in result
        assert "model" in result

    def test_all_caps_acronym_no_crash(self):
        result = mimir._decompose_identifier("HTTPSRequest")
        # Should not raise; result may vary but must be a list
        assert isinstance(result, list)

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
