"""Export JSON Schemas for the YAML configuration files (editor validation, CI drift).

These are ordinary Pydantic JSON Schemas, not the strict structured-output dialect of
ADR 0001: they describe files humans edit, not model output.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from llm_crossjury.core.config import CrossjuryConfig
from llm_crossjury.core.registry.schema import ModelRegistryFile

CONFIG_MODELS: tuple[type[BaseModel], ...] = (ModelRegistryFile, CrossjuryConfig)


def config_json_schema(model: type[BaseModel]) -> dict[str, object]:
    return model.model_json_schema(mode="validation")


def export_config_schemas(directory: Path) -> list[Path]:
    """Write ``<ModelName>.schema.json`` for every model in :data:`CONFIG_MODELS`."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for model in CONFIG_MODELS:
        path = directory / f"{model.__name__}.schema.json"
        text = json.dumps(config_json_schema(model), indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written
