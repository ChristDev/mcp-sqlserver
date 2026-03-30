"""FastMCP server creation with lifespan for connection management.

Lifespan handles:
  - Startup: load config, connect to databases, run health checks
  - Shutdown: close all connections
  - Shared state: inject conn_manager + config into ctx.lifespan_context
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from mcp_sqlserver.config import AppConfig
from mcp_sqlserver.connection import ConnectionManager
from mcp_sqlserver.prompts import register_prompts
from mcp_sqlserver.resources import register_resources
from mcp_sqlserver.tools import register_tools

logger = logging.getLogger(__name__)


@lifespan
async def app_lifespan(server: FastMCP):
    """Manage database connections lifecycle.

    Yields shared state accessible in tools/resources via ctx.lifespan_context.
    """
    # Config is attached to server instance by create_server()
    config: AppConfig = server._mcp_config  # type: ignore[attr-defined]

    conn_manager = ConnectionManager(config)

    # Connect to all non-lazy sources + health check
    logger.info("Connecting to %d database source(s)...", len(config.sources))
    results = conn_manager.health_check()

    ok_count = sum(1 for r in results if r["status"] == "ok")
    fail_count = len(results) - ok_count
    if fail_count > 0:
        logger.warning("%d source(s) failed health check — check VPN and credentials", fail_count)
    logger.info("%d/%d source(s) connected", ok_count, len(results))

    try:
        yield {"conn_manager": conn_manager, "config": config}
    finally:
        conn_manager.close_all()
        logger.info("All database connections closed")


def create_server(config: AppConfig) -> FastMCP:
    """Create and configure the FastMCP server instance."""
    mcp = FastMCP(
        "MCP SQL Server",
        instructions=(
            "MCP server for SQL Server databases. "
            "Use execute_sql tool for queries. "
            "Use db:// resources to explore schemas, tables, procedures, and more. "
            "Resources: db://schemas, db://{schema}/tables, db://{schema}/{table}, "
            "db://{schema}/procedures, db://{schema}/procedures/{name}, "
            "db://{schema}/functions, db://{schema}/views."
        ),
        lifespan=app_lifespan,
    )

    # Attach config to server for lifespan access
    mcp._mcp_config = config  # type: ignore[attr-defined]

    # Register all MCP primitives
    register_tools(mcp)
    register_resources(mcp)
    register_prompts(mcp)

    return mcp
