# Contract issues

Contract files are `src/llm_crossjury/core/schemas/**`, `src/llm_crossjury/core/providers/base.py`
and `src/llm_crossjury/core/registry/schema.py`.
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

### CI-4: Temperature support is a model/API-version fact, but the registry has no field for it
- Date / reporter: 2026-11-08 / providers-a agent (Anthropic + Gemini `api` adapters)
- File / symbol: `registry/schema.py` `ModelEntry`; `providers/base.py` `Capabilities.temperature`
- Problem: The compare-MVP convention sets `Capabilities.temperature = True` for every
  `api` model, but vendors now differ per model and per API version. Anthropic API
  version `2026-10-21` (used by `anthropic` SDK >= 2.0, installed 2.6.0) returns
  HTTP 400 for any request that sends `temperature`, `top_p` or `top_k`
  (https://platform.claude.com/docs/en/api/migration-2026-10-21#sampling-parameters),
  and the SDK no longer has the parameter. Gemini 4 and Gemini 3.6+ models accept but
  ignore custom `temperature` (https://aistudio.google.com/docs/text-generation).
  `ModelEntry` has no field to record either fact, so it can only live in code.
- Impact: The Anthropic adapter hard-wires `temperature=False` (vendor/API-version fact,
  not a model fact) and never sends it, so requests don't fail. The Gemini adapter
  reports `True` and passes it through, even for models that ignore it.
- Proposed change: Add `ModelEntry.temperature: bool | None` (`None` = unverified, treated
  as unsupported). Adapters then derive `Capabilities.temperature` from the entry, like
  `structured_output`.
- Status: open

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

### CI-2: Model registry (approved by the maintainer, 2026-11-04; ADR 0006)
- `registry/schema.py` (new contract file): `ModelEntry`, `ReasoningControl`
  (`levels|budget|always_on|none`, `allowed`, `mapping`), `EffortMapping`, `CliFlags`,
  `Pricing` (moved from `cost/pricing.py`'s former `PriceEntry`), `ProviderAliases`,
  `ModelRegistryFile`. `config/pricing.yaml` is merged into `config/models.yaml`.
- `providers/base.py`: new required `CompletionRequest.native_params` and
  `CompletionResult.native_params_used`. `CompletionRequest.model` is always a resolved
  id. Reasoning mapping moved from adapters to the ModelResolver.
- `schemas/__main__.py`: also writes the config-file schemas to `schemas/config/`.
- Status: done

### CI-3: Naming and per-model multipliers (approved by the maintainer, 2026-11-04)
- `providers/base.py`: `CompletionRequest.reasoning_effort` renamed to `reasoning`
  (matching `MemberConfig.reasoning`), and `Capabilities.reasoning_effort` renamed to
  `Capabilities.reasoning`. The `ReasoningEffort` enum keeps its name. No
  structured-output schema is affected (`providers/base.py` is not exported).
- `registry/schema.py`: new `ModelEntry.reasoning_multipliers:
  dict[ReasoningEffort, float] | None`, a per-model, per-level override of
  `cost.reasoning_multipliers`. `schemas/config/ModelRegistryFile.schema.json` was
  regenerated.
- Status: done

### CI-5: File attachments (approved by the maintainer, 2026-11-08)
- `providers/base.py`: new `AttachmentKind` and `Attachment`; `Message.attachments:
  tuple[Attachment, ...] = ()`; `Capabilities.input_modalities:
  frozenset[AttachmentKind] = frozenset()`. Both new fields have defaults, so existing
  callers are unaffected.
- `registry/schema.py`: new `ModelEntry.input_modalities: tuple[AttachmentKind, ...] |
  None = None` (`None` = unverified = text only). `schemas/config/ModelRegistryFile.schema.json`
  regenerated.
- Rationale and rules: ADR 0007.
- Status: done
