"""
Tests for mimir's symbol index: stopword filtering, normalized schema,
search correctness, and performance vs linear scan.

Run with:  pytest tests/ -v
Benchmark: pytest tests/ -v -k benchmark --no-header -rN
"""
import re
import sqlite3
import time
from pathlib import Path

import pytest

import mimir

# ---------------------------------------------------------------------------
# Sample blueprints (realistic blueprint format: L{lineno}  {signature})
# ---------------------------------------------------------------------------

SAMPLE_CS = """\
# src/UserService.cs  [cs · tree-sitter]
L1   namespace Acme.Services
L3   public class UserService
L5     public User GetById(int id)
L8     public void Delete(User user)
L11    public IEnumerable<User> GetAll()
L14    private readonly IUserRepository _repo
"""

SAMPLE_PY = """\
# auth/service.py  [python · tree-sitter]
L1   class AuthService:
L3     def authenticate(self, username: str, password: str) -> bool:
L6     def _hash_password(self, password: str) -> str:
L9     def create_session(self, user_id: int) -> Session:
"""

SAMPLE_TS = """\
# src/api/jobQueue.ts  [typescript · tree-sitter]
L2   export interface JobQueue
L4   export class JobQueueService implements JobQueue
L7     async processJob(job: Job): Promise<void>
L11    private readonly _queue: Queue<Job>
"""

CS_REL  = "src/UserService.cs"
PY_REL  = "auth/service.py"
TS_REL  = "src/api/jobQueue.ts"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    """In-memory SQLite database with the normalized schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE lines"
        " (file TEXT NOT NULL, lineno TEXT NOT NULL, context TEXT NOT NULL,"
        "  PRIMARY KEY (file, lineno))"
    )
    conn.execute(
        "CREATE TABLE symbols"
        " (token TEXT NOT NULL, file TEXT NOT NULL, lineno TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX idx_symbols_token ON symbols (token)")
    conn.commit()
    return conn


def _populate(conn, rel, blueprint):
    line_rows = mimir._extract_blueprint_lines(rel, blueprint)
    sym_rows  = mimir._index_blueprint_rows(rel, blueprint)
    conn.executemany("INSERT OR REPLACE INTO lines VALUES (?,?,?)", line_rows)
    conn.executemany("INSERT INTO symbols VALUES (?,?,?)", sym_rows)
    conn.commit()


# ---------------------------------------------------------------------------
# _extract_blueprint_lines
# ---------------------------------------------------------------------------

class TestExtractBlueprintLines:
    def test_returns_file_lineno_context_triples(self):
        rows = mimir._extract_blueprint_lines(CS_REL, SAMPLE_CS)
        assert all(len(r) == 3 for r in rows)
        assert all(r[0] == CS_REL for r in rows)

    def test_skips_comment_header(self):
        rows = mimir._extract_blueprint_lines(CS_REL, SAMPLE_CS)
        contexts = [r[2] for r in rows]
        assert not any(c.startswith("#") for c in contexts)

    def test_captures_all_definition_lines(self):
        rows = mimir._extract_blueprint_lines(CS_REL, SAMPLE_CS)
        contexts = [r[2] for r in rows]
        assert any("UserService" in c for c in contexts)
        assert any("GetById" in c for c in contexts)
        assert any("_repo" in c for c in contexts)

    def test_lineno_is_numeric_string(self):
        rows = mimir._extract_blueprint_lines(CS_REL, SAMPLE_CS)
        assert all(r[1].isdigit() for r in rows)

    def test_no_duplicate_file_lineno_pairs(self):
        # Regression: tree-sitter extraction previously emitted duplicate line
        # numbers for C# methods with array parameters, causing UNIQUE constraint
        # failures in the symbol index. This ensures extraction always yields
        # unique (file, lineno) pairs so INSERT OR IGNORE can serve as safety net.
        bp_with_dups = (
            "# src/Svc.cs  [cs · tree-sitter]\n"
            "L5    public class Svc\n"
            "L10     public void Write(string content, string[] args)\n"
            "L10       []\n"   # simulates old broken extraction output
            "L15     public string Read(byte[] data)\n"
            "L15       []\n"
        )
        rows = mimir._extract_blueprint_lines("src/Svc.cs", bp_with_dups)
        keys = [(r[0], r[1]) for r in rows]
        assert len(keys) == len(set(keys)), "Duplicate (file, lineno) pairs in output"


# ---------------------------------------------------------------------------
# _index_blueprint_rows  (stopwords + normalization)
# ---------------------------------------------------------------------------

class TestIndexBlueprintRows:
    def test_returns_token_file_lineno_triples(self):
        rows = mimir._index_blueprint_rows(CS_REL, SAMPLE_CS)
        assert all(len(r) == 3 for r in rows)
        assert all(r[1] == CS_REL for r in rows)

    def test_symbol_names_are_indexed(self):
        rows = mimir._index_blueprint_rows(CS_REL, SAMPLE_CS)
        tokens = {r[0] for r in rows}
        assert "UserService" in tokens
        assert "GetById" in tokens
        assert "Delete" in tokens
        assert "GetAll" in tokens

    def test_stopwords_excluded(self):
        rows = mimir._index_blueprint_rows(CS_REL, SAMPLE_CS)
        tokens = {r[0] for r in rows}
        for sw in ("public", "private", "void", "int", "class",
                   "readonly", "namespace", "static", "new", "this"):
            assert sw not in tokens, f"stopword '{sw}' should not be indexed"

    def test_python_stopwords_excluded(self):
        rows = mimir._index_blueprint_rows(PY_REL, SAMPLE_PY)
        tokens = {r[0] for r in rows}
        assert "def" not in tokens
        assert "self" not in tokens
        assert "str" not in tokens    # primitive type

    def test_python_symbol_names_indexed(self):
        rows = mimir._index_blueprint_rows(PY_REL, SAMPLE_PY)
        tokens = {r[0] for r in rows}
        assert "AuthService" in tokens
        assert "authenticate" in tokens
        assert "_hash_password" in tokens
        assert "create_session" in tokens

    def test_ts_symbols_indexed(self):
        rows = mimir._index_blueprint_rows(TS_REL, SAMPLE_TS)
        tokens = {r[0] for r in rows}
        assert "JobQueue" in tokens
        assert "JobQueueService" in tokens
        assert "processJob" in tokens

    def test_ts_stopwords_excluded(self):
        rows = mimir._index_blueprint_rows(TS_REL, SAMPLE_TS)
        tokens = {r[0] for r in rows}
        assert "export" not in tokens
        assert "implements" not in tokens
        assert "async" not in tokens

    def test_no_single_char_tokens(self):
        rows = mimir._index_blueprint_rows(CS_REL, SAMPLE_CS)
        assert all(len(r[0]) >= 2 for r in rows)

    def test_tokens_deduplicated_per_line(self):
        blueprint = "L1  public public UserService UserService"
        rows = mimir._index_blueprint_rows("f.cs", blueprint)
        us_tokens = [r for r in rows if r[0] == "UserService"]
        assert len(us_tokens) == 1

    def test_no_context_in_rows(self):
        rows = mimir._index_blueprint_rows(CS_REL, SAMPLE_CS)
        # Each row is (token, file, lineno) — three elements, no context
        assert all(len(r) == 3 for r in rows)

    def test_fewer_rows_than_denormalized(self):
        """Symbol rows should be fewer than (tokens × context) would produce."""
        sym_rows  = mimir._index_blueprint_rows(CS_REL, SAMPLE_CS)
        line_rows = mimir._extract_blueprint_lines(CS_REL, SAMPLE_CS)
        # symbols has one row per unique token per line; lines has one row per line
        assert len(sym_rows) >= len(line_rows)  # at least one token per line
        assert len(sym_rows) < len(line_rows) * 20  # sanity upper bound


# ---------------------------------------------------------------------------
# Normalized schema: lines + symbols tables
# ---------------------------------------------------------------------------

class TestNormalizedSchema:
    def test_context_stored_in_lines_not_symbols(self, db):
        _populate(db, CS_REL, SAMPLE_CS)
        # symbols has no context column
        cols = [d[1] for d in db.execute("PRAGMA table_info(symbols)").fetchall()]
        assert "context" not in cols
        assert "token" in cols
        assert "file" in cols
        assert "lineno" in cols

    def test_lines_holds_context(self, db):
        _populate(db, CS_REL, SAMPLE_CS)
        cols = [d[1] for d in db.execute("PRAGMA table_info(lines)").fetchall()]
        assert "context" in cols
        assert "file" in cols
        assert "lineno" in cols

    def test_context_not_duplicated(self, db):
        _populate(db, CS_REL, SAMPLE_CS)
        line_count = db.execute("SELECT COUNT(*) FROM lines WHERE file=?", (CS_REL,)).fetchone()[0]
        sym_count  = db.execute("SELECT COUNT(*) FROM symbols WHERE file=?", (CS_REL,)).fetchone()[0]
        # Should have more symbol rows than line rows (multiple tokens per line)
        # but context only stored once per line
        assert sym_count > line_count

    def test_join_query_returns_correct_context(self, db):
        _populate(db, CS_REL, SAMPLE_CS)
        rows = db.execute(
            "SELECT l.file, l.lineno, l.context"
            " FROM symbols s JOIN lines l ON s.file=l.file AND s.lineno=l.lineno"
            " WHERE s.token=?",
            ("GetById",)
        ).fetchall()
        assert len(rows) >= 1
        assert rows[0][0] == CS_REL
        assert "GetById" in rows[0][2]

    def test_stopwords_absent_from_symbols_table(self, db):
        _populate(db, CS_REL, SAMPLE_CS)
        for sw in ("public", "void", "int", "class", "readonly"):
            count = db.execute(
                "SELECT COUNT(*) FROM symbols WHERE token=?", (sw,)
            ).fetchone()[0]
            assert count == 0, f"stopword '{sw}' should not be in symbols table"

    def test_size_smaller_than_denormalized(self, db):
        """Normalized schema should store less context text than the old approach."""
        _populate(db, CS_REL, SAMPLE_CS)
        _populate(db, PY_REL, SAMPLE_PY)
        _populate(db, TS_REL, SAMPLE_TS)

        # Old approach: context duplicated once per token
        line_rows = (
            mimir._extract_blueprint_lines(CS_REL, SAMPLE_CS) +
            mimir._extract_blueprint_lines(PY_REL, SAMPLE_PY) +
            mimir._extract_blueprint_lines(TS_REL, SAMPLE_TS)
        )
        sym_rows = (
            mimir._index_blueprint_rows(CS_REL, SAMPLE_CS) +
            mimir._index_blueprint_rows(PY_REL, SAMPLE_PY) +
            mimir._index_blueprint_rows(TS_REL, SAMPLE_TS)
        )
        old_bytes = sum(
            len(tok) + len(f) + len(l) + len(ctx)
            for tok, f, l in sym_rows
            # pretend context was stored in each sym row (old schema)
            for ctx in [
                next((r[2] for r in line_rows if r[0] == f and r[1] == l), "")
            ]
        )
        new_sym_bytes  = sum(len(t) + len(f) + len(l) for t, f, l in sym_rows)
        new_line_bytes = sum(len(f) + len(l) + len(c) for f, l, c in line_rows)
        new_bytes = new_sym_bytes + new_line_bytes

        assert new_bytes < old_bytes, (
            f"Normalized ({new_bytes}B) should be smaller than denormalized ({old_bytes}B)"
        )


# ---------------------------------------------------------------------------
# Search correctness: SQL path must match linear-scan results
# ---------------------------------------------------------------------------

class TestSearchCorrectness:
    """SQL results must agree with the linear-scan fallback on the same data."""

    def _linear_hits(self, name, blueprints, max_results=25):
        word_re = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        hits = []
        for rel, bp in blueprints.items():
            if name not in bp:
                continue
            for line in bp.splitlines():
                if line.startswith("#") or not word_re.search(line):
                    continue
                m = re.match(r"L(\d+)\s*(.*)", line.strip())
                if m:
                    hits.append((rel, m.group(1), m.group(2).strip()))
        return hits[:max_results]

    def _sql_hits(self, db, name, max_results=25):
        word_re = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        rows = db.execute(
            "SELECT l.file, l.lineno, l.context"
            " FROM symbols s JOIN lines l ON s.file=l.file AND s.lineno=l.lineno"
            " WHERE s.token=? LIMIT ?",
            (name, max_results * 4),
        ).fetchall()
        return [(f, l, c) for f, l, c in rows if word_re.search(c)][:max_results]

    @pytest.fixture
    def populated_db(self, db):
        blueprints = {CS_REL: SAMPLE_CS, PY_REL: SAMPLE_PY, TS_REL: SAMPLE_TS}
        for rel, bp in blueprints.items():
            _populate(db, rel, bp)
        return db, blueprints

    @pytest.mark.parametrize("symbol", [
        "UserService", "GetById", "AuthService", "authenticate",
        "JobQueueService", "processJob", "create_session",
    ])
    def test_sql_matches_linear(self, populated_db, symbol):
        db, blueprints = populated_db
        sql    = sorted(self._sql_hits(db, symbol))
        linear = sorted(self._linear_hits(symbol, blueprints))
        assert sql == linear, (
            f"SQL and linear results differ for '{symbol}':\n"
            f"  SQL:    {sql}\n"
            f"  Linear: {linear}"
        )

    def test_word_boundary_respected(self, populated_db):
        db, blueprints = populated_db
        # "Get" should NOT match "GetById", "GetAll"
        sql_hits = self._sql_hits(db, "Get")
        for _, _, ctx in sql_hits:
            assert re.search(r"(?<!\w)Get(?!\w)", ctx), \
                f"'Get' matched inside a longer token in: {ctx!r}"

    def test_not_found_returns_empty(self, populated_db):
        db, _ = populated_db
        results = self._sql_hits(db, "NonExistentSymbolXYZ")
        assert results == []


# ---------------------------------------------------------------------------
# Performance benchmark
# ---------------------------------------------------------------------------

class TestPerformance:
    """Benchmark SQL lookup vs linear scan on a synthetic 1000-file corpus."""

    @pytest.fixture(scope="class")
    def corpus(self):
        """Generate 1000 blueprint strings and a populated in-memory db."""
        blueprints = {}
        for i in range(1000):
            bp = (
                f"L1   namespace Acme.Services{i}\n"
                f"L3   public class Service{i}\n"
                f"L5     public void Process{i}(Job job)\n"
                f"L8     public User GetUser{i}(int id)\n"
                f"L11    private readonly IRepo{i} _repo\n"
            )
            blueprints[f"src/Service{i}.cs"] = bp

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE lines"
            " (file TEXT, lineno TEXT, context TEXT, PRIMARY KEY(file,lineno))"
        )
        conn.execute("CREATE TABLE symbols (token TEXT, file TEXT, lineno TEXT)")
        conn.execute("CREATE INDEX idx ON symbols (token)")
        for rel, bp in blueprints.items():
            line_rows = mimir._extract_blueprint_lines(rel, bp)
            sym_rows  = mimir._index_blueprint_rows(rel, bp)
            conn.executemany("INSERT OR REPLACE INTO lines VALUES (?,?,?)", line_rows)
            conn.executemany("INSERT INTO symbols VALUES (?,?,?)", sym_rows)
        conn.commit()
        return conn, blueprints

    def _sql_lookup(self, conn, token, max_results=25):
        return conn.execute(
            "SELECT l.file, l.lineno, l.context"
            " FROM symbols s JOIN lines l ON s.file=l.file AND s.lineno=l.lineno"
            " WHERE s.token=? LIMIT ?",
            (token, max_results),
        ).fetchall()

    def _linear_scan(self, blueprints, name, max_results=25):
        word_re = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        hits = []
        for rel, bp in blueprints.items():
            if name not in bp:
                continue
            for line in bp.splitlines():
                if word_re.search(line):
                    m = re.match(r"L(\d+)\s*(.*)", line.strip())
                    if m:
                        hits.append((rel, m.group(1), m.group(2).strip()))
                        if len(hits) >= max_results:
                            return hits
        return hits

    def test_sql_faster_than_linear(self, corpus):
        conn, blueprints = corpus
        target = "Service500"
        RUNS = 200

        t0 = time.perf_counter()
        for _ in range(RUNS):
            self._sql_lookup(conn, target)
        sql_ms = (time.perf_counter() - t0) / RUNS * 1000

        t0 = time.perf_counter()
        for _ in range(RUNS):
            self._linear_scan(blueprints, target)
        linear_ms = (time.perf_counter() - t0) / RUNS * 1000

        speedup = linear_ms / sql_ms
        print(f"\n  SQL: {sql_ms:.3f}ms  Linear: {linear_ms:.3f}ms  Speedup: {speedup:.1f}x")
        assert sql_ms < linear_ms, (
            f"SQL ({sql_ms:.3f}ms) should be faster than linear ({linear_ms:.3f}ms)"
        )

    def test_sql_under_1ms(self, corpus):
        conn, _ = corpus
        RUNS = 500
        t0 = time.perf_counter()
        for _ in range(RUNS):
            self._sql_lookup(conn, "Service250")
        avg_ms = (time.perf_counter() - t0) / RUNS * 1000
        print(f"\n  SQL avg: {avg_ms:.3f}ms")
        assert avg_ms < 1.0, f"SQL lookup averaged {avg_ms:.3f}ms, expected <1ms"

    def test_stopword_reduction(self):
        """Stopword filter should produce meaningfully fewer symbol rows."""
        blueprint = "\n".join(
            f"L{i}   public static void Method{i}(int id, string name)"
            for i in range(100)
        )
        sym_rows = mimir._index_blueprint_rows("test.cs", blueprint)
        tokens = {r[0] for r in sym_rows}

        # All Method* tokens present
        assert all(f"Method{i}" in tokens for i in range(100))

        # None of the stopwords present
        for sw in ("public", "static", "void", "int", "string"):
            assert sw not in tokens

        # Count: without stopwords each line has ~3 tokens (Method*, id*, name*)
        # vs ~7 tokens with stopwords — at least 30% reduction
        stopwords_in_line = len({"public", "static", "void", "int", "string"})
        assert len(sym_rows) < 100 * (stopwords_in_line + 3), \
            "Expected fewer rows due to stopword filtering"
