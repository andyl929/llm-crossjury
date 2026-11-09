"""User-editable app settings (Settings page; ADR 0008).

Stored as JSON in ``<user config dir>/llm-crossjury/settings.json`` (next to the key
store and ``ui-state.json``; ``LLM_CROSSJURY_CONFIG_DIR`` overrides the directory). A
missing or damaged file means the built-in defaults; a damaged file is left in place
until the user saves.
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.keys import KeyStoreError, config_dir
from llm_crossjury.core.sessions.interfaces import AppSettings, BudgetSettings

SETTINGS_NAME = "settings.json"
_FILE_MODE = 0o600
_DIR_MODE = 0o700

SettingsListener = Callable[[AppSettings], None]


def default_settings_path() -> Path | None:
    """``<config dir>/settings.json``, or ``None`` if the config dir is unusable."""
    try:
        return config_dir() / SETTINGS_NAME
    except KeyStoreError:
        return None


def effective_budget(base: BudgetConfig, settings: BudgetSettings) -> BudgetConfig:
    """``base`` with its limits replaced by the user's (currency and flags kept)."""
    return base.model_copy(
        update={
            "run_limit": settings.run_limit,
            "confirm_above": settings.confirm_above,
            "daily_limit": settings.daily_limit,
        }
    )


class AppSettingsStore:
    """Loads, caches and saves :class:`AppSettings`.

    Args:
        path: The settings file, or ``None`` to keep changes in memory only.
        defaults: Settings used when the file is missing or damaged.
    """

    def __init__(self, path: Path | None, *, defaults: AppSettings | None = None) -> None:
        self._path = path
        self._defaults = defaults if defaults is not None else AppSettings()
        self._lock = threading.Lock()
        self._listeners: list[SettingsListener] = []
        self._current = self._load()

    @property
    def path(self) -> Path | None:
        """The settings file (``None`` = memory only)."""
        return self._path

    @property
    def defaults(self) -> AppSettings:
        """The settings used without a (valid) file."""
        return self._defaults

    def get(self) -> AppSettings:
        """The current settings (usable as a ``SettingsGetter``)."""
        return self._current

    def subscribe(self, listener: SettingsListener) -> None:
        """Call ``listener`` with the new settings after every :meth:`save`."""
        self._listeners.append(listener)

    def save(self, settings: AppSettings) -> AppSettings:
        """Store ``settings``, apply them, and notify the listeners.

        Raises:
            OSError: the file cannot be written (the settings are not applied).
        """
        with self._lock:
            if self._path is not None:
                _write_private(self._path, settings.model_dump_json(indent=2).encode("utf-8"))
            self._current = settings
        for listener in list(self._listeners):
            listener(settings)
        return settings

    def _load(self) -> AppSettings:
        if self._path is None:
            return self._defaults
        try:
            return AppSettings.model_validate_json(self._path.read_bytes())
        except (OSError, ValueError, ValidationError):
            return self._defaults


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        with contextlib.suppress(OSError):
            tmp.chmod(_FILE_MODE)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


__all__ = [
    "SETTINGS_NAME",
    "AppSettingsStore",
    "default_settings_path",
    "effective_budget",
]
