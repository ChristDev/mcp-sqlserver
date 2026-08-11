# MCP SQL Server

[Leer en Español](README.es.md)

Token-efficient [MCP](https://modelcontextprotocol.io/) server for SQL Server. Designed to minimize context window usage while maximizing database exploration capabilities.

**65% fewer tokens per turn** compared to generic database MCP servers, by using 1 tool + MCP Resources instead of multiple tools.

## Why

MCP tool schemas are sent to the LLM on **every conversation turn**. Generic database MCP servers ship 2+ tools with complex schemas, burning ~450 tokens/turn just on tool definitions. Over a 50-turn conversation, that's ~22,000 tokens wasted on schemas alone.

mcp-sqlserver uses **1 tool** (`execute_sql`) and moves schema exploration to **MCP Resources** — which are fetched on-demand and cost zero tokens per turn.

| | mcp-sqlserver | Generic (2 tools) |
|---|:---:|:---:|
| Schema tokens/turn | ~157 | ~453 |
| Over 50 turns | ~7,850 | ~22,650 |
| Resources | 11 URI templates | 0 |
| Prompts | 3 templates | 0 |

## Features

- **1 Tool + 11 Resources** — Minimal token overhead, maximum capability
- **Multi-Database** — Connect to multiple SQL Server instances via TOML config
- **Concurrent by design** — Bounded connection pool per source; many users and agents share one server safely
- **Per-Source Guardrails** — Read-only mode, row limiting, real statement timeout per database
- **Health Checks** — Validates network, auth, and query on startup with clear error messages
- **Docker Ready** — Port mapping on 8002, reaches corporate/VPN databases through Docker Desktop networking
- **Any MCP Client** — Works with Claude Desktop, Cursor, OpenCode, VS Code, Kiro, and any MCP-compatible client
- **No npx** — Pre-built Docker image or pip install. No downloads at runtime.

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/ChristDev/mcp-sqlserver.git
cd mcp-sqlserver

# 1. Create your config
cp mcp-sqlserver.toml.example mcp-sqlserver.toml
# Edit mcp-sqlserver.toml with your database credentials

# 2. Start
docker compose up -d

# 3. Verify
curl -s http://localhost:8002/mcp \
  -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

### Local (Python 3.11+)

```bash
pip install -e .
mcp-sqlserver --config ./mcp-sqlserver.toml
```

Or with a single DSN:

```bash
mcp-sqlserver --dsn "Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=mydb;UID=sa;PWD=pass"
```

## Client Configuration

Add to your MCP client config:

<details>
<summary><b>Claude Desktop</b> — claude_desktop_config.json</summary>

```json
{
  "mcpServers": {
    "sqlserver": {
      "url": "http://localhost:8002/mcp"
    }
  }
}
```
</details>

<details>
<summary><b>Cursor</b> — .cursor/mcp.json</summary>

```json
{
  "mcpServers": {
    "sqlserver": {
      "url": "http://localhost:8002/mcp"
    }
  }
}
```
</details>

<details>
<summary><b>VS Code</b> — .vscode/mcp.json</summary>

```json
{
  "servers": {
    "sqlserver": {
      "type": "http",
      "url": "http://localhost:8002/mcp"
    }
  }
}
```
</details>

<details>
<summary><b>OpenCode</b> — opencode.json</summary>

Connects to an already-running server. OpenCode spawns nothing; it just opens the HTTP connection.

Global file at `~/.config/opencode/opencode.json`, or local `opencode.json` inside the project — the local one wins.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "sqlserver": {
      "type": "remote",
      "url": "http://localhost:8002/mcp",
      "enabled": true
    }
  }
}
```

Restart OpenCode after saving. To confirm it connected, ask it to read the `db://connections` resource: it should answer with the configured databases.
</details>

<details>
<summary><b>Kiro</b> — .kiro/settings/mcp.json</summary>

Kiro supports both modes. The recommended one connects to the container that is already up:

```json
{
  "mcpServers": {
    "sqlserver": {
      "type": "remote",
      "url": "http://localhost:8002/mcp",
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

The second mode has Kiro spawn the server as a stdio subprocess. That requires the package installed locally (`pip install -e .`), and the configuration travels through environment variables, because the process inherits nothing from the container:

```json
{
  "mcpServers": {
    "sqlserver": {
      "command": "mcp-sqlserver",
      "args": ["--transport", "stdio"],
      "env": {
        "MCP_SQLSERVER_CONFIG": "C:/full/path/mcp-sqlserver.toml"
      },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

The file goes in `.kiro/settings/mcp.json` for one project, or `~/.kiro/settings/mcp.json` for all of them. When both exist they are merged, and the project one takes precedence.

On `autoApprove`: leaving it empty means every call is confirmed first. Adding `"execute_sql"` removes that prompt, which is only advisable when the source runs `readonly`, since that same tool also executes INSERT, UPDATE, DELETE and DDL.
</details>

<details>
<summary><b>Local stdio</b> (no Docker)</summary>

```json
{
  "mcpServers": {
    "sqlserver": {
      "command": "python",
      "args": ["-m", "mcp_sqlserver", "--transport", "stdio"],
      "env": {
        "MCP_SQLSERVER_DSN": "Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=mydb;UID=sa;PWD=pass"
      }
    }
  }
}
```
</details>

## MCP Primitives

### Tool: `execute_sql`

The only tool. Executes any SQL query.

```
execute_sql(query: str, database: str = "")
```

- `query` — SQL statement (SELECT, INSERT, UPDATE, DELETE, EXEC, DDL)
- `database` — Source ID for multi-database setups (optional, uses default)

### Resources

Navigate your database schema on-demand. Zero tokens per turn — content only loaded when the LLM reads them.

| URI | Description |
|-----|-------------|
| `db://connections` | List configured database connections |
| `db://schemas` | List all schemas with table/procedure counts |
| `db://{schema}/tables` | List tables in a schema |
| `db://{schema}/{table}` | Full table structure (columns, PKs, FKs, indexes) |
| `db://{schema}/{table}/indexes` | Table indexes with columns |
| `db://{schema}/procedures` | List stored procedures |
| `db://{schema}/procedures/{name}` | Stored procedure source code + parameters |
| `db://{schema}/functions` | List user-defined functions |
| `db://{schema}/functions/{name}` | Function source code |
| `db://{schema}/views` | List views |
| `db://{schema}/views/{name}` | View source code |

### Prompts

Reusable templates for common operations:

| Prompt | Description |
|--------|-------------|
| `generate-sp` | Generate stored procedure template for a table |
| `analyze-table` | Analyze table structure and suggest improvements |
| `generate-migration` | Generate idempotent migration script |

## Configuration

### TOML (multi-database)

```toml
[[sources]]
id = "production"
description = "Production Database"
dsn = "Driver={ODBC Driver 18 for SQL Server};Server=prod-db.example.com,1433;Database=MyApp;UID=app_user;PWD=secret;Encrypt=yes;TrustServerCertificate=yes"

pool_size = 4        # Concurrent queries (and SQL Server sessions) for this source
pool_timeout = 5.0   # Seconds to wait for a free slot before answering "busy"
connect_timeout = 10 # Seconds to wait for login

[[sources]]
id = "staging"
description = "Staging Database"
dsn = "Driver={ODBC Driver 18 for SQL Server};Server=staging-db.example.com,1433;Database=MyApp_Staging;UID=app_user;PWD=secret;Encrypt=yes;TrustServerCertificate=yes"
lazy = true          # Connect only on first use

[[guardrails]]
source = "production"
readonly = true       # Production = read only
max_rows = 500
query_timeout = 15

[[guardrails]]
source = "staging"
readonly = false
max_rows = 1000
query_timeout = 30

[server]
transport = "http"
host = "0.0.0.0"
port = 8002
log_level = "INFO"
```

### Environment variables (overrides — there is no `.env`)

The TOML above is the whole configuration. The server never reads a `.env` file. Environment variables exist so a container can override server settings without rebuilding the image, and so a single database can be pointed at with no file at all.

```bash
# Where the TOML lives, and how the server listens
MCP_SQLSERVER_CONFIG=/app/mcp-sqlserver.toml
MCP_SQLSERVER_TRANSPORT_MODE=http
MCP_SQLSERVER_HOST=0.0.0.0
MCP_SQLSERVER_PORT=8002
MCP_SQLSERVER_LOG_LEVEL=INFO

# Single-database mode, no TOML at all
MCP_SQLSERVER_DSN="Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=mydb;UID=sa;PWD=pass"
MCP_SQLSERVER_READONLY=false
MCP_SQLSERVER_MAX_ROWS=1000
MCP_SQLSERVER_QUERY_TIMEOUT=30
```

**Pool settings are TOML-only.** `pool_size`, `pool_timeout` and `connect_timeout` are per-source and have no environment equivalent — the env path is the single-database quick start, where the transport default is already the right answer.

### Config priority

1. CLI args (highest)
2. Environment variables
3. TOML config file
4. Defaults

## Guardrails

### Read-only mode

Per-source, and fail-closed. The statement must start with `SELECT`, `WITH`, `EXPLAIN`, `SHOWPLAN` or `SET`, **and** contain no write verb anywhere outside string literals, be a single statement, and not use `SELECT ... INTO`. So `WITH x AS (...) DELETE FROM t` and `SELECT 1; DROP TABLE t` are both rejected.

Read-only mode is a guardrail against accidents, not a security boundary — enforce real restrictions with SQL Server permissions on the login.

### Row limiting

Automatically injects `TOP N` into SELECT queries:

```sql
-- Your query
SELECT * FROM Users WHERE Active = 1

-- Executed (max_rows = 1000)
SELECT TOP 1000 * FROM Users WHERE Active = 1
```

### Query timeout

Per-source statement timeout in seconds, applied to the ODBC connection. When it fires, SQL Server cancels the statement, the caller gets a clear error, and the connection is discarded rather than returned to the pool.

## Concurrency

This server is built to be shared by many users and agents. Each source gets a **bounded connection pool**: one connection is leased to one complete query and returned afterwards, so two callers never touch the same pyodbc handle — which pyodbc (`threadsafety = 1`) does not permit.

You tune it per `[[sources]]` block in `mcp-sqlserver.toml` — there is no other place:

| Setting | Default | Meaning |
|---|---|---|
| `pool_size` | 4 over HTTP, 1 over stdio | Concurrent queries — and therefore SQL Server sessions — per source. Max 16. |
| `pool_timeout` | 5.0 | Seconds a caller waits for a free slot before being told the source is busy |
| `connect_timeout` | 10 | Seconds to wait for login |
| `query_timeout` | 30 | Statement timeout (see above) |

Admission happens before a worker thread is claimed, so a saturated source fails fast instead of queueing without limit:

```
Database 'production' is busy: 4 queries are already running and no slot became
available within 5s. Retry shortly or raise pool_size for this source.
```

**Sizing**: `pool_size` × number of sources is the ceiling on sessions this server opens against SQL Server. Start at 4 per source and raise only if callers actually see busy errors.

## Health Check

On startup, validates each database connection:

```
[mydb] OK — MyDatabase ready
[staging] Cannot reach staging-db.example.com:1433 — VPN connected?
[archive] Authentication failed — check credentials in mcp-sqlserver.toml
```

Run manually:

```bash
mcp-sqlserver --health --config ./mcp-sqlserver.toml
```

Docker health check runs every 30s — `docker ps` shows `(healthy)` or `(unhealthy)`.

## Architecture

```
AI Client (Claude Desktop / Cursor / VS Code / OpenCode)
    |
    |  MCP Protocol (HTTP)
    v
Docker Container (mcp-sqlserver)
    |  port mapping 8002:8002 (reaches VPN/corporate network)
    |
    |  Python + fastmcp + pyodbc
    |  one bounded connection pool per source
    |
    v
SQL Server
    ├── Database A (read-write)
    ├── Database B (read-only)
    └── ...
```

## Tech Stack

| Component | Library |
|-----------|---------|
| MCP Framework | [FastMCP](https://gofastmcp.com) >= 2.14 |
| SQL Server Driver | [pyodbc](https://github.com/mkleehammer/pyodbc) >= 5.0 |
| Config | [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) >= 2.0 |
| TOML Parser | `tomllib` (Python 3.11+ built-in) |

## Roadmap

| Version | Features |
|---------|----------|
| **v1.0** | 1 tool + resources + prompts, multi-connection TOML, guardrails |
| **v1.1** | Bounded connection pool per source, real statement timeout, parameterised catalog queries, fail-closed read-only |
| **v2.0** | SSH tunneling, per-user authentication and audit trail |
| **v3.0** | Extensible connector interface (PostgreSQL, MySQL) |

## Acknowledgments

Inspired by the simplicity of [dbhub](https://github.com/bytebase/dbhub). Built for teams that need SQL Server-specific features with minimal token overhead.

## License

MIT

---

If this saved you tokens (and headaches), star the repo!
