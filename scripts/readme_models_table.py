"""Print the README's supported-models table from ``config/models.yaml``.

Manual helper, offline. Paste the output between the ``models-table`` markers in
README.md after the registry changes::

    uv run python scripts/readme_models_table.py
"""

from __future__ import annotations

from pathlib import Path

from llm_crossjury.core.providers.base import Backend
from llm_crossjury.core.registry.resolver import ModelRegistry

ROOT = Path(__file__).resolve().parent.parent
VENDORS = {
    "anthropic": ("Anthropic", "Claude Code (`claude`)"),
    "openai": ("OpenAI", "Codex CLI (`codex`)"),
    "gemini": ("Google Gemini", "not offered (API key only)"),
    "xai": ("xAI", "Grok Build (`grok`), experimental"),
}


def table(registry: ModelRegistry) -> str:
    """The Markdown table: one row per vendor, models per backend with aliases."""
    rows = [
        "| Vendor | With an API key | With a subscription |",
        "|---|---|---|",
    ]
    for provider, (label, cli_product) in VENDORS.items():
        aliases: dict[str, list[str]] = {}
        defined = registry.aliases(provider)
        for alias in ("fast", "balanced", "best"):
            target = None if defined is None else getattr(defined, alias)
            if target:
                aliases.setdefault(target, []).append(alias)

        def name(model_id: str, aliases: dict[str, list[str]] = aliases) -> str:
            tags = aliases.get(model_id)
            return f"`{model_id}`" + (f" ({', '.join(tags)})" if tags else "")

        entries = [e for e in registry.entries() if e.provider == provider]
        api = [name(e.model_id) for e in entries if Backend.API in e.backends and e.pricing]
        cli = [
            f"`{e.model_id}`"
            for e in entries
            if Backend.CLI in e.backends and e.cli_flags is not None
        ]
        sub = f"{cli_product}: " + ", ".join(cli) if cli else cli_product
        rows.append(f"| {label} | {', '.join(api) or 'none'} | {sub} |")
    return "\n".join(rows)


if __name__ == "__main__":
    print(table(ModelRegistry.load(ROOT / "config" / "models.yaml")))
