"""The official SimpleQA grader prompt.

``simpleqa_grader_template.txt`` is copied verbatim from ``GRADER_TEMPLATE`` in
``simpleqa_eval.py`` of https://github.com/openai/simple-evals at commit
652c89d0ca9df547706735883097e9537d40dc47 (after ``.strip()``, as the original uses it).

Attribution: SimpleQA, Jason Wei, Nguyen Karina, Hyung Won Chung, Yunxin Joy Jiao,
Spencer Papay, Amelia Glaese, John Schulman, William Fedus
(https://cdn.openai.com/papers/simpleqa.pdf). simple-evals is released under the MIT
License, Copyright (c) 2024 OpenAI:

    Permission is hereby granted, free of charge, to any person obtaining a copy of
    this software and associated documentation files (the "Software"), to deal in the
    Software without restriction, including without limitation the rights to use,
    copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the
    Software, and to permit persons to whom the Software is furnished to do so,
    subject to the following conditions: The above copyright notice and this
    permission notice shall be included in all copies or substantial portions of the
    Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

Grade = Literal["correct", "incorrect", "not_attempted"]

TEMPLATE_PATH = Path(__file__).with_name("simpleqa_grader_template.txt")
TEMPLATE_SHA256 = "063c04ea798f91c4706c050b66a721afc77a458b12d8f0d72286d4b0eee8a3ee"
"""SHA-256 of the template with ``\\n`` line endings (guards against edits)."""

_LETTER_TO_GRADE: dict[str, Grade] = {"A": "correct", "B": "incorrect", "C": "not_attempted"}


@lru_cache(maxsize=1)
def template() -> str:
    """The verbatim grader template (line endings normalized to ``\\n``).

    Raises:
        ValueError: the file no longer matches the official text.
    """
    text = TEMPLATE_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != TEMPLATE_SHA256:
        raise ValueError("simpleqa_grader_template.txt differs from the official template")
    return text


def grader_prompt(question: str, target: str, predicted_answer: str) -> str:
    """Fill the template exactly as simple-evals does (``str.format``)."""
    return template().format(question=question, target=target, predicted_answer=predicted_answer)


def parse_grade(response_text: str) -> Grade:
    """Map the grader reply to a grade, as simple-evals does: the first ``A``/``B``/``C``
    in the reply; no match counts as ``not_attempted``."""
    match = re.search(r"(A|B|C)", response_text)
    return _LETTER_TO_GRADE[match.group(0)] if match else "not_attempted"
