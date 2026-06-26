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
        """Seven tools must be registered — any missing tool fails silently in production."""
        async with Client(_make_transport(workspace)) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
        expected = {
            "get_file_structure",
            "verify_symbol_existence",
            "scope_task",
            "get_imports",
            "find_callers",
            "get_directory_structure",
            "execute_local_sandbox",
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
            r = await client.call_tool("get_file_structure", {"file_path": "src/service.py"})
        assert _text(r), "get_file_structure returned empty string"
        assert "EXCEPTION" not in _text(r), "Unhandled exception escaped tool handler"


# ---------------------------------------------------------------------------
# get_file_structure
# ---------------------------------------------------------------------------

class TestGetFileStructureWire:
    async def test_returns_blueprint_with_l_lines(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_file_structure", {"file_path": "src/service.py"})
        text = _text(r)
        assert "UserService" in text
        assert text.count("\nL") >= 2, "Blueprint should have multiple L-lines"

    async def test_file_not_found_returns_error_string(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_file_structure", {"file_path": "does/not/exist.py"})
        text = _text(r)
        assert "not found" in text.lower() or "Error" in text
        assert "EXCEPTION" not in text

    async def test_path_traversal_rejected(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_file_structure", {"file_path": "../../etc/passwd"})
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
            r = await client.call_tool("get_imports", {"file_path": "src/controllers/auth.py"})
        text = _text(r)
        assert "workspace" in text.lower() or "service" in text.lower()
        assert "EXCEPTION" not in text

    async def test_file_with_no_imports_handled(self, workspace):
        async with Client(_make_transport(workspace)) as client:
            r = await client.call_tool("get_imports", {"file_path": "src/models.py"})
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
