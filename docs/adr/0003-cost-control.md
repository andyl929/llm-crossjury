# ADR 0003: Cost control strategy

- Status: Accepted
- Date: 2026-11-04

## Context

A council run multiplies spend. With N members it makes N answer calls, up to
N·(N−1) review calls, and two chair calls, and the review and chair calls carry long
contexts. Cost has to be visible before the run, enforced during it, and kept down
by design. Prices change often, and anything hard-coded or remembered goes stale.

## Decision

### 1. Prices come only from configuration

- `config/pricing.yaml` is the single source. Every entry has `source_url` and
  `last_verified`. The repository ships with **no entries**. There are no built-in
  defaults and no prices filled from memory.
- A model without an entry is unpriceable. The run is refused unless
  `allow_unknown_cost: true`, in which case its calls are counted as unknown.
- Reports warn about entries older than a configurable age.

### 2. Estimate before the run

- Each stage implements `plan(ctx) → [CallPlan]`. The estimator prices the plan as a
  **range**: input tokens come from vendor counting where available and a heuristic
  otherwise; output uses low/high bounds per stage; cache hits are assumed for calls
  sharing a `prefix_fingerprint` on providers that support caching.
- The run is refused if `total_high > budget.limit`. The user is asked to confirm if
  `total_high > confirm_above`.

### 3. Account during the run, and stop on budget

- Before each call: `Ledger.check(spent + reserved + call_high ≤ limit)`. If it fails,
  the call is not started. In-flight calls hold a reservation, which is released when
  the call is recorded.
- After each call: a `LedgerEntry` is written immediately (SQLite, no content).
- After each stage: the `BudgetGuard` hook aborts if the budget is exhausted.
  Artifacts are kept, and the run ends as `Stopped("budget")`.

### 4. Early stopping

- `AgreementEarlyStop`: if the stage-1 claims agree at or above a threshold, skip peer
  review and go straight to the draft synthesis. This is off by default (see SPEC
  Open Questions).
- Checkpoint A is also a cost lever: pruned answers are neither reviewed nor synthesized.
- If only one answer survives, review is skipped.

### 5. Review topology

| Topology | Review calls (n kept) | Use when |
|---|---|---|
| `full` | n·(n−1), or n if each reviewer gets all targets in one call | Small councils, high stakes |
| `ring` | n·k | Larger councils. Every answer still gets k independent reviews |
| `single_judge` | 1 | Cheapest. One judge reviews everything (weaker independence) |

The implementation sends **one call per reviewer, carrying all of that reviewer's
targets** (`ReviewAssignment.targets`). This keeps the call count at n for `full`
while the token count still grows with n·(n−1).

### 6. Prompt-cache prefixes

- Stages are designed so that many calls share one `stable_prefix`: the same system
  prompt plus task for all stage-1 calls, and the same system prompt plus task plus
  (in Document Review) the full document for every call in every stage.
- Anything that varies per call (labels, target order, rulings) goes in the
  `variable_suffix`.
- Calls sharing a prefix are sequenced so that the first one warms the cache before
  the rest fan out *(planned)*, where the vendor's caching needs that.

### 7. Batch processing

- If `cost.use_batch` is on and the provider declares `batch`, a stage's independent
  calls are submitted as one batch and priced with `batch_multiplier` from the price
  table. This is off by default, because batch latency does not fit interactive
  checkpoints. It is meant for `--non-interactive` runs.

## Consequences

- Users must maintain `pricing.yaml`. That is the price of never shipping wrong prices.
- Estimates are ranges. Reports compare the estimate against actual spend so the
  heuristics can be tuned.
- The reservation scheme can refuse a call that would in fact have fit. We choose
  that conservatism on purpose.

## Alternatives considered

- **Bundled price list.** Rejected: it goes stale silently, and it violates the
  "never from memory" rule.
- **Post-hoc accounting only.** Rejected: it cannot stop a run before overspending.
- **One review call per (reviewer, target) pair.** Rejected as the default: it
  multiplies the number of uncached suffixes for no gain in quality.
