# Benchmark: mcp-sqlserver vs dbhub

**Date**: 2026-03-30
**Environment**: Windows 11 + Docker Desktop (WSL2)
**Database**: SQL Server 2019 (remote, over VPN)
**Versions**: mcp-sqlserver v1.0.0 (fastmcp 3.1.1) vs dbhub v0.21.0

---

## Token Efficiency

The primary design goal. MCP tool schemas are sent to the LLM on **every conversation turn** — fewer tokens per schema = more context window for actual work.

| Metric | mcp-sqlserver | dbhub v0.21.0 | Delta |
|--------|:------------:|:-------------:|:-----:|
| Tool count | 1 | 2 | -50% |
| Resource count | 2 (+9 URI templates) | 0 | — |
| Prompt count | 3 | 0 | — |
| **Schema size (chars)** | **630** | **1,812** | **-65%** |
| **Schema size (~tokens)** | **~157** | **~453** | **-65%** |

### Token cost over conversation length

| Turns | mcp-sqlserver | dbhub | Tokens saved |
|------:|:------------:|:-----:|:------------:|
| 1 | ~157 | ~453 | 296 |
| 15 | ~2,355 | ~6,795 | 4,440 |
| 50 | ~7,850 | ~22,650 | 14,800 |
| 100 | ~15,700 | ~45,300 | 29,600 |

> Token estimation: 1 token ~= 4 characters (GPT/Claude average)

### Why the difference

- **mcp-sqlserver**: 1 tool (`execute_sql`) with 2 params. Schema exploration moved to **Resources** which are fetched on-demand and do NOT consume tokens per turn.
- **dbhub**: 2 tools (`execute_sql` + `search_objects`). The `search_objects` schema includes 6 params with enums and validation, adding ~1,200 chars of schema sent every turn.

---

## Connectivity & Stability

Tested with password containing special characters: `#$%&`

| Test | mcp-sqlserver | dbhub |
|------|:------------:|:-----:|
| Health check | PASS (network + auth + query) | N/A (no health check) |
| HTTP server startup | PASS (~2s) | PASS (~3s) |
| Docker `-p` port mapping | PASS | **FAIL** — crashed connecting to remote host |
| Docker `--network host` | PASS | PASS |
| Password with special chars | PASS (TOML, no escaping) | PASS (URL-encoded in DSN) |
| npx startup (no Docker) | N/A | ~30s+ (downloads package every time) |
| Connection error messages | Clear diagnostic | Stack trace |

### dbhub Docker failure detail

```
Fatal error: ConnectionError: Failed to connect to host.docker.internal:1433
  - Could not connect (sequence)
```

dbhub with `-p` port mapping could not resolve the remote host from inside the container. Required `--network host` or `--add-host` workaround with manually resolved IP.

---

## Latency

Both servers connect to the same remote SQL Server. Query latency is dominated by network round-trip, not server overhead.

| Metric | mcp-sqlserver | dbhub |
|--------|:------------:|:-----:|
| MCP `initialize` | ~2,300ms | ~2,500ms |
| `tools/list` | ~50ms | ~30ms |
| Query (SELECT TOP 5) | ~2,500ms | ~2,500ms |

**Conclusion**: Query latency is equivalent. The bottleneck is network to SQL Server, not the MCP server itself.

---

## Feature Comparison

| Feature | mcp-sqlserver | dbhub |
|---------|:------------:|:-----:|
| Token efficiency | ~157 tokens/turn | ~453 tokens/turn |
| MCP Resources | 11 URI templates | None |
| MCP Prompts | 3 templates | None |
| Multi-database (TOML) | Per-source config | Global config |
| Guardrails per-source | read-only, max_rows, timeout | Global only |
| Health check | Network + auth + query | None |
| Setup script | `setup.bat` + Docker | npx |
| Multi-client support | Any client via HTTP | Tied to spawning client |
| Password handling | TOML file (no escaping) | CLI args (URL encoding) |
| Multi-DB engine | SQL Server only | 5 engines |
| SSH Tunneling | Planned v2 | Supported |
| Web Workbench | No | Yes |

---

## Raw Data

### mcp-sqlserver `tools/list` (630 chars)

```json
[{"name":"execute_sql","description":"Execute SQL query. Supports SELECT, INSERT, UPDATE, DELETE, EXEC, DDL.\n\nArgs:\n    query: SQL statement to execute. Multiple statements separated by ;\n    database: Source ID for multi-connection (optional, uses default if empty)","inputSchema":{"additionalProperties":false,"properties":{"query":{"type":"string"},"database":{"default":"","type":"string"}},"required":["query"],"type":"object"},"outputSchema":{"properties":{"result":{"type":"string"}},"required":["result"],"type":"object","x-fastmcp-wrap-result":true},"_meta":{"fastmcp":{"tags":[]}}}]
```

### dbhub `tools/list` (1,812 chars)

```json
[{"name":"execute_sql","description":"Execute SQL queries on the sqlserver database","inputSchema":{"type":"object","properties":{"sql":{"type":"string","description":"SQL to execute (multiple statements separated by ;)"}},"required":["sql"],"additionalProperties":false,"$schema":"http://json-schema.org/draft-07/schema#"},"annotations":{"title":"Execute SQL (sqlserver)","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false},"execution":{"taskSupport":"forbidden"}},{"name":"search_objects","description":"Search and list database objects (schemas, tables, columns, procedures, functions, indexes) on the sqlserver database","inputSchema":{"type":"object","properties":{"object_type":{"type":"string","enum":["schema","table","column","procedure","function","index"],"description":"Object type to search"},"pattern":{"type":"string","default":"%","description":"LIKE pattern (% = any chars, _ = one char). Default: %"},"schema":{"type":"string","description":"Filter to schema"},"table":{"type":"string","description":"Filter to table (requires schema; column/index only)"},"detail_level":{"type":"string","enum":["names","summary","full"],"default":"names","description":"Detail: names (minimal), summary (metadata), full (all)"},"limit":{"type":"integer","exclusiveMinimum":0,"maximum":1000,"default":100,"description":"Max results (default: 100, max: 1000)"}},"required":["object_type"],"additionalProperties":false,"$schema":"http://json-schema.org/draft-07/schema#"},"annotations":{"title":"Search Database Objects (sqlserver)","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false},"execution":{"taskSupport":"forbidden"}}]
```

## Reproducing

```bash
# Start mcp-sqlserver
docker compose up -d

# Start dbhub
docker run --rm -d --name dbhub --network host \
  bytebase/dbhub --transport http --port 8080 \
  --dsn "sqlserver://user:pass@host:1433/db?trustServerCertificate=true"

# Run benchmark
python benchmark.py --ours-url http://localhost:8002/mcp --dbhub-url http://localhost:8080/mcp
```
