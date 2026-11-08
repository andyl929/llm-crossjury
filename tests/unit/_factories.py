"""Builders for fictitious registry entries and usages used by the cost tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from llm_crossjury.core.providers.base import Backend, ReasoningEffort, Usage
from llm_crossjury.core.registry import (
    CliFlags,
    EffortMapping,
    ModelEntry,
    ModelRegistry,
    ModelRegistryFile,
    Pricing,
    ReasoningControl,
    ReasoningControlType,
)

VERIFIED_ON = date(2026, 1, 1)


def price(
    *,
    currency: str = "USD",
    inp: str = "1",
    out: str = "4",
    cached: str | None = None,
    write: str | None = None,
    reasoning: str | None = None,
    batch: str | None = None,
) -> Pricing:
    def dec(v: str | None) -> Decimal | None:
        return None if v is None else Decimal(v)

    return Pricing(
        currency=currency,
        input_per_mtok=Decimal(inp),
        output_per_mtok=Decimal(out),
        cached_input_per_mtok=dec(cached),
        cache_write_per_mtok=dec(write),
        reasoning_per_mtok=dec(reasoning),
        batch_multiplier=dec(batch),
    )


def entry(
    model_id: str,
    *,
    pricing: Pricing | None = None,
    verified: bool = True,
    last_verified: date = VERIFIED_ON,
    backends: tuple[Backend, ...] = (Backend.API,),
    control: ReasoningControlType = ReasoningControlType.LEVELS,
    multipliers: dict[ReasoningEffort, float] | None = None,
) -> ModelEntry:
    mappable = control in (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET)
    return ModelEntry(
        provider="acme",
        model_id=model_id,
        backends=backends,
        reasoning_control=ReasoningControl(
            type=control,
            parameter="effort" if mappable else None,
            allowed=None,
            mapping=EffortMapping(low="l", medium="m", high="h") if mappable else None,
        ),
        cli_flags=CliFlags(model=("--m", "{model}"), reasoning=None)
        if Backend.CLI in backends
        else None,
        context_window=None,
        max_output=None,
        structured_output=None,
        pricing=pricing,
        reasoning_multipliers=multipliers,
        source_url="https://example.invalid/pricing" if verified else None,  # type: ignore[arg-type]
        last_verified=last_verified if verified else None,
        notes=None,
    )


def registry(*entries: ModelEntry) -> ModelRegistry:
    return ModelRegistry(ModelRegistryFile(schema_version=1, models=entries, aliases={}))


def usage(
    inp: int | None = 0,
    cached: int | None = 0,
    write: int | None = 0,
    out: int | None = 0,
    reasoning: int | None = 0,
) -> Usage:
    return Usage(
        input_tokens=inp,
        cached_input_tokens=cached,
        cache_write_tokens=write,
        output_tokens=out,
        reasoning_tokens=reasoning,
    )
