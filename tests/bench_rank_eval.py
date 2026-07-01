"""Ranking-accuracy eval for scope_task / semantic_search against real repos.

Ground-truth cases: each query has one or more acceptable expected files
(path substrings). We record the best rank of any expected file in the
top-10, then report MRR, top-1/top-5 hit rates, and latency percentiles.

Run:  python tests/bench_rank_eval.py                # Carps + Topcat
      python tests/bench_rank_eval.py --repo carps   # one repo only
      python tests/bench_rank_eval.py --warmup-only --workspace ~/Projects/Shlonak

Compare output before/after ranking changes — the 'ticket' cases are the
ones documented as failing in HowTo.md; 'guard' cases must never regress.
"""
import argparse, asyncio, os, re, statistics, time
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

PYTHON   = str(Path.home() / ".local/pipx/venvs/mimir-mcp/bin/python")
MIMIR_PY = str(Path(__file__).resolve().parent.parent / "mimir.py")

CARPS  = Path.home() / "Projects/Carps"
TOPCAT = Path.home() / "Projects/Topcat"

# (kind, tool, query, [acceptable expected path substrings])
#   kind: 'ticket' = documented failing case, 'guard' = must-not-regress case
CASES: dict[str, list[tuple[str, str, str, list[str]]]] = {
    "carps": [
        # Rollbar retry ticket (HowTo TODO #1): exact error strings from the ticket.
        # Both files are correct answers — one throws the retry message, the
        # other throws the (interpolated) ConnectionId message.
        ("ticket", "scope_task",
         "Rollbar error: Maximum retry attempts exceeded. "
         "ConnectionId not found for the operativeId",
         ["Services/BaseNotificationService.cs",
          "Services/DeviceRealTimeNotificationService.cs"]),
        ("ticket", "semantic_search",
         "Maximum retry attempts exceeded ConnectionId not found for the operativeId",
         ["Services/BaseNotificationService.cs",
          "Services/DeviceRealTimeNotificationService.cs"]),
        # Generic business-vocabulary ticket (HowTo TODO #2): right file was
        # present but buried below files matching common words.
        ("ticket", "scope_task",
         "Unavailable Types are set against a category but users should only "
         "see the Categories and Teams that they have access to",
         ["Common/Services/UnavailableReasonsService.cs",
          "Repository/Common/UnavailableTypeRepository.cs",
          "Repository/Common/UnavailableTypeCategoryRepository.cs"]),
        # Precise-identifier queries (currently working) — regression guards.
        ("guard", "scope_task",
         "MyCurrentViewModel StartOrStopAutoRefresh refresh interval",
         ["UI/ViewModels/MyCurrentViewModel.cs"]),
        ("guard", "scope_task",
         "JobCancelled JobPaused IRecipient MyCurrentViewModel",
         ["UI/ViewModels/MyCurrentViewModel.cs"]),
        ("guard", "scope_task",
         "notification send email smtp",
         ["Email", "Notification"]),
    ],
    "topcat": [
        ("guard", "scope_task",
         "CompletedAuditFragment onCreateOptionsMenu completed audits list",
         ["CompletedAuditFragment.java"]),
        ("guard", "scope_task",
         "SessionTimeoutWindow handleSessionTimeout",
         ["SessionTimeoutWindow.swift"]),
        ("ticket", "scope_task",
         "session times out after user interaction stops in the iOS app",
         ["SessionTimeoutWindow.swift"]),
    ],
}

WORKSPACES = {"carps": CARPS, "topcat": TOPCAT}


def make_transport(workspace: Path) -> StdioTransport:
    return StdioTransport(
        command=PYTHON, args=[MIMIR_PY],
        env={**os.environ, "MCP_WORKSPACE_ROOT": str(workspace)},
    )


def txt(r) -> str:
    return r.content[0].text


async def wait_warmup(client, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = txt(await client.call_tool("get_status", {}))
        if "warmup:" in s and "complete" in s.split("warmup:")[1].split("\n")[0]:
            return s
        await asyncio.sleep(2)
    return None


_RANKED_LINE_RE = re.compile(r'\s*\d+\.\s+([\w./\\-]+\.\w+)')


def parse_ranked_files(output: str) -> list[str]:
    files, in_section = [], False
    for line in output.splitlines():
        if "## Ranked files" in line or line.startswith("## Results"):
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                in_section = False
                continue
            m = _RANKED_LINE_RE.match(line)
            if m:
                files.append(m.group(1).strip())
    if not files:
        # semantic_search: numbered "N. path" lines at top level, no ## header
        for line in output.splitlines():
            m = re.match(r'\d+\.\s+([\w./\\-]+\.\w+)', line)
            if m:
                files.append(m.group(1).strip())
    return files


def best_rank(files: list[str], expected: list[str]) -> int:
    """1-based rank of first file matching any expected substring; 0 = miss."""
    for i, f in enumerate(files, 1):
        norm = f.replace("\\", "/")
        if any(exp in norm for exp in expected):
            return i
    return 0


async def run_repo(repo: str, workspace: Path) -> list[dict]:
    results = []
    print(f"\n=== {repo}  ({workspace}) ===")
    async with Client(make_transport(workspace)) as client:
        print("  warm-up...", end="", flush=True)
        t0 = time.perf_counter()
        status = await wait_warmup(client)
        print(f" {time.perf_counter() - t0:.1f}s")
        if not status:
            print("  WARMUP TIMEOUT — skipping repo")
            return results

        print(f"  {'kind':<7} {'tool':<16} {'rank':>4}  {'ms':>6}  query")
        print(f"  {'-'*7} {'-'*16} {'-'*4}  {'-'*6}  {'-'*46}")
        for kind, tool, query, expected in CASES[repo]:
            if tool == "scope_task":
                params = {"task": query, "max_files": 10}
            else:
                params = {"query": query, "max_results": 10}
            t0 = time.perf_counter()
            r = await client.call_tool(tool, params)
            ms = (time.perf_counter() - t0) * 1000
            rank = best_rank(parse_ranked_files(txt(r)), expected)
            results.append({"repo": repo, "kind": kind, "tool": tool,
                            "query": query, "rank": rank, "ms": ms})
            mark = "MISS" if rank == 0 else f"#{rank}"
            print(f"  {kind:<7} {tool:<16} {mark:>4}  {ms:6.0f}  {query[:46]}")
    return results


def summarize(results: list[dict]) -> None:
    if not results:
        return
    print(f"\n=== Summary ({len(results)} cases) ===")
    for kind in ("ticket", "guard"):
        rows = [r for r in results if r["kind"] == kind]
        if not rows:
            continue
        rr = [1.0 / r["rank"] if r["rank"] else 0.0 for r in rows]
        top1 = sum(1 for r in rows if r["rank"] == 1)
        top5 = sum(1 for r in rows if 1 <= r["rank"] <= 5)
        print(f"  {kind:<7}  MRR {statistics.mean(rr):.3f}   "
              f"top-1 {top1}/{len(rows)}   top-5 {top5}/{len(rows)}")
    lat = sorted(r["ms"] for r in results)
    p50 = lat[len(lat) // 2]
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
    print(f"  latency  p50 {p50:.0f}ms   p95 {p95:.0f}ms")
    misses = [r for r in results if r["rank"] == 0]
    if misses:
        print(f"  misses:")
        for r in misses:
            print(f"    [{r['repo']}/{r['tool']}] {r['query'][:60]}")


async def warmup_only(workspace: Path) -> None:
    print(f"=== warmup stress: {workspace} ===")
    async with Client(make_transport(workspace)) as client:
        t0 = time.perf_counter()
        status = await wait_warmup(client, timeout=1800)
        secs = time.perf_counter() - t0
        if not status:
            print(f"  TIMEOUT after {secs:.0f}s")
            return
        print(f"  warmup complete in {secs:.1f}s")
        for line in status.splitlines():
            if any(k in line for k in ("source_files", "blueprints",
                                       "indexed_tokens", "semantic")):
                print(" ", line.strip())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", choices=list(WORKSPACES), action="append")
    ap.add_argument("--warmup-only", action="store_true")
    ap.add_argument("--workspace", type=str, help="workspace for --warmup-only")
    args = ap.parse_args()

    if args.warmup_only:
        ws = Path(args.workspace).expanduser() if args.workspace else CARPS
        await warmup_only(ws)
        return

    repos = args.repo or list(WORKSPACES)
    results: list[dict] = []
    for repo in repos:
        ws = WORKSPACES[repo]
        if not ws.exists():
            print(f"skip {repo}: {ws} not found")
            continue
        results.extend(await run_repo(repo, ws))
    summarize(results)


if __name__ == "__main__":
    asyncio.run(main())
