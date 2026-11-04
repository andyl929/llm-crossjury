"""Compare mode: independent answers side by side. No review, no synthesis."""

from __future__ import annotations

from llm_crossjury.core.config import CrossjuryConfig
from llm_crossjury.core.orchestrator.pipeline import Pipeline


def build_compare_pipeline(config: CrossjuryConfig) -> Pipeline:
    """Stages: ``answer`` only."""
    raise NotImplementedError
