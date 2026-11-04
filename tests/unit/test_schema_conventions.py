"""Enforce the strict structured-output subset from ADR 0001."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from council.core.schemas import EXPORTED_MODELS, strict_json_schema

FORBIDDEN_KEYWORDS = {"anyOf", "oneOf", "allOf", "not", "$ref", "$defs", "default", "if"}
CONSTRAINT_KEYWORDS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "format",
}
REPO_ROOT = Path(__file__).resolve().parents[2]


def _walk(node: Any, path: str) -> list[str]:
    problems: list[str] = []
    if isinstance(node, list):
        for i, item in enumerate(node):
            problems += _walk(item, f"{path}[{i}]")
        return problems
    if not isinstance(node, dict):
        return problems

    for key in node.keys() & (FORBIDDEN_KEYWORDS | CONSTRAINT_KEYWORDS):
        problems.append(f"{path}: forbidden keyword {key!r}")

    node_type = node.get("type")
    types = node_type if isinstance(node_type, list) else [node_type]
    if isinstance(node_type, list):
        non_null = [t for t in node_type if t != "null"]
        if len(non_null) != 1 or non_null[0] in {"object", "array"}:
            problems.append(f"{path}: nullable only allowed on scalars, got {node_type}")
        if "enum" in node:
            problems.append(f"{path}: nullable enum")

    if "object" in types:
        props = node.get("properties", {})
        if node.get("additionalProperties") is not False:
            problems.append(f"{path}: additionalProperties must be false")
        if set(node.get("required", [])) != set(props):
            problems.append(f"{path}: every property must be required")
        for name, sub in props.items():
            problems += _walk(sub, f"{path}.{name}")
    if "items" in node:
        problems += _walk(node["items"], f"{path}[]")
    return problems


@pytest.mark.parametrize("model", EXPORTED_MODELS, ids=lambda m: m.__name__)
def test_schema_is_strict_subset(model: type[BaseModel]) -> None:
    assert _walk(strict_json_schema(model), model.__name__) == []


@pytest.mark.parametrize("model", EXPORTED_MODELS, ids=lambda m: m.__name__)
def test_committed_schema_matches_model(model: type[BaseModel]) -> None:
    path = REPO_ROOT / "schemas" / f"{model.__name__}.schema.json"
    assert path.exists(), "run: uv run python -m council.core.schemas schemas"
    committed = json.loads(path.read_text(encoding="utf-8"))
    assert committed == strict_json_schema(model), "schemas/ is stale; regenerate it"
