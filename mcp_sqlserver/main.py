"""CLI entry point for MCP SQL Server.

Usage:
    mcp-sqlserver                                          # HTTP (default)
    mcp-sqlserver --transport stdio                        # STDIO for MCP clients
    mcp-sqlserver --transport http --host 0.0.0.0 --port 8002  # HTTP explicit
    mcp-sqlserver --config /path/to/mcp-sqlserver.toml     # Custom config
    mcp-sqlserver --dsn "Driver={ODBC Driver 18};..."      # Single DSN
    mcp-sqlserver --health                                 # Health check only
"""

from __future__ import annotations

import argparse
import logging
import sys

import anyio

from mcp_sqlserver import __version__
from mcp_sqlserver.config import AppConfig, load_config
from mcp_sqlserver.connection import ConnectionManager, HealthStatus


def main() -> None:
    """Parse args and run the MCP server."""
    parser = argparse.ArgumentParser(
        description="MCP SQL Server — Token-efficient MCP server for SQL Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  mcp-sqlserver --config ./mcp-sqlserver.toml\n"
            '  mcp-sqlserver --dsn "Driver={ODBC Driver 18 for SQL Server};Server=host;Database=db;UID=u;PWD=p"\n'
            "  mcp-sqlserver --health\n"
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=None,
        help="Transport mode (default: from config or 'http')",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="HTTP host (default: from config or '0.0.0.0')",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP port (default: from config or 8002)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to TOML config file",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="Direct DSN connection string (single database mode)",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Run health check and exit",
    )
    args = parser.parse_args()

    # Load config (merges TOML + env + CLI)
    config = load_config(
        config_path=args.config,
        dsn=args.dsn,
        transport=args.transport,
        host=args.host,
        port=args.port,
    )

    # Configure logging (always to stderr — safe for stdio transport)
    logging.basicConfig(
        level=getattr(logging, config.server.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Health check mode
    if args.health:
        statuses = anyio.run(_probe_sources, config)
        sys.exit(0 if all(status.healthy for status in statuses) else 1)

    # Banner
    _print_banner(config)

    # Create and run server
    from mcp_sqlserver.server import create_server

    mcp = create_server(config)

    if config.server.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            host=config.server.host,
            port=config.server.port,
        )


async def _probe_sources(config: AppConfig) -> tuple[HealthStatus, ...]:
    """Run the startup health probe once and shut the pools down again."""
    conn_manager = ConnectionManager(config)
    try:
        return await conn_manager.start()
    finally:
        await conn_manager.aclose()


def _print_banner(config) -> None:
    """Print startup banner to stderr."""
    print(
        "\n"
        " __  __  ___ ___   ___  ___  _    ___\n"
        "|  \\/  |/ __| _ \\ / __|/ _ \\| |  / __| ___ _ ___ _____ _ _\n"
        "| |\\/| | (__|  _/ \\__ \\ (_) | |__\\__ \\/ -_) '_\\ V / -_) '_|\n"
        "|_|  |_|\\___|_|   |___/\\__\\_\\____|___/\\___|_|  \\_/\\___|_|\n"
        "\n"
        f"  v{__version__} — Token-efficient MCP for SQL Server\n"
        f"  Transport: {config.server.transport}\n"
        f"  Sources:   {len(config.sources)}\n",
        file=sys.stderr,
    )
    if config.server.transport != "stdio":
        print(
            f"  Endpoint:  http://{config.server.host}:{config.server.port}/mcp\n",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
