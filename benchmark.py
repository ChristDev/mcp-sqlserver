"""Benchmark: mcp-sqlserver vs @bytebase/dbhub

Compares token overhead, startup time, query latency, and memory usage.

Usage:
    python benchmark.py --ours-url http://localhost:8002/mcp
    python benchmark.py --ours-url http://localhost:8002/mcp --dbhub-url http://localhost:8080/mcp
    python benchmark.py --ours-url http://localhost:8002/mcp --runs 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

# ---------------------------------------------------------------------------
# MCP Client helpers
# ---------------------------------------------------------------------------

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# Approximate tokens per character (GPT/Claude average)
CHARS_PER_TOKEN = 4.0


def mcp_request(
    url: str,
    method: str,
    params: dict | None = None,
    session_id: str | None = None,
    req_id: int = 1,
    timeout: float = 30.0,
) -> tuple[dict, float, str | None]:
    """Send MCP JSON-RPC request. Returns (result, elapsed_ms, session_id)."""
    headers = {**HEADERS}
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    body = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params or {},
    }

    start = time.perf_counter()
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=body, headers=headers)
    elapsed = (time.perf_counter() - start) * 1000

    # Parse response — handles both SSE (mcp-sqlserver) and JSON (dbhub) formats
    new_session = resp.headers.get("mcp-session-id", session_id)
    text = resp.text.strip()
    result = {}

    # Try direct JSON first (dbhub format)
    try:
        data = json.loads(text)
        if "result" in data:
            result = data["result"]
        elif "error" in data:
            result = {"error": data["error"]}
        return result, elapsed, new_session
    except json.JSONDecodeError:
        pass

    # Try SSE format (mcp-sqlserver format)
    for line in text.split("\n"):
        if line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                if "result" in data:
                    result = data["result"]
                elif "error" in data:
                    result = {"error": data["error"]}
            except json.JSONDecodeError:
                pass

    return result, elapsed, new_session


def initialize(url: str) -> tuple[dict, float, str]:
    """Initialize MCP session."""
    result, elapsed, session_id = mcp_request(
        url,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "benchmark", "version": "1.0"},
        },
    )
    return result, elapsed, session_id or ""


# ---------------------------------------------------------------------------
# Benchmark functions
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    name: str
    init_ms: float = 0.0
    tools_schema_chars: int = 0
    tools_schema_tokens: int = 0
    tools_count: int = 0
    resources_count: int = 0
    query_latencies_ms: list[float] = field(default_factory=list)
    error: str = ""

    @property
    def avg_query_ms(self) -> float:
        return statistics.mean(self.query_latencies_ms) if self.query_latencies_ms else 0.0

    @property
    def p50_query_ms(self) -> float:
        return statistics.median(self.query_latencies_ms) if self.query_latencies_ms else 0.0

    @property
    def p95_query_ms(self) -> float:
        if not self.query_latencies_ms:
            return 0.0
        sorted_lat = sorted(self.query_latencies_ms)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    @property
    def tokens_per_turn(self) -> int:
        """Estimated tokens sent to LLM per conversation turn (tool schemas)."""
        return self.tools_schema_tokens


def benchmark_server(url: str, name: str, runs: int = 5) -> BenchmarkResult:
    """Run full benchmark against an MCP server."""
    result = BenchmarkResult(name=name)

    # 1. Initialize
    try:
        init_result, init_ms, session_id = initialize(url)
        result.init_ms = init_ms
    except Exception as exc:
        result.error = f"Init failed: {exc}"
        return result

    if "error" in init_result:
        result.error = f"Init error: {init_result['error']}"
        return result

    # 2. Tools list — measure schema size
    try:
        tools_result, _, _ = mcp_request(url, "tools/list", session_id=session_id, req_id=2)
        tools = tools_result.get("tools", [])
        result.tools_count = len(tools)

        # Measure schema size (this is what gets sent to LLM every turn)
        schema_json = json.dumps(tools)
        result.tools_schema_chars = len(schema_json)
        result.tools_schema_tokens = int(len(schema_json) / CHARS_PER_TOKEN)
    except Exception as exc:
        result.error = f"Tools list failed: {exc}"
        return result

    # 3. Resources list
    try:
        res_result, _, _ = mcp_request(url, "resources/list", session_id=session_id, req_id=3)
        result.resources_count = len(res_result.get("resources", []))
    except Exception:
        pass  # Not critical

    # 4. Query latency — run same queries multiple times
    test_queries = [
        "SELECT TOP 5 TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES ORDER BY TABLE_SCHEMA",
        "SELECT COUNT(*) AS total FROM INFORMATION_SCHEMA.COLUMNS",
        "SELECT DB_NAME() AS current_database, @@VERSION AS sql_version",
    ]

    for run in range(runs):
        for i, query in enumerate(test_queries):
            try:
                _, latency, _ = mcp_request(
                    url,
                    "tools/call",
                    {
                        "name": "execute_sql",
                        "arguments": {"query": query},
                    },
                    session_id=session_id,
                    req_id=100 + run * 10 + i,
                )
                result.query_latencies_ms.append(latency)
            except Exception:
                pass

    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_report(ours: BenchmarkResult, dbhub: BenchmarkResult | None, runs: int) -> None:
    """Print comparison table."""
    print("\n" + "=" * 70)
    print("  BENCHMARK: mcp-sqlserver vs dbhub")
    print("=" * 70)

    if ours.error:
        print(f"\n  [ERROR] {ours.name}: {ours.error}")
    if dbhub and dbhub.error:
        print(f"\n  [ERROR] {dbhub.name}: {dbhub.error}")

    # Header
    if dbhub and not dbhub.error:
        print(f"\n  {'Metric':<30} {'mcp-sqlserver':>15} {'dbhub':>15} {'Diff':>12}")
        print("  " + "-" * 72)
    else:
        print(f"\n  {'Metric':<30} {'mcp-sqlserver':>15}")
        print("  " + "-" * 45)

    def row(label: str, ours_val: str, dbhub_val: str = "", diff: str = ""):
        if dbhub and not dbhub.error:
            print(f"  {label:<30} {ours_val:>15} {dbhub_val:>15} {diff:>12}")
        else:
            print(f"  {label:<30} {ours_val:>15}")

    # Token efficiency
    row(
        "Tool count",
        str(ours.tools_count),
        str(dbhub.tools_count) if dbhub else "",
    )
    row(
        "Schema size (chars)",
        f"{ours.tools_schema_chars:,}",
        f"{dbhub.tools_schema_chars:,}" if dbhub else "",
        f"{_pct_diff(ours.tools_schema_chars, dbhub.tools_schema_chars if dbhub else 0)}"
        if dbhub
        else "",
    )
    row(
        "Schema size (tokens)",
        f"~{ours.tools_schema_tokens:,}",
        f"~{dbhub.tools_schema_tokens:,}" if dbhub else "",
        f"{_pct_diff(ours.tools_schema_tokens, dbhub.tools_schema_tokens if dbhub else 0)}"
        if dbhub
        else "",
    )
    row(
        "Tokens/turn (15 turns)",
        f"~{ours.tokens_per_turn * 15:,}",
        f"~{dbhub.tokens_per_turn * 15:,}" if dbhub else "",
    )
    row(
        "Resources available",
        str(ours.resources_count),
        str(dbhub.resources_count) if dbhub else "",
    )

    print()

    # Latency
    row(
        "Init latency (ms)",
        f"{ours.init_ms:.0f}",
        f"{dbhub.init_ms:.0f}" if dbhub else "",
    )

    total_queries = len(ours.query_latencies_ms)
    row(
        f"Query avg ({total_queries} queries)",
        f"{ours.avg_query_ms:.0f}ms",
        f"{dbhub.avg_query_ms:.0f}ms" if dbhub else "",
        f"{_pct_diff(ours.avg_query_ms, dbhub.avg_query_ms if dbhub else 0)}" if dbhub else "",
    )
    row(
        "Query p50",
        f"{ours.p50_query_ms:.0f}ms",
        f"{dbhub.p50_query_ms:.0f}ms" if dbhub else "",
    )
    row(
        "Query p95",
        f"{ours.p95_query_ms:.0f}ms",
        f"{dbhub.p95_query_ms:.0f}ms" if dbhub else "",
    )

    print()
    print("=" * 70)

    # Summary
    if dbhub and not dbhub.error:
        token_savings = (1 - ours.tools_schema_tokens / max(dbhub.tools_schema_tokens, 1)) * 100
        print(f"\n  Token savings: {token_savings:.0f}% fewer tokens per turn")
        if ours.avg_query_ms < dbhub.avg_query_ms:
            speed = (1 - ours.avg_query_ms / max(dbhub.avg_query_ms, 1)) * 100
            print(f"  Query speed:   {speed:.0f}% faster")
        else:
            speed = (ours.avg_query_ms / max(dbhub.avg_query_ms, 1) - 1) * 100
            print(f"  Query speed:   {speed:.0f}% slower")
    else:
        print(
            f"\n  mcp-sqlserver: {ours.tools_count} tool, ~{ours.tokens_per_turn} tokens/turn, {ours.avg_query_ms:.0f}ms avg query"
        )
        print("  Run with --dbhub-url to compare against dbhub")

    print()


def _pct_diff(ours: float, theirs: float) -> str:
    if theirs == 0:
        return ""
    pct = (1 - ours / theirs) * 100
    if pct > 0:
        return f"-{pct:.0f}%"
    return f"+{abs(pct):.0f}%"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Benchmark mcp-sqlserver vs dbhub")
    parser.add_argument("--ours-url", default="http://localhost:8002/mcp", help="mcp-sqlserver URL")
    parser.add_argument("--dbhub-url", default="", help="dbhub URL (optional)")
    parser.add_argument("--runs", type=int, default=5, help="Number of query runs (default: 5)")
    args = parser.parse_args()

    print(f"\nBenchmarking mcp-sqlserver at {args.ours_url}...")
    ours = benchmark_server(args.ours_url, "mcp-sqlserver", runs=args.runs)

    dbhub = None
    if args.dbhub_url:
        print(f"Benchmarking dbhub at {args.dbhub_url}...")
        dbhub = benchmark_server(args.dbhub_url, "dbhub", runs=args.runs)

    print_report(ours, dbhub, args.runs)


if __name__ == "__main__":
    main()
