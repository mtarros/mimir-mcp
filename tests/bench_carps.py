"""Accuracy + speed benchmark for Carps (~8280 C# files).

Run:  python tests/bench_carps.py

For each scope_task query we check whether the top-3 returned files actually
contain the expected keywords (ground-truth precision check). Also validates
that scope_task symbol suggestions are real symbol names (not keywords).
"""
import asyncio, os, re, time
from pathlib import Path
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

PYTHON    = str(Path.home() / ".local/pipx/venvs/mimir-mcp/bin/python")
MIMIR_PY  = str(Path.home() / "Projects/mimir-mcp/mimir.py")
WORKSPACE = Path.home() / "Projects/Carps"

# (task_description, keywords that MUST appear in >=1 top-3 file, max_files)
TEST_CASES = [
    ("user authentication login token",
     ["authentication", "login", "token", "identity", "jwt", "JWT"],
     5),
    ("database connection repository base class",
     ["Repository", "repository", "connection", "Connection", "DbContext"],
     5),
    ("airport screen controller api endpoint",
     ["AirportScreen", "Controller", "airport"],
     5),
    ("notification send email smtp",
     ["Email", "email", "Notification", "notification", "smtp", "SMTP"],
     5),
    ("permission role access control",
     ["Permission", "permission", "Role", "role", "access", "Access"],
     5),
    ("load test performance measurement results",
     ["LoadTest", "load", "performance", "Performance", "result"],
     5),
    ("database seeding scenario migration",
     ["Seed", "seed", "migration", "Migration", "scenario", "Scenario"],
     5),
    ("integration test fixture setup",
     ["integration", "Integration", "fixture", "Fixture", "Test"],
     5),
]

BAD_SYMBOLS = frozenset({
    "async", "await", "static", "readonly", "string", "int", "bool", "void",
    "Task", "List", "object", "var", "new", "override", "virtual", "abstract",
    "sealed", "partial", "const", "event", "delegate", "public", "private",
    "protected", "internal", "extern", "unsafe", "namespace", "class",
    "interface", "struct", "enum", "record", "def", "function",
})

def make_transport():
    return StdioTransport(
        command=PYTHON, args=[MIMIR_PY],
        env={**os.environ, "MCP_WORKSPACE_ROOT": str(WORKSPACE)},
    )

def txt(r): return r.content[0].text

async def wait_warmup(client, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = txt(await client.call_tool("get_status", {}))
        if "warmup:" in s and "complete" in s.split("warmup:")[1].split("\n")[0]:
            return s
        await asyncio.sleep(2)
    return None

def parse_ranked_files(output: str) -> list[str]:
    files = []
    in_section = False
    for line in output.splitlines():
        if "## Ranked files" in line:
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                break
            m = re.match(r'\s*\d+\.\s+([\w./\\-]+\.\w+)', line)
            if m:
                files.append(m.group(1).strip())
    return files

def parse_suggested_symbols(output: str) -> list[tuple[str, str]]:
    pairs = []
    for line in output.splitlines():
        m = re.search(r'get_symbol\("([^"]+)",\s*"([^"]+)"\)', line)
        if m:
            pairs.append((m.group(1), m.group(2)))
    return pairs

def file_contains_any(rel_path: str, keywords: list[str]) -> tuple[bool, str]:
    try:
        content = (WORKSPACE / rel_path).read_text(encoding='utf-8', errors='replace')
        for kw in keywords:
            if kw in content:
                return True, kw
    except OSError:
        pass
    return False, ""

async def main():
    print(f"\n=== Carps accuracy + speed benchmark ===\n")
    async with Client(make_transport()) as client:
        print("Waiting for warm-up...", end="", flush=True)
        t0 = time.perf_counter()
        status = await wait_warmup(client)
        wu_s = time.perf_counter() - t0
        if not status:
            print(" TIMEOUT"); return
        print(f" {wu_s:.1f}s")
        for line in status.splitlines():
            if any(k in line for k in ("source_files", "blueprints", "warmup")):
                print(" ", line.strip())

        # ── scope_task: speed + precision ────────────────────────────────────
        print(f"\n── scope_task ───────────────────────────────────────────────────────")
        print(f"  {'Task':<42} {'ms':>5}  {'top-3':>5}  {'matched kw'}")
        print(f"  {'-'*42}  {'-'*5}  {'-'*5}  {'-'*18}")

        total_hits = total_files = bad_syms = total_syms = 0

        for task, keywords, max_files in TEST_CASES:
            t0 = time.perf_counter()
            r = await client.call_tool("scope_task", {"task": task, "max_files": max_files})
            ms = (time.perf_counter() - t0) * 1000
            out = txt(r)
            files = parse_ranked_files(out)
            suggestions = parse_suggested_symbols(out)

            checked = files[:3]
            hits, kw_hit = 0, ""
            for f in checked:
                ok, kw = file_contains_any(f, keywords)
                if ok:
                    hits += 1
                    if not kw_hit: kw_hit = kw

            bad = [s for _, s in suggestions if s in BAD_SYMBOLS]
            bad_syms += len(bad)
            total_syms += len(suggestions)
            total_hits += hits
            total_files += len(checked)

            prec = f"{hits}/{len(checked)}"
            note = f"  ⚠ bad syms: {bad}" if bad else ""
            print(f"  {task[:42]:<42} {ms:5.0f}  {prec:>5}  {kw_hit}{note}")

        overall = total_hits / total_files * 100 if total_files else 0
        print(f"\n  File precision:    {total_hits}/{total_files} = {overall:.0f}%")
        print(f"  Symbol quality:    {total_syms - bad_syms}/{total_syms} valid ({bad_syms} keyword leaks)")

        # ── find_callers ──────────────────────────────────────────────────────
        print(f"\n── find_callers ─────────────────────────────────────────────────────")
        print(f"  {'Symbol':<20} {'ms':>5}  {'results':>7}  {'sample match'}")
        print(f"  {'-'*20}  {'-'*5}  {'-'*7}  {'-'*30}")
        for sym, expect_substr in [("GetById", "GetById"), ("Authorize", "Authorize"), ("SaveChanges", "SaveChanges")]:
            t0 = time.perf_counter()
            r = await client.call_tool("find_callers", {"symbol_name": sym})
            ms = (time.perf_counter() - t0) * 1000
            out = txt(r)
            lines = [l for l in out.splitlines() if l.strip()]
            hits = sum(1 for l in lines if expect_substr in l)
            sample = next((l.strip()[:50] for l in lines if expect_substr in l), "(none)")
            print(f"  {sym:<20} {ms:5.0f}  {hits:>7}  {sample}")

        # ── get_context ───────────────────────────────────────────────────────
        print(f"\n── get_context ──────────────────────────────────────────────────────")
        print(f"  {'Task':<42} {'ms':>5}  {'files':>5}  sym quality")
        print(f"  {'-'*42}  {'-'*5}  {'-'*5}  {'-'*15}")
        for task, keywords in [
            ("user authentication login token", ["authentication", "login", "token", "identity"]),
            ("database repository connection base class", ["Repository", "repository", "Connection", "connection"]),
        ]:
            t0 = time.perf_counter()
            r = await client.call_tool("get_context", {"task": task})
            ms = (time.perf_counter() - t0) * 1000
            out = txt(r)
            files = parse_ranked_files(out)
            suggestions = parse_suggested_symbols(out)
            bad = [s for _, s in suggestions if s in BAD_SYMBOLS]
            quality = f"{len(suggestions)-len(bad)}/{len(suggestions)}"
            if bad: quality += f"  ⚠ {bad[:2]}"
            print(f"  {task[:42]:<42} {ms:5.0f}  {len(files):5}  {quality}")

asyncio.run(main())
