"""Locations of run data. Nothing is ever written inside the repository.

Layout under ``platformdirs.user_data_dir("llm-crossjury", appauthor=False)``
(or ``LLM_CROSSJURY_DATA_DIR`` / ``CrossjuryConfig.data_dir``)::

    ledger.sqlite3              cost ledger (never contains prompt/response text)
    runs/<run_id>/run.json      run record: config snapshot, stages, labels, costs
    runs/<run_id>/content/      prompts and responses (only if store_content: true)
    checkpoints/<run_id>/       paused-run snapshot incl. content (temporary: deleted
                                on completion or after checkpoint_retention_days)
    attachments/<sha256>/       uploaded task attachments (temporary, owner-only:
                                deleted 24 h after upload; see core.attachments.store)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "llm-crossjury"
DATA_DIR_ENV = "LLM_CROSSJURY_DATA_DIR"
"""Environment variable that overrides the platformdirs data directory."""


class DataDirError(Exception):
    """The data directory is unsafe (inside a git working tree) or unusable."""


@dataclass(frozen=True)
class DataPaths:
    """Resolved locations of run data (see the module docstring for the layout)."""

    root: Path
    ledger: Path
    runs: Path
    checkpoints: Path


def resolve_data_paths(override: Path | None = None) -> DataPaths:
    """Resolve (and create) the data directory.

    Precedence: ``override`` > ``LLM_CROSSJURY_DATA_DIR`` > platformdirs default.
    Refuses a location inside a git working tree to avoid committing run data.

    Args:
        override: Explicit directory, e.g. ``CrossjuryConfig.data_dir``.

    Returns:
        The absolute paths; ``root``, ``runs`` and ``checkpoints`` exist afterwards.

    Raises:
        DataDirError: if the directory is inside a git working tree or cannot be
            created.
    """
    env = os.environ.get(DATA_DIR_ENV)
    if override is not None:
        root = override
    elif env:
        root = Path(env)
    else:
        root = Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    root = root.expanduser().resolve()
    for candidate in (root, *root.parents):
        if (candidate / ".git").exists():
            raise DataDirError(
                f"refusing data directory {root}: it is inside the git working tree "
                f"{candidate}; run data must never be committed"
            )
    paths = DataPaths(
        root=root,
        ledger=root / "ledger.sqlite3",
        runs=root / "runs",
        checkpoints=root / "checkpoints",
    )
    try:
        for directory in (paths.root, paths.runs, paths.checkpoints):
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataDirError(f"cannot create data directory {root}: {exc}") from exc
    return paths
