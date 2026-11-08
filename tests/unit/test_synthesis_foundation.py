"""Foundation of the synthesis upgrade: structured calls, prompt templates, scrubbing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llm_crossjury.core.anonymize import REDACTION, scrub, scrub_enabled_for
from llm_crossjury.core.prompting import PromptTemplateError, load_template
from llm_crossjury.core.providers.base import (
    Backend,
    CompletionResult,
    Message,
    Prompt,
    Role,
    Usage,
)
from llm_crossjury.core.schemas.agreement import AgreementJudgment
from llm_crossjury.core.synthesis.llm import (
    StructuredOutputError,
    extract_json,
    parse_structured,
    structured_call,
)
from llm_crossjury.core.synthesis.types import CallSpec

VALID = '{"groups": [{"statement": "x", "supporting": [], "contradicting": []}]}'


def _result(text: str) -> CompletionResult:
    return CompletionResult(
        text=text,
        usage=Usage(
            input_tokens=1,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=1,
            reasoning_tokens=0,
        ),
        provider="fake",
        model="fake-model",
        backend=Backend.MOCK,
        finish_reason="stop",
        started_at=datetime.now(UTC),
        latency_s=0.0,
        native_params_used={},
    )


def _spec() -> CallSpec:
    return CallSpec(
        stage="agreement",
        provider="fake",
        model_id="fake-model",
        prompt=Prompt(
            stable_prefix=(Message(role=Role.SYSTEM, content="sys"),),
            variable_suffix=(Message(role=Role.USER, content="go"),),
        ),
        response_schema=None,
        max_output_tokens=100,
        tag="t",
    )


@pytest.mark.parametrize(
    "text",
    [VALID, f"```json\n{VALID}\n```", f"Here you go:\n{VALID}\nThanks."],
)
def test_extract_json_variants(text: str) -> None:
    assert parse_structured(text, AgreementJudgment).groups[0].statement == "x"


def test_extract_json_without_object_fails() -> None:
    with pytest.raises(StructuredOutputError):
        extract_json("no json here")


def test_schema_violation_is_reported() -> None:
    with pytest.raises(StructuredOutputError, match="schema violation"):
        parse_structured('{"groups": [{"statement": 1}]}', AgreementJudgment)


def test_structured_call_retries_once_with_feedback() -> None:
    replies = iter(["not json", VALID])
    seen: list[CallSpec] = []

    async def call(spec: CallSpec) -> CompletionResult:
        seen.append(spec)
        return _result(next(replies))

    parsed, results = asyncio.run(structured_call(call, _spec(), AgreementJudgment))
    assert parsed.groups[0].statement == "x"
    assert len(results) == 2
    retry = seen[1]
    assert retry.tag == "t:retry"
    assert retry.prompt.stable_prefix == seen[0].prompt.stable_prefix  # cache-friendly
    assert retry.prompt.variable_suffix[-2].role is Role.ASSISTANT
    assert "could not be used" in retry.prompt.variable_suffix[-1].content


def test_structured_call_gives_up_after_retry() -> None:
    async def call(spec: CallSpec) -> CompletionResult:
        return _result("still not json")

    with pytest.raises(StructuredOutputError):
        asyncio.run(structured_call(call, _spec(), AgreementJudgment))


def test_template_rendering(tmp_path: Path) -> None:
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "t.v1.md").write_text(
        "---\nid: g/t.v1\nversion: 1\nschema: null\nvariables: [answers, task]\n---\n\n"
        '## stable_prefix\n\nRules. JSON like {"a": 1}.\n\n{answers}\n\n'
        "## variable_suffix\n\nTask: {task}\n",
        encoding="utf-8",
    )
    template = load_template("g/t.v1", tmp_path)
    prompt = template.render(prefix_values={"answers": "A1"}, suffix_values={"task": "Q"})
    assert prompt.stable_prefix[0].content.endswith("A1")
    assert '{"a": 1}' in prompt.stable_prefix[0].content
    assert prompt.variable_suffix[0].content == "Task: Q"
    with pytest.raises(PromptTemplateError):
        template.render(suffix_values={"nope": "x"})


def test_template_substitution_is_single_pass(tmp_path: Path) -> None:
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "t.v1.md").write_text(
        "---\nid: g/t.v1\nvariables: [task, extra]\n---\n\n## stable_prefix\n\n"
        "## variable_suffix\n\n{task} | {extra}\n",
        encoding="utf-8",
    )
    template = load_template("g/t.v1", tmp_path)
    prompt = template.render(suffix_values={"task": "literal {extra}", "extra": "E"})
    assert prompt.variable_suffix[0].content == "literal {extra} | E"


def test_scrub_replaces_names_and_counts() -> None:
    result = scrub("As Claude (by Anthropic) and gpt-4o say, claudette is fine.")
    assert result.text == f"As {REDACTION} (by {REDACTION}) and {REDACTION} say, claudette is fine."
    assert result.replacements == 3


def test_scrub_disabled_when_task_mentions_a_name() -> None:
    assert scrub_enabled_for(["Explain caching"], configured=True)
    assert not scrub_enabled_for(["Compare Gemini and others"], configured=True)
    assert not scrub_enabled_for(["Explain caching"], configured=False)
