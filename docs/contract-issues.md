# Contract issues

Contract files are `src/llm_crossjury/core/schemas/**` and `src/llm_crossjury/core/providers/base.py`.
They must not be modified without the maintainer's approval. If you find a problem
with a contract, add an entry below and keep working against the current contract.
Where needed, use an adapter-side workaround and note it in the entry.

Template:

```
## CI-<n>: <short title>
- Date / reporter:
- File / symbol:
- Problem:
- Impact (what is blocked or worked around):
- Proposed change:
- Status: open | accepted | rejected | done
```

## Open issues

_None._

## Approved changes

### CI-1: Pre-phase-1 contract changes (approved by the maintainer, 2026-11-04)
- `schemas/review.py`: removed `TargetReview.score` and `TargetReview.score_rationale`.
  `PeerReview` field order is now `reviews`, `ranking_rationale`, `ranking`. Rankings
  are Borda-aggregated in code.
- `schemas/agreement.py` (new): `ClaimRef`, `ClaimGroup`, `AgreementJudgment` for the
  checkpoint-A agreement ratio. Added to `EXPORTED_MODELS`.
- `providers/base.py`: new `ReasoningEffort` (`low|medium|high|default`); new required
  `CompletionRequest.reasoning_effort`; new `Capabilities.reasoning_effort` flag.
- Status: done
