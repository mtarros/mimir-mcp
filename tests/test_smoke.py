"""
Smoke tests: start mimir as a real subprocess and exercise each tool over the
MCP stdio JSON-RPC protocol. These catch wiring bugs (missing tools, broken
serialization, unhandled exceptions escaping tool handlers) that unit tests
cannot detect because they call Python functions directly.

These tests spawn a subprocess per test and take ~1-2s each.
Run them with: pytest tests/test_smoke.py -v

They are excluded from the default test run to keep `pytest tests/` fast.
Add -m smoke to target just these: pytest -m smoke tests/test_smoke.py -v
"""
import os
import re
import sys
import asyncio
from pathlib import Path

import pytest

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

# Mark every test in this file as both anyio (async) and smoke (slow/integration)
pytestmark = [pytest.mark.anyio, pytest.mark.smoke]

MIMIR_PY = Path(__file__).parent.parent / "mimir.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text(result) -> str:
    """Extract the string from a CallToolResult."""
    return result.content[0].text


def _make_transport(workspace: Path, sandbox: bool = False) -> StdioTransport:
    env = {
        **os.environ,
        "MCP_WORKSPACE_ROOT": str(workspace),
        "MCP_ENABLE_SANDBOX": "1" if sandbox else "0",
    }
    return StdioTransport(
        command=sys.executable,
        args=[str(MIMIR_PY)],
        env=env,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    """Minimal workspace with known Python source files for tool calls."""
    src = tmp_path / "src"
    src.mkdir()
    controllers = src / "controllers"
    controllers.mkdir()

    (src / "service.py").write_text(
        "class UserService:\n"
        "    def authenticate(self, username: str, password: str) -> bool:\n"
        "        return True\n"
        "\n"
        "    def get_user(self, user_id: int):\n"
        "        return None\n"
    )
    (src / "models.py").write_text(
        "class User:\n"
        "    name: str\n"
        "    email: str\n"
    )
    (controllers / "auth.py").write_text(
        "from src.service import UserService\n"
        "\n"
        "class AuthController:\n"
        "    def __init__(self):\n"
        "        self._svc = UserService()\n"
        "\n"
        "    def login(self, username, password):\n"
        "        return self._svc.authenticate(username, password)\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Wire protocol: tool registration
# ---------------------------------------------------------------------------

class TestToolRegistration:
    async def test_all_tools_listed(self, workspace):
        """All 20 tools must be registered — any missing tool fails silently in production."""
        async with Client(_make_transport(workspace)) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
        expected = {
            "get_file_structure",
            "verify_symbol_existence",
            "scope_task",
            "scope_hint",
            "get_context",
            "set_focus",
            "get_imports",
            "get_dependents",
            "find_callers",
            "get_directory_structure",
            "get_status",
            "record_alias",
            "record_note",
            "add_ignore",
            "execute_local_sandbox",
            "get_symbol",
            "get_changed_files",
            "get_architecture",
            "semantic_search",
            "audit_index_health",
        }
        assert expected == names, f"Tool mismatch. Extra: {names - expected}, Missing: {expected - names}"

    async def test_all_tools_have_descriptions(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            tools = await client.list_tools()
        for t in tools:
            assert t.description and len(t.description) > 20, \
                f"Tool '{t.name}' has missing/short description"

    async def test_tool_results_are_text_not_none(self, workspace):
        """Every tool must return a non-empty string — no None, no empty result."""
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_file_structure", {"path":"src/service.py"})
        assert _text(r), "get_file_structure returned empty string"
        assert "EXCEPTION" not in _text(r), "Unhandled exception escaped tool handler"


# ---------------------------------------------------------------------------
# get_file_structure
# ---------------------------------------------------------------------------

class TestGetFileStructureWire:
    async def test_returns_blueprint_with_l_lines(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_file_structure", {"path":"src/service.py"})
        text = _text(r)
        assert "UserService" in text
        assert text.count("\nL") >= 2, "Blueprint should have multiple L-lines"

    async def test_file_not_found_returns_error_string(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_file_structure", {"path":"does/not/exist.py"})
        text = _text(r)
        assert "not found" in text.lower() or "Error" in text
        assert "EXCEPTION" not in text

    async def test_path_traversal_rejected(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_file_structure", {"path":"../../etc/passwd"})
        text = _text(r)
        assert "outside" in text.lower() or "escapes" in text.lower() or "error" in text.lower()


# ---------------------------------------------------------------------------
# scope_task
# ---------------------------------------------------------------------------

class TestScopeTaskWire:
    async def test_finds_relevant_file(self, workspace):
        # Use exact symbol names — scope_task keyword search is token-exact, not stemmed
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("scope_task", {"task": "UserService authenticate login"})
        text = _text(r)
        assert "UserService" in text or "service.py" in text

    async def test_returns_nonempty_response(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("scope_task", {"task": "UserService"})
        text = _text(r)
        assert len(text) > 50
        assert "EXCEPTION" not in text

    async def test_tiebreaker_ranks_body_rich_file_first(self, tmp_path):
        """Integration: body-rich file beats definition-only file when symbol scores tie."""
        # Both files define the same class → same symbol score.
        # deep.py has many more keyword occurrences in method names and fields.
        (tmp_path / "shallow.py").write_text(
            "class TimerJob:\n"
            "    def run(self): pass\n"
        )
        (tmp_path / "deep.py").write_text(
            "class TimerJob:\n"
            "    _timer_interval = 30\n"
            "    _timer_handle = None\n"
            "    def start_timer(self): pass\n"
            "    def stop_timer(self): pass\n"
            "    def reset_timer(self): pass\n"
            "    def on_timer_tick(self): pass\n"
        )
        async with Client(_make_transport(tmp_path)) as client:
            r = await client.call_tool("scope_task", {"task": "TimerJob timer", "max_files": 2})
        text = _text(r)
        assert "EXCEPTION" not in text
        lines = text.splitlines()
        ranked = [l for l in lines if l.strip().startswith(("1.", "2."))]
        assert len(ranked) >= 2, f"Expected 2 ranked files:\n{text}"
        assert "deep.py" in ranked[0], (
            f"deep.py should rank first (more 'timer' occurrences in body):\n{text}"
        )


# ---------------------------------------------------------------------------
# verify_symbol_existence
# ---------------------------------------------------------------------------

class TestVerifySymbolExistenceWire:
    async def test_finds_known_symbol(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("verify_symbol_existence", {"symbol_name": "UserService"})
        text = _text(r)
        assert "UserService" in text
        assert "service.py" in text

    async def test_not_found_returns_graceful_message(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("verify_symbol_existence", {"symbol_name": "AbsolutelyNonExistentXYZ"})
        text = _text(r)
        assert "EXCEPTION" not in text
        assert len(text) > 0


# ---------------------------------------------------------------------------
# get_imports
# ---------------------------------------------------------------------------

class TestGetImportsWire:
    async def test_resolves_workspace_import(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_imports", {"path":"src/controllers/auth.py"})
        text = _text(r)
        assert "workspace" in text.lower() or "service" in text.lower()
        assert "EXCEPTION" not in text

    async def test_file_with_no_imports_handled(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_imports", {"path":"src/models.py"})
        text = _text(r)
        assert "EXCEPTION" not in text


# ---------------------------------------------------------------------------
# find_callers
# ---------------------------------------------------------------------------

class TestFindCallersWire:
    async def test_finds_call_site(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("find_callers", {"symbol_name": "authenticate"})
        text = _text(r)
        assert "auth.py" in text
        assert "EXCEPTION" not in text

    async def test_dotted_name_rejected_over_wire(self, workspace):
        """Validation must hold end-to-end, not just in unit tests."""
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("find_callers", {"symbol_name": "some.method"})
        text = _text(r)
        assert text.startswith("Error")
        assert "EXCEPTION" not in text

    async def test_not_found_returns_graceful_message(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("find_callers", {"symbol_name": "GhostFunctionXYZ"})
        text = _text(r)
        assert "EXCEPTION" not in text
        assert "No usages" in text or "not found" in text.lower()


# ---------------------------------------------------------------------------
# get_dependents
# ---------------------------------------------------------------------------

class TestGetDependentsWire:
    async def test_returns_importer(self, workspace):
        """auth.py imports src.service, so service.py should list auth.py as a dependent."""
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_dependents", {"path": "src/service.py"})
        text = _text(r)
        assert "auth.py" in text
        assert "EXCEPTION" not in text

    async def test_file_with_no_dependents(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_dependents", {"path": "src/models.py"})
        text = _text(r)
        assert "EXCEPTION" not in text

    async def test_unknown_path_handled(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_dependents", {"path": "src/does_not_exist.py"})
        text = _text(r)
        assert "EXCEPTION" not in text


# ---------------------------------------------------------------------------
# get_directory_structure
# ---------------------------------------------------------------------------

class TestGetDirectoryStructureWire:
    async def test_returns_blueprints_for_dir(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_directory_structure", {"dir_path": "src"})
        text = _text(r)
        assert "UserService" in text or "User" in text
        assert "EXCEPTION" not in text

    async def test_path_traversal_rejected(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_directory_structure", {"dir_path": "../../etc"})
        text = _text(r)
        assert "outside" in text.lower() or "Error" in text

    async def test_nonexistent_dir_handled(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_directory_structure", {"dir_path": "src/does_not_exist"})
        text = _text(r)
        assert "EXCEPTION" not in text
        assert "not found" in text.lower() or "No source" in text


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

class TestGetStatusWire:
    async def test_returns_workspace_path(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_status", {})
        text = _text(r)
        assert str(workspace) in text
        assert "EXCEPTION" not in text

    async def test_shows_source_file_count(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_status", {})
        text = _text(r)
        assert "source_files" in text
        assert "3" in text  # workspace fixture has 3 .py files

    async def test_shows_index_state(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_status", {})
        text = _text(r)
        assert "symbol_index" in text
        # Either warm or building — both are valid immediately after startup
        assert "warm" in text or "building" in text

    async def test_shows_mimirignore_hint_when_no_file(self, workspace):
        """When .mimirignore doesn't exist, get_status should tell the user how to create one."""
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_status", {})
        text = _text(r)
        assert ".mimirignore" in text

    async def test_shows_active_patterns_when_file_exists(self, workspace):
        (workspace / ".mimirignore").write_text("**/vendor/**\n**/obj/**\n")
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_status", {})
        text = _text(r)
        assert "**/vendor/**" in text or "2 active" in text


# ---------------------------------------------------------------------------
# record_alias
# ---------------------------------------------------------------------------

class TestRecordAliasWire:
    async def test_saves_alias_and_confirms(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool(
                "record_alias",
                {"domain_term": "live tutor", "code_name": "LiveTutor"},
            )
        text = _text(r)
        assert "live tutor" in text.lower()
        assert "LiveTutor" in text
        assert "EXCEPTION" not in text
        alias_file = workspace / ".mimiraliases"
        assert alias_file.exists()
        assert "LiveTutor" in alias_file.read_text()

    async def test_duplicate_returns_already_recorded(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            await client.call_tool("record_alias", {"domain_term": "foo feature", "code_name": "FooService"})
            r = await client.call_tool("record_alias", {"domain_term": "foo feature", "code_name": "FooService"})
        text = _text(r)
        assert "already" in text.lower() or "FooService" in text

    async def test_empty_inputs_rejected(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("record_alias", {"domain_term": "", "code_name": "Foo"})
        text = _text(r)
        assert "Error" in text


# ---------------------------------------------------------------------------
# record_note
# ---------------------------------------------------------------------------

class TestRecordNoteWire:
    async def test_saves_note_and_confirms(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool(
                "record_note",
                {"path_prefix": "src/service.py", "note": "watch for the auth quirk here"},
            )
        text = _text(r)
        assert "src/service.py" in text
        assert "watch for the auth quirk here" in text
        assert "EXCEPTION" not in text
        notes_file = workspace / ".mimirnotes"
        assert notes_file.exists()
        assert "watch for the auth quirk here" in notes_file.read_text()

    async def test_duplicate_returns_already_recorded(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            await client.call_tool("record_note", {"path_prefix": "src/models.py", "note": "dup note"})
            r = await client.call_tool("record_note", {"path_prefix": "src/models.py", "note": "dup note"})
        text = _text(r)
        assert "already" in text.lower()

    async def test_empty_inputs_rejected(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("record_note", {"path_prefix": "", "note": "x"})
        text = _text(r)
        assert "Error" in text

    async def test_prefix_with_equals_rejected(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("record_note", {"path_prefix": "Config=Prod", "note": "x"})
        text = _text(r)
        assert "Error" in text

    async def test_multiline_note_flattened(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool(
                "record_note",
                {"path_prefix": "src/models.py", "note": "line one\nline two\nline three"},
            )
            text = _text(r)
            assert "EXCEPTION" not in text
            notes_file = workspace / ".mimirnotes"
            raw = notes_file.read_text()
            assert "line one line two line three" in raw
            # No embedded raw newline inside a note entry — file stays one-line-per-note.
            assert "line one\nline two" not in raw
            # File must still parse cleanly after the flattened write.
            r2 = await client.call_tool("get_status", {})
            assert "EXCEPTION" not in _text(r2)

    async def test_note_surfaces_in_get_file_structure(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            await client.call_tool(
                "record_note",
                {"path_prefix": "src/service.py", "note": "check the retry logic here"},
            )
            r = await client.call_tool("get_file_structure", {"path": "src/service.py"})
        text = _text(r)
        assert "note: check the retry logic here" in text

    async def test_note_surfaces_in_scope_task(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            await client.call_tool(
                "record_note",
                {"path_prefix": "src/service.py", "note": "surfaces in ranked output"},
            )
            r = await client.call_tool("scope_task", {"task": "UserService authenticate login"})
        text = _text(r)
        assert "note: surfaces in ranked output" in text

    async def test_note_does_not_affect_ranking(self, workspace):
        """A note's text must never influence scope_task's scores — notes are
        display-only, unlike record_alias which deliberately does affect ranking."""
        async with Client(_make_transport(workspace)) as client:
            before = _text(await client.call_tool(
                "scope_task", {"task": "UserService authenticate login"}
            ))
            await client.call_tool(
                "record_note",
                {"path_prefix": "src/service.py",
                 "note": "totally unrelated keywords xylophone zeppelin quokka"},
            )
            after = _text(await client.call_tool(
                "scope_task", {"task": "UserService authenticate login"}
            ))

        def match_counts(text: str) -> list[str]:
            return [l for l in text.splitlines() if re.match(r"\s*\d+\.\s", l)]

        assert match_counts(before) == match_counts(after), (
            f"ranked-file match counts changed after recording an unrelated note:\n"
            f"before={match_counts(before)}\nafter={match_counts(after)}"
        )


# ---------------------------------------------------------------------------
# add_ignore
# ---------------------------------------------------------------------------

class TestAddIgnoreWire:
    async def test_adds_pattern_and_writes_file(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool(
                "add_ignore",
                {"pattern": "**/vendor/**", "reason": "third-party libs"},
            )
        text = _text(r)
        assert "**/vendor/**" in text
        assert "EXCEPTION" not in text
        ignore_file = workspace / ".mimirignore"
        assert ignore_file.exists()
        assert "**/vendor/**" in ignore_file.read_text()

    async def test_duplicate_pattern_rejected(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            await client.call_tool("add_ignore", {"pattern": "**/dist/**"})
            r = await client.call_tool("add_ignore", {"pattern": "**/dist/**"})
        text = _text(r)
        assert "already" in text.lower()
        assert "EXCEPTION" not in text

    async def test_empty_pattern_rejected(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("add_ignore", {"pattern": ""})
        text = _text(r)
        assert "Error" in text
        assert "EXCEPTION" not in text

    async def test_path_traversal_rejected(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("add_ignore", {"pattern": "../../etc/**"})
        text = _text(r)
        assert "Error" in text
        assert "EXCEPTION" not in text


# ---------------------------------------------------------------------------
# get_symbol
# ---------------------------------------------------------------------------

class TestGetSymbolWire:
    async def test_returns_function_body(self, workspace):
        """get_symbol should return the full authenticate method body."""
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_symbol", {"path": "src/service.py", "symbol_name": "authenticate"})
        text = _text(r)
        assert "authenticate" in text
        assert "return True" in text   # body content, not just signature
        assert "EXCEPTION" not in text

    async def test_returns_class_body(self, workspace):
        """get_symbol on a class name should return the whole class."""
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_symbol", {"path": "src/service.py", "symbol_name": "UserService"})
        text = _text(r)
        assert "UserService" in text
        assert "authenticate" in text  # method inside the class
        assert "EXCEPTION" not in text

    async def test_unknown_symbol_returns_blueprint_hint(self, workspace):
        """When the symbol is not found, the response should include available symbols."""
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_symbol", {"path": "src/service.py", "symbol_name": "GhostXYZ"})
        text = _text(r)
        assert "not found" in text.lower() or "GhostXYZ" in text
        # Should suggest what IS in the file
        assert "UserService" in text or "authenticate" in text
        assert "EXCEPTION" not in text

    async def test_unknown_file_returns_error(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_symbol", {"path": "src/does_not_exist.py", "symbol_name": "foo"})
        text = _text(r)
        assert "Error" in text or "not found" in text.lower()
        assert "EXCEPTION" not in text


# ---------------------------------------------------------------------------
# get_changed_files
# ---------------------------------------------------------------------------

@pytest.fixture
def git_workspace(tmp_path):
    """Workspace fixture that is a real git repo with one committed change."""
    import subprocess
    src = tmp_path / "src"
    src.mkdir()

    (src / "service.py").write_text(
        "class UserService:\n"
        "    def authenticate(self, username: str, password: str) -> bool:\n"
        "        return True\n"
    )

    # Init git repo and make an initial commit on 'main'
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True, capture_output=True)

    # Add a new file on a feature branch
    (src / "models.py").write_text("class User:\n    name: str\n")
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-b", "feature"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "add models"], check=True, capture_output=True)

    return tmp_path


class TestGetChangedFilesWire:
    async def test_returns_changed_file_blueprint(self, git_workspace):
        """Should return blueprint for models.py which was added on the feature branch."""
        async with Client(_make_transport(git_workspace)) as client:
            r = await client.call_tool("get_changed_files", {"base": "main"})
        text = _text(r)
        assert "models.py" in text
        assert "EXCEPTION" not in text

    async def test_shows_diff_summary_table(self, git_workspace):
        """Summary table with +/- line counts should appear before the blueprints."""
        async with Client(_make_transport(git_workspace)) as client:
            r = await client.call_tool("get_changed_files", {"base": "main"})
        text = _text(r)
        # models.py is a new untracked-then-committed file; summary line shows +N -0 or +N -M
        assert "+" in text
        assert "models.py" in text
        assert "EXCEPTION" not in text

    async def test_non_git_workspace_handled(self, workspace):
        """Plain tmp_path (no git repo) should return a clear error, not crash."""
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_changed_files", {})
        text = _text(r)
        # Either no changes found, or a clear error — never an EXCEPTION
        assert "EXCEPTION" not in text

    async def test_bad_base_branch_handled(self, git_workspace):
        async with Client(_make_transport(git_workspace)) as client:
            r = await client.call_tool("get_changed_files", {"base": "nonexistent-branch-xyz"})
        text = _text(r)
        assert "EXCEPTION" not in text


# ---------------------------------------------------------------------------
# get_architecture
# ---------------------------------------------------------------------------

class TestGetArchitectureWire:
    async def test_returns_directory_sections(self, workspace):
        """Architecture map should include directory headers."""
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_architecture", {})
        text = _text(r)
        assert "src" in text
        assert "EXCEPTION" not in text

    async def test_includes_top_level_symbols(self, workspace):
        """Architecture map should list top-level symbol names."""
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_architecture", {})
        text = _text(r)
        # service.py has UserService at top level
        assert "UserService" in text
        assert "EXCEPTION" not in text

    async def test_returns_nonempty_string(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_architecture", {})
        text = _text(r)
        assert len(text) > 50
        assert "EXCEPTION" not in text

    async def test_rebuilds_when_cache_invalidated(self, workspace):
        """After invalidation, get_architecture must discover files added since warmup."""
        new_file = workspace / "src" / "brandnew.py"
        try:
            async with Client(_make_transport(workspace)) as client:
                # Prime the architecture cache (warmup may or may not have run yet)
                await client.call_tool("get_architecture", {})
                # Write a new file AFTER the server started
                new_file.write_text("class BrandNewService:\n    pass\n")
                # add_ignore clears _ARCHITECTURE_MAP and _FILE_LIST as a side effect
                await client.call_tool("add_ignore", {"pattern": "__test_marker__"})
                # Next call must rebuild from scratch and discover brandnew.py
                r = await client.call_tool("get_architecture", {})
            text = _text(r)
            assert "BrandNewService" in text
            assert "EXCEPTION" not in text
        finally:
            new_file.unlink(missing_ok=True)
            ignore_file = workspace / ".mimirignore"
            if ignore_file.exists():
                content = ignore_file.read_text()
                ignore_file.write_text(
                    "\n".join(l for l in content.splitlines() if "__test_marker__" not in l) + "\n"
                )


# ---------------------------------------------------------------------------
# execute_local_sandbox
# ---------------------------------------------------------------------------

class TestSandboxWire:
    async def test_python_snippet_executes(self, workspace):
        async with Client(_make_transport(workspace, sandbox=True)) as client:
            r = await client.call_tool(
                "execute_local_sandbox",
                {"language": "python", "code": "print('smoke_ok')"},
            )
        text = _text(r)
        assert "smoke_ok" in text
        assert "EXCEPTION" not in text

    async def test_sandbox_disabled_returns_error(self, workspace):
        """When MCP_ENABLE_SANDBOX=0, tool must return a clear error, not crash."""
        async with Client(_make_transport(workspace, sandbox=False)) as client:
            r = await client.call_tool(
                "execute_local_sandbox",
                {"language": "python", "code": "print('should not run')"},
            )
        text = _text(r)
        assert "disabled" in text.lower() or "Error" in text
        assert "EXCEPTION" not in text

    async def test_python_runtime_error_captured(self, workspace):
        """A crashing snippet must be caught and returned as output, never propagated."""
        async with Client(_make_transport(workspace, sandbox=True)) as client:
            r = await client.call_tool(
                "execute_local_sandbox",
                {"language": "python", "code": "raise ValueError('boom')"},
            )
        text = _text(r)
        assert "EXCEPTION" not in text
        assert "ValueError" in text or "boom" in text or "error" in text.lower()


# ---------------------------------------------------------------------------
# Per-call focus parameter and set_focus persist flag
# ---------------------------------------------------------------------------

@pytest.fixture
def dual_workspace(tmp_path):
    """Two directories that both match 'authenticate user', allowing focus ranking tests."""
    (tmp_path / "frontend" / "auth").mkdir(parents=True)
    (tmp_path / "backend" / "auth").mkdir(parents=True)
    (tmp_path / "frontend" / "auth" / "service.py").write_text(
        "class FrontendAuthService:\n"
        "    def authenticate_user(self, token: str) -> bool:\n"
        "        return True\n"
    )
    (tmp_path / "backend" / "auth" / "service.py").write_text(
        "class BackendAuthService:\n"
        "    def authenticate_user(self, credentials: dict) -> bool:\n"
        "        return True\n"
    )
    return tmp_path


class TestPerCallFocus:
    async def test_scope_task_focus_boosts_matching_path(self, dual_workspace):
        async with Client(_make_transport(dual_workspace)) as client:
            r = await client.call_tool("scope_task", {
                "task": "authenticate user",
                "focus": "backend:5.0",
            })
        text = _text(r)
        assert "EXCEPTION" not in text
        # scope_task output has a "Ranked files" section: "  1. backend/auth/..." etc.
        # Find the first ranked entry and confirm it's backend.
        ranked_lines = [
            ln.strip() for ln in text.splitlines()
            if ln.strip().startswith("1.")
        ]
        assert ranked_lines, f"No ranked files found in output:\n{text}"
        assert "backend" in ranked_lines[0], (
            f"Expected backend as #1 with focus=backend:5.0, got: {ranked_lines[0]}\n{text}"
        )

    async def test_scope_task_focus_does_not_mutate_global(self, dual_workspace):
        """A second call without focus should give unbiased results independent of the first."""
        async with Client(_make_transport(dual_workspace)) as client:
            r1 = await client.call_tool("scope_task", {
                "task": "authenticate user",
                "focus": "backend:100.0",
            })
            r2 = await client.call_tool("scope_task", {
                "task": "authenticate user",
            })
        text1 = _text(r1)
        text2 = _text(r2)
        assert "EXCEPTION" not in text1
        assert "EXCEPTION" not in text2
        # Focused call: backend must rank #1
        ranked1 = [ln.strip() for ln in text1.splitlines() if ln.strip().startswith("1.")]
        assert ranked1 and "backend" in ranked1[0], (
            f"Expected backend #1 with focus=backend:100, got: {ranked1}"
        )
        # After the focused call, global weights are unchanged — r2 should contain
        # both files (the server did not permanently bias toward backend).
        assert "frontend" in text2 and "backend" in text2


class TestSetFocusPersist:
    async def test_persist_false_no_file_written(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("set_focus", {
                "entries": "src:2.0",
                "persist": False,
            })
        text = _text(r)
        assert "EXCEPTION" not in text
        assert "session" in text.lower(), f"Expected 'session' in response: {text!r}"
        assert not (workspace / ".mimir-focus").exists(), (
            ".mimir-focus must not be written when persist=False"
        )

    async def test_persist_true_file_written(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("set_focus", {
                "entries": "src:2.0",
                "persist": True,
            })
        text = _text(r)
        assert "EXCEPTION" not in text
        assert (workspace / ".mimir-focus").exists(), (
            ".mimir-focus must be written when persist=True"
        )


@pytest.mark.asyncio
class TestGetContextWire:
    async def test_returns_ranked_files_and_blueprint(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_context", {
                "task": "UserService authenticate login",
                "max_files": 2,
            })
        text = _text(r)
        assert "EXCEPTION" not in text
        assert "Ranked files" in text or "ranked" in text.lower()
        # Blueprint content: at least one L-prefixed line number should appear
        assert re.search(r'\bL\d+\b', text), "Expected blueprint line numbers in output"

    async def test_no_crash_on_unknown_task(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_context", {
                "task": "xyzzy_nonexistent_feature_qwerty",
            })
        text = _text(r)
        assert "EXCEPTION" not in text
        assert len(text) > 0
