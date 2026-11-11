"""Shared types for the "AI synthesis" upgrade of a Compare run.

The upgrade reuses the answers of a finished Compare run (step 1) and runs, in order:

1. **Agreement** (``agreement.py``): answers are split into numbered segments locally;
   one cheap judge call groups them by meaning (``AgreementJudgment``). Groups are
   classified into consensus / disputed / unique in Python.
2. **Review** (``review.py``): one round of anonymous peer review (``PeerReview``),
   focused on disputed and unique points; rankings, never scores (Borda aggregation).
   Optional: the user may skip it.
3. **Chair** (``chair.py``): one call produces the final answer plus the material for
   the additional list (``Synthesis``). Re-synthesis with user rulings is one more
   chair call.

Stage modules never talk to providers, the ledger or the budget directly. They build
:class:`CallSpec` objects and await an injected :data:`CallFn`, which the service
layer implements (model resolution, budget check, ledger, provider dispatch). This
keeps every stage testable with a fake ``CallFn``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.providers.base import CompletionResult, Prompt


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelChoice(_Frozen):
    """A concrete model to call: provider id plus a registry ``model_id``."""

    provider: str
    model_id: str


class AnonAnswer(_Frozen):
    """One step-1 answer as reviewers and the chair see it.

    Attributes:
        label: Anonymous label, ``"Response A"``, ``"Response B"``, ...
        text: The answer text, already scrubbed of model/vendor names when scrubbing
            is enabled for the run.
    """

    label: str
    text: str


class Segment(_Frozen):
    """A numbered piece of one answer (a paragraph or list item).

    ``segment_id`` is unique within its answer (``"s1"``, ``"s2"``, ...) and is what
    ``ClaimRef.claim_id`` points at in the agreement judgment.
    """

    label: str
    segment_id: str
    text: str


@dataclass(frozen=True)
class CallSpec:
    """One model call a stage wants to make.

    Attributes:
        stage: Stage name for the ledger and estimate lines (``"agreement"``,
            ``"review"``, ``"chair"``).
        provider: Provider id.
        model_id: Concrete registry model id.
        prompt: Stable prefix (instructions and shared material) and variable suffix.
        response_schema: Strict JSON schema from
            ``llm_crossjury.core.schemas.export.strict_json_schema``, or ``None``.
        max_output_tokens: Output cap for this call (a product setting per stage).
        tag: Free-form label for the ledger (e.g. the reviewer's own label).
    """

    stage: str
    provider: str
    model_id: str
    prompt: Prompt
    response_schema: dict[str, Any] | None
    max_output_tokens: int
    tag: str = ""


CallFn = Callable[[CallSpec], Awaitable[CompletionResult]]
"""Executes a :class:`CallSpec` (resolve, budget-check, call, record) and returns the
result. Raises ``ProviderError`` / ``BudgetExceededError`` on failure."""


class PlannedCall(_Frozen):
    """A call a stage expects to make, for the pre-run cost estimate.

    Token counts are estimates (``llm_crossjury.core.cost.estimate.estimate_tokens``
    over the rendered prompt; later-stage inputs that do not exist yet are estimated
    from the step-1 answer lengths).
    """

    stage: str
    provider: str
    model_id: str
    input_tokens: int
    output_tokens_low: int
    output_tokens_high: int
