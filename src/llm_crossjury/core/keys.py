"""Load, save and describe API keys.

A provider's key may come from four places. Precedence, highest first:

1. ``environment``: a real environment variable (e.g. ``ANTHROPIC_API_KEY``).
2. ``dotenv``: the ``.env`` file in the base directory.
3. ``key_file``: a one-key file ``.<provider>.key`` (e.g. ``.anthropic.key``) in the
   base directory. Such files must be git-ignored (``*.key`` in ``.gitignore``).
4. ``settings``: the key store written by the settings page,
   ``<user config dir>/llm-crossjury/keys.env`` (platformdirs; override the directory
   with ``LLM_CROSSJURY_CONFIG_DIR``). It is never inside a git working tree.

Every source fills the environment variable named by the provider; lower sources only
fill variables that are unset or empty. Saving a key from the settings page writes the
store and, unless a higher source is active, updates ``os.environ`` so the change
applies without a restart.

Key store permissions: on POSIX the file is created with mode ``0600`` (owner only) in
a directory created with mode ``0700``. On Windows, file modes do not apply; the file
lives in the user's profile (``%LOCALAPPDATA%``), whose ACL already restricts access to
the user, SYSTEM and administrators.

Key values are never logged, printed, returned or included in exception messages.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

import platformdirs

APP_NAME = "llm-crossjury"
CONFIG_DIR_ENV = "LLM_CROSSJURY_CONFIG_DIR"
"""Environment variable that overrides the platformdirs user config directory."""
KEY_STORE_NAME = "keys.env"

PROVIDER_KEY_ENVS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "xai": "XAI_API_KEY",
    }
)
"""Known provider ids -> environment variable holding the key (display order)."""

KeySource = Literal["environment", "dotenv", "key_file", "settings"]

MIN_KEY_CHARS = 8
MAX_KEY_CHARS = 1024
_KEY_PATTERN = re.compile(r"[A-Za-z0-9._~+/=:-]+")

_lock = threading.RLock()
_origins: dict[str, tuple[KeySource, bytes]] = {}
"""Env var -> (source, SHA-256 of the value) as recorded when the value was loaded."""


# --------------------------------------------------------------------------- errors


class KeyStoreError(Exception):
    """A key could not be saved or deleted. Messages never contain key material."""


class UnknownProviderError(KeyStoreError, ValueError):
    """The provider id is not one of :data:`PROVIDER_KEY_ENVS`."""


class InvalidKeyError(KeyStoreError, ValueError):
    """The submitted value does not look like an API key."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class KeyInfo:
    """Where a provider's key comes from. Never holds any part of the value.

    Attributes:
        provider: Provider id.
        env_var: Name of the environment variable.
        is_set: The variable currently holds a non-empty value.
        source: The source of the current value, ``None`` when not set.
        stored: A key is saved in the settings store (it may be overridden by a
            higher source).
    """

    provider: str
    env_var: str
    is_set: bool
    source: KeySource | None
    stored: bool


# --------------------------------------------------------------------------- paths


def config_dir() -> Path:
    """Return the user config directory (``LLM_CROSSJURY_CONFIG_DIR`` or platformdirs).

    Raises:
        KeyStoreError: if the directory is inside a git working tree.
    """
    env = os.environ.get(CONFIG_DIR_ENV)
    root = Path(env) if env else Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))
    root = root.expanduser().resolve()
    for candidate in (root, *root.parents):
        if (candidate / ".git").exists():
            raise KeyStoreError(
                f"refusing config directory {root}: it is inside the git working tree "
                f"{candidate}; keys must never be committed"
            )
    return root


def key_store_path(directory: Path | None = None) -> Path:
    """Return the key store location (``<config dir>/keys.env``)."""
    return (directory if directory is not None else config_dir()) / KEY_STORE_NAME


def key_file_path(base_dir: Path, provider: str) -> Path:
    """Return the key file location for ``provider`` (``<base_dir>/.<provider>.key``)."""
    return base_dir / f".{provider}.key"


def env_var_for(provider: str) -> str:
    """Return the key's environment variable for a known provider.

    Raises:
        UnknownProviderError: if ``provider`` is not known.
    """
    try:
        return PROVIDER_KEY_ENVS[provider]
    except KeyError:
        raise UnknownProviderError("Unknown provider.") from None


# --------------------------------------------------------------------------- parsing


def _parse_key_file(text: str, env_var: str) -> str:
    """Return the key from a key file's text.

    Accepts a bare key on the first non-empty line, or a ``VAR=value`` line for
    ``env_var`` (quotes around the value are removed). Comment lines (``#``) are
    skipped. Returns ``""`` if no key is found.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if sep and name.strip() == env_var:
            line = value.strip()
        return line.strip().strip("\"'")
    return ""


def _read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8-sig")  # tolerate a Notepad BOM
    except (OSError, UnicodeDecodeError):
        return None


def _key_file_value(base_dir: Path, provider: str, env_var: str) -> str:
    text = _read_text(key_file_path(base_dir, provider))
    return _parse_key_file(text, env_var) if text is not None else ""


def _dotenv_values(base_dir: Path) -> dict[str, str]:
    path = base_dir / ".env"
    if not path.is_file():
        return {}
    from dotenv import dotenv_values  # noqa: PLC0415 - only needed when .env exists

    try:
        values = dotenv_values(path)
    except (OSError, UnicodeDecodeError):
        return {}
    return {k: v.strip() for k, v in values.items() if v}


def _read_store(path: Path) -> dict[str, str]:
    """Read ``VAR=value`` lines from the key store (unknown variables are ignored)."""
    text = _read_text(path)
    if text is None:
        return {}
    known = set(PROVIDER_KEY_ENVS.values())
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if sep and name.strip() in known and value.strip():
            values[name.strip()] = value.strip()
    return values


def _write_store(path: Path, values: Mapping[str, str]) -> None:
    """Atomically replace the key store; owner-only permissions on POSIX."""
    lines = ["# llm-crossjury API keys saved from the settings page. Do not share.\n"]
    lines += [f"{name}={values[name]}\n" for name in PROVIDER_KEY_ENVS.values() if name in values]
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(lines)
        os.replace(tmp, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except OSError as exc:
        # The OS message names the error, never the value.
        raise KeyStoreError(f"cannot write the key store {path}: {exc.strerror}") from None


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _record(env_var: str, source: KeySource, value: str) -> None:
    _origins[env_var] = (source, _digest(value))


def normalize_key(value: str, env_var: str) -> str:
    """Return the cleaned key or raise :class:`InvalidKeyError` (without the value).

    Strips whitespace, surrounding quotes and a pasted ``VAR=`` prefix.
    """
    cleaned = value.strip().strip("\"'").strip()
    prefix = f"{env_var}="
    if cleaned.startswith(prefix):
        cleaned = cleaned[len(prefix) :].strip().strip("\"'").strip()
    if not cleaned:
        raise InvalidKeyError("The key is empty.")
    if len(cleaned) < MIN_KEY_CHARS or len(cleaned) > MAX_KEY_CHARS:
        raise InvalidKeyError("This does not look like an API key (unexpected length).")
    if not _KEY_PATTERN.fullmatch(cleaned):
        raise InvalidKeyError(
            "This does not look like an API key (it contains spaces or unexpected characters)."
        )
    return cleaned


# --------------------------------------------------------------------------- loading


def load_key_files(base_dir: Path, key_envs: Mapping[str, str]) -> tuple[str, ...]:
    """Fill unset key variables from ``.<provider>.key`` files in ``base_dir``.

    Args:
        base_dir: Directory holding the key files.
        key_envs: Provider id -> environment variable name.

    Returns:
        The provider ids whose key was loaded from a file (never the values).
    """
    loaded: list[str] = []
    with _lock:
        for provider, env_var in key_envs.items():
            if os.environ.get(env_var, "").strip():
                continue
            value = _key_file_value(base_dir, provider, env_var)
            if value:
                os.environ[env_var] = value
                _record(env_var, "key_file", value)
                loaded.append(provider)
    return tuple(loaded)


def load_all_keys(
    base_dir: Path,
    key_envs: Mapping[str, str],
    *,
    store_path: Path | None = None,
) -> tuple[str, ...]:
    """Fill unset key variables from ``.env``, key files and the settings store.

    Precedence: real environment > ``.env`` > ``.<provider>.key`` > settings store.
    Safe to call after ``load_dotenv(override=False)``: a variable whose value equals
    the ``.env`` value is attributed to ``.env``. Never prints or logs anything.

    Args:
        base_dir: Directory holding ``.env`` and the key files.
        key_envs: Provider id -> environment variable name.
        store_path: Key store override (default: :func:`key_store_path`).

    Returns:
        The provider ids whose key was loaded from a file or the store (never values).
    """
    dotenv = _dotenv_values(base_dir)
    try:
        store = _read_store(store_path if store_path is not None else key_store_path())
    except KeyStoreError:
        store = {}
    loaded: list[str] = []
    with _lock:
        for provider, env_var in key_envs.items():
            current = os.environ.get(env_var, "").strip()
            if current:
                origin: KeySource = "dotenv" if dotenv.get(env_var) == current else "environment"
                _record(env_var, origin, current)
                continue
            candidates: tuple[tuple[KeySource, str], ...] = (
                ("dotenv", dotenv.get(env_var, "")),
                ("key_file", _key_file_value(base_dir, provider, env_var)),
                ("settings", store.get(env_var, "")),
            )
            for source, value in candidates:
                if value:
                    os.environ[env_var] = value
                    _record(env_var, source, value)
                    loaded.append(provider)
                    break
    return tuple(loaded)


# --------------------------------------------------------------------------- status


def _effective_source(
    provider: str, env_var: str, base_dir: Path | None, store: Mapping[str, str]
) -> KeySource | None:
    current = os.environ.get(env_var, "").strip()
    if not current:
        return None
    recorded = _origins.get(env_var)
    if recorded is not None and recorded[1] == _digest(current):
        return recorded[0]
    if base_dir is not None:
        if _dotenv_values(base_dir).get(env_var) == current:
            return "dotenv"
        if _key_file_value(base_dir, provider, env_var) == current:
            return "key_file"
    if store.get(env_var) == current:
        return "settings"
    return "environment"


def _info(provider: str, env_var: str, base_dir: Path | None, store: Mapping[str, str]) -> KeyInfo:
    source = _effective_source(provider, env_var, base_dir, store)
    return KeyInfo(
        provider=provider,
        env_var=env_var,
        is_set=source is not None,
        source=source,
        stored=env_var in store,
    )


def key_sources(
    base_dir: Path | None,
    key_envs: Mapping[str, str] = PROVIDER_KEY_ENVS,
    *,
    store_path: Path | None = None,
) -> dict[str, KeyInfo]:
    """Describe each provider's key: set or not, and its source. Never the value.

    Args:
        base_dir: Directory holding ``.env`` and the key files (``None``: skip them).
        key_envs: Provider id -> environment variable name.
        store_path: Key store override (default: :func:`key_store_path`).

    Returns:
        Provider id -> :class:`KeyInfo`, in ``key_envs`` order.
    """
    store = _read_store(store_path if store_path is not None else key_store_path())
    with _lock:
        return {p: _info(p, var, base_dir, store) for p, var in key_envs.items()}


def save_key(
    provider: str,
    value: str,
    *,
    base_dir: Path | None = None,
    store_path: Path | None = None,
) -> KeyInfo:
    """Save a key in the settings store and apply it to ``os.environ`` if it wins.

    The environment is updated unless a higher source (environment, ``.env``, key
    file) currently provides the key; :attr:`KeyInfo.source` then shows that source.

    Args:
        provider: A known provider id.
        value: The key. Never logged or echoed.
        base_dir: Directory holding ``.env`` and the key files.
        store_path: Key store override (default: :func:`key_store_path`).

    Returns:
        The provider's key info after saving.

    Raises:
        UnknownProviderError: unknown provider id.
        InvalidKeyError: the value does not look like a key.
        KeyStoreError: the store cannot be written.
    """
    env_var = env_var_for(provider)
    cleaned = normalize_key(value, env_var)
    path = store_path if store_path is not None else key_store_path()
    with _lock:
        store = _read_store(path)
        before = _effective_source(provider, env_var, base_dir, store)
        store[env_var] = cleaned
        _write_store(path, store)
        if before in (None, "settings"):
            os.environ[env_var] = cleaned
            _record(env_var, "settings", cleaned)
        return _info(provider, env_var, base_dir, store)


def delete_key(
    provider: str,
    *,
    base_dir: Path | None = None,
    store_path: Path | None = None,
) -> bool:
    """Delete a key from the settings store (other sources are never touched).

    If the store provided the current value, the variable is removed from
    ``os.environ`` and refilled from ``.env`` or a key file when ``base_dir`` has one.

    Args:
        provider: A known provider id.
        base_dir: Directory holding ``.env`` and the key files.
        store_path: Key store override (default: :func:`key_store_path`).

    Returns:
        Whether a stored key was deleted.

    Raises:
        UnknownProviderError: unknown provider id.
        KeyStoreError: the store cannot be written.
    """
    env_var = env_var_for(provider)
    path = store_path if store_path is not None else key_store_path()
    with _lock:
        store = _read_store(path)
        if env_var not in store:
            return False
        source = _effective_source(provider, env_var, base_dir, store)
        del store[env_var]
        _write_store(path, store)
        if source == "settings":
            os.environ.pop(env_var, None)
            _origins.pop(env_var, None)
            if base_dir is not None:
                fallbacks: tuple[tuple[KeySource, str], ...] = (
                    ("dotenv", _dotenv_values(base_dir).get(env_var, "")),
                    ("key_file", _key_file_value(base_dir, provider, env_var)),
                )
                for fallback, value in fallbacks:
                    if value:
                        os.environ[env_var] = value
                        _record(env_var, fallback, value)
                        break
        return True


def forget_origins() -> None:
    """Forget the recorded key origins (for tests; sources are then re-derived)."""
    with _lock:
        _origins.clear()
