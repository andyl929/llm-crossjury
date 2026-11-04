# Architecture

Status: draft · Covers the skeleton as committed. Things marked *(planned)* are not built yet.

## 1. Layers

```
        ┌────────────────────┐   ┌────────────────────────────┐
        │  llm-crossjury CLI │   │  Web UI + FastAPI (later)  │
        │  (Typer)           │   │  frontend/ placeholder     │
        └─────────┬──────────┘   └──────────────┬─────────────┘
                  │        same core API        │
┌─────────────────▼─────────────────────────────▼───────────────┐
│ llm_crossjury.core                                            │
│  modes/         pipeline builders: compare, council, doc      │
│  orchestrator/  Pipeline, Stage, CheckpointStage, hooks,      │
│                 topology, agreement, ranking (Borda),         │
│                 RunContext, checkpoint store                  │
│  schemas/       Pydantic structured-output models (CONTRACT)  │
│  cost/          PricingTable, CostEstimator, Ledger           │
│  providers/     base.py (CONTRACT), api/, cli/, mock          │
│  anonymize.py   identity scrubbing                            │
│  config.py      CrossjuryConfig     storage.py  DataPaths     │
└─────────────────┬─────────────────────────────┬───────────────┘
                  │                             │
         vendor SDKs (api)          vendor CLIs as subprocesses (cli)
```

Dependency rule: `cli` → `core.modes` → `core.orchestrator` → `core.{schemas,cost,providers}`.
`core` never imports from `cli`. `schemas` and `providers/base.py` import nothing else
from `llm_crossjury`, except that `schemas.synthesis` imports `schemas.edits`.

## 2. Package map

| Path | Responsibility |
|---|---|
| `core/providers/base.py` | `Provider` / `BatchProvider` protocols, `Prompt` (stable prefix + variable suffix), `Usage` (fields may be unknown), `Capabilities`, `ReasoningEffort`, errors. **Contract** |
| `core/providers/api/` | One adapter per vendor SDK. Reads its key from the environment at call time, and maps `ReasoningEffort` to the vendor's controls |
| `core/providers/cli/` | One adapter per vendor CLI (`claude`, `codex`, `gemini`, `grok`), plus `_subprocess.py` with the hardened runner (ADR 0002) |
| `core/providers/mock.py` | Deterministic provider for tests |
| `core/schemas/` | `Stage1Answer`, `AgreementJudgment`, `PeerReview`, `EditList`, `Synthesis`, `DocumentSynthesis`, and `export.py` (canonical strict JSON Schema). **Contract** |
| `core/cost/` | `PricingTable` (from YAML), `CostEstimator` → `CostEstimate`, `Ledger` (SQLite; per-run and daily limits; CLI call counts and durations), `compute_cost` |
| `core/orchestrator/` | `Pipeline` (run/resume/skip-review), `Stage`, `CheckpointStage`, `PipelineHook` (`BudgetGuard`), `ReviewTopology` + `assign_reviews`, `agreement` (judge stage + ratio), `ranking` (Borda), `RunContext`, `CheckpointStore` |
| `core/modes/` | Concrete stages and pipeline builders per mode. Document loading, chunking, merging and presets |
| `core/anonymize.py` | Regex scrubbing of self-identifying phrases |
| `cli/app.py` | `llm-crossjury run / resume / cost estimate / cost report / providers check` |
| `prompts/` | Versioned prompt templates (`<stage>.v<N>.md`), always in English |
| `schemas/` | Generated `*.schema.json`. A test and CI fail when they drift from the models |
| `config/` | `pricing.yaml` (empty, user-maintained) and `llm-crossjury.example.yaml` |
| `scripts/` | Manual, possibly billed scripts. Never collected by pytest |
| `.github/workflows/ci.yml` | ruff, mypy, pytest and the schema-drift check on Ubuntu + Windows × Python 3.11 + 3.12 |

## 3. Data flow (Council mode)

```
task ─► Stage 0: print budget, plan, CostEstimate ─► [refuse | confirm] ─►
  Stage 1 answer (N parallel calls, shared stable prefix) ─► scrub ─► {label: Stage1Answer}
  ─► Stage 1b agreement (cheap judge, effort low) ─► AgreementJudgment ─► ratio (code)
  ─► Checkpoint A (agreement ratio + review cost) ─► PruneDecision(drop, skip_review)
        │ skip_review=false                              │ skip_review=true
        ▼                                                │
  Stage 2 review (topology → assignments, effort low)    │
     ─► [PeerReview] ─► Borda + severity counts          │
        ▼                                                ▼
  Stage 3 draft synthesis (chair) ─► Synthesis(final_text=None)
  ─► Checkpoint B ─► AdjudicationDecision (decided_by user|auto)
  ─► Stage 4 final synthesis (chair) ─► Synthesis(final_text)
  ─► report (labels de-anonymized for the user only; auto decisions flagged)
```

Document Review follows the same flow, with these differences. The document is
chunked, and stage 1 makes one call per (member, chunk): the whole document sits in
the cached stable prefix, and the chunk id goes in the variable suffix. The stage-1
output is an `EditList`. A deterministic merge step runs before the chair, and the
chair's output is a `DocumentSynthesis`.

## 4. Provider layer

- **Prompt assembly.** Every stage builds a `Prompt`. The `stable_prefix` holds the
  system instructions, the shared context and, in Document Review, the whole
  document. The `variable_suffix` holds the per-call part: the chunk to focus on, the
  targets for a reviewer, the rulings for the chair. Adapters map the end of the
  prefix to the vendor's caching mechanism. The prefix must not contain labels, ids or
  timestamps.
- **Structured output.** The stage passes `strict_json_schema(Model)` as
  `response_schema`. If the adapter supports structured output, it hands the schema
  to the vendor, applying a lossless dialect transform where needed. If it does not,
  it appends format instructions. In every case the stage validates the returned text
  with `Model.model_validate_json` and retries once on failure *(planned)*.
- **Reasoning effort.** `CompletionRequest.reasoning_effort` is one of
  `low | medium | high | default`. Each adapter maps it to vendor parameters. If a
  model has no such control (`Capabilities.reasoning_effort = false`), the value is
  ignored, and that is recorded.
- **Capabilities** are declared per model, and the orchestrator checks them before
  planning batches, caching, temperature or effort settings.
- **Usage** maps vendor counters onto five fields. `None` means unknown.

## 5. Orchestrator

- `Pipeline` holds an ordered list of `Stage` and `CheckpointStage` items. `run(ctx)`
  executes from `ctx.cursor` and returns one of `Completed`, `Paused(request)` or
  `Stopped(reason)`. `resume(ctx, decision)` records the decision and continues. A
  `PruneDecision` with `skip_review` jumps to the checkpoint's `skip_review_to`
  stage. In the skeleton this control flow is real. Everything else is a placeholder.
- **Hooks** run after each model stage and return `CONTINUE`, `SKIP_TO(stage)` or
  `ABORT`. There is deliberately no automatic early-stop hook. Skipping is always a
  user decision, made at checkpoint A.
- **Budget enforcement** happens at two levels. Before each call, `Ledger.check`
  refuses a call that could overshoot the run limit or the daily limit. After each
  stage, `BudgetGuard` acts as a backstop.
- **Topology.** `resolve_topology` picks `full` for ≤ 3 reviewed answers and
  `ring` (k = 1) for ≥ 4, unless one is configured.
- **Ranking.** `borda_aggregate` uses a normalized Borda count: each review
  contributes on a 0..1 scale, and reviews that ranked only one target contribute nothing.
- **`--auto`** sets `ctx.auto`. At checkpoints the CLI then produces decisions with
  `decided_by = auto` (keep all, no skip, defer every dispute to the chair) and
  resumes immediately.
- **Concurrency** *(planned)*: per-provider semaphores. Calls within a stage run in
  parallel, and stages run sequentially.

### Checkpoint mechanism

1. When the pipeline reaches a `CheckpointStage` whose kind has no decision yet, it
   builds a `CheckpointRequest` (run id, kind, candidates and, for A, an
   `AgreementSummary`) and returns `Paused`.
2. Interactive CLI: the CLI renders the request, collects a `UserDecision`, and calls
   `resume` in the same process.
3. Non-interactive or deferred: `RunContext.to_snapshot()` and the request are saved
   by `FileCheckpointStore` (including the content needed to resume; see §6). The
   process exits with code 3. `llm-crossjury resume <run_id>` loads the snapshot,
   validates the decision against the request, and calls `resume`.
4. The decision is stored in `ctx.artifacts[<checkpoint stage name>]`, so reports
   show what was decided and by whom.

## 6. Storage and privacy

Run data **never lives in the repository**. All of it goes to the platform user-data
directory from `platformdirs.user_data_dir("llm-crossjury", appauthor=False)`:

| OS | Typical location |
|---|---|
| Windows | `%LOCALAPPDATA%\llm-crossjury\` |
| macOS | `~/Library/Application Support/llm-crossjury/` |
| Linux | `$XDG_DATA_HOME/llm-crossjury/` (default `~/.local/share/...`) |

Precedence: `CrossjuryConfig.data_dir` > `LLM_CROSSJURY_DATA_DIR` > platformdirs. The
resolver refuses a directory inside a git working tree.

```
<data>/
  ledger.sqlite3              # cost ledger: usage, costs, durations; never text
  runs/<run_id>/run.json      # config snapshot (no secrets), stage status, labels map,
                              # prompt ids/versions, content hashes, cost summary,
                              # decided_by flags, scrub counts
  runs/<run_id>/content/      # prompts, responses, artifacts: ONLY if store_content
  checkpoints/<run_id>/       # unfinished-run snapshot, including content (temporary)
```

**`store_content: true|false`** (default `false`) controls *permanent* storage of
prompt and response text:

| | `store_content: false` | `store_content: true` |
|---|---|---|
| Ledger | counts, costs, durations | counts, costs, durations |
| Run record | metadata + SHA-256 of content | metadata + content under `content/` |
| Checkpoint snapshot (unfinished runs) | full content, **temporary** | full content, temporary |

Checkpoint content is deleted when the run completes. Otherwise it is purged after
`checkpoint_retention_days` (default 7) by `CheckpointStore.purge_expired`, which
runs at the start of every command *(planned)*. This is what makes cross-process
resume possible without keeping content permanently.

No mode ever stores API keys, OAuth tokens, cookies, or CLI credential paths.

## 7. Configuration and secrets

- `llm-crossjury.yaml` (from `config/llm-crossjury.example.yaml`) defines members,
  chair, `chairman_excluded`, review, agreement, budget, cost, document,
  `output_language`, `pricing_path`, `store_content`, `checkpoint_retention_days` and
  `data_dir`. It contains no secrets. A test checks that the example validates.
- Budget defaults are built in ($1.00 per run, $0.25 confirmation threshold, $5.00
  per day). The effective values are printed at the start of every run.
- API keys come only from environment variables, which may be loaded from `.env`.
  They are read at call time inside the `api` adapter and never stored on objects that
  get printed, never written to the ledger or run records, and never forwarded to
  `cli` subprocesses.
- Output redaction *(planned)*: every error message and log line from a provider
  passes through a redactor that masks key-shaped strings.

## 8. Testing strategy

- `tests/unit`: contract shape checks, the schema strict-subset rules, config
  defaults, the example config, the CLI surface.
- `tests/smoke`: end-to-end pipeline runs with `MockProvider` only (the full path and
  the skip-review path).
- The network is blocked by pytest-socket, with only loopback allowed. Real API keys
  are removed from the environment by an autouse fixture, and blanked in CI. Warnings
  are errors.
- CI runs on `ubuntu-latest` and `windows-latest` × Python 3.11 and 3.12, with
  actions pinned by commit SHA.
- `scripts/`: manual live checks, never collected.

## 9. Contract files

`core/schemas/**` and `core/providers/base.py` are contracts shared by every adapter,
stage and future UI. They change only with the maintainer's approval. Problems go to
`docs/contract-issues.md` (see AGENTS.md). The changes approved before phase 1 are
listed in that file.
