"""ModelRegistry validation and ModelResolver steps (ADR 0006). Fictitious ids only."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from llm_crossjury.core.providers.base import Backend, ReasoningEffort
from llm_crossjury.core.registry import (
    CliFlags,
    EffortMapping,
    ModelEntry,
    ModelRegistry,
    ModelRegistryFile,
    ModelResolver,
    ProviderAliases,
    ReasoningControl,
    ReasoningControlType,
    RegistryError,
    ResolutionError,
    ResolutionMode,
    Role,
    deep_merge,
    resolve_models_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _entry(
    model_id: str,
    *,
    control: ReasoningControl | None = None,
    backends: tuple[Backend, ...] = (Backend.API,),
    cli_flags: CliFlags | None = None,
    verified: bool = False,
) -> ModelEntry:
    return ModelEntry(
        provider="acme",
        model_id=model_id,
        backends=backends,
        reasoning_control=control
        or ReasoningControl(
            type=ReasoningControlType.NONE, parameter=None, allowed=None, mapping=None
        ),
        cli_flags=cli_flags,
        context_window=None,
        max_output=None,
        structured_output=None,
        pricing=None,
        reasoning_multipliers=None,
        source_url="https://example.invalid/docs" if verified else None,  # type: ignore[arg-type]
        last_verified=date(2026, 1, 1) if verified else None,
        notes=None,
    )


LEVELS = ReasoningControl(
    type=ReasoningControlType.LEVELS,
    parameter="thinking.effort",
    allowed=("lo", "hi"),
    mapping=EffortMapping(low="lo", medium=None, high="hi"),
)
BUDGET = ReasoningControl(
    type=ReasoningControlType.BUDGET,
    parameter="budget_tokens",
    allowed=(0, 1000),
    mapping=EffortMapping(low=10, medium=100, high=1000),
)
FLAGS = CliFlags(model=("--m", "{model}"), reasoning=("--effort={value}",))


def _registry(*entries: ModelEntry, aliases: ProviderAliases | None = None) -> ModelRegistry:
    return ModelRegistry(
        ModelRegistryFile(
            schema_version=1,
            models=entries,
            aliases={"acme": aliases or ProviderAliases(fast=None, balanced=None, best=None)},
        )
    )


@pytest.fixture
def registry() -> ModelRegistry:
    return _registry(
        _entry(
            "acme-levels",
            control=LEVELS,
            backends=(Backend.API, Backend.CLI),
            cli_flags=FLAGS,
            verified=True,
        ),
        _entry("acme-budget", control=BUDGET),
        _entry("acme-plain"),
        aliases=ProviderAliases(fast="acme-plain", balanced=None, best="acme-levels"),
    )


def _resolve(
    resolver: ModelResolver,
    model: str,
    reasoning: ReasoningEffort = ReasoningEffort.DEFAULT,
    backend: Backend = Backend.API,
    native: dict[str, Any] | None = None,
) -> Any:
    return resolver.resolve(
        member_id="m1",
        provider="acme",
        model=model,
        backend=backend,
        role=Role.ANSWERER,
        reasoning=reasoning,
        native=native,
    )


# ----------------------------------------------------------------- registry


def test_registry_indexes_and_reports_unverified(registry: ModelRegistry) -> None:
    assert registry.get("acme", "acme-budget") is not None
    assert registry.get("acme", "nope") is None
    assert registry.get("other", "acme-budget") is None
    assert [e.model_id for e in registry.entries()] == ["acme-levels", "acme-budget", "acme-plain"]
    assert [e.model_id for e in registry.unverified()] == ["acme-budget", "acme-plain"]


def test_alias_to_missing_entry_is_rejected() -> None:
    with pytest.raises(RegistryError, match=r"alias acme\.fast"):
        _registry(_entry("a"), aliases=ProviderAliases(fast="b", balanced=None, best=None))


def test_duplicate_entry_is_rejected() -> None:
    with pytest.raises(RegistryError, match="duplicate"):
        _registry(_entry("a"), _entry("a"))


def test_levels_without_mapping_is_rejected() -> None:
    control = ReasoningControl(
        type=ReasoningControlType.LEVELS, parameter="p", allowed=None, mapping=None
    )
    with pytest.raises(RegistryError, match="needs a mapping"):
        _registry(_entry("a", control=control))


def test_mapping_outside_allowed_is_rejected() -> None:
    control = BUDGET.model_copy(update={"allowed": (0, 50)})
    with pytest.raises(RegistryError, match="not allowed"):
        _registry(_entry("a", control=control))


def test_cli_backend_needs_flags() -> None:
    with pytest.raises(RegistryError, match="cli_flags"):
        _registry(_entry("a", backends=(Backend.CLI,)))


def test_load_shipped_registry() -> None:
    registry = ModelRegistry.load(REPO_ROOT / "config" / "models.yaml")
    assert registry.entries()
    assert registry.unverified() == ()


def test_load_example_registry() -> None:
    registry = ModelRegistry.load(REPO_ROOT / "config" / "models.example.yaml")
    assert registry.entries()
    assert len(registry.unverified()) == len(registry.entries())


def test_load_reports_bad_files(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="cannot read"):
        ModelRegistry.load(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nmodels: []\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="invalid"):
        ModelRegistry.load(bad)


def test_load_round_trip(tmp_path: Path, registry: ModelRegistry) -> None:
    data = ModelRegistryFile(
        schema_version=1,
        models=registry.entries(),
        aliases={"acme": registry.aliases("acme")},  # type: ignore[dict-item]
    )
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(data.model_dump(mode="json")), encoding="utf-8")
    assert ModelRegistry.load(path).entries() == registry.entries()


# ----------------------------------------------------------------- models path


def test_models_path_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_CROSSJURY_MODELS", raising=False)
    (tmp_path / "config").mkdir()
    assert resolve_models_path(tmp_path) == tmp_path / "config" / "models.yaml"
    local = tmp_path / "config" / "models.local.yaml"
    local.write_text("", encoding="utf-8")
    assert resolve_models_path(tmp_path) == local
    monkeypatch.setenv("LLM_CROSSJURY_MODELS", str(tmp_path / "x.yaml"))
    assert resolve_models_path(tmp_path) == tmp_path / "x.yaml"


# ----------------------------------------------------------------- resolver


def test_resolve_alias(registry: ModelRegistry) -> None:
    resolver = ModelResolver(registry, ResolutionMode.NORMAL)
    assert resolver.resolve_alias("acme", "fast") == "acme-plain"
    assert resolver.resolve_alias("acme", "acme-budget") == "acme-budget"
    with pytest.raises(ResolutionError, match="balanced"):
        resolver.resolve_alias("acme", "balanced")
    with pytest.raises(ResolutionError):
        resolver.resolve_alias("unknown-provider", "best")


def test_levels_mapping_builds_nested_params(registry: ModelRegistry) -> None:
    resolver = ModelResolver(registry, ResolutionMode.STRICT)
    r = _resolve(resolver, "best", ReasoningEffort.HIGH)
    assert r.model_id == "acme-levels"
    assert r.requested_model == "best"
    assert r.registered
    assert r.effective_reasoning is ReasoningEffort.HIGH
    assert r.native_reasoning == "hi"
    assert r.native_params == {"thinking": {"effort": "hi"}}
    assert r.cli_args == ()
    assert r.warnings == ()


def test_native_passthrough_is_deep_merged_and_wins(registry: ModelRegistry) -> None:
    resolver = ModelResolver(registry, ResolutionMode.NORMAL)
    native = {"thinking": {"effort": "custom", "extra": 1}, "top": True}
    r = _resolve(resolver, "acme-budget", ReasoningEffort.MEDIUM, native=native)
    assert r.native_params == {
        "budget_tokens": 100,
        "thinking": {"effort": "custom", "extra": 1},
        "top": True,
    }
    r = _resolve(resolver, "best", ReasoningEffort.LOW, native=native)
    assert r.native_params == {"thinking": {"effort": "custom", "extra": 1}, "top": True}


def test_default_reasoning_adds_nothing(registry: ModelRegistry) -> None:
    resolver = ModelResolver(registry, ResolutionMode.STRICT)
    r = _resolve(resolver, "best")
    assert r.native_params == {}
    assert r.native_reasoning is None


@pytest.mark.parametrize(
    ("model", "level"),
    [
        ("acme-levels", ReasoningEffort.MEDIUM),  # mapping is None
        ("acme-plain", ReasoningEffort.HIGH),  # control none
        ("unregistered", ReasoningEffort.LOW),  # not in the registry
    ],
)
def test_unmappable_level_downgrades_or_raises(
    registry: ModelRegistry, model: str, level: ReasoningEffort
) -> None:
    r = _resolve(ModelResolver(registry, ResolutionMode.NORMAL), model, level)
    assert r.effective_reasoning is ReasoningEffort.DEFAULT
    assert r.requested_reasoning is level
    assert r.native_params == {}
    assert len(r.warnings) == 1
    assert r.warnings[0].startswith("m1 (answerer): ")
    with pytest.raises(ResolutionError):
        _resolve(ModelResolver(registry, ResolutionMode.STRICT), model, level)


def test_unregistered_model_with_default_is_allowed(registry: ModelRegistry) -> None:
    r = _resolve(ModelResolver(registry, ResolutionMode.STRICT), "acme-new", native={"k": 1})
    assert not r.registered
    assert r.model_id == "acme-new"
    assert r.native_params == {"k": 1}
    assert r.warnings == ()


def test_unsupported_backend_raises(registry: ModelRegistry) -> None:
    with pytest.raises(ResolutionError, match="backend"):
        _resolve(ModelResolver(registry, ResolutionMode.NORMAL), "acme-plain", backend=Backend.CLI)


def test_mock_backend_skips_backend_check(registry: ModelRegistry) -> None:
    r = _resolve(ModelResolver(registry, ResolutionMode.STRICT), "acme-plain", backend=Backend.MOCK)
    assert r.backend is Backend.MOCK


def test_cli_args_from_registry_templates(registry: ModelRegistry) -> None:
    resolver = ModelResolver(registry, ResolutionMode.STRICT)
    r = _resolve(resolver, "best", ReasoningEffort.HIGH, backend=Backend.CLI, native={"x": 1})
    assert r.cli_args == ("--m", "acme-levels", "--effort=hi")
    # The API parameter path means nothing to a CLI: reasoning goes into argv only.
    assert r.native_params == {"x": 1}
    r = _resolve(resolver, "best", backend=Backend.CLI)
    assert r.cli_args == ("--m", "acme-levels")


def test_cli_without_reasoning_flag_downgrades() -> None:
    flags = CliFlags(model=("--m", "{model}"), reasoning=None)
    registry = _registry(_entry("a", control=LEVELS, backends=(Backend.CLI,), cli_flags=flags))
    r = _resolve(
        ModelResolver(registry, ResolutionMode.NORMAL),
        "a",
        ReasoningEffort.LOW,
        backend=Backend.CLI,
    )
    assert r.effective_reasoning is ReasoningEffort.DEFAULT
    assert r.cli_args == ("--m", "a")
    assert r.warnings
    with pytest.raises(ResolutionError):
        _resolve(
            ModelResolver(registry, ResolutionMode.STRICT),
            "a",
            ReasoningEffort.LOW,
            backend=Backend.CLI,
        )


def test_unregistered_cli_model_warns_or_raises(registry: ModelRegistry) -> None:
    r = _resolve(ModelResolver(registry, ResolutionMode.NORMAL), "acme-new", backend=Backend.CLI)
    assert r.cli_args == ()
    assert "default model" in r.warnings[0]
    with pytest.raises(ResolutionError):
        _resolve(ModelResolver(registry, ResolutionMode.STRICT), "acme-new", backend=Backend.CLI)


def test_deep_merge_does_not_mutate_inputs() -> None:
    base: dict[str, Any] = {"a": {"b": 1}}
    over: dict[str, Any] = {"a": {"c": 2}}
    assert deep_merge(base, over) == {"a": {"b": 1, "c": 2}}
    assert base == {"a": {"b": 1}}
    assert over == {"a": {"c": 2}}
