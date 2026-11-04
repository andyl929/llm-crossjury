"""Compare mode: independent answers side by side. No review, no synthesis."""

from __future__ import annotations

from council.core.config import CouncilConfig
from council.core.orchestrator.pipeline import Pipeline


def build_compare_pipeline(config: CouncilConfig) -> Pipeline:
    """Stages: ``answer`` only."""
    raise NotImplementedError
