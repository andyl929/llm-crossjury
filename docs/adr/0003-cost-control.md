# ADR 0003: Cost control strategy

- Status: Accepted
- Date: 2026-11-04
- Amended: 2026-11-04 (pre-phase-1 decisions; see SPEC §10 Decided)
- Amended: 2026-11-04 (clarification: prices now live in the model registry, ADR 0006;
  ring default k = 2)
- Amended: 2026-11-09, ADR 0008 (the default limits below are now $5.00 per run,
  confirmation above $1.00 and $15.00 per day, and the web UI's Settings page can
  change them; the decision itself is unchanged)

## Context

A Council-mode run multiplies spend. With N members it makes N answer calls, up to
N·(N−1) review calls, and two chair calls, and the review and chair calls carry long
contexts. Cost has to be visible before the run, enforced during it, and kept down
by design. Prices change often, and anything hard-coded or remembered goes stale.

## Decision

### 1. Prices come only from configuration

- The `pricing` block of the model registry `config/models.yaml` (ADR 0006) is the
  single source. Only entries with `source_url` and `last_verified` are used. The repository ships with **no entries**. There are no built-in
  defaults and no prices filled from memory.
- An `api` model without an entry is unpriceable, and the run is **refused**. There
  is no override.
- `cli` members have unknowable per-call cost. They require an explicit
  `allow_unknown_cost: true`, and the ledger records their call count and duration.
- Reports warn about entries older than a configurable age.

### 2. Built-in budget defaults

| Limit | Default | Scope |
|---|---|---|
| `run_limit` | $1.00 | one run (hard) |
| `confirm_above` | $0.25 | ask before a run whose high estimate exceeds this |
| `daily_limit` | $5.00 | all runs on the local calendar day (hard) |

Amended 2026-11-09 (ADR 0008): the defaults are now `run_limit` $5.00,
`confirm_above` $1.00 and `daily_limit` $15.00, sized for the 32k output default, and
the web UI's Settings page stores user values that replace them.

The effective values and today's spend are printed at the start of every run.

### 3. Estimate before the run

- Each stage implements `plan(ctx) → [CallPlan]`. The estimator prices the plan as a
  **range**: input tokens come from vendor counting where available and a heuristic
  otherwise; output uses low/high bounds per stage; cache hits are assumed for calls
  sharing a `prefix_fingerprint` on providers that support caching.
- The run is refused if `total_high` exceeds the run limit or the remaining daily
  budget. The user is asked to confirm if `total_high > confirm_above`.
- Document Review estimates one stage-1 call per (member, chunk), with the whole
  document as a cached prefix (§7).

### 4. Account during the run, and stop on budget

- Before each call: `Ledger.check(spent + reserved + call_high)` against both the run
  limit and the daily limit. If either check fails, the call is not started. In-flight calls hold a reservation, which is released when
  the call is recorded.
- After each call: a `LedgerEntry` is written immediately (SQLite, no content).
- After each stage: the `BudgetGuard` hook aborts if the budget is exhausted.
  Artifacts are kept, and the run ends as `Stopped("budget")`.

### 5. Early stopping is a user decision

- Nothing is skipped automatically. After stage 1, a **cheap judge model** (reasoning
  effort `low`) groups all claims into an `AgreementJudgment`. The code computes the
  agreement ratio: groups that every kept answer supports and none contradicts,
  divided by all groups.
- Checkpoint A shows the ratio, the estimated cost of peer review (the saving if it is
  skipped), and a "consider skipping" hint when the ratio is at or above
  `agreement.threshold`. The threshold is configurable; its default is set in the
  evaluation phase. The user decides with `PruneDecision.skip_review`.
- The judge call is itself estimated and budgeted. It is skipped when no judge is
  configured or fewer than two answers exist.
- Checkpoint A is also a cost lever: pruned answers are neither reviewed nor synthesized.
- If only one answer survives, review is skipped.
- Under `--auto`, review is never skipped.

### 6. Review topology

| Topology | Review calls (n kept) | Use when |
|---|---|---|
| `full` | n·(n−1), or n if each reviewer gets all targets in one call | Small councils, high stakes |
| `ring` | n·k | Larger councils. Every answer still gets k independent reviews |
| `single_judge` | 1 | Cheapest. One judge reviews everything (weaker independence) |

Default (no topology configured): `full` for ≤ 3 reviewed answers, `ring` with
k = 2 for ≥ 4 (k = 1 = "issues only", no ranking). Peer review runs at reasoning
effort `low` by default (role `reviewer`).

The implementation sends **one call per reviewer, carrying all of that reviewer's
targets** (`ReviewAssignment.targets`). This keeps the call count at n for `full`
while the token count still grows with n·(n−1).

### 7. Prompt-cache prefixes

- Stages are designed so that many calls share one `stable_prefix`: the same system
  prompt plus task for all stage-1 calls, and the same system prompt plus task plus
  (in Document Review) the full document for every call in every stage.
- Anything that varies per call (labels, target order, rulings) goes in the
  `variable_suffix`.
- Calls sharing a prefix are sequenced so that the first one warms the cache before
  the rest fan out *(planned)*, where the vendor's caching needs that.
- Document chunking keeps the **whole document** in the stable prefix and puts only
  the chunk id in the suffix. Per-member chunk calls therefore share one cached
  prefix, while output stays bounded per call. `document.max_document_tokens` is a
  hard limit, checked before any call.

### 8. Batch processing

- If `cost.use_batch` is on and the provider declares `batch`, a stage's independent
  calls are submitted as one batch and priced with `batch_multiplier` from the price
  table. This is off by default, because batch latency does not fit interactive
  checkpoints. It is meant for `--non-interactive` runs.

## Consequences

- Users must maintain `config/models.yaml`. That is the price of never shipping wrong prices.
- Reasoning tokens are estimated with configurable per-level multipliers and later
  calibrated from the ledger (ADR 0006 §5).
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
