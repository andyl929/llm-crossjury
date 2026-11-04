"""Export schemas as strict, provider-neutral JSON Schema.

CONTRACT FILE. Do not modify; report problems in ``docs/contract-issues.md``.

The output is the *canonical* dialect described in ADR 0001: every ``$ref`` is
inlined, ``title``/``default`` keywords are dropped, and nullable scalars are
written as ``{"type": ["string", "null"]}`` instead of ``anyOf``. Provider adapters
may apply further lossless transforms for their own dialects.

Run ``python -m llm_crossjury.core.schemas schemas/`` to regenerate the committed
files; a test fails if they drift from the models.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema

from llm_crossjury.core.schemas.agreement import AgreementJudgment
from llm_crossjury.core.schemas.edits import EditList
from llm_crossjury.core.schemas.review import PeerReview
from llm_crossjury.core.schemas.stage1 import Stage1Answer
from llm_crossjury.core.schemas.synthesis import DocumentSynthesis, Synthesis

EXPORTED_MODELS: tuple[type[BaseModel], ...] = (
    Stage1Answer,
    AgreementJudgment,
    PeerReview,
    EditList,
    Synthesis,
    DocumentSynthesis,
)
"""Top-level models that can be requested as structured output."""

_DROPPED_KEYWORDS = frozenset({"title", "default", "$defs"})
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})


class SchemaConventionError(TypeError):
    """A model violates the strict-subset convention of ADR 0001."""


class _StrictGenerator(GenerateJsonSchema):
    def nullable_schema(self, schema: core_schema.NullableSchema) -> JsonSchemaValue:
        inner = self.generate_inner(schema["schema"])
        inner_type = inner.get("type")
        if not isinstance(inner_type, str) or inner_type not in _SCALAR_TYPES or "enum" in inner:
            raise SchemaConventionError(f"nullable is only allowed on plain scalars, got {inner!r}")
        return {**inner, "type": [inner_type, "null"]}


def _inline(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_inline(item, defs, stack) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        if name in stack:
            raise SchemaConventionError(f"recursive schema via {name!r}")
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        resolved = _inline(defs[name], defs, (*stack, name))
        return {**resolved, **_inline(siblings, defs, stack)}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROPPED_KEYWORDS:
            continue
        if key == "properties":
            out[key] = {prop: _inline(sub, defs, stack) for prop, sub in value.items()}
        else:
            out[key] = _inline(value, defs, stack)
    return out


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return the canonical strict JSON schema of ``model``.

    Raises:
        SchemaConventionError: if the model cannot be expressed in the strict subset.
    """
    raw = model.model_json_schema(schema_generator=_StrictGenerator, mode="validation")
    defs: dict[str, Any] = raw.get("$defs", {})
    result: dict[str, Any] = _inline(raw, defs, ())
    return result


def export_all(directory: Path) -> list[Path]:
    """Write ``<ModelName>.schema.json`` for every model in :data:`EXPORTED_MODELS`."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for model in EXPORTED_MODELS:
        path = directory / f"{model.__name__}.schema.json"
        text = json.dumps(strict_json_schema(model), indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written
