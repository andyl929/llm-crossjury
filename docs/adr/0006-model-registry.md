# ADR 0006: Model registry, aliases and reasoning resolution

- Status: Accepted
- Date: 2026-11-04
- Supersedes: ADR 0001 rule 5 ("each adapter maps reasoning effort internally").
  Amends: ADR 0003 §1 (the price source is now `config/models.yaml`).
- Amended: 2026-11-04 (clarification: per-model multiplier override; `refresh --draft`;
  `CompletionRequest.reasoning` naming; SPEC items 22-24)

## Context

Every vendor names its models differently, exposes reasoning differently (named
levels, token budgets, always-on, or nothing), passes them differently to its CLI,
and prices them differently. Phase 0 kept prices in `config/pricing.yaml` and left
reasoning mapping to each adapter. That spread model facts over files and code. The
user could not see what a portable level such as `high` would actually send, and the
estimator could not tell which levels a model supports. Model ids also change often,
so configs that name concrete ids go stale.

## Decision

### 1. One registry file: `config/models.yaml`

`config/pricing.yaml` is merged into it and removed. Each `ModelEntry` has:

| Field | Meaning |
|---|---|
| `provider`, `model_id` | Vendor and exact model id |
| `backends` | Which of `api` / `cli` can reach it |
| `reasoning_control` | `type` (`levels` \| `budget` \| `always_on` \| `none`), the native `parameter`, the `allowed` values (levels, or budget min/max), and the `mapping` from `low`/`medium`/`high` to native values |
| `cli_flags` | argv templates for the model and reasoning flags (`{model}`, `{value}`), one placeholder per token |
| `context_window`, `max_output`, `structured_output` | Limits and capability (`null` = not verified) |
| `pricing` | The former price fields: currency, input, output, cached input, cache write, reasoning, batch multiplier |
| `reasoning_multipliers` | Optional per-level override of the global reasoning-token multipliers (`null` = global) |
| `source_url`, `last_verified` | Provenance. Without both, the entry is *unverified* and its pricing is never used |

The schema (`core/registry/schema.py`) is a **contract file**. JSON Schemas for
`models.yaml` and the run config are exported to `schemas/config/`. The repository
ships only obviously fictitious example entries. No real model id, parameter or
price is ever filled from memory.

### 2. Aliases

Each provider has three aliases, `fast`, `balanced` and `best`, each pointing to a
`model_id` of the same provider (or `null`). Configs should prefer aliases, so a
model update is a one-line registry change.

### 3. Member configuration

- `model`: an alias or a concrete id.
- `reasoning`: `low | medium | high | default`, or unset to use the role default.
- `native`: an optional dict of vendor-native parameters, passed through verbatim
  and merged last.
- `roles`: defaults for `answerer`, `reviewer`, `agreement_judge` and `chairman`.
  Reviewer and agreement judge default to `low`; answerer and chairman default to
  `default`. Reviewer and judge calls always use the role default. Answerer and
  chairman calls use the member's `reasoning` if it is set.

### 4. Resolution (`ModelResolver`)

1. Resolve the alias to a `model_id`.
2. Look up the entry. **Unregistered models are allowed, but only with `default`
   reasoning.**
3. Check that the backend is in `entry.backends`.
4. Map the level through `reasoning_control`. If it cannot be mapped (`none`,
   `always_on`, or a `null` mapping):
   - in **normal** mode, downgrade to `default` and warn;
   - in **strict** mode (`resolution: strict` or `--strict`), raise an error.
5. Merge `native`, which wins.
6. For `cli`, build argv tokens from `cli_flags`. Values come only from the registry,
   never from user or model text (ADR 0002).

Adapters receive the result in `CompletionRequest.native_params` and send it
verbatim. They no longer map reasoning themselves.

### 5. Estimation and records

- Before a run, the estimate lists every (member, role) resolution: requested model,
  resolved id, backend, effective level, native value and warnings.
- Reasoning tokens are estimated as `visible output × multiplier[level]`. The
  multipliers are global (`cost.reasoning_multipliers`; the provisional defaults are
  uncalibrated) and can be overridden per model and level by the entry's
  `reasoning_multipliers`. Once the ledger has `calibration_min_samples` complete
  observations for a (provider, model, level), the observed
  `reasoning_tokens / output_tokens` ratio takes precedence over both.
- `CompletionResult.native_params_used` records the parameters actually sent. The run
  record stores them for every call, and the ledger stores the effective level.

### 6. CLI

- `llm-crossjury models list` shows entries, aliases, verification and pricing status
  (offline).
- `llm-crossjury models refresh` calls each vendor's model-list endpoint (`api`
  keys, no tokens spent) and prints the differences from the registry. It never
  edits the file. With `--draft <file>`, it also writes draft entries for
  vendor-listed models that are missing from the registry. Each draft has only
  `provider` and `model_id`, taken verbatim from the official endpoint; every other
  field is `null`, and the notes say "UNVERIFIED DRAFT". Drafts fail validation until a
  human completes them from the vendor docs and moves them into `models.yaml`. The
  command never overwrites an existing file. It is implemented in phase 1A.

## Consequences

- One place to verify and update model facts. The resolution is visible before
  money is spent.
- Users must maintain `models.yaml`. The shipped file is deliberately empty of real
  models.
- An unregistered model has no verified price. Under the cost rules (ADR 0003), an
  `api` run that needs it is refused, so unregistered models are in practice usable
  only on the `cli` backend.
- The contract changes: the new `registry/schema.py`, plus `native_params` and
  `native_params_used` in `providers/base.py` (logged as CI-2).

## Alternatives considered

- **Mapping in adapters (ADR 0001 rule 5).** Rejected: invisible to the user and the
  estimator, and it hard-codes vendor facts in code.
- **Fetching model facts from vendor APIs at runtime.** Rejected as the source of
  truth: list endpoints do not reliably expose reasoning controls or prices. They are
  used only for `models refresh` diffs.
- **Keeping a separate `pricing.yaml`.** Rejected: it duplicates keys and drifts.
