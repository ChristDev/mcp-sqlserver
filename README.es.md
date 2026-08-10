# MCP SQL Server

Servidor [MCP](https://modelcontextprotocol.io/) para SQL Server optimizado en tokens. Diseñado para minimizar el uso de la ventana de contexto mientras maximiza las capacidades de exploración de base de datos.

**65% menos tokens por turno** comparado con servidores MCP genéricos de base de datos, usando 1 tool + MCP Resources en vez de múltiples tools.

## Por qué

Los schemas de tools MCP se envían al LLM en **cada turno de conversación**. Los servidores MCP genéricos traen 2+ tools con schemas complejos, quemando ~450 tokens/turno solo en definiciones de tools. En una conversación de 50 turnos, son ~22,000 tokens desperdiciados solo en schemas.

mcp-sqlserver usa **1 tool** (`execute_sql`) y mueve la exploración de schema a **MCP Resources** — que se obtienen bajo demanda y cuestan cero tokens por turno.

| | mcp-sqlserver | Genérico (2 tools) |
|---|:---:|:---:|
| Tokens de schema/turno | ~157 | ~453 |
| En 50 turnos | ~7,850 | ~22,650 |
| Resources | 11 URI templates | 0 |
| Prompts | 3 templates | 0 |

## Características

- **1 Tool + 11 Resources** — Mínimo overhead de tokens, máxima capacidad
- **Multi-Base de Datos** — Conecta a múltiples instancias de SQL Server via config TOML
- **Concurrencia real** — Pool de conexiones acotado por fuente; varios usuarios y agentes comparten un mismo server sin pisarse
- **Guardrails por fuente** — Modo solo-lectura, límite de filas, timeout de statement real por base de datos
- **Health Checks** — Valida red, autenticación y query al iniciar con mensajes claros
- **Docker Ready** — Port mapping en 8002, alcanza bases corporativas/VPN via el networking de Docker Desktop
- **Cualquier cliente MCP** — Funciona con Claude Desktop, Cursor, OpenCode, VS Code, Kiro y cualquier cliente compatible con MCP
- **Sin npx** — Imagen Docker o pip install. Sin descargas en tiempo de ejecución.

## Inicio Rápido

### Docker (recomendado)

```bash
git clone https://github.com/ChristDev/mcp-sqlserver.git
cd mcp-sqlserver

# 1. Crear tu config
cp mcp-sqlserver.toml.example mcp-sqlserver.toml
# Editar mcp-sqlserver.toml con tus credenciales de base de datos

# 2. Levantar
docker compose up -d

# 3. Verificar
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

O con un DSN directo:

```bash
mcp-sqlserver --dsn "Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=mydb;UID=sa;PWD=pass"
```

## Configuración de Clientes

Agrega esto a la config de tu cliente MCP:

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

```json
{
  "mcp": {
    "sqlserver": {
      "type": "remote",
      "url": "http://localhost:8002/mcp",
      "enabled": true
    }
  }
}
```
</details>

<details>
<summary><b>Local stdio</b> (sin Docker)</summary>

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

## Primitivas MCP

### Tool: `execute_sql`

El único tool. Ejecuta cualquier query SQL.

```
execute_sql(query: str, database: str = "")
```

- `query` — Sentencia SQL (SELECT, INSERT, UPDATE, DELETE, EXEC, DDL)
- `database` — ID del source para multi-base de datos (opcional, usa el default)

### Resources

Navega el schema de tu base de datos bajo demanda. Cero tokens por turno — el contenido solo se carga cuando el LLM los lee.

| URI | Descripción |
|-----|-------------|
| `db://connections` | Lista las conexiones configuradas |
| `db://schemas` | Lista todos los schemas con conteo de tablas/procedimientos |
| `db://{schema}/tables` | Lista tablas de un schema |
| `db://{schema}/{table}` | Estructura completa de tabla (columnas, PKs, FKs, índices) |
| `db://{schema}/{table}/indexes` | Índices de la tabla con columnas |
| `db://{schema}/procedures` | Lista stored procedures |
| `db://{schema}/procedures/{name}` | Código fuente del SP + parámetros |
| `db://{schema}/functions` | Lista funciones definidas por el usuario |
| `db://{schema}/functions/{name}` | Código fuente de la función |
| `db://{schema}/views` | Lista vistas |
| `db://{schema}/views/{name}` | Código fuente de la vista |

### Prompts

Templates reutilizables para operaciones comunes:

| Prompt | Descripción |
|--------|-------------|
| `generate-sp` | Genera template de stored procedure para una tabla |
| `analyze-table` | Analiza estructura de tabla y sugiere mejoras |
| `generate-migration` | Genera script de migración idempotente |

## Configuración

### TOML (multi-base de datos)

```toml
[[sources]]
id = "production"
description = "Base de datos de Producción"
dsn = "Driver={ODBC Driver 18 for SQL Server};Server=prod-db.example.com,1433;Database=MyApp;UID=app_user;PWD=secret;Encrypt=yes;TrustServerCertificate=yes"

pool_size = 4        # Queries concurrentes (y sesiones SQL Server) para esta fuente
pool_timeout = 5.0   # Segundos esperando un slot libre antes de responder "ocupado"
connect_timeout = 10 # Segundos de espera para el login

[[sources]]
id = "staging"
description = "Base de datos de Staging"
dsn = "Driver={ODBC Driver 18 for SQL Server};Server=staging-db.example.com,1433;Database=MyApp_Staging;UID=app_user;PWD=secret;Encrypt=yes;TrustServerCertificate=yes"
lazy = true          # Conectar solo al primer uso

[[guardrails]]
source = "production"
readonly = true       # Producción = solo lectura
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

### Variables de entorno (base de datos única)

```bash
MCP_SQLSERVER_DSN="Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=mydb;UID=sa;PWD=pass"
MCP_SQLSERVER_READONLY=false
MCP_SQLSERVER_MAX_ROWS=1000
MCP_SQLSERVER_QUERY_TIMEOUT=30
```

### Prioridad de config

1. Args de CLI (máxima prioridad)
2. Variables de entorno
3. Archivo TOML
4. Defaults

## Guardrails

### Modo solo-lectura

Por fuente, y falla cerrado. El statement debe empezar con `SELECT`, `WITH`, `EXPLAIN`, `SHOWPLAN` o `SET`, **y además** no contener ningún verbo de escritura fuera de literales de texto, ser un único statement, y no usar `SELECT ... INTO`. Así, `WITH x AS (...) DELETE FROM t` y `SELECT 1; DROP TABLE t` quedan rechazados.

El modo solo-lectura es un guardrail contra accidentes, no una frontera de seguridad — las restricciones reales se aplican con permisos de SQL Server sobre el login.

### Límite de filas

Inyecta automáticamente `TOP N` en queries SELECT:

```sql
-- Tu query
SELECT * FROM Users WHERE Active = 1

-- Lo que se ejecuta (max_rows = 1000)
SELECT TOP 1000 * FROM Users WHERE Active = 1
```

### Timeout de query

Timeout de statement por fuente, en segundos, aplicado sobre la conexión ODBC. Cuando salta, SQL Server cancela el statement, el llamador recibe un error claro, y la conexión se descarta en vez de volver al pool.

## Concurrencia

Este server está pensado para ser compartido por varios usuarios y agentes. Cada fuente tiene un **pool de conexiones acotado**: se presta una conexión a una query completa y se devuelve al terminar, de modo que dos llamadores nunca tocan el mismo handle de pyodbc — algo que pyodbc (`threadsafety = 1`) no permite.

| Setting | Default | Significado |
|---|---|---|
| `pool_size` | 4 en HTTP, 1 en stdio | Queries concurrentes — y por lo tanto sesiones SQL Server — por fuente. Máximo 16. |
| `pool_timeout` | 5.0 | Segundos que espera un llamador por un slot libre antes de recibir "ocupado" |
| `connect_timeout` | 10 | Segundos de espera para el login |
| `query_timeout` | 30 | Timeout de statement (ver arriba) |

La admisión ocurre antes de tomar un hilo, así que una fuente saturada falla rápido en vez de encolar sin límite:

```
Database 'production' is busy: 4 queries are already running and no slot became
available within 5s. Retry shortly or raise pool_size for this source.
```

**Dimensionamiento**: `pool_size` × cantidad de fuentes es el techo de sesiones que este server abre contra SQL Server. Empezá en 4 por fuente y subilo solo si los usuarios efectivamente ven errores de "busy".

## Health Check

Al iniciar, valida cada conexión a base de datos:

```
[mydb] OK — MyDatabase ready
[staging] Cannot reach staging-db.example.com:1433 — VPN connected?
[archive] Authentication failed — check credentials in mcp-sqlserver.toml
```

Ejecutar manualmente:

```bash
mcp-sqlserver --health --config ./mcp-sqlserver.toml
```

El health check de Docker corre cada 30s — `docker ps` muestra `(healthy)` o `(unhealthy)`.

## Arquitectura

```
Cliente AI (Claude Desktop / Cursor / VS Code / OpenCode)
    |
    |  Protocolo MCP (HTTP)
    v
Docker Container (mcp-sqlserver)
    |  port mapping 8002:8002 (alcanza VPN/red corporativa)
    |
    |  Python + fastmcp + pyodbc
    |  un pool de conexiones acotado por fuente
    |
    v
SQL Server
    |-- Base de datos A (lectura-escritura)
    |-- Base de datos B (solo lectura)
    |-- ...
```

## Stack Técnico

| Componente | Librería |
|-----------|---------|
| MCP Framework | [FastMCP](https://gofastmcp.com) >= 2.14 |
| Driver SQL Server | [pyodbc](https://github.com/mkleehammer/pyodbc) >= 5.0 |
| Config | [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) >= 2.0 |
| Parser TOML | `tomllib` (built-in en Python 3.11+) |

## Roadmap

| Versión | Features |
|---------|----------|
| **v1.0** | 1 tool + resources + prompts, multi-conexión TOML, guardrails |
| **v1.1** | Pool de conexiones acotado por fuente, timeout de statement real, queries de catálogo parametrizadas, solo-lectura fail-closed |
| **v2.0** | SSH tunneling, autenticación por usuario y trazabilidad |
| **v3.0** | Interface de conector extensible (PostgreSQL, MySQL) |

## Agradecimientos

Inspirado por la simplicidad de [dbhub](https://github.com/bytebase/dbhub). Construido para equipos que necesitan features específicas de SQL Server con mínimo overhead de tokens.

## Licencia

MIT

---

Si te ahorró tokens (y dolores de cabeza), dale una estrellita al repo.
