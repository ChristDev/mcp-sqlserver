@echo off
setlocal enabledelayedexpansion

echo.
echo ============================================================
echo   MCP SQL Server — Setup
echo ============================================================
echo.

:: Check Docker
docker --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker not found. Install Docker Desktop with WSL2.
    echo         https://docs.docker.com/desktop/install/windows-install/
    exit /b 1
)

:: Check if config exists
if not exist "mcp-sqlserver.toml" (
    echo [INFO] Creating mcp-sqlserver.toml from template...
    copy "mcp-sqlserver.toml.example" "mcp-sqlserver.toml" >nul
    echo.
    echo [ACTION REQUIRED] Edit mcp-sqlserver.toml with your database credentials.
    echo                   Then run this script again.
    echo.
    notepad "mcp-sqlserver.toml"
    exit /b 0
)

:: Build Docker image
echo [1/3] Building Docker image...
docker compose build
if errorlevel 1 (
    echo [ERROR] Docker build failed.
    exit /b 1
)

:: Start container
echo [2/3] Starting container...
docker compose up -d
if errorlevel 1 (
    echo [ERROR] Docker start failed.
    exit /b 1
)

:: Wait for health check
echo [3/3] Waiting for health check...
timeout /t 5 /nobreak >nul

:: Check health
docker compose ps | findstr "healthy" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [WARNING] Server started but health check pending.
    echo           Check VPN connection and credentials.
    echo           Run: docker compose logs
    echo.
) else (
    echo [OK] Health check passed.
)

echo.
echo ============================================================
echo   MCP SQL Server running on http://localhost:8002/mcp
echo ============================================================
echo.
echo Add this config to your AI client:
echo.
echo -- OpenCode (opencode.json) --------------------------------
echo {
echo   "mcp": {
echo     "sqlserver": {
echo       "type": "remote",
echo       "url": "http://localhost:8002/mcp",
echo       "enabled": true
echo     }
echo   }
echo }
echo.
echo -- Claude Desktop (claude_desktop_config.json) -------------
echo {
echo   "mcpServers": {
echo     "sqlserver": {
echo       "url": "http://localhost:8002/mcp"
echo     }
echo   }
echo }
echo.
echo -- Cursor (.cursor/mcp.json) -------------------------------
echo {
echo   "mcpServers": {
echo     "sqlserver": {
echo       "url": "http://localhost:8002/mcp"
echo     }
echo   }
echo }
echo.
echo -- Kiro (kiro_mcp_config.json) -----------------------------
echo {
echo   "mcpServers": {
echo     "sqlserver": {
echo       "url": "http://localhost:8002/mcp"
echo     }
echo   }
echo }
echo.
echo -- VS Code (.vscode/mcp.json) ------------------------------
echo {
echo   "servers": {
echo     "sqlserver": {
echo       "type": "http",
echo       "url": "http://localhost:8002/mcp"
echo     }
echo   }
echo }
echo.
echo ============================================================
echo   Commands:
echo     docker compose up -d       Start
echo     docker compose down        Stop
echo     docker compose logs -f     Logs
echo     docker compose up --build  Rebuild
echo ============================================================
echo.
