"""FTS5 memory index: indexing, search, hostile queries, deletion, fallback."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llm_crossjury.core.memory import index as index_module
from llm_crossjury.core.memory.index import MemoryIndex, chunk_text, fts_query, open_index
from llm_crossjury.core.sessions.models import Session
from tests.unit._session_fakes import chat_turn, finish_result, jury_turn, make_session

HOSTILE = [
    "",
    '"',
    "'; DROP TABLE chunks; --",
    "NEAR(a b) AND OR NOT *",
    "body:secret",
    "a* ^b -c +d {x y}:z",
    '"unterminated',
    "(((",
    "\x00\x01",
    "日本語のテスト",
    "emoji 🎉 test",
]


def _session_with_turns() -> Session:
    return make_session(
        [
            chat_turn("What database should we use?", "Use PostgreSQL with pgvector."),
            jury_turn(
                "Which queue?",
                [("openai", "openai-model", "RabbitMQ is fine."), ("xai", "x", "Kafka scales.")],
                synthesis={"finish": finish_result("Pick Kafka for throughput.")},
            ),
            chat_turn("Deployment target?", "Kubernetes on Hetzner."),
        ]
    )


def test_search_finds_turns_by_content() -> None:
    session = _session_with_turns()
    index = MemoryIndex(None)
    assert index.available
    index.sync_session(session)
    hits = index.search(session.id, "postgresql database", 3)
    assert hits
    assert hits[0].source == "turn"
    assert "PostgreSQL" in hits[0].text
    jury_hits = index.search(session.id, "kafka", 3)
    assert any("Kafka" in h.text for h in jury_hits)


def test_search_is_scoped_to_the_session_and_respects_exclusions() -> None:
    a = make_session([chat_turn("alpha topic", "zebra facts")])
    b = make_session([chat_turn("other", "zebra elsewhere")])
    index = MemoryIndex(None)
    index.sync_session(a)
    index.sync_session(b)
    hits = index.search(a.id, "zebra", 5)
    assert [h.ref for h in hits] == [a.turns[0].id]
    assert index.search(a.id, "zebra", 5, exclude_refs=[a.turns[0].id]) == []


@pytest.mark.parametrize("query", HOSTILE)
def test_hostile_queries_never_raise(query: str) -> None:
    session = _session_with_turns()
    index = MemoryIndex(None)
    index.sync_session(session)
    hits = index.search(session.id, query, 5)
    assert isinstance(hits, list)
    # The index survives every query.
    assert index.search(session.id, "kubernetes", 5)


def test_fts_query_quotes_every_term() -> None:
    query = fts_query('foo OR "bar" NEAR(x) col:val')
    for term in query.split(" OR "):
        assert term.startswith('"')
        assert term.endswith('"')
    assert fts_query("!!! ??") == ""


def test_cjk_text_is_searchable() -> None:
    session = make_session([chat_turn("数据库选择", "我们决定使用向量数据库。")])
    index = MemoryIndex(None)
    index.sync_session(session)
    hits = index.search(session.id, "向量数据库", 3)
    assert hits
    assert "向量数据库" in hits[0].text


def test_sync_reindexes_changed_turns_and_drops_removed_ones() -> None:
    turn = chat_turn("q", "first wording")
    session = make_session([turn])
    index = MemoryIndex(None)
    index.sync_session(session)
    changed = session.model_copy(
        update={"turns": (turn.model_copy(update={"reply": "second phrasing"}),)}
    )
    index.sync_session(changed)
    assert index.search(session.id, "wording", 3) == []
    assert index.search(session.id, "phrasing", 3)
    index.sync_session(changed.model_copy(update={"turns": ()}))
    assert index.search(session.id, "phrasing", 3) == []


def test_attachment_chunks_and_delete_session() -> None:
    index = MemoryIndex(None)
    text = "\n".join(f"line {i} filler" for i in range(1000)) + "\nunique marker quasar"
    index.index_attachment("s1", "a" * 64, "report.pdf", text)
    index.index_attachment("s1", "a" * 64, "report.pdf", text)  # idempotent
    hits = index.search("s1", "quasar", 5)
    assert len(hits) == 1
    assert hits[0].source == "attachment"
    assert hits[0].label.startswith("report.pdf, part")
    index.delete_session("s1")
    assert index.search("s1", "quasar", 5) == []


def test_file_index_persists_and_is_shared_per_path(tmp_path: Path) -> None:
    path = tmp_path / "sessions" / "index.sqlite3"
    session = make_session([chat_turn("persist", "durable answer")])
    first = MemoryIndex(path)
    first.sync_session(session)
    first.close()
    second = MemoryIndex(path)
    assert second.search(session.id, "durable", 3)
    second.close()
    assert open_index(path) is open_index(path)
    open_index(path).close()


def test_without_fts5_retrieval_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(path: Path | None) -> sqlite3.Connection:
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(index_module.MemoryIndex, "_open", staticmethod(broken))
    index = MemoryIndex(None)
    assert not index.available
    session = make_session([chat_turn("q", "a")])
    index.sync_session(session)
    index.index_attachment("s", "id", "f", "text")
    index.delete_session("s")
    assert index.search(session.id, "q", 3) == []


def test_chunk_text_overlaps_and_covers_everything() -> None:
    text = " ".join(f"w{i}" for i in range(2000))
    chunks = chunk_text(text, size=500, overlap=100)
    assert all(len(c) <= 500 for c in chunks)
    assert chunks[0].startswith("w0")
    assert chunks[-1].endswith("w1999")
    assert chunk_text("") == []
