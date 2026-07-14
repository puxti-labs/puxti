"""Shared helpers for CLI command tests."""

import re

from typer.testing import CliRunner

runner = CliRunner()


def plain(text: str) -> str:
    """Strip ANSI escape codes from output for portable assertions."""
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
