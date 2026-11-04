"""Grok via xAI's official Grok Build CLI, command ``grok`` (experimental)."""

from llm_crossjury.core.providers.cli._subprocess import CliProviderBase


class GrokCliProvider(CliProviderBase):
    """Grok Build CLI adapter.

    The CLI supports subscription login and ``XAI_API_KEY``. Under ADR 0002 the child
    environment never contains ``XAI_API_KEY``, so this backend always uses the
    user's own CLI login. Evaluation against conditions S2-S7 happens in phase 1B.
    """

    name = "xai"
    command = "grok"
