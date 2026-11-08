"""Key store, source precedence and the no-leak rule (``core/keys.py``)."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from llm_crossjury.core import keys
from llm_crossjury.core.keys import (
    CONFIG_DIR_ENV,
    PROVIDER_KEY_ENVS,
    InvalidKeyError,
    KeyStoreError,
    UnknownProviderError,
    delete_key,
    key_sources,
    key_store_path,
    load_all_keys,
    save_key,
)

FAKE_KEY = "sk-test-SECRETVALUE-0123456789abcdef"
OTHER = "sk-other-ANOTHERSECRET-9876543210"


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "config"
    monkeypatch.setenv(CONFIG_DIR_ENV, str(directory))
    keys.forget_origins()
    yield directory
    keys.forget_origins()
    for var in PROVIDER_KEY_ENVS.values():  # the code under test sets os.environ directly
        os.environ.pop(var, None)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


def _fragments(value: str) -> list[str]:
    return [value, value[:8], value[-8:], value[8:20]]


def assert_no_leak(text: str, value: str = FAKE_KEY) -> None:
    for fragment in _fragments(value):
        assert fragment not in text


def test_provider_envs_match_the_adapters() -> None:
    from llm_crossjury.core.providers.api import (  # noqa: PLC0415
        AnthropicApiProvider,
        GeminiApiProvider,
        OpenAIApiProvider,
        XaiApiProvider,
    )

    assert dict(PROVIDER_KEY_ENVS) == {
        "anthropic": AnthropicApiProvider.api_key_env,
        "openai": OpenAIApiProvider.api_key_env,
        "gemini": GeminiApiProvider.api_key_env,
        "xai": XaiApiProvider.api_key_env,
    }


def test_store_lives_in_the_config_dir(config_dir: Path) -> None:
    assert key_store_path() == config_dir.resolve() / "keys.env"


def test_config_dir_inside_a_git_tree_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv(CONFIG_DIR_ENV, str(repo / "cfg"))
    with pytest.raises(KeyStoreError):
        key_store_path()
    with pytest.raises(KeyStoreError) as info:
        save_key("anthropic", FAKE_KEY)
    assert_no_leak(str(info.value))


def test_save_writes_store_and_applies_to_environment(base: Path) -> None:
    info = save_key("anthropic", FAKE_KEY, base_dir=base)
    assert os.environ["ANTHROPIC_API_KEY"] == FAKE_KEY
    assert info.is_set
    assert info.source == "settings"
    assert info.stored
    assert_no_leak(repr(info))
    text = key_store_path().read_text(encoding="utf-8")
    assert f"ANTHROPIC_API_KEY={FAKE_KEY}" in text


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_store_is_owner_only_on_posix(base: Path) -> None:
    save_key("openai", FAKE_KEY, base_dir=base)
    assert stat.S_IMODE(key_store_path().stat().st_mode) == 0o600


def test_save_replaces_and_keeps_other_providers(base: Path) -> None:
    save_key("anthropic", FAKE_KEY, base_dir=base)
    save_key("xai", OTHER, base_dir=base)
    save_key("anthropic", OTHER, base_dir=base)
    assert os.environ["ANTHROPIC_API_KEY"] == OTHER
    text = key_store_path().read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    assert f"XAI_API_KEY={OTHER}" in text


def test_pasted_assignment_and_quotes_are_cleaned(base: Path) -> None:
    save_key("gemini", f'  GEMINI_API_KEY="{FAKE_KEY}"\n', base_dir=base)
    assert os.environ["GEMINI_API_KEY"] == FAKE_KEY


@pytest.mark.parametrize(
    "bad", ["", "   ", "short", "has space inside-0123456789", "x" * 2000, "kéy-0123456789"]
)
def test_invalid_values_are_rejected_without_echo(base: Path, bad: str) -> None:
    with pytest.raises(InvalidKeyError) as info:
        save_key("anthropic", bad, base_dir=base)
    if bad.strip():
        assert bad.strip()[:8] not in str(info.value)
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert not key_store_path().exists()


def test_unknown_provider_is_rejected(base: Path) -> None:
    with pytest.raises(UnknownProviderError):
        save_key("mistral", FAKE_KEY, base_dir=base)
    with pytest.raises(UnknownProviderError):
        delete_key("../etc", base_dir=base)


def test_delete_removes_stored_key_and_environment(base: Path) -> None:
    save_key("openai", FAKE_KEY, base_dir=base)
    assert delete_key("openai", base_dir=base) is True
    assert "OPENAI_API_KEY" not in os.environ
    assert FAKE_KEY not in key_store_path().read_text(encoding="utf-8")
    assert delete_key("openai", base_dir=base) is False
    assert key_sources(base)["openai"].is_set is False


def test_delete_falls_back_to_a_key_file(base: Path) -> None:
    save_key("openai", FAKE_KEY, base_dir=base)
    (base / ".openai.key").write_text(OTHER, encoding="utf-8")
    delete_key("openai", base_dir=base)
    assert os.environ["OPENAI_API_KEY"] == OTHER
    assert key_sources(base)["openai"].source == "key_file"


def test_precedence_env_over_dotenv_over_key_file_over_store(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys._write_store(  # seed the store directly
        key_store_path(),
        {
            "ANTHROPIC_API_KEY": "store-anthropic-000",
            "OPENAI_API_KEY": "store-openai-0000",
            "GEMINI_API_KEY": "store-gemini-0000",
            "XAI_API_KEY": "store-xai-000000",
        },
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic-000")
    (base / ".env").write_text(
        "ANTHROPIC_API_KEY=dotenv-anthropic\nOPENAI_API_KEY=dotenv-openai-00\n", encoding="utf-8"
    )
    (base / ".openai.key").write_text("file-openai-0000", encoding="utf-8")
    (base / ".gemini.key").write_text("file-gemini-0000", encoding="utf-8")
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    loaded = load_all_keys(base, PROVIDER_KEY_ENVS)

    assert set(loaded) == {"openai", "gemini", "xai"}
    assert os.environ["ANTHROPIC_API_KEY"] == "env-anthropic-000"
    assert os.environ["OPENAI_API_KEY"] == "dotenv-openai-00"
    assert os.environ["GEMINI_API_KEY"] == "file-gemini-0000"
    assert os.environ["XAI_API_KEY"] == "store-xai-000000"
    sources = {p: i.source for p, i in key_sources(base).items()}
    assert sources == {
        "anthropic": "environment",
        "openai": "dotenv",
        "gemini": "key_file",
        "xai": "settings",
    }
    assert all(i.stored for i in key_sources(base).values())


def test_dotenv_already_loaded_is_attributed_to_dotenv(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (base / ".env").write_text(f"XAI_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY", FAKE_KEY)  # as if load_dotenv ran first
    assert load_all_keys(base, PROVIDER_KEY_ENVS) == ()
    assert key_sources(base)["xai"].source == "dotenv"


def test_save_does_not_override_a_higher_source(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", OTHER)
    info = save_key("gemini", FAKE_KEY, base_dir=base)
    assert os.environ["GEMINI_API_KEY"] == OTHER
    assert info.source == "environment"
    assert info.stored
    assert delete_key("gemini", base_dir=base) is True
    assert os.environ["GEMINI_API_KEY"] == OTHER  # other sources are never touched


def test_sources_without_load_are_derived(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    info = key_sources(base)
    assert info["openai"].source == "environment"
    assert info["anthropic"].is_set is False
    assert info["anthropic"].source is None


def test_load_all_keys_is_compatible_with_load_key_files(base: Path) -> None:
    (base / ".anthropic.key").write_text(FAKE_KEY, encoding="utf-8")
    assert load_all_keys(base, {"anthropic": "ANTHROPIC_API_KEY"}) == ("anthropic",)
    assert os.environ["ANTHROPIC_API_KEY"] == FAKE_KEY


def test_nothing_is_printed_or_logged(
    base: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    save_key("anthropic", FAKE_KEY, base_dir=base)
    load_all_keys(base, PROVIDER_KEY_ENVS)
    key_sources(base)
    delete_key("anthropic", base_dir=base)
    out = capsys.readouterr()
    assert_no_leak(out.out + out.err + caplog.text)
