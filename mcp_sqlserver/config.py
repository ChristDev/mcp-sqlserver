"""Configuration loading — TOML + env vars + CLI merge.

Priority (highest first):
  1. CLI args
  2. Environment variables (MCP_SQLSERVER_*)
  3. TOML config file
  4. Defaults

The typed model itself lives in :mod:`mcp_sqlserver.settings` and is re-exported
here, so ``from mcp_sqlserver.config import AppConfig`` keeps working.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

from mcp_sqlserver.errors import InvalidConfigError
from mcp_sqlserver.settings import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_POOL_TIMEOUT,
    HTTP_POOL_SIZE,
    MAX_POOL_SIZE,
    STDIO_POOL_SIZE,
    AppConfig,
    GuardrailConfig,
    ServerConfig,
    SourceConfig,
)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_POOL_TIMEOUT",
    "HTTP_POOL_SIZE",
    "MAX_POOL_SIZE",
    "STDIO_POOL_SIZE",
    "AppConfig",
    "GuardrailConfig",
    "ServerConfig",
    "SourceConfig",
    "load_config",
]


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------


def _load_toml(path: Path) -> dict[str, Any]:
    """Load and parse a TOML config file."""
    with open(path, "rb") as f:
        return tomllib.load(f)


def _positive_int(name: str, value: Any, maximum: int | None = None) -> int:
    """Parse a config value that must be a whole number of at least one."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidConfigError(field=name, value=repr(value), expected="an integer >= 1")
    if maximum is not None and value > maximum:
        raise InvalidConfigError(field=name, value=repr(value), expected=f"an integer <= {maximum}")
    return value


def _positive_float(name: str, value: Any) -> float:
    """Parse a config value that must be a number greater than zero."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise InvalidConfigError(field=name, value=repr(value), expected="a number > 0")
    return float(value)


def _parse_source(raw: dict[str, Any]) -> SourceConfig:
    """Build one typed source, rejecting values the pool cannot honour."""
    source_id = str(raw["id"])
    declared_pool_size = raw.get("pool_size")
    pool_size = (
        None
        if declared_pool_size is None
        else _positive_int(f"sources.{source_id}.pool_size", declared_pool_size, MAX_POOL_SIZE)
    )
    return SourceConfig(
        id=source_id,
        dsn=raw["dsn"],
        description=raw.get("description", ""),
        lazy=raw.get("lazy", False),
        pool_size=pool_size,
        pool_timeout=_positive_float(
            f"sources.{source_id}.pool_timeout",
            raw.get("pool_timeout", DEFAULT_POOL_TIMEOUT),
        ),
        connect_timeout=_positive_int(
            f"sources.{source_id}.connect_timeout",
            raw.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT),
        ),
    )


def _parse_toml(data: dict[str, Any]) -> AppConfig:
    """Parse raw TOML dict into typed AppConfig."""
    sources = [_parse_source(s) for s in data.get("sources", [])]

    guardrails = [
        GuardrailConfig(
            source=g["source"],
            readonly=g.get("readonly", False),
            max_rows=g.get("max_rows", 1000),
            query_timeout=_positive_int(
                f"guardrails.{g['source']}.query_timeout", g.get("query_timeout", 30)
            ),
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
    """Build the single-source config from MCP_SQLSERVER_DSN, if it is set.

    Server settings are NOT read here — see :func:`_apply_server_env`, which
    applies them to whichever config won, TOML or DSN.
    """
    import os

    dsn = os.environ.get("MCP_SQLSERVER_DSN", "")
    if not dsn:
        return AppConfig()

    # Pool sizing is a TOML-only setting: env mode is the single-DSN quick start,
    # where the transport default (see AppConfig.pool_size_for) is the right answer.
    sources = [SourceConfig(id="default", dsn=dsn, description="From environment")]

    readonly_str = os.environ.get("MCP_SQLSERVER_READONLY", "false")
    guardrails = [
        GuardrailConfig(
            source="default",
            readonly=readonly_str.lower() in ("true", "1", "yes"),
            max_rows=int(os.environ.get("MCP_SQLSERVER_MAX_ROWS", "1000")),
            query_timeout=_positive_int(
                "MCP_SQLSERVER_QUERY_TIMEOUT",
                int(os.environ.get("MCP_SQLSERVER_QUERY_TIMEOUT", "30")),
            ),
        )
    ]

    return AppConfig(sources=sources, guardrails=guardrails)


def _apply_server_env(server: ServerConfig) -> None:
    """Override the [server] block with whatever the environment actually sets.

    Each variable is applied with ITS OWN value, and an absent variable leaves
    the TOML untouched. The previous version rebuilt a whole ServerConfig from
    hardcoded fallbacks and assigned that, so setting MCP_SQLSERVER_PORT
    replaced the configured port with the code default instead of the value.
    """
    import os

    transport = os.environ.get("MCP_SQLSERVER_TRANSPORT_MODE")
    if transport:
        server.transport = transport

    host = os.environ.get("MCP_SQLSERVER_HOST")
    if host:
        server.host = host

    port = os.environ.get("MCP_SQLSERVER_PORT")
    if port:
        if not port.isdigit():
            raise InvalidConfigError(
                field="MCP_SQLSERVER_PORT", value=repr(port), expected="an integer >= 1"
            )
        server.port = _positive_int("MCP_SQLSERVER_PORT", int(port))

    log_level = os.environ.get("MCP_SQLSERVER_LOG_LEVEL")
    if log_level:
        server.log_level = log_level


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
    _apply_server_env(config.server)

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
        return _readable_toml(Path(cli_path), "--config")

    # Env var
    env_path = os.environ.get("MCP_SQLSERVER_CONFIG", "")
    if env_path:
        return _readable_toml(Path(env_path), "MCP_SQLSERVER_CONFIG")

    # Default location — absent is normal here, the DSN paths may still apply
    default = Path("mcp-sqlserver.toml")
    if default.is_file():
        return default
    _warn_when_directory(default, "./mcp-sqlserver.toml")
    return None


def _readable_toml(path: Path, origin: str) -> Path | None:
    """Accept a configured path only when it is really a file."""
    if path.is_file():
        return path
    if not _warn_when_directory(path, origin):
        _log_stderr(f"WARNING: Config file from {origin} not found: {path}")
    return None


def _warn_when_directory(path: Path, origin: str) -> bool:
    """Report the Docker bind-mount trap, and say whether it applied.

    Docker creates a DIRECTORY when the host side of a bind mount does not
    exist. Handing that to the TOML parser raises a bare OSError, so name the
    real problem instead.
    """
    if not path.is_dir():
        return False
    _log_stderr(
        f"WARNING: {origin} points at a directory, not a file: {path}. "
        "Docker creates one when a bind mount source is missing on the host — "
        "check the path on the left side of the -v flag."
    )
    return True


def _log_stderr(msg: str) -> None:
    """Log to stderr (safe for stdio transport)."""
    print(msg, file=sys.stderr)
