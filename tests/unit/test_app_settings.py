"""App settings store: defaults, saving, damaged files, listeners, effective budget."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from llm_crossjury.core import keys
from llm_crossjury.core.app_settings import (
    AppSettingsStore,
    default_settings_path,
    effective_budget,
)
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.sessions.interfaces import AppSettings, BudgetSettings
from llm_crossjury.core.sessions.models import ModelRef


def test_defaults_without_a_file(tmp_path: Path) -> None:
    store = AppSettingsStore(tmp_path / "settings.json")
    assert store.get() == AppSettings()
    assert store.get().budget.run_limit == Decimal("5.00")
    assert store.get().budget.confirm_above == Decimal("1.00")
    assert store.get().budget.daily_limit == Decimal("15.00")
    assert not (tmp_path / "settings.json").exists()


def test_budget_settings_match_budget_config_defaults() -> None:
    config = BudgetConfig()
    settings = BudgetSettings()
    assert (config.run_limit, config.confirm_above, config.daily_limit) == (
        settings.run_limit,
        settings.confirm_above,
        settings.daily_limit,
    )


def test_save_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "settings.json"
    store = AppSettingsStore(path)
    wanted = AppSettings(
        budget=BudgetSettings(run_limit=Decimal("2.50")),
        save_history=False,
        memory_model=ModelRef(provider="gemini", model="fast"),
        context_budget_tokens=50_000,
    )
    seen: list[AppSettings] = []
    store.subscribe(seen.append)
    assert store.save(wanted) == wanted
    assert store.get() == wanted
    assert seen == [wanted]
    assert AppSettingsStore(path).get() == wanted
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


def test_damaged_file_means_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"budget": {"run_limit": "-1"}}', encoding="utf-8")
    defaults = AppSettings(save_history=False)
    assert AppSettingsStore(path, defaults=defaults).get() == defaults
    assert path.read_text(encoding="utf-8").startswith('{"budget"')  # left in place


def test_memory_only_store(tmp_path: Path) -> None:
    store = AppSettingsStore(None)
    store.save(AppSettings(save_history=False))
    assert store.get().save_history is False
    assert store.path is None


def test_default_path_is_in_the_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(keys.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    assert default_settings_path() == (tmp_path / "cfg").resolve() / "settings.json"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv(keys.CONFIG_DIR_ENV, str(repo / "cfg"))
    assert default_settings_path() is None


def test_effective_budget_keeps_currency_and_flags() -> None:
    base = BudgetConfig(currency="EUR", allow_unknown_cost=True)
    budget = effective_budget(
        base, BudgetSettings(run_limit=Decimal("3"), daily_limit=Decimal("9"))
    )
    assert budget.run_limit == Decimal("3")
    assert budget.daily_limit == Decimal("9")
    assert budget.confirm_above == Decimal("1.00")
    assert budget.currency == "EUR"
    assert budget.allow_unknown_cost is True
