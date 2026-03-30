FROM python:3.11-slim

# Install ODBC Driver 18 for SQL Server
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        gnupg2 \
        unixodbc-dev \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Copy application code
COPY mcp_sqlserver/ ./mcp_sqlserver/

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD ["python", "-m", "mcp_sqlserver", "--health"]

# Default: HTTP transport
ENV MCP_SQLSERVER_TRANSPORT_MODE=http
ENV MCP_SQLSERVER_HOST=0.0.0.0
ENV MCP_SQLSERVER_PORT=8002

EXPOSE 8002

ENTRYPOINT ["python", "-m", "mcp_sqlserver"]
