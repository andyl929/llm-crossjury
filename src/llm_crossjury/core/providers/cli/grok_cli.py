"""Grok via a ``grok`` CLI (experimental, status pending verification)."""

from llm_crossjury.core.providers.cli._subprocess import CliProviderBase


class GrokCliProvider(CliProviderBase):
    """Placeholder for a Grok CLI adapter.

    ADR 0002 requires the CLI to be *official* (published by the vendor) and to meet
    every security condition. Whether an official xAI CLI exists that does so has not
    yet been verified, so this adapter is skipped until it is (see SPEC Open
    Questions). Grok remains available through the ``api`` backend.
    """

    name = "xai"
    command = "grok"
    skipped_reason = (
        "Not verified to be an official xAI CLI meeting ADR 0002 conditions; "
        "use the api backend for Grok."
    )
