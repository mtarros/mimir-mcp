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
        """All 13 tools must be registered — any missing tool fails silently in production."""
        async with Client(_make_transport(workspace)) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
        expected = {
            "get_file_structure",
            "verify_symbol_existence",
            "scope_task",
            "get_imports",
            "get_dependents",
            "find_callers",
            "get_directory_structure",
            "get_status",
            "record_alias",
            "execute_local_sandbox",
            "get_symbol",
            "get_changed_files",
            "get_architecture",
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
