"""MCP Prompts — reusable templates for common developer operations.

Prompts are invoked explicitly by the user, not on every turn.
They provide structured guidance for common SQL Server tasks.
"""

from __future__ import annotations

from fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Register all prompts on the FastMCP server."""

    @mcp.prompt
    def generate_sp(
        table: str,
        schema: str = "dbo",
        operation: str = "CRUD",
    ) -> str:
        """Generate a stored procedure template for a table.

        Args:
            table: Table name to generate SP for.
            schema: Schema name (default: dbo).
            operation: Type of SP — CRUD, pagination, search, insert, update, delete.
        """
        return (
            f"Generate a SQL Server stored procedure for [{schema}].[{table}].\n"
            f"Operation type: {operation}\n\n"
            "Requirements:\n"
            "- Use TRY/CATCH with explicit transactions\n"
            "- Follow naming convention: {Schema}.sp_{Verb}{Entity}\n"
            "- Use parameterized inputs with proper data types\n"
            "- Include SET NOCOUNT ON\n"
            "- For pagination: use OFFSET/FETCH with @PageNumber and @PageSize params, "
            "return total count in second result set\n"
            "- For search: use LIKE with @Filter parameter across relevant text columns\n"
            "- Include header comment with author, date, description\n"
            "- Use explicit column lists (no SELECT *)\n\n"
            f"First, read the table structure from resource db://{schema}/{table} "
            "to understand the columns, types, and constraints."
        )

    @mcp.prompt
    def analyze_table(
        table: str,
        schema: str = "dbo",
    ) -> str:
        """Analyze a table's structure and suggest improvements.

        Args:
            table: Table name to analyze.
            schema: Schema name (default: dbo).
        """
        return (
            f"Analyze the table [{schema}].[{table}] and provide recommendations.\n\n"
            "Steps:\n"
            f"1. Read table structure from resource db://{schema}/{table}\n"
            f"2. Read indexes from resource db://{schema}/{table}/indexes\n"
            f"3. Run: SELECT TOP 100 * FROM [{schema}].[{table}] to see sample data\n"
            f"4. Run: SELECT COUNT(*) FROM [{schema}].[{table}] to get row count\n\n"
            "Analyze and report on:\n"
            "- Data type appropriateness (VARCHAR lengths, numeric precision)\n"
            "- Missing indexes based on likely query patterns\n"
            "- Redundant or duplicate indexes\n"
            "- Missing foreign key constraints\n"
            "- NULL vs NOT NULL correctness\n"
            "- Missing default values\n"
            "- Naming convention compliance\n"
            "- Suggestions for computed columns or indexed views if applicable"
        )

    @mcp.prompt
    def generate_migration(
        description: str,
    ) -> str:
        """Generate an idempotent SQL migration script.

        Args:
            description: What the migration should do (e.g., 'add Status column to Cases.Case').
        """
        return (
            f"Generate an idempotent SQL Server migration script for: {description}\n\n"
            "Requirements:\n"
            "- Script must be re-runnable (idempotent) — use IF NOT EXISTS checks\n"
            "- Wrap in a transaction with TRY/CATCH\n"
            "- Include rollback on error\n"
            "- Use PRINT statements for progress logging\n"
            "- Follow this structure:\n"
            "  1. Header comment (description, date, author)\n"
            "  2. Pre-checks (verify objects exist)\n"
            "  3. BEGIN TRY / BEGIN TRANSACTION\n"
            "  4. Schema changes\n"
            "  5. Data migrations (if needed)\n"
            "  6. COMMIT TRANSACTION\n"
            "  7. END TRY / BEGIN CATCH / ROLLBACK\n"
            "  8. Post-verification query\n\n"
            "Before generating, explore the current schema using resources to understand "
            "the existing table structure and constraints."
        )
