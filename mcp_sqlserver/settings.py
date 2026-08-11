"""The typed configuration model.

Kept apart from :mod:`mcp_sqlserver.config`, which owns *loading* — reading
TOML, merging environment variables and CLI arguments. This module owns only
the shape of a valid configuration and its defaults, so nothing here does I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# ---------------------------------------------------------------------------
# Concurrency defaults
# ---------------------------------------------------------------------------

#: Concurrent queries allowed per source when the server speaks HTTP to many
#: clients. Each slot is one SQL Server session.
HTTP_POOL_SIZE: Final = 4

#: A stdio server has exactly one client, so one connection is enough.
STDIO_POOL_SIZE: Final = 1

#: Upper bound on any configured pool, so a typo cannot exhaust SQL Server.
MAX_POOL_SIZE: Final = 16

DEFAULT_POOL_TIMEOUT: Final = 5.0
DEFAULT_CONNECT_TIMEOUT: Final = 10


@dataclass
class SourceConfig:
    """A single database connection source."""

    id: str
    dsn: str
    description: str = ""
    lazy: bool = False
    pool_size: int | None = None
    pool_timeout: float = DEFAULT_POOL_TIMEOUT
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT


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

    #: Serve HTTP without per-client MCP sessions. A session lives in one
    #: process's memory, so the caller is bound to the replica that opened it:
    #: any restart, scale event or load balancer without affinity answers
    #: "Session not found". Self-contained requests remove that coupling, which
    #: is what a server shared by many clients needs. Ignored over stdio, which
    #: has exactly one client and no HTTP layer.
    stateless_http: bool = True


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

    def pool_size_for(self, source: SourceConfig) -> int:
        """Concurrent queries allowed for a source, defaulted by transport."""
        if source.pool_size is not None:
            return source.pool_size
        return STDIO_POOL_SIZE if self.server.transport == "stdio" else HTTP_POOL_SIZE
