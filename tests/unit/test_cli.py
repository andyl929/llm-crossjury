"""The CLI skeleton exposes the planned commands."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from council.cli.app import app

runner = CliRunner()


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["run", "--help"],
        ["resume", "--help"],
        ["cost", "estimate", "--help"],
        ["cost", "report", "--help"],
        ["providers", "check", "--help"],
    ],
)
def test_help(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
