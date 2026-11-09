"""Which model writes memory summaries (ADR 0008).

Order:

1. ``AppSettings.memory_model`` when it is usable (registered on its backend, priced
   for ``api`` with its key set, a signed-in CLI for ``cli``);
2. else the ``fast`` alias on a signed-in subscription CLI (no extra cost);
3. else the cheapest priced ``fast`` API model whose key is set;
4. else ``None``: summaries are skipped.

"Signed in" comes from the CLI providers' ``check()`` results, passed in as
``cli_available`` (provider id -> available); a CLI without a result is not used.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from llm_crossjury.core.compare_service import (
    CompareDeps,
    is_usable,
    key_is_set,
    provider_names,
)
from llm_crossjury.core.cost.pricing import PricingError
from llm_crossjury.core.providers.base import Backend
from llm_crossjury.core.sessions.interfaces import AppSettings

FAST_ALIAS = "fast"


@dataclass(frozen=True)
class MemoryModel:
    """The model chosen for memory calls."""

    provider: str
    backend: Backend
    model_id: str
    """Resolved registry model id."""


def _resolve(provider: str, name: str, deps: CompareDeps) -> str | None:
    try:
        return deps.resolver.resolve_alias(provider, name)
    except Exception:  # an unset alias (or unknown provider) is simply not usable
        return None


def _key_ok(provider: str, deps: CompareDeps) -> bool:
    env = deps.key_envs.get(provider)
    return env is None or key_is_set(env)


def _usable(
    provider: str,
    model_id: str,
    backend: Backend,
    deps: CompareDeps,
    cli_available: Mapping[str, bool],
) -> bool:
    if not is_usable(provider, model_id, backend, deps):
        return False
    if backend is Backend.CLI:
        return cli_available.get(provider, False)
    if backend is Backend.API:
        return _key_ok(provider, deps)
    return True


def pick_memory_model(
    deps: CompareDeps,
    settings: AppSettings,
    *,
    cli_available: Mapping[str, bool] | None = None,
) -> MemoryModel | None:
    """Pick the memory model (see the module docstring)."""
    clis = cli_available or {}
    chosen = settings.memory_model
    if chosen is not None:
        model_id = _resolve(chosen.provider, chosen.model, deps)
        if model_id is not None and _usable(chosen.provider, model_id, chosen.backend, deps, clis):
            return MemoryModel(chosen.provider, chosen.backend, model_id)

    for provider in provider_names(deps):
        if (provider, Backend.CLI) not in deps.providers:
            continue
        model_id = _resolve(provider, FAST_ALIAS, deps)
        if (
            model_id is not None
            and model_id != FAST_ALIAS
            and _usable(provider, model_id, Backend.CLI, deps, clis)
        ):
            return MemoryModel(provider, Backend.CLI, model_id)

    best: tuple[Decimal, MemoryModel] | None = None
    for provider in provider_names(deps):
        if (provider, Backend.API) not in deps.providers:
            continue
        model_id = _resolve(provider, FAST_ALIAS, deps)
        if (
            model_id is None
            or model_id == FAST_ALIAS
            or not _usable(provider, model_id, Backend.API, deps, clis)
        ):
            continue
        try:
            price = deps.pricing.lookup(provider, model_id)
        except PricingError:
            continue
        if price.currency != deps.budget.currency:
            continue
        # Summaries read much more than they write: weigh input over output.
        weight = price.input_per_mtok * 4 + price.output_per_mtok
        if best is None or weight < best[0]:
            best = (weight, MemoryModel(provider, Backend.API, model_id))
    return best[1] if best is not None else None


__all__ = ["MemoryModel", "pick_memory_model"]
