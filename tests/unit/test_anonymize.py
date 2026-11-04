"""The name-scrubbing pattern: case-insensitive, whole words, version suffixes included."""

from __future__ import annotations

import pytest

from llm_crossjury.core.anonymize import NAME_PATTERN, REDACTION


def _sub(text: str) -> str:
    return NAME_PATTERN.sub(REDACTION, text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # canonical case
        ("As Claude, I think", "As [model], I think"),
        ("trained by OpenAI.", "trained by [model]."),
        ("built by xAI", "built by [model]"),
        # lowercase
        ("i am claude", "i am [model]"),
        ("from openai and google", "from [model] and [model]"),
        ("chatgpt says", "[model] says"),
        ("as grok would put it", "as [model] would put it"),
        ("gemini here", "[model] here"),
        ("by anthropic", "by [model]"),
        ("xai built me", "[model] built me"),
        # upper and mixed case
        ("CLAUDE", "[model]"),
        ("OpenAi's view", "[model]'s view"),
        ("ChAtGpT", "[model]"),
        ("XAI", "[model]"),
        # hyphenated version suffixes, any case
        ("GPT-4o is", "[model] is"),
        ("gpt-5.1-mini answers", "[model] answers"),
        ("Gpt-X.", "[model]."),
        ("gemini-pro-1.5 and Claude-3", "[model] and [model]"),
        # plain GPT
        ("a GPT model", "a [model] model"),
    ],
)
def test_names_are_replaced(text: str, expected: str) -> None:
    assert _sub(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "claudette",
        "googleplex",
        "geminis",
        "groking",
        "openairport",
        "maxai",
        "chatgpts",
        "anthropically",
    ],
)
def test_word_boundaries_prevent_partial_matches(text: str) -> None:
    assert _sub(text) == text
