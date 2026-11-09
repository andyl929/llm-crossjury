"""Default M1.5 wiring in create_app: chat router, case file builder, index cleanup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from llm_crossjury.core.sessions.store import new_id
from tests.unit.test_web_sessions import TASK, Env, _run_body


def _chat(env: Env, sid: str, message: str) -> dict[str, Any]:
    body = {"message": message, "model": {"provider": "anthropic", "model": "balanced"}}
    response = env.client.post(f"/api/chat?session={sid}&turn={new_id()}", json=body)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


def test_loop_jury_chat_jury_sends_a_default_case_file(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid = env.new_session()
    first = env.client.post(
        f"/api/run?session={sid}&turn={new_id()}", json=_run_body("anthropic", "openai")
    )
    assert first.status_code == 200, first.text

    turn = _chat(env, sid, "Tell me more about zebra-indexing tradeoffs")
    assert turn["status"] == "done", turn
    assert turn["context_tokens"]

    again = env.client.post(
        f"/api/run?session={sid}&turn={new_id()}", json=_run_body("anthropic", "openai")
    )
    assert again.status_code == 200, again.text
    turns = env.session(sid)["turns"]
    assert [t["kind"] for t in turns] == ["jury", "chat", "jury"]
    assert turns[0]["case_file"] is None
    case_file = turns[2]["case_file"]
    assert case_file
    assert "zebra-indexing" in case_file or TASK in case_file


def test_deleting_a_session_drops_its_memory_rows(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid = env.new_session()
    _chat(env, sid, "quokka migration plan")
    index = env.app.state.crossjury_memory_index
    assert index.search(sid, "quokka", 3)
    assert env.client.delete(f"/api/sessions/{sid}").json() == {"deleted": True}
    assert not index.search(sid, "quokka", 3)
