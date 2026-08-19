"""Scaffold sanity checks: the package imports and the CLI entry point runs."""

import subprocess
import sys

import accrue_ui


def test_version():
    assert accrue_ui.__version__ == "0.1.0.dev0"


def test_cli_main_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "accrue_ui"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "accrue-ui" in result.stdout
