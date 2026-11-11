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

``native_params`` format: a nested dict that the adapter deep-merges into the vendor
request. The dotted ``reasoning_control.parameter`` plus the native value becomes
nested dicts (``"a.b"`` with ``5`` -> ``{"a": {"b": 5}}``); the member's ``native``
dict is then deep-merged on top and wins. ``default`` reasoning adds nothing.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from llm_crossjury.core.providers.base import Backend, ReasoningEffort
from llm_crossjury.core.registry.schema import (
    ModelEntry,
    ModelRegistryFile,
    NativeValue,
    ProviderAliases,
    ReasoningControlType,
)

MODELS_PATH_ENV = "LLM_CROSSJURY_MODELS"
"""Environment variable that overrides the registry location."""

ALIAS_NAMES = ("fast", "balanced", "best")
"""The portable alias names every provider may define."""

_MAPPABLE = (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET)
_MAPPED_LEVELS = (ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH)


class Role(StrEnum):
    ANSWERER = "answerer"
    REVIEWER = "reviewer"
    AGREEMENT_JUDGE = "agreement_judge"
    CHAIRMAN = "chairman"


# No docstring: it would leak into the committed run-config JSON Schema.
class ResolutionMode(StrEnum):
    NORMAL = "normal"
    STRICT = "strict"


class ResolutionError(Exception):
    """Unknown alias, unsupported backend, or (strict mode) unmappable reasoning."""


class RegistryError(Exception):
    """The registry file is missing, malformed, or internally inconsistent."""


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


def resolve_models_path(base_dir: Path) -> Path:
    """Return the registry file to load.

    Precedence: the ``LLM_CROSSJURY_MODELS`` environment variable (if set and
    non-empty), then ``base_dir/config/models.local.yaml`` if it exists (git-ignored,
    a full replacement for local experiments; it hides models added to the shipped
    file later), then ``base_dir/config/models.yaml`` (committed, verified entries,
    ADR 0006 as amended by D-7).

    Args:
        base_dir: Directory that contains ``config/`` (usually the project root).

    Returns:
        The path to load. It is not checked for existence except for the local file.
    """
    override = os.environ.get(MODELS_PATH_ENV)
    if override:
        return Path(override)
    local = base_dir / "config" / "models.local.yaml"
    if local.is_file():
        return local
    return base_dir / "config" / "models.yaml"


def _validate_entry(entry: ModelEntry) -> list[str]:
    problems: list[str] = []
    name = f"{entry.provider}/{entry.model_id}"
    control = entry.reasoning_control
    if control.type in _MAPPABLE:
        if control.mapping is None:
            problems.append(f"{name}: reasoning_control '{control.type}' needs a mapping")
        if not control.parameter:
            problems.append(f"{name}: reasoning_control '{control.type}' needs a parameter")
        if control.mapping is not None and control.allowed is not None:
            mapping = control.mapping
            values = [v for v in (mapping.low, mapping.medium, mapping.high) if v is not None]
            if control.type is ReasoningControlType.LEVELS:
                bad = [v for v in values if v not in control.allowed]
            else:
                bad = _out_of_budget(values, control.allowed)
            if bad:
                problems.append(f"{name}: mapping values {bad!r} are not allowed")
    if Backend.CLI in entry.backends and entry.cli_flags is None:
        problems.append(f"{name}: the cli backend needs cli_flags")
    return problems


def _out_of_budget(
    values: list[NativeValue], allowed: tuple[NativeValue, ...]
) -> list[NativeValue]:
    bounds = [v for v in allowed if isinstance(v, int)]
    if len(bounds) != len(allowed) or len(bounds) != 2:  # noqa: PLR2004
        return values  # allowed is not a valid (min, max): flag every value
    low, high = bounds
    return [v for v in values if not isinstance(v, int) or not low <= v <= high]


class ModelRegistry:
    """Validated, indexed view over a :class:`ModelRegistryFile`.

    Construction validates the data: no duplicate ``(provider, model_id)``, every
    alias points at an entry of the same provider, ``levels``/``budget`` controls have
    a mapping and a parameter (with values inside ``allowed``), and ``cli`` entries
    have ``cli_flags``.

    Raises:
        RegistryError: if the data is inconsistent.
    """

    def __init__(self, data: ModelRegistryFile) -> None:
        problems: list[str] = []
        index: dict[tuple[str, str], ModelEntry] = {}
        for entry in data.models:
            key = (entry.provider, entry.model_id)
            if key in index:
                problems.append(f"{entry.provider}/{entry.model_id}: duplicate entry")
            index[key] = entry
            problems.extend(_validate_entry(entry))
        for provider, aliases in data.aliases.items():
            for alias in ALIAS_NAMES:
                target = getattr(aliases, alias)
                if target is not None and (provider, target) not in index:
                    problems.append(
                        f"alias {provider}.{alias} -> {target!r}: no entry of that provider"
                    )
        if problems:
            raise RegistryError("invalid model registry: " + "; ".join(problems))
        self._data = data
        self._index = index

    @classmethod
    def load(cls, path: Path) -> ModelRegistry:
        """Load ``config/models.yaml``. Validates that every alias points at an entry
        of the same provider and that ``levels``/``budget`` controls have a mapping.

        Args:
            path: Registry file, usually from :func:`resolve_models_path`.

        Raises:
            RegistryError: if the file is missing, not valid YAML, or invalid.
        """
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RegistryError(f"cannot read model registry {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise RegistryError(f"model registry {path} is not valid YAML: {exc}") from exc
        try:
            data = ModelRegistryFile.model_validate(raw)
        except ValidationError as exc:
            raise RegistryError(f"model registry {path} is invalid: {exc}") from exc
        return cls(data)

    def get(self, provider: str, model_id: str) -> ModelEntry | None:
        """Return the entry for ``(provider, model_id)``, or ``None`` if unregistered."""
        return self._index.get((provider, model_id))

    def entries(self) -> tuple[ModelEntry, ...]:
        """Return all entries in file order."""
        return self._data.models

    def aliases(self, provider: str) -> ProviderAliases | None:
        """Return the aliases of ``provider``, or ``None`` if it has none."""
        return self._data.aliases.get(provider)

    def unverified(self) -> tuple[ModelEntry, ...]:
        """Entries without ``source_url`` or ``last_verified``."""
        return tuple(
            e for e in self._data.models if e.source_url is None or e.last_verified is None
        )


def _nest(path: str, value: NativeValue) -> dict[str, Any]:
    parts = path.split(".")
    nested: dict[str, Any] = {parts[-1]: value}
    for part in reversed(parts[:-1]):
        nested = {part: nested}
    return nested


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict: ``override`` deep-merged onto ``base`` (``override`` wins).

    Nested dicts are merged key by key; any other value in ``override`` replaces the
    value in ``base``. Neither input is modified.

    Args:
        base: The lower-precedence parameters.
        override: The higher-precedence parameters.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _fill(template: tuple[str, ...], placeholder: str, value: str) -> tuple[str, ...]:
    # Plain replacement (never str.format): each template token stays one argv token.
    return tuple(token.replace(placeholder, value) for token in template)


class ModelResolver:
    """Implements the steps in the module docstring."""

    def __init__(self, registry: ModelRegistry, mode: ResolutionMode) -> None:
        self._registry = registry
        self._mode = mode

    @property
    def registry(self) -> ModelRegistry:
        """The registry this resolver reads."""
        return self._registry

    def _unmappable(self, message: str) -> tuple[str, ...]:
        if self._mode is ResolutionMode.STRICT:
            raise ResolutionError(message)
        return (message + "; downgraded to 'default'",)

    def resolve_alias(self, provider: str, name: str) -> str:
        """Return the concrete ``model_id`` for an alias, or ``name`` unchanged if it is
        not an alias. Raises :class:`ResolutionError` for an alias set to ``None``."""
        if name not in ALIAS_NAMES:
            return name
        aliases = self._registry.aliases(provider)
        target = None if aliases is None else getattr(aliases, name)
        if target is None:
            raise ResolutionError(
                f"alias '{name}' of provider '{provider}' is not set in the model registry"
            )
        return str(target)

    def map_reasoning(
        self, entry: ModelEntry | None, level: ReasoningEffort
    ) -> tuple[ReasoningEffort, NativeValue | None, tuple[str, ...]]:
        """Return ``(effective_level, native_value, warnings)``; may raise in strict mode.

        ``default`` always maps to ``(default, None, ())``. Any other level needs a
        registered entry with a ``levels``/``budget`` control whose mapping for the
        level is not ``None``.

        Raises:
            ResolutionError: in strict mode, if the level cannot be mapped.
        """
        if level is ReasoningEffort.DEFAULT:
            return ReasoningEffort.DEFAULT, None, ()
        if entry is None:
            return (
                ReasoningEffort.DEFAULT,
                None,
                self._unmappable(f"reasoning '{level}' needs a registered model"),
            )
        control = entry.reasoning_control
        value: NativeValue | None = None
        if control.type in _MAPPABLE and control.mapping is not None:
            value = getattr(control.mapping, level.value)
        if value is None:
            return (
                ReasoningEffort.DEFAULT,
                None,
                self._unmappable(
                    f"reasoning '{level}' is not supported by {entry.provider}/{entry.model_id} "
                    f"(control '{control.type}')"
                ),
            )
        return level, value, ()

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
        (explicit member setting or role default; see llm_crossjury.core.config).

        Reasoning goes into ``native_params`` for the ``api`` and ``mock`` backends
        and into ``cli_args`` for the ``cli`` backend (the API parameter path means
        nothing to a CLI). ``native`` is merged into ``native_params`` for every
        backend. The ``mock`` backend skips the backend check.

        Raises:
            ResolutionError: unknown alias, unsupported backend, or (strict mode) an
                unmappable level or an unregistered model on the ``cli`` backend.
        """
        model_id = self.resolve_alias(provider, model)
        entry = self._registry.get(provider, model_id)
        if entry is not None and backend is not Backend.MOCK and backend not in entry.backends:
            supported = ", ".join(b.value for b in entry.backends)
            raise ResolutionError(
                f"{provider}/{model_id} does not support backend '{backend}' "
                f"(registry lists: {supported})"
            )

        effective, value, warnings = self.map_reasoning(entry, reasoning)
        cli_args: tuple[str, ...] = ()
        if backend is Backend.CLI:
            if entry is None or entry.cli_flags is None:
                warnings += self._cli_unregistered(provider, model_id)
            else:
                flags = entry.cli_flags
                if value is not None and flags.reasoning is None:
                    warnings += self._unmappable(
                        f"reasoning '{effective}' cannot be set via the CLI for "
                        f"{provider}/{model_id}"
                    )
                    effective, value = ReasoningEffort.DEFAULT, None
                if flags.model is not None:
                    cli_args += _fill(flags.model, "{model}", entry.model_id)
                if value is not None and flags.reasoning is not None:
                    cli_args += _fill(flags.reasoning, "{value}", str(value))

        params: dict[str, Any] = {}
        parameter = entry.reasoning_control.parameter if entry is not None else None
        if backend is not Backend.CLI and value is not None and parameter:
            params = _nest(parameter, value)
        if native:
            params = deep_merge(params, native)

        prefix = f"{member_id} ({role}): "
        return ResolvedModel(
            member_id=member_id,
            role=role,
            provider=provider,
            requested_model=model,
            model_id=model_id,
            backend=backend,
            registered=entry is not None,
            requested_reasoning=reasoning,
            effective_reasoning=effective,
            native_reasoning=value,
            native_params=params,
            cli_args=cli_args,
            warnings=tuple(prefix + w for w in warnings),
        )

    def _cli_unregistered(self, provider: str, model_id: str) -> tuple[str, ...]:
        message = (
            f"{provider}/{model_id} has no registry cli_flags, so the model cannot be "
            "selected on the CLI; the CLI's own default model will be used"
        )
        if self._mode is ResolutionMode.STRICT:
            raise ResolutionError(message)
        return (message,)


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
