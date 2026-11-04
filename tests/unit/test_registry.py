"""The shipped model registry is valid, placeholder-only, and internally consistent."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from llm_crossjury.core.config_export import CONFIG_MODELS, config_json_schema
from llm_crossjury.core.providers.base import Backend, ReasoningEffort
from llm_crossjury.core.registry import ModelEntry, ModelRegistryFile, ReasoningControlType

REPO_ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER = re.compile(r"^<[^<>]+>$")


@pytest.fixture(scope="module")
def registry() -> ModelRegistryFile:
    raw = yaml.safe_load((REPO_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    return ModelRegistryFile.model_validate(raw)


def test_no_real_models_shipped(registry: ModelRegistryFile) -> None:
    # Facts must come from vendor docs, not from memory: the repo ships placeholders only.
    for entry in registry.models:
        assert entry.provider == "example"
        assert PLACEHOLDER.match(entry.model_id), entry.model_id
        assert entry.source_url is None
        assert entry.last_verified is None


def test_every_provider_has_three_aliases(registry: ModelRegistryFile) -> None:
    for provider in ("anthropic", "openai", "gemini", "xai"):
        assert provider in registry.aliases


def test_aliases_point_to_entries_of_same_provider(registry: ModelRegistryFile) -> None:
    ids = {(e.provider, e.model_id) for e in registry.models}
    for provider, aliases in registry.aliases.items():
        for target in (aliases.fast, aliases.balanced, aliases.best):
            assert target is None or (provider, target) in ids


def test_reasoning_controls_are_complete(registry: ModelRegistryFile) -> None:
    for entry in registry.models:
        control = entry.reasoning_control
        mappable = control.type in (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET)
        assert (control.mapping is not None) == mappable, entry.model_id
        if Backend.CLI in entry.backends:
            assert entry.cli_flags is not None, entry.model_id


@pytest.mark.parametrize("model", CONFIG_MODELS, ids=lambda m: m.__name__)
def test_committed_config_schema_matches_model(model: type[BaseModel]) -> None:
    path = REPO_ROOT / "schemas" / "config" / f"{model.__name__}.schema.json"
    assert path.exists(), "run: uv run python -m llm_crossjury.core.schemas schemas"
    assert json.loads(path.read_text(encoding="utf-8")) == config_json_schema(model)


def test_per_model_multiplier_override_is_partial(registry: ModelRegistryFile) -> None:
    raw = registry.models[0].model_dump(mode="json")
    raw["reasoning_multipliers"] = {"high": 3.0}
    entry = ModelEntry.model_validate(raw)
    assert entry.reasoning_multipliers == {ReasoningEffort.HIGH: 3.0}

    raw["reasoning_multipliers"] = {"extreme": 9.0}
    with pytest.raises(ValidationError):
        ModelEntry.model_validate(raw)
