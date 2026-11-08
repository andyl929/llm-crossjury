"""Per-provider key files (``.<provider>.key``)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from llm_crossjury.core.keys import load_key_files

ENVS = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
FAKE = "fake-key-value-123"


@pytest.mark.parametrize(
    "content",
    [
        FAKE,
        f"{FAKE}\n",
        f"﻿{FAKE}\r\n",
        f"# my key\n\n{FAKE}\n",
        f'ANTHROPIC_API_KEY="{FAKE}"\n',
    ],
)
def test_key_file_formats(tmp_path: Path, content: str) -> None:
    (tmp_path / ".anthropic.key").write_bytes(content.encode("utf-8"))
    loaded = load_key_files(tmp_path, ENVS)
    assert loaded == ("anthropic",)
    assert os.environ["ANTHROPIC_API_KEY"] == FAKE


def test_environment_wins_over_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    (tmp_path / ".gemini.key").write_text(FAKE, encoding="utf-8")
    assert load_key_files(tmp_path, ENVS) == ()
    assert os.environ["GEMINI_API_KEY"] == "from-env"


def test_missing_or_empty_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / ".gemini.key").write_text("\n# nothing\n", encoding="utf-8")
    assert load_key_files(tmp_path, ENVS) == ()
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "GEMINI_API_KEY" not in os.environ


def test_key_files_are_git_ignored() -> None:
    gitignore = Path(__file__).resolve().parents[2] / ".gitignore"
    assert "*.key" in gitignore.read_text(encoding="utf-8").splitlines()
