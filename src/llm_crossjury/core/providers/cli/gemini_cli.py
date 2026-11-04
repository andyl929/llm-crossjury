"""Gemini via the official ``gemini`` CLI (experimental)."""

from llm_crossjury.core.providers.cli._subprocess import CliProviderBase


class GeminiCliProvider(CliProviderBase):
    """Runs ``gemini`` non-interactively with tools disabled and an empty cwd."""

    name = "gemini"
    command = "gemini"
