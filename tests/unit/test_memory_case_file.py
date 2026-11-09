"""Case file for a jury question asked mid-session."""

from __future__ import annotations

import asyncio

from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.memory.case_file import (
    CASE_FILE_MAX_TOKENS,
    build_case_file_sync,
    make_case_file_builder,
)
from llm_crossjury.core.memory.index import MemoryIndex
from llm_crossjury.core.sessions.interfaces import CaseFileBuilder
from llm_crossjury.core.sessions.models import MemoryState, TurnStatus
from tests.unit._session_fakes import chat_turn, finish_result, jury_turn, make_session


def test_empty_session_has_no_case_file() -> None:
    builder: CaseFileBuilder = make_case_file_builder(MemoryIndex(None))
    pending = make_session([chat_turn("q", None, status=TurnStatus.PENDING)])

    async def both() -> list[str | None]:
        return [await builder(make_session(), "question"), await builder(pending, "question")]

    assert asyncio.run(both()) == [None, None]


def test_builder_returns_the_case_file() -> None:
    builder = make_case_file_builder(MemoryIndex(None))
    session = make_session([chat_turn("Which license?", "Apache-2.0.")])

    async def build() -> str | None:
        return await builder(session, "license?")

    text = asyncio.run(build())
    assert text is not None
    assert "Apache-2.0." in text


def test_case_file_holds_summary_facts_snippets_and_last_exchange() -> None:
    old = chat_turn("Which license?", "We chose the Apache-2.0 license.")
    filler = [chat_turn(f"other {i}", f"answer {i}") for i in range(3)]
    last = jury_turn(
        "Which database?",
        [("openai", "o", "Postgres."), ("xai", "x", "SQLite.")],
        synthesis={"finish": finish_result("Postgres for production.")},
    )
    memory = MemoryState(summary="A project setup chat.", facts=("Python 3.11 only.",))
    session = make_session([old, *filler, last], memory=memory)
    text = build_case_file_sync(session, "Is the license compatible?", index=MemoryIndex(None))
    assert text is not None
    assert "A project setup chat." in text
    assert "- Python 3.11 only." in text
    assert "Apache-2.0" in text
    assert text.rstrip().endswith("Postgres for production.")
    assert "Last exchange:" in text


def test_case_file_is_capped() -> None:
    turns = [chat_turn(f"question {i} topic", "topic " * 5000) for i in range(6)]
    memory = MemoryState(summary="s " * 20_000, facts=tuple(f"fact {i}" for i in range(50)))
    session = make_session(turns, memory=memory)
    text = build_case_file_sync(session, "topic", index=MemoryIndex(None))
    assert text is not None
    assert estimate_tokens(text) <= CASE_FILE_MAX_TOKENS
    small = build_case_file_sync(session, "topic", index=None, max_tokens=500)
    assert small is not None
    assert estimate_tokens(small) <= 500
