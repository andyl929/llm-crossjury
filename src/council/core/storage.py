"""Locations of run data. Nothing is ever written inside the repository.

Layout under ``platformdirs.user_data_dir("council-placeholder", appauthor=False)``
(or ``COUNCIL_DATA_DIR`` / ``CouncilConfig.data_dir``)::

    ledger.sqlite3              cost ledger (never contains prompt/response text)
    runs/<run_id>/run.json      run record: config snapshot, stages, labels, costs
    runs/<run_id>/content/      prompts and responses (only if store_content: true)
    checkpoints/<run_id>.json   paused-run snapshots
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

APP_NAME = "council-placeholder"


@dataclass(frozen=True)
class DataPaths:
    root: Path
    ledger: Path
    runs: Path
    checkpoints: Path


def resolve_data_paths(override: Path | None = None) -> DataPaths:
    """Resolve (and create) the data directory.

    Precedence: ``override`` > ``COUNCIL_DATA_DIR`` > platformdirs default.
    Refuses a location inside a git working tree to avoid committing run data.
    """
    raise NotImplementedError
