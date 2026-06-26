"""
Pytest configuration — sets MCP_WORKSPACE_ROOT to a temp directory before
mimir is imported so the module uses a test-specific SQLite cache rather than
the real workspace cache.
"""
import os
import sys
import tempfile
from pathlib import Path

# Must be set before mimir is first imported (module-level globals read it).
_tmp = tempfile.mkdtemp(prefix="mimir_test_")
os.environ.setdefault("MCP_WORKSPACE_ROOT", _tmp)
os.environ["MCP_ENABLE_SANDBOX"] = "0"

sys.path.insert(0, str(Path(__file__).parent.parent))
