"""Resolve configured members into concrete, callable models (ADR 0006).

Resolution steps for one (member, role):

1. **Alias.** ``model`` may be an alias (``fast`` | ``balanced`` | ``best``) of the
   member's provider or a concrete ``model_id``.
2. **Entry.** Look the id up in the registry. A model that is not registered may
   still be used, but only with reasoning ``default`` (and, for ``api``, it has no
   price, so the run is refused by the cost rules).
3. **Backend.** The member's backend must be in ``entry.backends``.
4. **Reasoning.** The effective level is the member's explicit setting for the
   answerer/chairman role, otherwise the role default from ``roles``. It is mapped to
   a native value through ``entry.reasoning_control``. If the level cannot be mapped
   (control ``none``/``always_on``, or the mapping is ``None``):

   * ``ResolutionMode.NORMAL``: downgrade to ``default`` and add a warning;
   * ``ResolutionMode.STRICT``: raise :class:`ResolutionError`.
5. **Native passthrough.** The member's ``native`` dict is merged last and wins.
6. **CLI args.** For the ``cli`` backend, build argv tokens from ``entry.cli_flags``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from llm_crossjury.core.providers.base import Backend, ReasoningEffort
from llm_crossjury.core.registry.schema import ModelEntry, ModelRegistryFile, NativeValue


class Role(StrEnum):
    ANSWERER = "answerer"
    REVIEWER = "reviewer"
    AGREEMENT_JUDGE = "agreement_judge"
    CHAIRMAN = "chairman"


class ResolutionMode(StrEnum):
    NORMAL = "normal"
    STRICT = "strict"


class ResolutionError(Exception):
    """Unknown alias, unsupported backend, or (strict mode) unmappable reasoning."""


@dataclass(frozen=True)
class ResolvedModel:
    """A member resolved for one role. Shown in the pre-run estimate and recorded."""

    member_id: str
    role: Role
    provider: str
    requested_model: str
    """As written in the config (alias or id)."""
    model_id: str
    backend: Backend
    registered: bool
    requested_reasoning: ReasoningEffort
    effective_reasoning: ReasoningEffort
    native_reasoning: NativeValue | None
    native_params: dict[str, Any]
    """Final vendor parameters (mapped reasoning + passthrough)."""
    cli_args: tuple[str, ...]
    """Model and reasoning argv tokens for the ``cli`` backend; empty for ``api``."""
    warnings: tuple[str, ...]


class ModelRegistry:
    """Validated, indexed view over a :class:`ModelRegistryFile`."""

    def __init__(self, data: ModelRegistryFile) -> None:
        raise NotImplementedError

    @classmethod
    def load(cls, path: Path) -> ModelRegistry:
        """Load ``config/models.yaml``. Validates that every alias points at an entry
        of the same provider and that ``levels``/``budget`` controls have a mapping."""
        raise NotImplementedError

    def get(self, provider: str, model_id: str) -> ModelEntry | None:
        raise NotImplementedError

    def entries(self) -> tuple[ModelEntry, ...]:
        raise NotImplementedError

    def unverified(self) -> tuple[ModelEntry, ...]:
        """Entries without ``source_url`` or ``last_verified``."""
        raise NotImplementedError


class ModelResolver:
    """Implements the steps in the module docstring. Interface only (phase 1A)."""

    def __init__(self, registry: ModelRegistry, mode: ResolutionMode) -> None:
        raise NotImplementedError

    def resolve_alias(self, provider: str, name: str) -> str:
        """Return the concrete ``model_id`` for an alias, or ``name`` unchanged if it is
        not an alias. Raises :class:`ResolutionError` for an alias set to ``None``."""
        raise NotImplementedError

    def map_reasoning(
        self, entry: ModelEntry | None, level: ReasoningEffort
    ) -> tuple[ReasoningEffort, NativeValue | None, tuple[str, ...]]:
        """Return ``(effective_level, native_value, warnings)``; may raise in strict mode."""
        raise NotImplementedError

    def resolve(
        self,
        *,
        member_id: str,
        provider: str,
        model: str,
        backend: Backend,
        role: Role,
        reasoning: ReasoningEffort,
        native: dict[str, Any] | None,
    ) -> ResolvedModel:
        """Resolve one member for one role. ``reasoning`` is the already-chosen level
        (explicit member setting or role default; see llm_crossjury.core.config)."""
        raise NotImplementedError


@dataclass(frozen=True)
class RegistryDiff:
    """Output of ``llm-crossjury models refresh`` (printed by default)."""

    provider: str
    listed_by_vendor_only: tuple[str, ...]
    """Ids the vendor lists that the registry lacks."""
    registry_only: tuple[str, ...]
    """Registry ids the vendor no longer lists (possibly retired)."""


async def refresh_from_vendors(registry: ModelRegistry) -> list[RegistryDiff]:
    """Call each provider's model-list endpoint (api backend, no tokens spent) and
    diff against the registry. Never edits the registry file. Phase 1A."""
    raise NotImplementedError


def write_draft_entries(diffs: list[RegistryDiff], path: Path) -> Path:
    """Write ``models refresh --draft`` output: one draft entry per
    ``listed_by_vendor_only`` id.

    Each draft has the ModelEntry keys with only ``provider`` and ``model_id`` filled
    (exactly as returned by the official endpoint) and every other field ``null``,
    plus ``notes: "UNVERIFIED DRAFT: generated by models refresh on <date>"``. Drafts
    deliberately fail registry validation (e.g. ``reasoning_control: null``) until a
    human completes them from the vendor docs and moves them into ``models.yaml``.
    Refuses to overwrite an existing file.
    """
    raise NotImplementedError
