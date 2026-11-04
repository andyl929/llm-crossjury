"""Claude via the official ``claude`` CLI (experimental)."""

from council.core.providers.cli._subprocess import CliProviderBase


class ClaudeCliProvider(CliProviderBase):
    """Runs ``claude`` in non-interactive print mode with tools disabled."""

    name = "anthropic"
    command = "claude"
