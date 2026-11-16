"""Session store: files, persistence switch, locking, recovery, damaged files."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

import llm_crossjury.core.sessions.store as store_module
from llm_crossjury.core.sessions.interfaces import SessionNotFoundError
from llm_crossjury.core.sessions.models import ChatTurn, JuryTurn, ModelRef, Session, TurnStatus
from llm_crossjury.core.sessions.store import (
    INTERRUPTED_ERROR,
    SessionStore,
    is_session_id,
    new_id,
    stored_attachment_ids,
)

ATT_A = "a" * 64
ATT_B = "b" * 64


def _jury(turn_id: str | None = None, **extra: object) -> JuryTurn:
    return JuryTurn(
        id=turn_id or new_id(),
        created_at=datetime.now(UTC),
        task="What is 2+2?",
        **extra,  # type: ignore[arg-type]
    )


def _chat(**extra: object) -> ChatTurn:
    return ChatTurn(
        id=new_id(),
        created_at=datetime.now(UTC),
        message="Hello there",
        model=ModelRef(provider="openai", model="balanced"),
        **extra,  # type: ignore[arg-type]
    )


def _append(turn: JuryTurn | ChatTurn):  # type: ignore[no-untyped-def]
    return lambda s: s.model_copy(update={"turns": (*s.turns, turn)})


def test_ids() -> None:
    assert is_session_id(new_id())
    assert not is_session_id("../etc/passwd")
    assert not is_session_id("A" * 32)
    assert not is_session_id("a" * 31)


def test_create_get_list_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    first = store.create("First")
    second = store.create()
    assert (tmp_path / "sessions" / f"{first.id}.json").is_file()
    assert store.get(first.id) == first
    store.update(second.id, _append(_jury(attachment_ids=(ATT_A,))))
    rows = store.list()
    assert [r.id for r in rows] == [second.id, first.id]  # newest updated_at first
    assert rows[0].title == "What is 2+2?"
    assert rows[0].turns == 1
    assert rows[0].pending is True
    # a new store instance reads the same files
    again = SessionStore(tmp_path / "sessions")
    assert again.get(first.id).title == "First"


def test_update_moves_past_a_frozen_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Python 3.11 on Windows reads the clock at ~15 ms, so writes can share a timestamp.
    frozen = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(store_module, "_now", lambda: frozen)
    store = SessionStore(tmp_path / "sessions")
    first = store.create("First")
    second = store.create("Second")
    updated = store.update(second.id, lambda s: s)
    assert updated.updated_at > frozen
    assert [r.id for r in store.list()] == [second.id, first.id]


def test_update_bumps_updated_at_and_rejects_id_change(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create()
    updated = store.update(session.id, lambda s: s.model_copy(update={"title": "T"}))
    assert updated.updated_at >= session.updated_at
    assert store.get(session.id).title == "T"
    with pytest.raises(ValueError, match="id"):
        store.update(session.id, lambda s: s.model_copy(update={"id": new_id()}))
    with pytest.raises(SessionNotFoundError):
        store.update(new_id(), lambda s: s)


def test_update_is_serialized_per_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create()

    def add_many() -> None:
        for _ in range(20):
            store.update(session.id, _append(_chat()))

    threads = [threading.Thread(target=add_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.get(session.id).turns) == 80


def test_unknown_and_malformed_ids(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    for bad in (new_id(), "..", "../../x", "x" * 32):
        with pytest.raises(SessionNotFoundError):
            store.get(bad)
    assert store.delete("../x") is False
    assert store.delete(new_id()) is False


def test_delete_and_delete_all(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    a = store.create()
    b = store.create()
    assert store.delete(a.id) is True
    assert store.delete(a.id) is False
    assert not (tmp_path / f"{a.id}.json").exists()
    (tmp_path / f"{new_id()}.json").write_text("{broken", encoding="utf-8")
    assert store.delete_all() == 2  # b and the damaged file
    assert store.list() == []
    with pytest.raises(SessionNotFoundError):
        store.get(b.id)


def test_damaged_file_is_skipped_and_not_found(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    good = store.create("ok")
    broken = new_id()
    (tmp_path / f"{broken}.json").write_text('{"id": "nope"}', encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    assert [r.id for r in store.list()] == [good.id]
    with pytest.raises(SessionNotFoundError):
        store.get(broken)
    # a store created over the damaged file starts fine
    assert [r.id for r in SessionStore(tmp_path).list()] == [good.id]


def test_memory_only_mode_never_writes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, persist=False)
    session = store.create("in memory")
    store.update(session.id, _append(_chat()))
    assert list(tmp_path.iterdir()) == []
    assert store.get(session.id).title == "in memory"
    assert [r.id for r in store.list()] == [session.id]
    assert SessionStore(None).create().id  # no directory at all


def test_turning_saving_off_keeps_files_and_on_flushes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    saved = store.create("saved")
    store.set_persist(False)
    assert store.persist is False
    store.update(saved.id, lambda s: s.model_copy(update={"title": "changed"}))
    memory = store.create("memory")
    assert (tmp_path / f"{saved.id}.json").is_file()
    assert json.loads((tmp_path / f"{saved.id}.json").read_text("utf-8"))["title"] == "saved"
    assert store.get(saved.id).title == "changed"
    assert not (tmp_path / f"{memory.id}.json").exists()
    store.set_persist(True)
    assert (tmp_path / f"{memory.id}.json").is_file()
    assert SessionStore(tmp_path).get(saved.id).title == "changed"


def test_start_up_marks_pending_work_interrupted(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create()
    done = _jury(status=TurnStatus.DONE, synthesis={"pending": "finish", "agreement": {"x": 1}})
    store.update(session.id, _append(done))
    store.update(session.id, _append(_chat()))
    recovered = SessionStore(tmp_path).get(session.id)
    jury, chat = recovered.turns
    assert isinstance(jury, JuryTurn)
    assert jury.status is TurnStatus.DONE
    assert "pending" not in jury.synthesis
    assert jury.synthesis["agreement"] == {"x": 1}
    assert jury.synthesis["finish_error"] == INTERRUPTED_ERROR
    assert chat.status is TurnStatus.ERROR
    assert chat.error == INTERRUPTED_ERROR


def test_attachment_ids(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    a = store.create()
    store.update(a.id, _append(_jury(attachment_ids=(ATT_A,))))
    store.set_persist(False)
    b = store.create()
    store.update(b.id, _append(_chat(attachment_ids=(ATT_B,))))
    assert store.attachment_ids() == frozenset({ATT_A, ATT_B})
    assert stored_attachment_ids(tmp_path) == frozenset({ATT_A})
    assert stored_attachment_ids(tmp_path / "missing") == frozenset()


def test_writes_are_atomic(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create()
    for _ in range(3):
        store.update(session.id, _append(_chat()))
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{session.id}.json"]
    Session.model_validate_json((tmp_path / f"{session.id}.json").read_bytes())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_owner_only_permissions(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    store = SessionStore(root)
    session = store.create()
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / f"{session.id}.json").st_mode) == 0o600
