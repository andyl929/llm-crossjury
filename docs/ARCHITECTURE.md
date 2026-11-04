# Architecture

Status: draft · Covers the skeleton as committed. Things marked *(planned)* are not built yet.

## 1. Layers

```
        ┌───────────────┐      ┌────────────────────────────┐
        │  council CLI  │      │  Web UI + FastAPI (later)  │
        │  (Typer)      │      │  frontend/ placeholder     │
        └───────┬───────┘      └──────────────┬─────────────┘
                │      same core API          │
┌───────────────▼─────────────────────────────▼───────────────┐
│ council.core                                                │
│  modes/         pipeline builders: compare, council, doc    │
│  orchestrator/  Pipeline, Stage, CheckpointStage, hooks,    │
│                 topology, RunContext, checkpoint store      │
│  schemas/       Pydantic structured-output models (CONTRACT)│
│  cost/          PricingTable, CostEstimator, Ledger         │
│  providers/     base.py (CONTRACT), api/, cli/, mock        │
│  config.py      CouncilConfig        storage.py  DataPaths  │
└───────────────┬─────────────────────────────┬───────────────┘
                │                             │
       vendor SDKs (api)            vendor CLIs as subprocesses (cli)
```

Dependency rule: `cli` → `core.modes` → `core.orchestrator` → `core.{schemas,cost,providers}`.
`core` never imports from `cli`. `schemas` and `providers/base.py` import nothing
else from `council`, except that `schemas.synthesis` imports `schemas.edits`.

## 2. Package map

| Path | Responsibility |
|---|---|
| `core/providers/base.py` | `Provider` / `BatchProvider` protocols, `Prompt` (stable prefix + variable suffix), `Usage` (fields may be unknown), `Capabilities`, errors. **Contract** |
| `core/providers/api/` | One adapter per vendor SDK. Reads its key from the environment at call time |
| `core/providers/cli/` | One adapter per vendor CLI, plus `_subprocess.py` with the hardened runner (ADR 0002) |
| `core/providers/mock.py` | Deterministic provider for tests |
| `core/schemas/` | `Stage1Answer`, `PeerReview`, `EditList`, `Synthesis`, `DocumentSynthesis`, and `export.py` (canonical strict JSON Schema). **Contract** |
| `core/cost/` | `PricingTable` (from YAML), `CostEstimator` → `CostEstimate`, `Ledger` (SQLite), `compute_cost` |
| `core/orchestrator/` | `Pipeline` (run/resume), `Stage` protocol, `CheckpointStage`, `PipelineHook` (`BudgetGuard`, `AgreementEarlyStop`), `ReviewTopology` + `assign_reviews`, `RunContext`, `CheckpointStore` |
| `core/modes/` | Concrete stages and pipeline builders for each mode, plus document loading and presets |
| `cli/app.py` | `council run / resume / cost estimate / cost report / providers check` |
| `prompts/` | Versioned prompt templates (`<stage>.v<N>.md`) |
| `schemas/` | Generated `*.schema.json`. A test fails when they drift from the models |
| `config/` | `pricing.yaml` (empty, user-maintained) and `council.example.yaml` |
| `scripts/` | Manual, possibly billed scripts. Never collected by pytest |

## 3. Data flow (Council mode)

```
task ─► Stage 0 plan ─► CostEstimate ─► [confirm] ─►
  Stage 1 answer (N parallel calls, shared stable prefix) ─► {label: Stage1Answer}
  ─► Checkpoint A ─► PruneDecision
  ─► Stage 2 review (topology → assignments) ─► [PeerReview]
  ─► hooks: BudgetGuard, AgreementEarlyStop (may SKIP_TO draft)
  ─► Stage 3 draft synthesis (chair) ─► Synthesis(final_text=None)
  ─► Checkpoint B ─► AdjudicationDecision
  ─► Stage 4 final synthesis (chair) ─► Synthesis(final_text)
  ─► report (labels de-anonymized for the user only)
```

Document Review follows the same flow. The document sits in the stable prefix, the
stage 1 output is `EditList`, and the chair output is `DocumentSynthesis`.

## 4. Provider layer

- **Prompt assembly.** Every stage builds a `Prompt`. The `stable_prefix` holds the
  system instructions, the shared context and, in Document Review, the document. The
  `variable_suffix` holds the per-call part: the specific targets for a reviewer, the
  rulings for the chair. Adapters map the end of the prefix to the vendor's caching
  mechanism. The prefix must not contain labels, ids or timestamps.
- **Structured output.** The stage passes `strict_json_schema(Model)` as
  `response_schema`. If the adapter supports structured output, it hands the schema
  to the vendor, applying a lossless dialect transform where needed. If it does not,
  it appends format instructions. In every case the stage validates the returned text
  with `Model.model_validate_json` and retries once on failure *(planned)*.
- **Capabilities** are declared per model, and the orchestrator checks them before
  planning batches, caching or temperature settings.
- **Usage** maps vendor counters onto five fields. `None` means unknown.

## 5. Orchestrator

- `Pipeline` holds an ordered list of `Stage` and `CheckpointStage` items. `run(ctx)`
  executes from `ctx.cursor` and returns one of `Completed`, `Paused(request)` or
  `Stopped(reason)`. `resume(ctx, decision)` records the decision and continues. In
  the skeleton this control flow is real. Everything else is a placeholder.
- **Hooks** run after each model stage and return `CONTINUE`, `SKIP_TO(stage)` or `ABORT`.
- **Budget enforcement** happens at two levels. Before each call, `Ledger.check`
  refuses a call that could overshoot. After each stage, `BudgetGuard` acts as a
  backstop.
- **Concurrency** *(planned)*: per-provider semaphores. Calls within a stage run in
  parallel, and stages run sequentially.

### Checkpoint mechanism

1. When the pipeline reaches a `CheckpointStage` whose kind has no decision yet, it
   builds a `CheckpointRequest` (run id, kind, candidates) and returns `Paused`.
2. Interactive CLI: the CLI renders the request, collects a `UserDecision`, and calls
   `resume` in the same process.
3. Non-interactive or deferred: `RunContext.to_snapshot()` and the request are saved
   by `FileCheckpointStore`. The process exits with code 3. `council resume <run_id>`
   loads the snapshot, validates the decision against the request, and calls `resume`.
4. The decision itself is stored in `ctx.artifacts[<checkpoint stage name>]`, so
   reports show what the user decided.

## 6. Storage and privacy

Run data **never lives in the repository**. All of it goes to the platform user-data
directory from `platformdirs.user_data_dir("council-placeholder", appauthor=False)`:

| OS | Typical location |
|---|---|
| Windows | `%LOCALAPPDATA%\council-placeholder\` |
| macOS | `~/Library/Application Support/council-placeholder/` |
| Linux | `$XDG_DATA_HOME/council-placeholder/` (default `~/.local/share/...`) |

Precedence: `CouncilConfig.data_dir` > `COUNCIL_DATA_DIR` > platformdirs. The
resolver refuses a directory inside a git working tree.

```
<data>/
  ledger.sqlite3              # cost ledger: usage counts and costs only, never text
  runs/<run_id>/run.json      # config snapshot (no secrets), stage status, labels map,
                              # prompt ids/versions, content hashes, cost summary
  runs/<run_id>/content/      # prompts, responses, artifacts — ONLY if store_content
  checkpoints/<run_id>.json   # paused-run snapshot
```

**`store_content: true|false`** (default `false`, see SPEC Open Questions) controls
whether prompt and response text is ever written to disk:

| | `store_content: false` | `store_content: true` |
|---|---|---|
| Ledger | counts and costs | counts and costs |
| Run record | metadata + SHA-256 of content | metadata + content under `content/` |
| Checkpoint snapshot | in-process only; cross-process resume not possible | full snapshot, resumable later |

No mode ever stores API keys, OAuth tokens, cookies, or CLI credential paths.

## 7. Configuration and secrets

- `council.yaml` (from `config/council.example.yaml`) defines members, chair, review,
  budget, cost options, `pricing_path`, `store_content` and `data_dir`. It contains no
  secrets.
- API keys come only from environment variables, which may be loaded from `.env`.
  They are read at call time inside the `api` adapter and never stored on objects that
  get printed, never written to the ledger or run records, and never forwarded to
  `cli` subprocesses.
- Output redaction *(planned)*: every error message and log line from a provider
  passes through a redactor that masks key-shaped strings.

## 8. Testing strategy

- `tests/unit`: contract shape checks, the schema strict-subset rules, the CLI surface.
- `tests/smoke`: end-to-end pipeline runs with `MockProvider` only.
- The network is blocked by pytest-socket, with only loopback allowed. Real API keys
  are removed from the environment by an autouse fixture. Warnings are errors.
- `scripts/`: manual live checks, never collected.

## 9. Contract files

`core/schemas/**` and `core/providers/base.py` are contracts shared by every adapter,
stage and future UI. They change only with the maintainer's approval. Problems go to
`docs/contract-issues.md` (see AGENTS.md).
