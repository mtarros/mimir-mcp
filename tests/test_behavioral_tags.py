"""
Tests for behavioral tag extraction (#tags blueprint section).

A method named process_payload() gives no hint it touches URLSession or
Keychain internally -- blueprints strip bodies, so that context is normally
invisible. A curated set of framework/infra identifiers, matched against
every identifier-kind leaf node the SAME tree-sitter walk() pass in
_extract_tree_sitter already visits (piggybacking on the walk _literal_row/
#strings already does), surfaces that as a "#tags" section. Tags flow
through the EXISTING _index_blueprint_rows/_fts_rows_for_blueprint L<n>-line
tokenizers with zero code changes there -- these tests confirm that claim
end to end, not just that the blueprint text looks right.
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


def _tags_section(bp: str) -> list[str]:
    if "#tags" not in bp:
        return []
    return [l for l in bp.split("#tags", 1)[1].splitlines() if l.startswith("L")]


# ---------------------------------------------------------------------------
# Per-language tag extraction — covers both leaf-kind paths
# (_is_tag_leaf_kind: "identifier" substring match, and the "constant" kind
# needed for Ruby's capitalized module/class references).
# ---------------------------------------------------------------------------

class TestSwiftTags:
    def test_urlsession_tagged_inside_generic_method(self):
        bp = _blueprint_for(
            "class Sync {\n"
            "    func commitPayload() {\n"
            "        somethingElse()\n"
            "        URLSession.shared.dataTask()\n"
            "    }\n"
            "}\n",
            ".swift",
        )
        rows = _tags_section(bp)
        assert any("urlsession" in r for r in rows), bp
        # The method's own signature line has no hint of URLSession.
        sig_lines = [l for l in bp.splitlines() if "commitPayload" in l]
        assert sig_lines and "urlsession" not in sig_lines[0].lower()


class TestCSharpTags:
    def test_dbcontext_tagged(self):
        bp = _blueprint_for(
            "public class Sync {\n"
            "    public void Run() {\n"
            "        var ctx = new DbContext();\n"
            "    }\n"
            "}\n",
            ".cs",
        )
        rows = _tags_section(bp)
        assert any("dbcontext" in r for r in rows), bp


class TestPythonTags:
    def test_sqlalchemy_tagged(self):
        bp = _blueprint_for(
            "import sqlalchemy\n\n"
            "class Worker:\n"
            "    def process_payload(self):\n"
            "        return sqlalchemy.create_engine('sqlite://')\n",
            ".py",
        )
        rows = _tags_section(bp)
        assert any("sqlalchemy" in r for r in rows), bp


class TestKotlinTags:
    def test_coroutinescope_tagged(self):
        # A bare call statement, not a `val`/`var` declaration -- a
        # declaration line gets captured whole as its own signature line by
        # _is_def_node, which already makes the anchor text visible without
        # needing a #tags entry (see TestTagExtractionBehavior's dedup test).
        bp = _blueprint_for(
            "class Sync {\n"
            "    fun run() {\n"
            "        prepare()\n"
            "        CoroutineScope(Dispatchers.IO).launch { doWork() }\n"
            "    }\n"
            "}\n",
            ".kt",
        )
        rows = _tags_section(bp)
        assert any("coroutinescope" in r for r in rows), bp


class TestRubyTags:
    def test_sidekiq_constant_kind_tagged(self):
        """Ruby's capitalized module/class refs (Sidekiq) are a distinct
        'constant' leaf kind, not 'identifier' -- exercises the second half
        of _is_tag_leaf_kind's condition."""
        bp = _blueprint_for(
            "class Worker\n"
            "  def process\n"
            "    Sidekiq.logger.info('go')\n"
            "  end\n"
            "end\n",
            ".rb",
        )
        rows = _tags_section(bp)
        assert any("sidekiq" in r for r in rows), bp


# ---------------------------------------------------------------------------
# Behavior of the extraction itself
# ---------------------------------------------------------------------------

class TestTagExtractionBehavior:
    def test_dedup_by_tag_not_by_line(self):
        """A file calling URLSession many times should produce ONE #tags
        row, not one per call site. Bare call statements (not declarations)
        so each call site is genuinely hidden, not already surfaced as its
        own signature line."""
        bp = _blueprint_for(
            "class Net {\n"
            "    func a() {\n"
            "        URLSession.shared.dataTask()\n"
            "    }\n"
            "    func b() {\n"
            "        URLSession.shared.dataTask()\n"
            "    }\n"
            "    func c() {\n"
            "        URLSession.shared.dataTask()\n"
            "    }\n"
            "}\n",
            ".swift",
        )
        rows = _tags_section(bp)
        urlsession_rows = [r for r in rows if "urlsession" in r]
        assert len(urlsession_rows) == 1, rows

    def test_multiple_distinct_anchors_all_captured(self):
        bp = _blueprint_for(
            "class Creds {\n"
            "    func commitPayload() {\n"
            "        URLSession.shared.dataTask()\n"
            "        Keychain.save()\n"
            "    }\n"
            "}\n",
            ".swift",
        )
        rows = _tags_section(bp)
        assert any("urlsession" in r for r in rows), rows
        assert any("keychain" in r for r in rows), rows

    def test_no_false_positive_on_unrelated_code(self):
        bp = _blueprint_for(
            "class Calculator {\n"
            "    func add(a: Int, b: Int) -> Int {\n"
            "        return a + b\n"
            "    }\n"
            "}\n",
            ".swift",
        )
        assert "#tags" not in bp

    def test_no_duplicate_row_when_anchor_shares_a_signature_line(self):
        """A local declaration classified as its own def-node (e.g. Swift's
        `let session = URLSession.shared`) already carries the anchor text
        in its OWN signature line -- tagging that same line again would
        just duplicate the file:line in verify_symbol_existence results."""
        bp = _blueprint_for(
            "class Creds {\n"
            "    func commitPayload() {\n"
            "        let session = URLSession.shared\n"
            "    }\n"
            "}\n",
            ".swift",
        )
        sig_line = next(l for l in bp.splitlines() if "URLSession" in l and l.startswith("L"))
        rows = _tags_section(bp)
        # The anchor's line number must not also appear in the tags section.
        sig_lineno = sig_line.split()[0]
        assert not any(r.split()[0] == sig_lineno for r in rows), (sig_line, rows)

    def test_bound_on_tags_per_file(self):
        # Build a file with more distinct anchors than _TAG_MAX_PER_FILE.
        anchors = sorted(mimir._BEHAVIORAL_ANCHORS)[: mimir._TAG_MAX_PER_FILE + 5]
        body = "\n".join(f"    {a}()" for a in anchors)
        bp = _blueprint_for(
            "class Kitchen {\n    func everything() {\n" + body + "\n    }\n}\n",
            ".swift",
        )
        rows = _tags_section(bp)
        assert len(rows) <= mimir._TAG_MAX_PER_FILE


# ---------------------------------------------------------------------------
# Non-pollution: tags must stay invisible to top-level symbol listings
# ---------------------------------------------------------------------------

class TestTagsDoNotPolluteTopLevelSymbols:
    def test_toplevel_names_excludes_tags(self):
        bp = _blueprint_for(
            "class Creds {\n"
            "    func commitPayload() {\n"
            "        URLSession.shared.dataTask()\n"
            "        Keychain.save()\n"
            "    }\n"
            "}\n",
            ".swift",
        )
        names = mimir._toplevel_names_from_blueprint(bp)
        assert "urlsession" not in [n.lower() for n in names]
        assert "keychain" not in [n.lower() for n in names]
        assert "Creds" in names


# ---------------------------------------------------------------------------
# End-to-end: tags must actually reach symbols/symbol_fts (the load-bearing
# claim of this whole feature) via the EXISTING, unmodified tokenizers.
# ---------------------------------------------------------------------------

class TestTagsFlowThroughToSymbolIndex:
    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, monkeypatch):
        (tmp_path / "creds.swift").write_text(
            "class CredentialsManager {\n"
            "    func commitPayload() {\n"
            "        let x = 1\n"
            "        URLSession.shared.dataTask()\n"
            "        Keychain.save()\n"
            "    }\n"
            "}\n"
        )
        monkeypatch.setattr(mimir, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(mimir, "_FILE_LIST", [])
        monkeypatch.setattr(mimir, "_FILE_LIST_TS", 0.0)
        monkeypatch.setattr(mimir, "_CACHE", mimir._CACHE.__class__())
        return tmp_path

    def test_index_blueprint_rows_contains_tag_tokens(self):
        bp = mimir._build_blueprint(mimir.WORKSPACE_ROOT / "creds.swift")
        rows = mimir._index_blueprint_rows("creds.swift", bp)
        tokens = {r[0] for r in rows}
        assert "urlsession" in tokens, tokens
        assert "keychain" in tokens, tokens

    def test_fts_rows_contains_tag_tokens(self):
        bp = mimir._build_blueprint(mimir.WORKSPACE_ROOT / "creds.swift")
        rows = mimir._fts_rows_for_blueprint("creds.swift", bp)
        # rows are (file, lineno, symbol_name, signature, decomposed) or similar;
        # check the raw text of any field for the tag substrings.
        joined = " ".join(str(f) for row in rows for f in row).lower()
        assert "urlsession" in joined, rows
        assert "keychain" in joined, rows

    def test_verify_symbol_existence_finds_tagged_method_by_framework_name(self):
        mimir._warm_cache()
        out = mimir.verify_symbol_existence("urlsession")
        assert "FOUND" in out
        assert "creds.swift" in out

    def test_generic_method_name_hides_no_context_the_tag_reveals(self):
        """The actual value proposition: a plain-English/framework query
        finds the file even though 'commitPayload' as a name gives no hint."""
        mimir._warm_cache()
        out = mimir.locate("keychain")
        assert "creds.swift" in out
