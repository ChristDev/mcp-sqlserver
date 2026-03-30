"""Tests for connection — DSN parsing, host extraction."""

import pytest

from mcp_sqlserver.connection import _extract_host_port


class TestExtractHostPort:
    """Test DSN/connection string parsing."""

    # ODBC format: Server=host,port
    def test_odbc_with_port(self):
        dsn = "Driver={ODBC Driver 18};Server=myserver.example.com,1433;Database=mydb"
        host, port = _extract_host_port(dsn)
        assert host == "myserver.example.com"
        assert port == 1433

    def test_odbc_without_port(self):
        dsn = "Driver={ODBC Driver 18};Server=myserver.example.com;Database=mydb"
        host, port = _extract_host_port(dsn)
        assert host == "myserver.example.com"
        assert port == 1433  # default

    def test_odbc_localhost(self):
        dsn = "Driver={ODBC Driver 18};Server=localhost,1434;Database=mydb"
        host, port = _extract_host_port(dsn)
        assert host == "localhost"
        assert port == 1434

    def test_odbc_data_source(self):
        dsn = "Driver={ODBC Driver 18};Data Source=myserver,2433;Database=mydb"
        host, port = _extract_host_port(dsn)
        assert host == "myserver"
        assert port == 2433

    def test_odbc_case_insensitive(self):
        dsn = "DRIVER={ODBC Driver 18};SERVER=MyServer,1433;DATABASE=mydb"
        host, port = _extract_host_port(dsn)
        assert host == "MyServer"
        assert port == 1433

    # URI format: sqlserver://user:pass@host:port/db
    def test_uri_with_port(self):
        dsn = "sqlserver://user:pass@myserver.example.com:1433/mydb"
        host, port = _extract_host_port(dsn)
        assert host == "myserver.example.com"
        assert port == 1433

    def test_uri_without_port(self):
        dsn = "sqlserver://user:pass@myserver.example.com/mydb"
        host, port = _extract_host_port(dsn)
        assert host == "myserver.example.com"
        assert port == 1433  # default

    def test_uri_with_encoded_password(self):
        dsn = "sqlserver://user:%23%24%25@myserver:1433/mydb"
        host, port = _extract_host_port(dsn)
        assert host == "myserver"
        assert port == 1433

    # Edge cases
    def test_empty_dsn(self):
        host, port = _extract_host_port("")
        assert host is None
        assert port == 1433

    def test_gibberish(self):
        host, port = _extract_host_port("not-a-real-dsn")
        assert host is None
        assert port == 1433
