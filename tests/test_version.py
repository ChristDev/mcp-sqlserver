"""The version has exactly one literal, and the startup banner shows it.

Guards the drift that shipped once already: pyproject.toml said 1.1.0 while
__init__.py and the banner still said 1.0.0.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import mcp_sqlserver
from mcp_sqlserver.config import AppConfig, ServerConfig, SourceConfig
from mcp_sqlserver.main import _print_banner

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_pyproject_derives_the_version_from_the_package():
    # Given: the project manifest
    manifest = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

    # When/Then: it declares the version dynamic rather than repeating the literal
    assert "version" in manifest["project"].get("dynamic", [])
    assert "version" not in manifest["project"]
    assert manifest["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "mcp_sqlserver.__version__"
    }


def test_banner_prints_the_package_version(capsys):
    # Given: a minimal HTTP config
    config = AppConfig(
        sources=[SourceConfig(id="one", dsn="Driver={x};Server=h;Database=d")],
        server=ServerConfig(transport="http", host="127.0.0.1", port=8002),
    )

    # When: the startup banner is printed
    _print_banner(config)

    # Then: it carries the real package version, not a frozen literal
    assert f"v{mcp_sqlserver.__version__}" in capsys.readouterr().err
