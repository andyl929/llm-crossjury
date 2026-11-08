"""Structured calls: send a :class:`CallSpec`, parse and validate the JSON reply.

Providers with native structured output return bare JSON. Providers without it get
format instructions appended by the adapter and may wrap the JSON in prose or a code
fence; :func:`extract_json` handles both. One retry is made on invalid output, with
the validation error fed back to the model.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from llm_crossjury.core.providers.base import CompletionResult, Message, Prompt, Role
from llm_crossjury.core.synthesis.types import CallFn, CallSpec

M = TypeVar("M", bound=BaseModel)

_MAX_ERROR_CHARS = 1500
_RETRY_INSTRUCTION = (
    "Your previous reply could not be used: {error}\n\n"
    "Reply again with only one JSON object that satisfies the required schema. "
    "No prose, no code fence."
)


class StructuredOutputError(Exception):
    """The model's reply was not valid JSON for the expected schema (after retry)."""


def extract_json(text: str) -> str:
    """Return the JSON object contained in ``text``.

    Accepts bare JSON, a fenced code block, or JSON surrounded by prose (the outermost
    ``{...}`` span is taken).

    Raises:
        StructuredOutputError: if no ``{...}`` span exists.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        closing = stripped.rfind("```")
        if first_newline != -1 and closing > first_newline:
            stripped = stripped[first_newline + 1 : closing].strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end < start:
        raise StructuredOutputError("reply contains no JSON object")
    return stripped[start : end + 1]


def parse_structured(text: str, model_cls: type[M]) -> M:
    """Parse ``text`` into ``model_cls``.

    Raises:
        StructuredOutputError: on malformed JSON or a schema violation. The message
            describes the problem without echoing the full reply.
    """
    raw = extract_json(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"invalid JSON: {exc.msg} at char {exc.pos}") from None
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        detail = str(exc)[:_MAX_ERROR_CHARS]
        raise StructuredOutputError(f"schema violation: {detail}") from None


async def structured_call(
    call: CallFn, spec: CallSpec, model_cls: type[M]
) -> tuple[M, list[CompletionResult]]:
    """Run ``spec`` and parse the reply into ``model_cls``, retrying once if invalid.

    Returns:
        The parsed object and every :class:`CompletionResult` (one or two), so the
        caller can show tokens and cost.

    Raises:
        StructuredOutputError: if the retry is invalid too.
    """
    first = await call(spec)
    try:
        return parse_structured(first.text, model_cls), [first]
    except StructuredOutputError as exc:
        error = str(exc)
    retry_prompt = Prompt(
        stable_prefix=spec.prompt.stable_prefix,
        variable_suffix=(
            *spec.prompt.variable_suffix,
            Message(role=Role.ASSISTANT, content=first.text or "(empty reply)"),
            Message(role=Role.USER, content=_RETRY_INSTRUCTION.format(error=error)),
        ),
    )
    second = await call(replace(spec, prompt=retry_prompt, tag=f"{spec.tag}:retry"))
    return parse_structured(second.text, model_cls), [first, second]
