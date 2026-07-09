"""
Tests for indexing config/script/migration files (.sql/.sh/.yml/.yaml/.json/
.md/.ocl/.props/.xaml).

_iter_source_files() filters by EXT_LANG membership at the os.walk() level --
a file with an unrecognized extension is never even read, let alone indexed.
Real tickets frequently touch SQL migrations, pipeline scripts, and config
files; a real Carps A/B (60 real tickets) found 13/36 misses had a target
file with an extension absent from EXT_LANG -- invisible to search entirely,
not just poorly ranked. These tests confirm the fix: .xaml reuses the
existing XML extractor; the rest get the new lightweight "text" regex
profile (keyword/path findability, not structure).
"""
import tempfile
from pathlib import Path

import pytest

import mimir


def _blueprint_for(content: str, suffix: str) -> str:
    with tempfile.NamedTemporaryFile(
        suffix=suffix, mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        tmp = Path(f.name)
    try:
        orig = mimir.WORKSPACE_ROOT
        mimir.WORKSPACE_ROOT = tmp.parent
        bp = mimir._build_blueprint(tmp)
        mimir.WORKSPACE_ROOT = orig
        return bp
    finally:
        tmp.unlink(missing_ok=True)


class TestExtLangCoverage:
    @pytest.mark.parametrize("ext", [
        ".sql", ".sh", ".yml", ".yaml", ".json", ".md", ".ocl", ".props", ".xaml",
    ])
    def test_extension_is_recognized(self, ext):
        assert ext in mimir.EXT_LANG, f"{ext} must be in EXT_LANG or it's never even walked"

    def test_xaml_routes_to_xml_extractor(self):
        assert mimir.EXT_LANG[".xaml"] == (None, "xml")

    @pytest.mark.parametrize("ext", [".sql", ".sh", ".yml", ".yaml", ".json", ".md", ".ocl", ".props"])
    def test_config_extensions_route_to_text_profile(self, ext):
        assert mimir.EXT_LANG[ext] == (None, "text")


class TestTextProfileExtraction:
    def test_sql_migration_content_is_indexed(self):
        bp = _blueprint_for(
            "ALTER TABLE tblJob ALTER COLUMN KeypadTaskID INT NULL\n"
            "GO\n",
            ".sql",
        )
        assert "KeypadTaskID" in bp
        assert "tblJob" in bp

    def test_yaml_content_is_indexed(self):
        bp = _blueprint_for(
            "image: mcr.microsoft.com/dotnet/sdk:10.0\n"
            "steps:\n"
            "  - script: publish-carps-mobile-ios.sh\n",
            ".yml",
        )
        assert "publish-carps-mobile-ios" in bp

    def test_shell_script_content_is_indexed(self):
        bp = _blueprint_for(
            "#!/bin/bash\n"
            "set -e\n"
            "echo Publishing carps mobile ios build\n",
            ".sh",
        )
        assert "Publishing carps mobile ios build" in bp

    def test_json_content_is_indexed(self):
        bp = _blueprint_for(
            '{\n  "BackgroundServiceSettings": {\n    "PollIntervalSeconds": 30\n  }\n}\n',
            ".json",
        )
        assert "BackgroundServiceSettings" in bp

    def test_markdown_content_is_indexed(self):
        bp = _blueprint_for(
            "# Settings Editor\n\nControls the AllowCustomNewTask setting.\n",
            ".md",
        )
        assert "AllowCustomNewTask" in bp

    def test_blank_and_pure_punctuation_lines_excluded(self):
        bp = _blueprint_for(
            "\n"
            "}\n"
            "---\n"
            "{\n"
            "\n"
            "realContentLine here\n",
            ".yml",
        )
        rows = [l for l in bp.splitlines() if l.startswith("L")]
        assert len(rows) == 1
        assert "realContentLine" in rows[0]

    def test_line_cap_bounds_large_files(self):
        # A real Carps SQL seed script ran 6,999 lines -- unbounded, this
        # would produce thousands of blueprint/symbol rows for one file.
        content = "\n".join(f"INSERT INTO Foo VALUES ({i})" for i in range(500))
        bp = _blueprint_for(content, ".sql")
        rows = [l for l in bp.splitlines() if l.startswith("L")]
        assert len(rows) == mimir._REGEX_PROFILE_MAX_LINES["text"]

    def test_xaml_uses_xml_extractor_not_text_profile(self):
        bp = _blueprint_for(
            '<ContentPage xmlns="http://schemas.microsoft.com/dotnet/2021/maui">\n'
            '  <Label x:Name="StatusLabel" Text="Ready"/>\n'
            "</ContentPage>\n",
            ".xaml",
        )
        assert "xml" in bp.splitlines()[0]  # header says "[xml · ...]", not "[xaml · regex ...]"


class TestTextProfileFlowsThroughToSymbolIndex:
    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, monkeypatch):
        (tmp_path / "settings.sql").write_text(
            "UPDATE SettingsDefinition SET DefaultValue = 'FALSE' WHERE SettingName = 'AdjustTasksForDLS'\n"
        )
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
        monkeypatch.setattr(mimir, "_CACHE", mimir._CACHE.__class__())
        return tmp_path

    def test_previously_invisible_extension_now_appears_in_file_list(self):
        files = mimir._iter_source_files()
        assert any(f.name == "settings.sql" for f in files)

    def test_sql_content_reaches_symbols_table(self):
        bp = mimir._build_blueprint(mimir.WORKSPACE_ROOT / "settings.sql")
        rows = mimir._index_blueprint_rows("settings.sql", bp)
        tokens = {r[0] for r in rows}
        assert "AdjustTasksForDLS" in tokens or "adjusttasksfordls" in {t.lower() for t in tokens}

    def test_verify_symbol_existence_finds_sql_content(self):
        mimir._warm_cache()
        out = mimir.verify_symbol_existence("AdjustTasksForDLS")
        assert "FOUND" in out
        assert "settings.sql" in out
