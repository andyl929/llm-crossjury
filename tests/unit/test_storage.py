"""Data directory resolution: precedence, creation, and the git-tree refusal."""

from __future__ import annotations

from pathlib import Path

import platformdirs
import pytest

from llm_crossjury.core.storage import DataDirError, resolve_data_paths


def test_override_wins_and_creates_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CROSSJURY_DATA_DIR", str(tmp_path / "env"))
    paths = resolve_data_paths(tmp_path / "override")
    root = (tmp_path / "override").resolve()
    assert paths.root == root
    assert paths.ledger == root / "ledger.sqlite3"
    assert paths.runs.is_dir()
    assert paths.checkpoints.is_dir()
    assert not paths.ledger.exists()
    assert not (tmp_path / "env").exists()


def test_env_beats_platformdirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CROSSJURY_DATA_DIR", str(tmp_path / "env"))
    assert resolve_data_paths().root == (tmp_path / "env").resolve()


def test_platformdirs_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_CROSSJURY_DATA_DIR", raising=False)
    calls: list[tuple[str, object]] = []

    def fake_user_data_dir(appname: str, appauthor: object) -> str:
        calls.append((appname, appauthor))
        return str(tmp_path / "platform")

    monkeypatch.setattr(platformdirs, "user_data_dir", fake_user_data_dir)
    assert resolve_data_paths().root == (tmp_path / "platform").resolve()
    assert calls == [("llm-crossjury", False)]


def test_refuses_git_working_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(DataDirError, match="git working tree"):
        resolve_data_paths(repo / "data" / "nested")
    assert not (repo / "data").exists()


def test_refuses_git_worktree_file(tmp_path: Path) -> None:
    # Linked worktrees have a ``.git`` file instead of a directory.
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    with pytest.raises(DataDirError):
        resolve_data_paths(repo)
