"""The CLI skeleton exposes the planned commands."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from llm_crossjury.cli.app import app

runner = CliRunner()

# Rich forces colour when CI/GITHUB_ACTIONS is set; ANSI codes can split option names.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["run", "--help"],
        ["resume", "--help"],
        ["cost", "estimate", "--help"],
        ["cost", "report", "--help"],
        ["providers", "check", "--help"],
        ["models", "list", "--help"],
        ["models", "refresh", "--help"],
    ],
)
def test_help(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output


def test_models_refresh_has_draft_option() -> None:
    result = runner.invoke(app, ["models", "refresh", "--help"])
    assert result.exit_code == 0
    assert "--draft" in _ANSI.sub("", result.output)
