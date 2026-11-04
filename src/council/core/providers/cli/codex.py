"""OpenAI models via the official ``codex`` CLI (experimental)."""

from council.core.providers.cli._subprocess import CliProviderBase


class CodexCliProvider(CliProviderBase):
    """Runs ``codex`` non-interactively in a read-only sandbox with an empty cwd."""

    name = "openai"
    command = "codex"
