"""Structured-output schemas (CONTRACT FILES; see AGENTS.md and ADR 0001)."""

from llm_crossjury.core.schemas._base import Confidence, Severity, StrictModel
from llm_crossjury.core.schemas.agreement import AgreementJudgment, ClaimGroup, ClaimRef
from llm_crossjury.core.schemas.edits import Edit, EditCategory, EditList, EditLocation
from llm_crossjury.core.schemas.export import EXPORTED_MODELS, strict_json_schema
from llm_crossjury.core.schemas.review import PeerReview, ReviewIssue, TargetReview
from llm_crossjury.core.schemas.stage1 import Claim, Stage1Answer
from llm_crossjury.core.schemas.synthesis import (
    ConsensusPoint,
    DisputedPoint,
    DocumentSynthesis,
    Position,
    Synthesis,
    UniqueInsight,
)

__all__ = [
    "EXPORTED_MODELS",
    "AgreementJudgment",
    "Claim",
    "ClaimGroup",
    "ClaimRef",
    "Confidence",
    "ConsensusPoint",
    "DisputedPoint",
    "DocumentSynthesis",
    "Edit",
    "EditCategory",
    "EditList",
    "EditLocation",
    "PeerReview",
    "Position",
    "ReviewIssue",
    "Severity",
    "Stage1Answer",
    "StrictModel",
    "Synthesis",
    "TargetReview",
    "UniqueInsight",
    "strict_json_schema",
]
