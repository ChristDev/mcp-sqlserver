"""Tests for config — TOML parsing, env vars, defaults, priority."""

import os
import tempfile
from pathlib import Path

import pytest

from mcp_sqlserver.config import (
    AppConfig,
    GuardrailConfig,
    ServerConfig,
    SourceConfig,
    _load_toml,
    _parse_toml,
    _resolve_toml_path,
    load_config,
)
from mcp_sqlserver.errors import InvalidConfigError


class TestServerEnvOverrides:
    """Env vars override the [server] block with THEIR value, not a default.

    Regression: the merge used to rebuild a ServerConfig from hardcoded
    fallbacks, so setting MCP_SQLSERVER_PORT replaced the configured port with
    the code default (8002) instead of the value asked for.
    """

    # port 1433 on purpose: it must differ from the 8002 code default, or the
    # assertions would pass whether or not the override was honoured.
    TOML = (
        '[[sources]]\nid = "x"\n'
        'dsn = "Driver={ODBC Driver 18 for SQL Server};Server=h,1433;Database=D;UID=u;PWD=p"\n\n'
        '[server]\ntransport = "http"\nhost = "0.0.0.0"\nport = 1433\nlog_level = "INFO"\n'
    )

    @pytest.fixture
    def configured(self, tmp_path: Path, monkeypatch):
        (tmp_path / "mcp-sqlserver.toml").write_text(self.TOML, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        for var in (
            "MCP_SQLSERVER_PORT",
            "MCP_SQLSERVER_HOST",
            "MCP_SQLSERVER_TRANSPORT_MODE",
            "MCP_SQLSERVER_LOG_LEVEL",
            "MCP_SQLSERVER_STATELESS_HTTP",
            "MCP_SQLSERVER_DSN",
            "MCP_SQLSERVER_CONFIG",
        ):
            monkeypatch.delenv(var, raising=False)
        return tmp_path

    def test_absent_env_leaves_the_toml_alone(self, configured):
        assert load_config().server.port == 1433

    def test_env_port_applies_its_own_value(self, configured, monkeypatch):
        monkeypatch.setenv("MCP_SQLSERVER_PORT", "9999")
        assert load_config().server.port == 9999

    def test_env_port_matching_the_toml_is_not_reset_to_the_code_default(
        self, configured, monkeypatch
    ):
        # This is the exact deployment case: the image sets the same port the
        # TOML declares. It used to come back as 8002.
        monkeypatch.setenv("MCP_SQLSERVER_PORT", "1433")
        assert load_config().server.port == 1433

    def test_host_and_transport_apply_their_own_values(self, configured, monkeypatch):
        monkeypatch.setenv("MCP_SQLSERVER_HOST", "127.0.0.1")
        monkeypatch.setenv("MCP_SQLSERVER_TRANSPORT_MODE", "stdio")
        config = load_config()
        assert config.server.host == "127.0.0.1"
        assert config.server.transport == "stdio"

    def test_a_non_numeric_port_is_rejected(self, configured, monkeypatch):
        monkeypatch.setenv("MCP_SQLSERVER_PORT", "not-a-port")
        with pytest.raises(InvalidConfigError):
            load_config()


class TestStatelessHttp:
    """HTTP is served without per-client sessions unless asked otherwise.

    A session lives in the memory of one process, which binds the caller to the
    replica that opened it: a restart, a scale event or a load balancer without
    affinity all answer "Session not found". Stateless is therefore the default,
    and giving sessions back has to be a deliberate act.
    """

    BASE = (
        '[[sources]]\nid = "x"\n'
        'dsn = "Driver={ODBC Driver 18 for SQL Server};Server=h,1433;Database=D;UID=u;PWD=p"\n\n'
    )

    @pytest.fixture
    def configure(self, tmp_path: Path, monkeypatch):
        """Write a [server] block, load it, and hand back the parsed config."""

        def write(server_block: str = "") -> AppConfig:
            (tmp_path / "mcp-sqlserver.toml").write_text(
                self.BASE + server_block, encoding="utf-8"
            )
            return load_config()

        monkeypatch.chdir(tmp_path)
        for var in (
            "MCP_SQLSERVER_STATELESS_HTTP",
            "MCP_SQLSERVER_DSN",
            "MCP_SQLSERVER_CONFIG",
        ):
            monkeypatch.delenv(var, raising=False)
        return write

    def test_defaults_to_stateless_when_the_toml_is_silent(self, configure):
        assert configure("[server]\nport = 8002\n").server.stateless_http is True

    def test_the_toml_can_ask_for_sessions_back(self, configure):
        assert configure("[server]\nstateless_http = false\n").server.stateless_http is False

    def test_env_applies_its_own_value_over_the_toml(self, configure, monkeypatch):
        monkeypatch.setenv("MCP_SQLSERVER_STATELESS_HTTP", "false")
        assert configure("[server]\nstateless_http = true\n").server.stateless_http is False

    def test_env_accepts_the_usual_spellings_of_true(self, configure, monkeypatch):
        monkeypatch.setenv("MCP_SQLSERVER_STATELESS_HTTP", "YES")
        assert configure("[server]\nstateless_http = false\n").server.stateless_http is True

    def test_a_quoted_toml_boolean_is_rejected(self, configure):
        # "false" is a string, and a truthy one. Accepting it would do the exact
        # opposite of what the file says, silently.
        with pytest.raises(InvalidConfigError):
            configure('[server]\nstateless_http = "false"\n')

    def test_an_unparseable_env_value_is_rejected(self, configure, monkeypatch):
        monkeypatch.setenv("MCP_SQLSERVER_STATELESS_HTTP", "maybe")
        with pytest.raises(InvalidConfigError):
            configure()


class TestResolveTomlPath:
    """A configured path is only accepted when it is really a file."""

    def test_accepts_a_real_file(self, tmp_path: Path):
        # Given: a TOML file on disk
        config = tmp_path / "mcp-sqlserver.toml"
        config.write_text("[server]\nport = 8002\n", encoding="utf-8")

        # When/Then: it is accepted
        assert _resolve_toml_path(str(config)) == config

    def test_rejects_a_directory_left_by_a_broken_bind_mount(self, tmp_path: Path, capsys):
        # Given: Docker created a directory because the host file was missing
        mounted = tmp_path / "mcp-sqlserver.toml"
        mounted.mkdir()

        # When: the server resolves the configured path
        resolved = _resolve_toml_path(str(mounted))

        # Then: it declines instead of handing a directory to the TOML parser
        assert resolved is None
        assert "directory" in capsys.readouterr().err

    def test_reports_a_missing_path(self, tmp_path: Path, capsys):
        # Given: a path that does not exist at all
        missing = tmp_path / "nope.toml"

        # When/Then: the warning names it as not found, not as a directory
        assert _resolve_toml_path(str(missing)) is None
        error = capsys.readouterr().err
        assert "not found" in error
        assert "directory" not in error


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------


class TestTomlParsing:
    """Test TOML config file parsing."""

    def _write_toml(self, content: str) -> Path:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False)
        f.write(content)
        f.close()
        return Path(f.name)

    def test_parse_single_source(self):
        path = self._write_toml("""
[[sources]]
id = "mydb"
dsn = "Driver={ODBC Driver 18};Server=localhost;Database=test"
description = "Test DB"
""")
        data = _load_toml(path)
        config = _parse_toml(data)
        assert len(config.sources) == 1
        assert config.sources[0].id == "mydb"
        assert config.sources[0].description == "Test DB"
        assert "localhost" in config.sources[0].dsn
        os.unlink(path)

    def test_parse_multiple_sources(self):
        path = self._write_toml("""
[[sources]]
id = "db1"
dsn = "Driver={ODBC Driver 18};Server=host1;Database=db1"

[[sources]]
id = "db2"
dsn = "Driver={ODBC Driver 18};Server=host2;Database=db2"
lazy = true
""")
        data = _load_toml(path)
        config = _parse_toml(data)
        assert len(config.sources) == 2
        assert config.sources[0].id == "db1"
        assert config.sources[1].id == "db2"
        assert config.sources[1].lazy is True
        os.unlink(path)

    def test_parse_guardrails(self):
        path = self._write_toml("""
[[sources]]
id = "mydb"
dsn = "test"

[[guardrails]]
source = "mydb"
readonly = true
max_rows = 500
query_timeout = 15
""")
        data = _load_toml(path)
        config = _parse_toml(data)
        g = config.get_guardrail("mydb")
        assert g.readonly is True
        assert g.max_rows == 500
        assert g.query_timeout == 15
        os.unlink(path)

    def test_parse_server_config(self):
        path = self._write_toml("""
[[sources]]
id = "mydb"
dsn = "test"

[server]
transport = "stdio"
host = "127.0.0.1"
port = 9000
log_level = "DEBUG"
""")
        data = _load_toml(path)
        config = _parse_toml(data)
        assert config.server.transport == "stdio"
        assert config.server.host == "127.0.0.1"
        assert config.server.port == 9000
        assert config.server.log_level == "DEBUG"
        os.unlink(path)

    def test_defaults_when_missing(self):
        path = self._write_toml("""
[[sources]]
id = "mydb"
dsn = "test"
""")
        data = _load_toml(path)
        config = _parse_toml(data)
        assert config.server.transport == "http"
        assert config.server.port == 8002
        assert config.server.log_level == "INFO"
        os.unlink(path)


# ---------------------------------------------------------------------------
# AppConfig helpers
# ---------------------------------------------------------------------------


class TestAppConfig:
    """Test AppConfig helper methods."""

    def _config(self) -> AppConfig:
        return AppConfig(
            sources=[
                SourceConfig(id="primary", dsn="dsn1"),
                SourceConfig(id="secondary", dsn="dsn2"),
            ],
            guardrails=[
                GuardrailConfig(source="primary", readonly=False, max_rows=1000),
                GuardrailConfig(source="secondary", readonly=True, max_rows=500),
            ],
        )

    def test_get_guardrail_existing(self):
        config = self._config()
        g = config.get_guardrail("secondary")
        assert g.readonly is True
        assert g.max_rows == 500

    def test_get_guardrail_missing_returns_default(self):
        config = self._config()
        g = config.get_guardrail("unknown")
        assert g.readonly is False
        assert g.max_rows == 1000
        assert g.source == "unknown"

    def test_get_source(self):
        config = self._config()
        s = config.get_source("secondary")
        assert s is not None
        assert s.dsn == "dsn2"

    def test_get_source_missing(self):
        config = self._config()
        assert config.get_source("nonexistent") is None

    def test_default_source(self):
        config = self._config()
        assert config.default_source is not None
        assert config.default_source.id == "primary"

    def test_default_source_empty(self):
        config = AppConfig()
        assert config.default_source is None


# ---------------------------------------------------------------------------
# CLI DSN override
# ---------------------------------------------------------------------------


class TestCliOverride:
    """Test that CLI args override TOML and env."""

    def test_dsn_creates_default_source(self):
        config = load_config(dsn="Driver={ODBC Driver 18};Server=localhost;Database=test")
        assert len(config.sources) == 1
        assert config.sources[0].id == "default"
        assert "localhost" in config.sources[0].dsn

    def test_transport_override(self):
        config = load_config(
            dsn="Driver={ODBC Driver 18};Server=localhost;Database=test",
            transport="stdio",
        )
        assert config.server.transport == "stdio"

    def test_port_override(self):
        config = load_config(
            dsn="Driver={ODBC Driver 18};Server=localhost;Database=test",
            port=9999,
        )
        assert config.server.port == 9999
