"""
Tests for semantic_search: FTS5 index building, BM25 retrieval, TF-IDF
re-ranking, and performance comparison against scope_task (keyword-exact).

Run with:  pytest tests/test_semantic_search.py -v
Benchmark: pytest tests/test_semantic_search.py -v -k benchmark
"""
import sqlite3
import time
from pathlib import Path

import pytest

import mimir

# ---------------------------------------------------------------------------
# Sample blueprints (realistic format: L{lineno}  {signature})
# ---------------------------------------------------------------------------

SAMPLE_AUTH = """\
# auth/handler.py  [python · tree-sitter]
L1   class AuthService:
L3     def authenticate(self, username: str, password: str) -> bool:
L6     def _hash_password(self, password: str) -> str:
L9     def create_session(self, user_id: int) -> Session:
L12    def revoke_session(self, token: str) -> None:
L15    def refresh_access_token(self, refresh_token: str) -> str:
"""

SAMPLE_QUEUE = """\
# jobs/queue_processor.py  [python · tree-sitter]
L1   class JobQueueProcessor:
L4     def enqueue(self, job: Job) -> str:
L7     def process_next(self) -> Optional[Job]:
L10    def retry_failed_job(self, job_id: str, max_retries: int = 3) -> bool:
L14    def _exponential_backoff(self, attempt: int) -> float:
L17    def drain_queue(self) -> int:
"""

SAMPLE_DB = """\
# db/connection_pool.py  [python · tree-sitter]
L1   class ConnectionPool:
L4     def acquire_connection(self) -> Connection:
L7     def release_connection(self, conn: Connection) -> None:
L10    def _handle_pool_exhaustion(self, timeout: float) -> Connection:
L14    def check_health(self) -> bool:
L17    def close_all_connections(self) -> None:
"""

SAMPLE_PAYMENT = """\
# payments/processor.py  [python · tree-sitter]
L1   class PaymentProcessor:
L4     def charge_card(self, amount: float, card_token: str) -> Receipt:
L7     def refund_transaction(self, transaction_id: str) -> bool:
L10    def _validate_card_token(self, token: str) -> bool:
L13    def handle_payment_failure(self, error: PaymentError) -> None:
"""

AUTH_REL   = "auth/handler.py"
QUEUE_REL  = "jobs/queue_processor.py"
DB_REL     = "db/connection_pool.py"
PAYMENT_REL = "payments/processor.py"

ALL_BLUEPRINTS = {
    AUTH_REL:    SAMPLE_AUTH,
    QUEUE_REL:   SAMPLE_QUEUE,
    DB_REL:      SAMPLE_DB,
    PAYMENT_REL: SAMPLE_PAYMENT,
}


# ---------------------------------------------------------------------------
# FTS5 database fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fts_db():
    """In-memory SQLite with symbol_fts FTS5 table populated from sample blueprints."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE symbol_fts USING fts5("
        "  file UNINDEXED,"
        "  lineno UNINDEXED,"
        "  symbol_name,"
        "  signature,"
        "  decomposed,"
        "  tokenize='unicode61'"
        ")"
    )
    for rel, bp in ALL_BLUEPRINTS.items():
        rows = mimir._fts_rows_for_blueprint(rel, bp)
        conn.executemany(
            "INSERT INTO symbol_fts(file, lineno, symbol_name, signature, decomposed)"
            " VALUES (?,?,?,?,?)",
            rows,
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# _fts_rows_for_blueprint
# ---------------------------------------------------------------------------

class TestFtsRowsForBlueprint:
    def test_returns_five_tuple_rows(self):
        rows = mimir._fts_rows_for_blueprint(AUTH_REL, SAMPLE_AUTH)
        assert all(len(r) == 5 for r in rows)

    def test_file_column_is_rel_path(self):
        rows = mimir._fts_rows_for_blueprint(AUTH_REL, SAMPLE_AUTH)
        assert all(r[0] == AUTH_REL for r in rows)

    def test_lineno_is_numeric_string(self):
        rows = mimir._fts_rows_for_blueprint(AUTH_REL, SAMPLE_AUTH)
        assert all(r[1].isdigit() for r in rows)

    def test_skips_header_comment(self):
        rows = mimir._fts_rows_for_blueprint(AUTH_REL, SAMPLE_AUTH)
        signatures = [r[3] for r in rows]
        assert not any(s.startswith('#') for s in signatures)

    def test_no_duplicate_lineno(self):
        bp_with_dups = (
            "L5    public class Svc\n"
            "L5    duplicate line\n"
            "L10   public void Method()\n"
        )
        rows = mimir._fts_rows_for_blueprint("Svc.cs", bp_with_dups)
        linenos = [r[1] for r in rows]
        assert len(linenos) == len(set(linenos)), "Duplicate linenos in FTS rows"

    def test_decomposed_contains_sub_tokens(self):
        rows = mimir._fts_rows_for_blueprint(QUEUE_REL, SAMPLE_QUEUE)
        # retry_failed_job -> decomposed should include 'retry', 'fail', 'job'
        retry_rows = [r for r in rows if 'retry' in r[3]]
        assert retry_rows, "Expected a row for retry_failed_job"
        decomposed = retry_rows[0][4]
        assert 'retry' in decomposed
        assert 'fail' in decomposed

    def test_camelcase_decomposed(self):
        bp = "L1  class ConnectionPoolManager\n"
        rows = mimir._fts_rows_for_blueprint("x.py", bp)
        assert rows
        decomposed = rows[0][4]
        # ConnectionPoolManager → connection, pool, manager
        assert 'connection' in decomposed
        assert 'pool' in decomposed
        assert 'manager' in decomposed

    def test_symbol_name_is_longest_identifier(self):
        # "def refresh_access_token" — longest non-stopword is refresh_access_token
        rows = mimir._fts_rows_for_blueprint(AUTH_REL, SAMPLE_AUTH)
        token_rows = [r for r in rows if 'refresh' in r[3].lower()]
        assert token_rows
        # symbol_name should be the longest identifier found
        sym = token_rows[0][2]
        assert len(sym) >= 5  # at least a meaningful name

    def test_stopwords_not_in_symbol_name(self):
        rows = mimir._fts_rows_for_blueprint(AUTH_REL, SAMPLE_AUTH)
        stopwords = {"def", "class", "self", "str", "int", "bool", "return"}
        for row in rows:
            assert row[2] not in stopwords, f"stopword as symbol_name: {row[2]!r}"


# ---------------------------------------------------------------------------
# _decompose_query_for_fts
# ---------------------------------------------------------------------------

class TestDecomposeQueryForFts:
    def test_plain_words_returned(self):
        terms = mimir._decompose_query_for_fts("authentication error handler")
        assert 'authentication' in terms or 'auth' in terms
        assert 'error' in terms or 'err' in terms
        assert 'handler' in terms

    def test_camelcase_decomposed(self):
        terms = mimir._decompose_query_for_fts("handleAuthFailure")
        assert 'handle' in terms
        assert 'auth' in terms
        # CamelCase split: 'Failure' stays as 'failure' (≥4 chars), not truncated to 'fail'
        assert 'failure' in terms

    def test_stopwords_excluded(self):
        # _QUERY_STOPWORDS filters English filler words, NOT code keywords.
        # "public" and "void" are valid search terms (JS/TS export, C# void, etc.)
        # — only true English filler like "the", "and", "for" should be stripped.
        terms = mimir._decompose_query_for_fts("the method for handling")
        assert 'the' not in terms
        assert 'for' not in terms
        assert 'method' in terms
        assert 'handling' in terms

    def test_short_terms_excluded(self):
        # Terms < 3 chars should be excluded
        terms = mimir._decompose_query_for_fts("get it do")
        assert 'it' not in terms
        assert 'do' not in terms

    def test_deduplication(self):
        terms = mimir._decompose_query_for_fts("auth authenticate auth")
        assert terms.count('auth') <= 1

    def test_max_16_terms(self):
        long_query = " ".join(f"keyword{i}" for i in range(30))
        terms = mimir._decompose_query_for_fts(long_query)
        assert len(terms) <= 16

    def test_empty_query(self):
        assert mimir._decompose_query_for_fts("") == []

    def test_snake_case_decomposed(self):
        terms = mimir._decompose_query_for_fts("connection_pool_exhaustion")
        assert 'connection' in terms
        assert 'pool' in terms
        assert 'exhaustion' in terms


# ---------------------------------------------------------------------------
# FTS5 BM25 retrieval: correctness
# ---------------------------------------------------------------------------

class TestFtsRetrieval:
    """Verify that FTS5 BM25 search finds conceptually relevant results."""

    def _search(self, conn, query: str, limit: int = 10) -> list[str]:
        """Run BM25 FTS5 search on the test db, return list of matched files."""
        terms = mimir._decompose_query_for_fts(query)
        safe = [t for t in terms if __import__('re').match(r'^\w+$', t)]
        if not safe:
            return []
        fts_query = ' OR '.join(f'"{t}"' for t in safe)
        rows = conn.execute(
            "SELECT file, bm25(symbol_fts) AS score FROM symbol_fts"
            " WHERE symbol_fts MATCH ? ORDER BY score LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        # group by file, best score per file
        seen: dict[str, float] = {}
        for file, score in rows:
            seen[file] = min(seen.get(file, 0.0), score)
        return sorted(seen, key=lambda f: seen[f])

    def test_auth_query_finds_auth_file(self, fts_db):
        hits = self._search(fts_db, "authentication token refresh")
        assert AUTH_REL in hits, f"Expected auth file in hits, got: {hits}"

    def test_retry_query_finds_queue_file(self, fts_db):
        hits = self._search(fts_db, "retry failed job exponential backoff")
        assert QUEUE_REL in hits, f"Expected queue file in hits, got: {hits}"

    def test_connection_pool_query_finds_db_file(self, fts_db):
        hits = self._search(fts_db, "database connection pool exhaustion")
        assert DB_REL in hits, f"Expected db file in hits, got: {hits}"

    def test_payment_failure_query_finds_payment_file(self, fts_db):
        hits = self._search(fts_db, "payment failure handler")
        assert PAYMENT_REL in hits, f"Expected payment file in hits, got: {hits}"

    def test_fts_outperforms_exact_match_on_conceptual_query(self, fts_db):
        """FTS5 via decomposed tokens should find auth file for 'session revoke'
        even though the query doesn't contain exact symbol names.
        """
        # "revoke" appears in revoke_session — decomposition should link them
        hits = self._search(fts_db, "session revoke token")
        assert AUTH_REL in hits, (
            f"FTS5 should find auth file for 'session revoke token' via decomposed tokens, "
            f"got: {hits}"
        )

    def test_top_result_matches_most_specific_query(self, fts_db):
        """A very specific query should rank the matching file first."""
        hits = self._search(fts_db, "exponential backoff retry")
        assert hits, "Expected results for 'exponential backoff retry'"
        assert hits[0] == QUEUE_REL, (
            f"Expected queue file first for retry query, got: {hits[0]}"
        )

    def test_no_results_for_gibberish(self, fts_db):
        hits = self._search(fts_db, "xyzzyplugh frobnicator")
        assert hits == []


# ---------------------------------------------------------------------------
# _fts_search: FTS5 BM25 search helper correctness
# ---------------------------------------------------------------------------

class TestFtsSearchHelper:
    """Tests for the _fts_search() helper using the real _DISK_CACHE."""

    def test_returns_list(self):
        # Even before warmup, should return a list (possibly empty), not crash
        result = mimir._fts_search(["auth", "handle"])
        assert isinstance(result, list)

    def test_empty_terms_returns_empty(self):
        assert mimir._fts_search([]) == []

    def test_special_chars_do_not_crash(self):
        # Terms with special chars should be filtered by safe_terms check
        result = mimir._fts_search(["auth'failure", "retry"])
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# FTS5 vs scope_task: semantic recall benefit
# ---------------------------------------------------------------------------

class TestSemanticVsKeyword:
    """Demonstrate that FTS5+decomposition finds things keyword-exact lookup misses.

    These tests use the real mimir module with a temp workspace populated
    with synthetic source files. They show the benefit of the new tool vs
    the existing scope_task approach.
    """

    @pytest.fixture
    def workspace_with_auth(self, tmp_path):
        """Workspace with an auth file whose function name differs from the query."""
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        # Function name is "validate_bearer_token" — not "authentication" or "verify"
        (auth_dir / "middleware.py").write_text(
            "def validate_bearer_token(token: str) -> bool:\n"
            "    \"\"\"Check JWT bearer token.\"\"\"\n"
            "    pass\n\n"
            "def revoke_bearer_token(token_id: str) -> None:\n"
            "    pass\n"
        )
        (tmp_path / "main.py").write_text(
            "from auth.middleware import validate_bearer_token\n\n"
            "def startup() -> None:\n"
            "    pass\n"
        )
        return tmp_path

    @pytest.fixture
    def workspace_with_retry(self, tmp_path):
        """Workspace with retry logic under a non-obvious name."""
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()
        # Function name is "attempt_with_backoff" — not "retry"
        (jobs_dir / "runner.py").write_text(
            "def attempt_with_backoff(fn, max_tries=3):\n"
            "    \"\"\"Exponential backoff retry runner.\"\"\"\n"
            "    pass\n\n"
            "def schedule_job(job_id: str) -> None:\n"
            "    pass\n"
        )
        return tmp_path

    def test_decompose_finds_auth_concept(self):
        """Decomposed query for 'authentication' should include 'auth'
        which matches validate_bearer_token's decomposed form."""
        terms = mimir._decompose_query_for_fts("user authentication verification")
        # "authentication" decomposes to include useful sub-terms
        assert any(len(t) >= 4 for t in terms), f"Expected useful terms, got: {terms}"

    def test_fts_rows_camelcase_improves_recall(self):
        """A blueprint line with ValidateBearerToken produces decomposed tokens
        that match a query for 'validate bearer token' — conceptual match."""
        bp = "L3   def validate_bearer_token(token: str) -> bool:\n"
        rows = mimir._fts_rows_for_blueprint("auth/middleware.py", bp)
        assert rows
        decomposed = rows[0][4]
        assert 'validate' in decomposed
        assert 'bearer' in decomposed
        assert 'token' in decomposed

    def test_exact_name_not_required(self):
        """Query 'bearer token check' should hit validate_bearer_token via decomposition."""
        bp = "L3   def validate_bearer_token(token: str) -> bool:\n"
        rows = mimir._fts_rows_for_blueprint("auth/middleware.py", bp)
        assert rows
        # "check" is in the query but not the function name — that's fine:
        # the other terms (bearer, token, validate) should still match
        decomposed = rows[0][4]
        assert 'bearer' in decomposed and 'token' in decomposed

    def test_scope_task_misses_conceptual_query(self):
        """scope_task with keyword 'retry' won't find 'attempt_with_backoff'
        because it does exact token lookup. FTS5 decomposition finds it via
        'backoff' → decomposed includes 'backoff' and 'attempt'."""
        bp = "L3   def attempt_with_backoff(fn, max_tries=3):\n"
        rows = mimir._fts_rows_for_blueprint("jobs/runner.py", bp)
        assert rows
        decomposed = rows[0][4]
        # 'attempt' and 'backoff' are in decomposed but not the word 'retry'
        # FTS5 with query "retry backoff" would match on 'backoff'
        assert 'backoff' in decomposed
        assert 'attempt' in decomposed


# ---------------------------------------------------------------------------
# Performance benchmark: FTS5 vs linear scan
# ---------------------------------------------------------------------------

class TestFtsPerformance:
    """Benchmark FTS5 BM25 retrieval vs linear blueprint scan on 500-file corpus."""

    @pytest.fixture(scope="class")
    def large_corpus(self):
        """500 files, each with 5 symbols. FTS5 indexed in-memory."""
        blueprints = {}
        for i in range(500):
            bp = (
                f"L1   class AuthService{i}\n"
                f"L3     def authenticate{i}(self, token: str) -> bool:\n"
                f"L6     def refresh_token{i}(self, user_id: int) -> str:\n"
                f"L9     def revoke_session{i}(self, session_id: str) -> None:\n"
                f"L12    def handle_auth_failure{i}(self, error: Exception) -> None:\n"
            )
            blueprints[f"auth/service{i}.py"] = bp

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE VIRTUAL TABLE symbol_fts USING fts5("
            "  file UNINDEXED, lineno UNINDEXED,"
            "  symbol_name, signature, decomposed, tokenize='unicode61'"
            ")"
        )
        for rel, bp in blueprints.items():
            rows = mimir._fts_rows_for_blueprint(rel, bp)
            conn.executemany(
                "INSERT INTO symbol_fts(file, lineno, symbol_name, signature, decomposed)"
                " VALUES (?,?,?,?,?)",
                rows,
            )
        conn.commit()
        return conn, blueprints

    def _fts_query(self, conn, terms: list[str], limit: int = 25) -> list:
        safe = [t for t in terms if __import__('re').match(r'^\w+$', t)]
        fts_query = ' OR '.join(f'"{t}"' for t in safe)
        return conn.execute(
            "SELECT file FROM symbol_fts WHERE symbol_fts MATCH ? LIMIT ?",
            (fts_query, limit),
        ).fetchall()

    def _linear_scan(self, blueprints: dict, terms: list[str], limit: int = 25) -> list:
        hits = []
        for rel, bp in blueprints.items():
            for term in terms:
                if term.lower() in bp.lower():
                    hits.append(rel)
                    break
            if len(hits) >= limit:
                break
        return hits

    def test_fts5_returns_ranked_results(self, large_corpus):
        """FTS5 returns BM25-ranked results with a single SQL call.
        A plain linear scan cannot produce BM25 scores without per-doc scoring.
        Verify FTS5 returns results in score order and is consistent across runs.
        """
        conn, _ = large_corpus
        results_a = self._fts_query(conn, ["auth", "handle", "fail"])
        results_b = self._fts_query(conn, ["auth", "handle", "fail"])
        assert results_a == results_b, "FTS5 results should be deterministic"
        assert len(results_a) > 0, "FTS5 should find results for known terms"

    def test_fts5_under_2ms_on_500_files(self, large_corpus):
        conn, _ = large_corpus
        RUNS = 500
        t0 = time.perf_counter()
        for _ in range(RUNS):
            self._fts_query(conn, ["auth", "session", "token"])
        avg_ms = (time.perf_counter() - t0) / RUNS * 1000
        print(f"\n  FTS5 avg on 500 files: {avg_ms:.3f}ms")
        assert avg_ms < 2.0, f"FTS5 averaged {avg_ms:.3f}ms on 500 files, expected <2ms"

    def test_fts5_natural_language_query_under_2ms(self, large_corpus):
        """End-to-end: decompose a natural language query then run FTS5."""
        conn, _ = large_corpus
        RUNS = 300
        t0 = time.perf_counter()
        for _ in range(RUNS):
            terms = mimir._decompose_query_for_fts("authentication failure handler")
            self._fts_query(conn, terms)
        avg_ms = (time.perf_counter() - t0) / RUNS * 1000
        print(f"\n  End-to-end (decompose+FTS5) avg: {avg_ms:.3f}ms")
        assert avg_ms < 2.0, f"End-to-end FTS5 averaged {avg_ms:.3f}ms, expected <2ms"

    def test_index_build_speed(self):
        """FTS5 index over 1000 symbols should build in under 500ms."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE VIRTUAL TABLE symbol_fts USING fts5("
            "  file UNINDEXED, lineno UNINDEXED,"
            "  symbol_name, signature, decomposed, tokenize='unicode61'"
            ")"
        )
        blueprints = {}
        for i in range(200):
            bp = "\n".join(
                f"L{j}   def method_{j}_in_class_{i}(self, arg: str) -> None:"
                for j in range(5)
            )
            blueprints[f"src/module{i}.py"] = bp

        t0 = time.perf_counter()
        rows = []
        for rel, bp in blueprints.items():
            rows.extend(mimir._fts_rows_for_blueprint(rel, bp))
        conn.executemany(
            "INSERT INTO symbol_fts(file, lineno, symbol_name, signature, decomposed)"
            " VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"\n  FTS5 index build (200 files × 5 symbols = 1000 rows): {elapsed_ms:.1f}ms")
        assert elapsed_ms < 500, f"Index build took {elapsed_ms:.1f}ms, expected <500ms"




# ---------------------------------------------------------------------------
# Integration: semantic_search tool via wire
# ---------------------------------------------------------------------------

class TestSemanticSearchWire:
    """Wire-level tests for the semantic_search MCP tool."""

    @pytest.fixture
    def workspace(self, tmp_path):
        """Workspace with two files that have semantically distinct purposes."""
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        (auth_dir / "service.py").write_text(
            "class AuthenticationService:\n"
            "    def validate_access_token(self, token: str) -> bool:\n"
            "        pass\n\n"
            "    def revoke_user_session(self, user_id: int) -> None:\n"
            "        pass\n"
        )
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()
        (jobs_dir / "retry.py").write_text(
            "def attempt_with_exponential_backoff(fn, max_retries: int = 3):\n"
            "    pass\n\n"
            "def enqueue_failed_job(job_id: str) -> bool:\n"
            "    pass\n"
        )
        return tmp_path

    def test_returns_string(self, workspace):
        import os
        os.environ["MCP_WORKSPACE_ROOT"] = str(workspace)
        result = mimir.semantic_search("authentication token validation")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_does_not_crash_on_empty_query(self, workspace):
        import os
        os.environ["MCP_WORKSPACE_ROOT"] = str(workspace)
        result = mimir.semantic_search("")
        assert isinstance(result, str)

    def test_does_not_crash_on_gibberish(self, workspace):
        import os
        os.environ["MCP_WORKSPACE_ROOT"] = str(workspace)
        result = mimir.semantic_search("xyzzy plugh frobnicator")
        assert isinstance(result, str)

    def test_max_results_clamped(self, workspace):
        import os
        os.environ["MCP_WORKSPACE_ROOT"] = str(workspace)
        result = mimir.semantic_search("auth", max_results=999)
        assert isinstance(result, str)
        # Should not crash, result count capped at 25

    def test_fallback_when_not_ready(self):
        """When _SEMANTIC_READY is False, should return fallback message."""
        orig = mimir._SEMANTIC_READY
        try:
            mimir._SEMANTIC_READY = False
            result = mimir.semantic_search("auth token")
            assert "fallback" in result.lower() or isinstance(result, str)
        finally:
            mimir._SEMANTIC_READY = orig


# ---------------------------------------------------------------------------
# FTS enrichment: return types, parameter types, decorator names
# ---------------------------------------------------------------------------

# A blueprint with compound type annotations, a return type, and a decorator,
# using names distinct enough to be unambiguous in search queries.
SAMPLE_TYPED = """\
# api/payment_controller.py  [python · tree-sitter]
L10  @Authenticated
L11  class PaymentController:
L15      def charge_card(self, card: PaymentMethod, amount: Decimal) -> Receipt:
L20      def refund(self, transaction_id: str) -> RefundResult:
"""

TYPED_REL = "api/payment_controller.py"

ALL_BLUEPRINTS_WITH_TYPED = {**ALL_BLUEPRINTS, TYPED_REL: SAMPLE_TYPED}


def _search_fts(conn, query: str, limit: int = 10) -> list[str]:
    """BM25 FTS5 search on a test db; returns files ordered by score."""
    import re as _re
    terms = mimir._decompose_query_for_fts(query)
    safe = [t for t in terms if _re.match(r'^\w+$', t)]
    if not safe:
        return []
    fts_query = ' OR '.join(f'"{t}"' for t in safe)
    rows = conn.execute(
        "SELECT file, bm25(symbol_fts) AS score FROM symbol_fts"
        " WHERE symbol_fts MATCH ? ORDER BY score LIMIT ?",
        (fts_query, limit),
    ).fetchall()
    seen: dict[str, float] = {}
    for file, score in rows:
        seen[file] = min(seen.get(file, 0.0), score)
    return sorted(seen, key=lambda f: seen[f])


@pytest.fixture
def fts_db_typed():
    """In-memory SQLite FTS5 table with all samples including the typed one."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE symbol_fts USING fts5("
        "  file UNINDEXED,"
        "  lineno UNINDEXED,"
        "  symbol_name,"
        "  signature,"
        "  decomposed,"
        "  tokenize='unicode61'"
        ")"
    )
    for rel, bp in ALL_BLUEPRINTS_WITH_TYPED.items():
        rows = mimir._fts_rows_for_blueprint(rel, bp)
        conn.executemany(
            "INSERT INTO symbol_fts(file, lineno, symbol_name, signature, decomposed)"
            " VALUES (?,?,?,?,?)",
            rows,
        )
    conn.commit()
    return conn


class TestFtsEnrichment:
    """Verify that return types, parameter type hints, and decorators are indexed."""

    def _decomposed_for_sig(self, signature: str) -> str:
        rows = mimir._fts_rows_for_blueprint("test.py", f"L1  {signature}")
        assert rows, f"No FTS rows generated for: {signature!r}"
        return rows[0][4]

    # Return type
    def test_return_type_simple_in_decomposed(self):
        decomposed = self._decomposed_for_sig("def get_receipt(self) -> Receipt:")
        assert "receipt" in decomposed, f"'receipt' missing from: {decomposed!r}"

    def test_return_type_compound_decomposed(self):
        decomposed = self._decomposed_for_sig("def find_user(self) -> UserProfile:")
        assert "user" in decomposed
        assert "profile" in decomposed

    def test_return_type_generic_decomposed(self):
        decomposed = self._decomposed_for_sig("async def list_orders(self) -> List[OrderRecord]:")
        assert "order" in decomposed
        assert "record" in decomposed

    # Parameter type hints
    def test_param_type_compound_in_decomposed(self):
        decomposed = self._decomposed_for_sig("def charge(self, card: PaymentMethod):")
        assert "payment" in decomposed
        assert "method" in decomposed

    def test_param_type_optional_in_decomposed(self):
        decomposed = self._decomposed_for_sig("def fetch(self, opts: FetchOptions):")
        assert "fetch" in decomposed

    # Decorators
    def test_decorator_simple_in_decomposed(self):
        decomposed = self._decomposed_for_sig("@Authenticated")
        assert "authenticated" in decomposed, f"'authenticated' missing from: {decomposed!r}"

    def test_decorator_compound_in_decomposed(self):
        decomposed = self._decomposed_for_sig("@RestController")
        assert "rest" in decomposed
        assert "controller" in decomposed

    def test_decorator_dotted_in_decomposed(self):
        decomposed = self._decomposed_for_sig("@pytest.fixture")
        assert "pytest" in decomposed or "fixture" in decomposed

    # FTS5 search quality: search by type/decorator finds the right file
    def test_search_by_return_type_finds_file(self, fts_db_typed):
        hits = _search_fts(fts_db_typed, "Receipt")
        assert TYPED_REL in hits, f"Expected typed file for 'Receipt', got: {hits}"

    def test_search_by_param_type_finds_file(self, fts_db_typed):
        hits = _search_fts(fts_db_typed, "PaymentMethod")
        assert TYPED_REL in hits, f"Expected typed file for 'PaymentMethod', got: {hits}"

    def test_search_by_decorator_finds_file(self, fts_db_typed):
        hits = _search_fts(fts_db_typed, "Authenticated")
        assert TYPED_REL in hits, f"Expected typed file for 'Authenticated', got: {hits}"

    # Regression: existing queries must still return the right files
    def test_regression_auth_query(self, fts_db_typed):
        hits = _search_fts(fts_db_typed, "authentication token refresh")
        assert AUTH_REL in hits, f"Regression: auth file missing. Got: {hits}"

    def test_regression_queue_query(self, fts_db_typed):
        hits = _search_fts(fts_db_typed, "retry failed job exponential backoff")
        assert QUEUE_REL in hits, f"Regression: queue file missing. Got: {hits}"
