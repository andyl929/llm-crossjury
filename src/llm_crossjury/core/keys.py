"""Load API keys from per-provider key files.

Besides real environment variables and ``.env``, a key may live in a one-key file
named ``.<provider>.key`` (e.g. ``.anthropic.key``) in the base directory. Such files
must be git-ignored (``*.key`` in ``.gitignore``).

Precedence: an already-set environment variable (including one loaded from ``.env``)
wins; key files only fill variables that are unset or empty.

Key values are never logged, printed, returned or included in exception messages.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def key_file_path(base_dir: Path, provider: str) -> Path:
    """Return the key file location for ``provider`` (``<base_dir>/.<provider>.key``)."""
    return base_dir / f".{provider}.key"


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


def load_key_files(base_dir: Path, key_envs: Mapping[str, str]) -> tuple[str, ...]:
    """Fill unset key variables from ``.<provider>.key`` files in ``base_dir``.

    Args:
        base_dir: Directory holding the key files.
        key_envs: Provider id -> environment variable name.

    Returns:
        The provider ids whose key was loaded from a file (never the values).
    """
    loaded: list[str] = []
    for provider, env_var in key_envs.items():
        if os.environ.get(env_var, "").strip():
            continue
        path = key_file_path(base_dir, provider)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")  # tolerate a Notepad BOM
        except (OSError, UnicodeDecodeError):
            continue
        value = _parse_key_file(text, env_var)
        if value:
            os.environ[env_var] = value
            loaded.append(provider)
    return tuple(loaded)
