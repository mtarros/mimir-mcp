"""
Tests for exception/log string-literal extraction (#strings blueprint section).

Literal messages from throw/raise statements and known logging calls are
appended to blueprints under a '#strings' header so scope_task and
semantic_search can find files by quoted error text from tickets.
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


def _strings_section(bp: str) -> list[str]:
    if "#strings" not in bp:
        return []
    return [l for l in bp.split("#strings", 1)[1].splitlines() if l.startswith("L")]


# ---------------------------------------------------------------------------
# C#
# ---------------------------------------------------------------------------

class TestCSharpLiterals:
    def test_throw_literal_in_blueprint(self):
        bp = _blueprint_for(
            "public class A {\n"
            "    public void B() {\n"
            "        throw new InvalidOperationException(\"Maximum retry attempts exceeded.\");\n"
            "    }\n"
            "}\n",
            ".cs",
        )
        rows = _strings_section(bp)
        assert len(rows) == 1
        assert 'throw InvalidOperationException("Maximum retry attempts exceeded.")' in rows[0]
        assert rows[0].startswith("L3")

    def test_interpolated_string_literal(self):
        bp = _blueprint_for(
            "public class A {\n"
            "    public void B() {\n"
            "        throw new Exception($\"ConnectionId not found for the operativeId: {operativeId}\");\n"
            "    }\n"
            "}\n",
            ".cs",
        )
        rows = _strings_section(bp)
        assert len(rows) == 1
        # interpolation hole skipped, literal text kept
        assert "ConnectionId not found for the operativeId" in rows[0]
        assert "{operativeId}" not in rows[0]

    def test_logwarning_literal(self):
        bp = _blueprint_for(
            "public class A {\n"
            "    public void B() {\n"
            "        _logger.LogWarning(\"connection lost during retry loop\");\n"
            "    }\n"
            "}\n",
            ".cs",
        )
        rows = _strings_section(bp)
        assert len(rows) == 1
        assert 'LogWarning("connection lost during retry loop")' in rows[0]

    def test_wrapped_exception_constructor(self):
        # Real-world Carps pattern: exception constructed but never thrown —
        # wrapped in a Result. The constructor literal must still be indexed.
        bp = _blueprint_for(
            "public class A {\n"
            "    public Result B() {\n"
            "        return Result.ErrorResult(new Exception(\"Maximum retry attempts exceeded.\", lastException));\n"
            "    }\n"
            "}\n",
            ".cs",
        )
        rows = _strings_section(bp)
        assert len(rows) == 1
        assert 'new Exception("Maximum retry attempts exceeded.")' in rows[0]

    def test_non_logging_invocation_excluded(self):
        bp = _blueprint_for(
            "public class A {\n"
            "    public void B() {\n"
            "        builder.Append(\"this is just a string builder call\");\n"
            "        Login(\"user credentials with spaces here\");\n"
            "    }\n"
            "}\n",
            ".cs",
        )
        assert _strings_section(bp) == []


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

class TestPythonLiterals:
    def test_raise_literal(self):
        bp = _blueprint_for(
            "def f():\n"
            "    raise ValueError(\"connection pool exhausted for tenant\")\n",
            ".py",
        )
        rows = _strings_section(bp)
        assert len(rows) == 1
        assert 'raise ValueError("connection pool exhausted for tenant")' in rows[0]

    def test_logging_warning_literal(self):
        bp = _blueprint_for(
            "import logging\n"
            "def f():\n"
            "    logging.warning(\"retry limit reached for job queue\")\n",
            ".py",
        )
        rows = _strings_section(bp)
        assert len(rows) == 1
        assert 'warning("retry limit reached for job queue")' in rows[0]

    def test_plain_call_not_indexed(self):
        bp = _blueprint_for(
            "def f():\n"
            "    print(\"this message is long enough to index\")\n"
            "    do_work(\"another multi word string argument\")\n",
            ".py",
        )
        assert _strings_section(bp) == []


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------

class TestJsTsLiterals:
    def test_throw_error_literal(self):
        bp = _blueprint_for(
            "function f() {\n"
            "  throw new Error(\"maximum retry attempts exceeded\");\n"
            "}\n",
            ".js",
        )
        rows = _strings_section(bp)
        assert len(rows) == 1
        assert 'throw Error("maximum retry attempts exceeded")' in rows[0]

    def test_console_error_literal(self):
        bp = _blueprint_for(
            "export function f(id: string) {\n"
            "  console.error(`websocket closed for client ${id} during sync`);\n"
            "}\n",
            ".ts",
        )
        rows = _strings_section(bp)
        assert len(rows) == 1
        # template substitution hole skipped
        assert "websocket closed for client" in rows[0]
        assert "${id}" not in rows[0]


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------

class TestJavaLiterals:
    def test_throw_and_log(self):
        bp = _blueprint_for(
            "class A {\n"
            "  void b() {\n"
            "    throw new IllegalStateException(\"maximum retry attempts exceeded\");\n"
            "  }\n"
            "  void c() {\n"
            "    log.warn(\"connection not found for device\");\n"
            "  }\n"
            "}\n",
            ".java",
        )
        rows = _strings_section(bp)
        assert len(rows) == 2
        assert any("IllegalStateException" in r for r in rows)
        assert any('warn("connection not found for device")' in r for r in rows)


# ---------------------------------------------------------------------------
# Filters and caps
# ---------------------------------------------------------------------------

class TestLiteralFilters:
    def test_short_literal_excluded(self):
        bp = _blueprint_for(
            "def f():\n    raise ValueError(\"too bad\")\n",  # 7 chars < min 10
            ".py",
        )
        assert _strings_section(bp) == []

    def test_single_word_literal_excluded(self):
        bp = _blueprint_for(
            "def f():\n    raise ValueError(\"operativeIdentifier\")\n",
            ".py",
        )
        assert _strings_section(bp) == []

    def test_literal_cap_per_file(self):
        body = "def f():\n" + "".join(
            f"    logging.warning(\"repeated warning message number {i} here\")\n"
            for i in range(mimir._LIT_MAX_PER_FILE + 20)
        )
        bp = _blueprint_for(body, ".py")
        rows = _strings_section(bp)
        assert len(rows) == mimir._LIT_MAX_PER_FILE

    def test_long_literal_truncated(self):
        msg = "word " * 60  # 300 chars
        bp = _blueprint_for(
            f"def f():\n    raise ValueError(\"{msg.strip()}\")\n", ".py"
        )
        rows = _strings_section(bp)
        assert len(rows) == 1
        quoted = rows[0].split('("', 1)[1]
        assert len(quoted) <= mimir._LIT_MAX_LEN + 2

    def test_no_strings_section_when_no_literals(self):
        bp = _blueprint_for("def f():\n    return 1\n", ".py")
        assert "#strings" not in bp


# ---------------------------------------------------------------------------
# Index integration
# ---------------------------------------------------------------------------

class TestLiteralIndexing:
    BP = (
        "L2    def process()\n"
        "#strings\n"
        "L4      raise ValueError(\"connection pool exhausted for tenant\")\n"
    )

    def test_literal_tokens_reach_symbols_index(self):
        rows = mimir._index_blueprint_rows("worker.py", self.BP)
        tokens = {t for t, _, _ in rows}
        assert "exhausted" in tokens
        assert "connection" in tokens
        linenos = {ln for t, _, ln in rows if t == "exhausted"}
        assert linenos == {"4"}

    def test_literal_reaches_fts_rows(self):
        rows = mimir._fts_rows_for_blueprint("worker.py", self.BP)
        lit_rows = [r for r in rows if r[1] == "4"]
        assert len(lit_rows) == 1
        assert "exhausted" in lit_rows[0][4]  # decomposed column

    def test_strings_header_not_indexed(self):
        rows = mimir._extract_blueprint_lines("worker.py", self.BP)
        assert all(not ctx.startswith("#") for _, _, ctx in rows)

    def test_literal_not_marked_as_definition(self):
        # Literal rows must score as usages (1x), not definitions (3x), and
        # must never generate get_symbol suggestions.
        assert not mimir._DEF_LINE_PAT.search(
            'raise ValueError("connection pool exhausted for tenant")'
        )
        assert not mimir._DEF_LINE_PAT.search(
            'throw InvalidOperationException("Maximum retry attempts exceeded.")'
        )

    def test_literal_not_in_toplevel_architecture_names(self):
        names = mimir._toplevel_names_from_blueprint(self.BP)
        assert "raise" not in names
        assert "ValueError" not in names
