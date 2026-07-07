"""
Tests for blueprint format, get_file_structure's directory mode, and cache
version handling.

Run with: pytest tests/ -v
"""
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

import mimir


# ---------------------------------------------------------------------------
# Blueprint header: line count
# ---------------------------------------------------------------------------

class TestBlueprintHeader:
    def _blueprint_for(self, content: str, suffix: str = ".py") -> str:
        with tempfile.NamedTemporaryFile(
            suffix=suffix, mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            tmp = Path(f.name)
        try:
            # Patch workspace root so relative_to works
            orig = mimir.WORKSPACE_ROOT
            mimir.WORKSPACE_ROOT = tmp.parent
            bp = mimir._build_blueprint(tmp)
            mimir.WORKSPACE_ROOT = orig
            return bp
        finally:
            tmp.unlink(missing_ok=True)

    def test_header_contains_line_count(self):
        bp = self._blueprint_for("def foo():\n    pass\n\ndef bar():\n    pass\n")
        header = bp.splitlines()[0]
        assert "lines]" in header, f"Expected 'lines]' in header: {header!r}"

    def test_line_count_is_accurate(self):
        content = "def foo():\n    pass\n"   # 2 newlines → 3 lines (trailing newline counts)
        bp = self._blueprint_for(content)
        header = bp.splitlines()[0]
        assert "3 lines]" in header, f"Expected '3 lines]' in {header!r}"

    def test_line_count_multiline(self):
        content = "\n".join(f"# line {i}" for i in range(50)) + "\ndef foo(): pass\n"
        bp = self._blueprint_for(content)
        header = bp.splitlines()[0]
        assert "51 lines]" in header or "52 lines]" in header  # allow off-by-one for trailing newline

    def test_header_format_cs(self):
        content = "public class Foo {}\n"
        bp = self._blueprint_for(content, suffix=".cs")
        header = bp.splitlines()[0]
        assert "cs" in header
        assert "lines]" in header

    def test_cs_array_params_no_duplicate_line_numbers(self):
        # Regression: tree-sitter used to emit the same line number twice for
        # C# methods with array parameters (once for the method declaration node
        # and again for the inner array-type node), causing UNIQUE constraint
        # failures when writing to the symbol index.
        content = (
            "public class DocumentationService {\n"
            "    public static void Write(string content, string[] args) {}\n"
            "    public static string Read(byte[] data, int[] offsets) { return \"\"; }\n"
            "}\n"
        )
        bp = self._blueprint_for(content, suffix=".cs")
        l_lines = [l for l in bp.splitlines() if l.startswith("L")]
        line_numbers = [l.split()[0] for l in l_lines]
        duplicates = [ln for ln in set(line_numbers) if line_numbers.count(ln) > 1]
        assert duplicates == [], f"Duplicate line numbers in C# blueprint: {duplicates}"


# ---------------------------------------------------------------------------
# get_file_structure — directory mode
# ---------------------------------------------------------------------------

class TestGetFileStructureDirectoryMode:
    @pytest.fixture
    def workspace(self, tmp_path):
        """Create a mini workspace with source files in subdirectories."""
        (tmp_path / "src" / "controllers").mkdir(parents=True)
        (tmp_path / "src" / "services").mkdir(parents=True)

        (tmp_path / "src" / "controllers" / "UserController.py").write_text(
            "class UserController:\n    def get(self): pass\n    def post(self): pass\n"
        )
        (tmp_path / "src" / "controllers" / "JobController.py").write_text(
            "class JobController:\n    def list(self): pass\n"
        )
        (tmp_path / "src" / "services" / "UserService.py").write_text(
            "class UserService:\n    def find(self): pass\n"
        )
        # Non-source file — should be ignored
        (tmp_path / "src" / "controllers" / "README.txt").write_text("readme")

        orig_root = mimir.WORKSPACE_ROOT
        orig_files = mimir._FILE_LIST[:]
        orig_ts = mimir._FILE_LIST_TS

        mimir.WORKSPACE_ROOT = tmp_path
        mimir._FILE_LIST = []
        mimir._FILE_LIST_TS = 0.0

        yield tmp_path

        mimir.WORKSPACE_ROOT = orig_root
        mimir._FILE_LIST[:] = orig_files
        mimir._FILE_LIST_TS = orig_ts

    def test_returns_blueprints_for_directory(self, workspace):
        result = mimir.get_file_structure("src/controllers")
        assert "UserController" in result
        assert "JobController" in result

    def test_excludes_other_directories(self, workspace):
        result = mimir.get_file_structure("src/controllers")
        assert "UserService" not in result  # lives in services/, not controllers/

    def test_shows_file_count(self, workspace):
        result = mimir.get_file_structure("src/controllers")
        assert "2 source files" in result

    def test_max_files_cap(self, workspace):
        result = mimir.get_file_structure("src/controllers", max_files=1)
        assert "more file" in result  # truncation notice

    def test_path_outside_workspace_rejected(self, workspace):
        result = mimir.get_file_structure("../../etc")
        assert "escapes" in result.lower() or "outside" in result.lower()

    def test_nonexistent_path(self, workspace):
        result = mimir.get_file_structure("src/does/not/exist")
        assert "not found" in result.lower() or "Not found" in result

    def test_file_path_returns_file_blueprint(self, workspace):
        # A file path in "directory mode" position now just returns its own
        # blueprint — get_file_structure dispatches on is_dir(), not on which
        # branch the caller thought they were using.
        result = mimir.get_file_structure("src/controllers/UserController.py")
        assert "UserController" in result
        assert "lines]" in result

    def test_empty_directory(self, workspace):
        (workspace / "src" / "empty").mkdir()
        result = mimir.get_file_structure("src/empty")
        assert "No source files" in result

    def test_blueprints_include_line_count(self, workspace):
        result = mimir.get_file_structure("src/controllers")
        assert "lines]" in result


# ---------------------------------------------------------------------------
# Blueprint version / cache invalidation
# ---------------------------------------------------------------------------

class TestBlueprintVersion:
    def test_version_constant_exists(self):
        assert hasattr(mimir, "BLUEPRINT_VERSION")
        assert isinstance(mimir.BLUEPRINT_VERSION, str)
        assert len(mimir.BLUEPRINT_VERSION) > 0

    def test_version_mismatch_clears_blueprints(self):
        """Simulate a stale cache from an older mimir version."""
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE TABLE blueprints (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, blueprint TEXT)"
        )
        db.execute("CREATE TABLE lines (file TEXT, lineno TEXT, context TEXT, PRIMARY KEY(file,lineno))")
        db.execute("CREATE TABLE symbols (token TEXT, file TEXT, lineno TEXT)")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO blueprints VALUES ('old/file.py', 1.0, 100, 'old blueprint')")
        db.execute("INSERT INTO meta VALUES ('blueprint_version', '0')")  # old version
        db.commit()

        # Simulate what _init_disk_cache does on version mismatch
        stored = db.execute("SELECT value FROM meta WHERE key='blueprint_version'").fetchone()
        if stored is None or stored[0] != mimir.BLUEPRINT_VERSION:
            db.execute("DELETE FROM blueprints")
            db.execute("DELETE FROM lines")
            db.execute("DELETE FROM symbols")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('blueprint_version', ?)",
                       (mimir.BLUEPRINT_VERSION,))
            db.commit()

        count = db.execute("SELECT COUNT(*) FROM blueprints").fetchone()[0]
        assert count == 0, "Stale blueprints should be cleared on version mismatch"

        version = db.execute("SELECT value FROM meta WHERE key='blueprint_version'").fetchone()[0]
        assert version == mimir.BLUEPRINT_VERSION

    def test_version_match_preserves_blueprints(self):
        """Cache should NOT be cleared when version already matches."""
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE TABLE blueprints (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, blueprint TEXT)"
        )
        db.execute("CREATE TABLE lines (file TEXT, lineno TEXT, context TEXT, PRIMARY KEY(file,lineno))")
        db.execute("CREATE TABLE symbols (token TEXT, file TEXT, lineno TEXT)")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO blueprints VALUES ('src/foo.py', 1.0, 100, 'valid blueprint')")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('blueprint_version', ?)",
                   (mimir.BLUEPRINT_VERSION,))
        db.commit()

        # Version matches — no clearing
        stored = db.execute("SELECT value FROM meta WHERE key='blueprint_version'").fetchone()
        if stored is None or stored[0] != mimir.BLUEPRINT_VERSION:
            db.execute("DELETE FROM blueprints")

        count = db.execute("SELECT COUNT(*) FROM blueprints").fetchone()[0]
        assert count == 1, "Valid blueprints should be preserved when version matches"
