"""Application configuration — TOML + env vars + CLI merge.

Priority (highest first):
  1. CLI args
  2. Environment variables (MCP_SQLSERVER_*)
  3. TOML config file
  4. Defaults
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data classes for typed config
# ---------------------------------------------------------------------------


@dataclass
class SourceConfig:
    """A single database connection source."""

    id: str
    dsn: str
    description: str = ""
    lazy: bool = False


@dataclass
class GuardrailConfig:
    """Guardrails for a specific source."""

    source: str
    readonly: bool = False
    max_rows: int = 1000
    query_timeout: int = 30


@dataclass
class ServerConfig:
    """MCP server settings."""

    transport: str = "http"
    host: str = "0.0.0.0"
    port: int = 8002
    log_level: str = "INFO"


@dataclass
class AppConfig:
    """Root application configuration."""

    sources: list[SourceConfig] = field(default_factory=list)
    guardrails: list[GuardrailConfig] = field(default_factory=list)
    server: ServerConfig = field(default_factory=ServerConfig)

    def get_guardrail(self, source_id: str) -> GuardrailConfig:
        """Get guardrail config for a source, or defaults."""
        for g in self.guardrails:
            if g.source == source_id:
                return g
        return GuardrailConfig(source=source_id)

    def get_source(self, source_id: str) -> SourceConfig | None:
        """Get source config by ID."""
        for s in self.sources:
            if s.id == source_id:
                return s
        return None

    @property
    def default_source(self) -> SourceConfig | None:
        """Return the first source as default."""
        return self.sources[0] if self.sources else None


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------


def _load_toml(path: Path) -> dict[str, Any]:
    """Load and parse a TOML config file."""
    with open(path, "rb") as f:
        return tomllib.load(f)


def _parse_toml(data: dict[str, Any]) -> AppConfig:
    """Parse raw TOML dict into typed AppConfig."""
    sources = [
        SourceConfig(
            id=s["id"],
            dsn=s["dsn"],
            description=s.get("description", ""),
            lazy=s.get("lazy", False),
        )
        for s in data.get("sources", [])
    ]

    guardrails = [
        GuardrailConfig(
            source=g["source"],
            readonly=g.get("readonly", False),
            max_rows=g.get("max_rows", 1000),
            query_timeout=g.get("query_timeout", 30),
        )
        for g in data.get("guardrails", [])
    ]

    server_data = data.get("server", {})
    server = ServerConfig(
        transport=server_data.get("transport", "http"),
        host=server_data.get("host", "0.0.0.0"),
        port=server_data.get("port", 8002),
        log_level=server_data.get("log_level", "INFO"),
    )

    return AppConfig(sources=sources, guardrails=guardrails, server=server)


# ---------------------------------------------------------------------------
# Env var loading
# ---------------------------------------------------------------------------


def _load_from_env() -> AppConfig:
    """Build config from environment variables (single-source mode)."""
    import os

    dsn = os.environ.get("MCP_SQLSERVER_DSN", "")
    if not dsn:
        return AppConfig()

    sources = [SourceConfig(id="default", dsn=dsn, description="From environment")]

    readonly_str = os.environ.get("MCP_SQLSERVER_READONLY", "false")
    guardrails = [
        GuardrailConfig(
            source="default",
            readonly=readonly_str.lower() in ("true", "1", "yes"),
            max_rows=int(os.environ.get("MCP_SQLSERVER_MAX_ROWS", "1000")),
            query_timeout=int(os.environ.get("MCP_SQLSERVER_QUERY_TIMEOUT", "30")),
        )
    ]

    server = ServerConfig(
        transport=os.environ.get("MCP_SQLSERVER_TRANSPORT_MODE", "http"),
        host=os.environ.get("MCP_SQLSERVER_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_SQLSERVER_PORT", "8002")),
        log_level=os.environ.get("MCP_SQLSERVER_LOG_LEVEL", "INFO"),
    )

    return AppConfig(sources=sources, guardrails=guardrails, server=server)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def load_config(
    config_path: str | None = None,
    dsn: str | None = None,
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> AppConfig:
    """Load config with priority: CLI > env > TOML > defaults.

    Args:
        config_path: Path to TOML config file (CLI --config).
        dsn: Direct DSN string (CLI --dsn).
        transport: Transport mode override (CLI --transport).
        host: Host override (CLI --host).
        port: Port override (CLI --port).
    """
    config = AppConfig()

    # Layer 1: TOML file
    toml_path = _resolve_toml_path(config_path)
    if toml_path:
        data = _load_toml(toml_path)
        config = _parse_toml(data)
        _log_stderr(f"Loaded config from {toml_path}")

    # Layer 2: Env vars (merge — env sources only if TOML had none)
    env_config = _load_from_env()
    if not config.sources and env_config.sources:
        config.sources = env_config.sources
        config.guardrails = env_config.guardrails
    if env_config.server.transport != "http" or config.server.transport == "http":
        # Env overrides server settings if explicitly set
        import os

        if os.environ.get("MCP_SQLSERVER_TRANSPORT_MODE"):
            config.server.transport = env_config.server.transport
        if os.environ.get("MCP_SQLSERVER_HOST"):
            config.server.host = env_config.server.host
        if os.environ.get("MCP_SQLSERVER_PORT"):
            config.server.port = env_config.server.port
        if os.environ.get("MCP_SQLSERVER_LOG_LEVEL"):
            config.server.log_level = env_config.server.log_level

    # Layer 3: CLI args (highest priority)
    if dsn:
        config.sources = [SourceConfig(id="default", dsn=dsn, description="From CLI")]
        if not config.guardrails:
            config.guardrails = [GuardrailConfig(source="default")]
    if transport:
        config.server.transport = transport
    if host:
        config.server.host = host
    if port:
        config.server.port = port

    # Validate
    if not config.sources:
        _log_stderr(
            "ERROR: No database sources configured.\n"
            "Provide one of:\n"
            "  1. --config path/to/mcp-sqlserver.toml\n"
            "  2. --dsn 'Driver={ODBC Driver 18 for SQL Server};Server=host;...'\n"
            "  3. MCP_SQLSERVER_DSN environment variable\n"
            "  4. MCP_SQLSERVER_CONFIG pointing to a TOML file"
        )
        sys.exit(1)

    return config


def _resolve_toml_path(cli_path: str | None) -> Path | None:
    """Find the TOML config file."""
    import os

    # CLI arg
    if cli_path:
        p = Path(cli_path)
        if p.exists():
            return p
        _log_stderr(f"WARNING: Config file not found: {cli_path}")
        return None

    # Env var
    env_path = os.environ.get("MCP_SQLSERVER_CONFIG", "")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        _log_stderr(f"WARNING: Config file from MCP_SQLSERVER_CONFIG not found: {env_path}")
        return None

    # Default location
    default = Path("mcp-sqlserver.toml")
    if default.exists():
        return default

    return None


def _log_stderr(msg: str) -> None:
    """Log to stderr (safe for stdio transport)."""
    print(msg, file=sys.stderr)
