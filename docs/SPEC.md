# llm-crossjury: Product Specification (v1)

Status: draft for review · Phase: design/skeleton (phase 0 accepted; pre-phase-1 decisions and model registry applied)

## 1. Purpose

llm-crossjury runs one task across several LLMs (Claude, OpenAI, Gemini, Grok). Each
model answers independently. The answers are then reviewed anonymously by the other
models, and a designated **chair** model synthesizes them. llm-crossjury does not hide
disagreement behind a smooth summary. It shows the points where members disagree and
lets the **user** settle them.

It is built for general professional work: analysis, design decisions, technical
writing, review. Document Review is a built-in mode that turns the same process into a
list of concrete, checkable edits. Academic paper revision is one preset of that mode.

### Glossary

| Term | Meaning |
|---|---|
| Member | A configured (provider, model or alias, backend) that answers the task |
| Registry | `config/models.yaml`: model ids, backends, reasoning controls, CLI flags, limits, prices (ADR 0006) |
| Alias | `fast` / `balanced` / `best` per provider, pointing to a registry entry |
| Chair | The model that writes the synthesis. It may also be a member (§3.2) |
| Label | Anonymous name of an answer within a run, e.g. `Response B`. Shuffled per run |
| Claim | An atomic, checkable statement a member extracts from its own answer |
| Agreement ratio | Share of claim groups that every kept answer supports and none contradicts (§3.2) |
| Checkpoint | A point where the run pauses for a user decision |
| Run | One execution of a mode on one task |

## 2. User stories

1. **US-1 Compare.** As a professional, I want to see how four models answer the same
   question side by side, with what each answer cost, so I can judge them myself.
2. **US-2 Council.** I want the models to critique each other without knowing who
   wrote what, so that the final answer reflects the arguments and not brand loyalty.
3. **US-3 See disagreement.** I want consensus, disputed points (with each side's
   position) and unique insights listed separately, so I know what is settled and
   what is not.
4. **US-4 Decide disputes.** I want to rule on each disputed point before the final
   answer is written, so the final text reflects my judgment where it matters.
5. **US-5 Prune and skip.** After the first round, I want to drop weak answers, and
   see how much the answers already agree and what skipping review would save, so I
   can decide whether peer review is worth its cost.
6. **US-6 Document review.** I want to submit a Markdown or LaTeX file and get back a
   list of proposed edits (location, original, replacement, reason, category), so I
   stay in control of my text and can accept edits one by one.
7. **US-7 Paper preset.** As an academic, I want a `paper` preset that focuses on
   correctness, clarity, structure, consistency and citations, including for papers
   not written in English.
8. **US-8 Cost before commitment.** I want an estimate before anything runs, plus
   per-run and daily hard limits, so I never get surprised by a bill.
9. **US-9 Cost after.** I want a per-run, per-stage, per-provider cost report,
   including how many calls went through CLIs and how long they took.
10. **US-10 Use my subscriptions.** I want to optionally use the official CLIs I am
    already signed in to, without llm-crossjury ever touching my credentials.
11. **US-11 Pause and come back.** I want to stop at a checkpoint, close the terminal,
    and resume later with my decision.
12. **US-12 Privacy.** I want to choose whether llm-crossjury permanently stores
    prompt and response text on disk.
13. **US-13 Unattended runs.** I want an explicit `--auto` mode for batch use, where
    every decision the tool made on my behalf is clearly flagged.
14. **US-14 Language.** I want the output in the language of my task or document,
    or a language I choose.

## 3. Modes

Every mode starts with **Stage 0: Plan & estimate**:

| | |
|---|---|
| Input | Task text, mode, config (members, chair, topology, budget), optional document and preset |
| Output | The effective budget is printed (run limit, confirmation threshold, daily limit, today's spend). Then every member's **resolved** model (alias → id), backend and native reasoning parameter, with any downgrade warnings (§5.1). Then the `CostEstimate`, including estimated reasoning tokens (range, unpriced calls, assumptions). The run is **refused** if any `api` model lacks a verified registry price, if the high estimate exceeds the run limit or the remaining daily budget, or if a `cli` member is configured without `allow_unknown_cost: true`. The user must confirm if the high estimate exceeds `confirm_above` |

Unless stated otherwise, member and chair calls ask for structured output that follows
the schemas in `core/schemas` (`schemas/*.schema.json`).

### 3.1 Compare

| Stage | Input | Output |
|---|---|---|
| 1. Answer | Task | One `Stage1Answer` per member, labelled |
| Report | All answers + ledger | Side-by-side view (answer, claims, caveats), cost per member. Labels are revealed here, because Compare is not anonymous at the reporting step |

There are no checkpoints, no review, and no chair.

### 3.2 Council

| Stage | Input | Output |
|---|---|---|
| 1. Answer | Task | `Stage1Answer` per member, in parallel, each at its resolved reasoning level (role `answerer`). Labels are assigned by random shuffle. Answers are anonymity-scrubbed (§6) |
| 1b. Agreement | All claims (labels only) | A cheap judge model (reasoning effort `low`) returns an `AgreementJudgment` (claim groups with supporting and contradicting claim refs). The **agreement ratio** is computed in code: the number of groups supported by every kept answer and contradicted by none, divided by the number of groups |
| **Checkpoint A: Prune / skip** | Labelled answers, agreement ratio, estimated cost of peer review (= the saving if skipped), a recommendation if the ratio ≥ the configured threshold | `PruneDecision(drop=[labels], skip_review=bool)`. **Nothing is skipped automatically** |
| 2. Peer review | Task + kept answers (labels only) + topology | `PeerReview` per review call: per target, issues (verbatim quote, problem, severity, optional fix) and strengths, then a ranking rationale and a **ranking**. No numeric scores. Reasoning effort `low` by default |
| 2b. Aggregate | All reviews | Normalized Borda count over rankings (in code), plus issue counts by severity per label |
| 3. Draft synthesis | Task + kept answers + reviews + aggregate (labels only) | `Synthesis` with `consensus[]`, `disputed[]` (each with `positions[]` per label), `unique[]`, `final_text = null` |
| **Checkpoint B: Adjudicate** | `disputed[]` | `AdjudicationDecision(rulings[])`. Per dispute the user picks one of: adopt a position, custom text, defer to chair, leave open |
| 4. Final synthesis | Draft + rulings | `Synthesis` with `final_text` |
| Report | All artifacts + ledger | Final text, the dispute table with rulings (auto rulings flagged), the Borda ranking, cost report. Labels are de-anonymized for the user only |

**Chair as member.** The chair may also be a member (default). It always works on
anonymous labels, including its own answer, and is not told which answer is its own.
With `chairman_excluded: true`, any member identical to the chair (same provider,
model and backend) is removed from the answer and review stages.

**Default topology.** If `review.topology` is unset: `full` when ≤ 3 answers survive
checkpoint A, and `ring` with **k = 2** when there are 4 or more. Any topology can be
configured explicitly. `ring` with **k = 1** is kept as the **"issues only"** mode: each
reviewer sees one target, so rankings carry no information. Borda aggregation is
skipped, and the report shows issue counts by severity without a ranking.

If the draft has no disputes, checkpoint B is satisfied automatically and the run goes
straight to stage 4.

### 3.3 Document Review

v1 input formats: **Markdown (`.md`)** and **LaTeX (`.tex`)**, single file, UTF-8.

| Stage | Input | Output |
|---|---|---|
| 0'. Load & chunk | File path, preset | `SourceDocument`. Refused above `document.max_document_tokens` (estimated, configurable hard limit). Split into `DocumentChunk`s at Markdown headings (`#`, `##` by default, configurable depth) or LaTeX `\chapter`/`\section`. The estimate shown before the run accounts for one call per (member, chunk) |
| 1. Edit lists | Full document in the cached prefix, plus the chunk to focus on in the suffix | `EditList` per member (merged over chunks). Each `Edit` has `location`, verbatim `original`, `replacement`, `reason`, `category`, `severity`. Edits whose `original` cannot be found are flagged |
| 1b. Agreement | Edit lists | As in Council, with edits as the "claims" |
| **Checkpoint A** | Edit lists (by label, counts per category), agreement, review cost | `PruneDecision` |
| 2. Peer review | Document + kept edit lists | `PeerReview`. Issues quote the reviewed edit, and `claim_id` carries the edit id |
| 3a. Merge (code) | Kept edit lists | Identical edits and edits with non-overlapping spans are **merged automatically**. Overlapping spans with different replacements become `EditConflict`s |
| 3b. Draft synthesis | Document + merged edits + conflicts + reviews | `DocumentSynthesis`: `edits[]` = the auto-merged edits. Each conflict becomes one `synthesis.disputed[]` entry with a position per label. `unique[]` notes single-member edits |
| **Checkpoint B** | Disputed edits | `AdjudicationDecision` |
| 4. Final | Draft + rulings | Final `DocumentSynthesis` (edits + summary) |
| Report | Final edits | Edit table (auto rulings flagged), plus an optional unified diff for the user to apply. **The input file is never overwritten** |

Presets (`paper` in v1) set the instructions, the allowed `EditCategory` values, and
the prompt version.

### 3.4 Language

- Output follows the language of the task (Council, Compare) or the document
  (Document Review). `output_language` (config) or `--output-language` (CLI)
  overrides this with a BCP 47 tag.
- The `paper` preset supports papers in any language. `original` is always quoted
  verbatim in the source language.
- Internal prompt templates are written in English, and the target output language
  is passed to them as a variable.

## 4. Human checkpoints

| | A: Prune / skip | B: Adjudicate |
|---|---|---|
| When | After stage 1 and the agreement measurement | After the draft synthesis, before the final synthesis |
| User sees | Each answer or edit list under its label; the agreement ratio with the judge model used; the estimated cost of peer review (the saving if skipped); a "consider skipping" hint if the ratio ≥ the threshold; cost so far | Each disputed point: the question, every position with its label and argument, and the chair's assessment shown separately |
| User returns | Labels to drop (empty keeps all), and whether to skip peer review | One ruling per dispute |
| Validation | At least one answer must remain, and two for peer review. If only one remains, review is skipped | Every dispute id needs a ruling. `adopt_position` needs a label that exists in that dispute |
| Interactive CLI | Prompted inline | Prompted inline |
| `--non-interactive` | Run state is saved and the process exits with code 3, printing the run id. `llm-crossjury resume <id> --decision file.json` continues | Same |
| `--auto` (explicit opt-in only) | Keep all, do not skip review | Every dispute is `defer_to_chair` |

Every decision carries `decided_by: user | auto`. Every output that depends on an
automatic decision flags it (for example "⚠ auto-decided by chair").

Paused runs can be resumed from another process (§7, storage).

## 5. Cost behavior

- **Built-in budget defaults** (overridable in config or with `--budget` for the run
  limit): run limit **$5.00**, confirm above **$1.00**, daily limit **$15.00** (the
  local calendar day, across all runs). The effective values and today's spend are
  printed at the start of every run. In the web UI the three limits are edited on the
  Settings page (`AppSettings.budget`, ADR 0008) and replace these defaults.
- **Prices:** prices come only from the model registry `config/models.yaml` (ADR 0006),
  and only from verified entries (`source_url` and `last_verified` set). If an `api`
  model has no verified price, the run is **refused**. There is no override. The report shows the
  `last_verified` date of each price used and warns about stale entries.
- **Before the run:** an estimate range is computed from the call plan, the price
  table, and token heuristics or vendor token counting. The run is refused if the
  high end exceeds the run limit or the remaining daily budget.
- **During the run:** every call is checked against `spent + reserved + call_high`
  for both the run and the day before it starts, and is recorded in the ledger as soon
  as it finishes. A call that could exceed either limit is not started. The run stops
  cleanly: artifacts so far are kept, and the report says "stopped: run budget" or
  "stopped: daily budget".
- **CLI backend:** per-call cost is unknowable. Any `cli` member requires an explicit
  `allow_unknown_cost: true`. The ledger records the **number of CLI calls and their
  wall-clock duration**, and the report lists them separately from priced spend.
- **Reasoning tokens** are part of the estimate: visible output × a multiplier per
  level. Precedence: the ledger-observed ratio for (model, level) once enough calls
  are recorded; then the registry entry's optional `reasoning_multipliers` (a partial
  per-model override); then the global `cost.reasoning_multipliers` (provisional
  defaults).

### 5.1 Models and reasoning (ADR 0006)

- Members name a model by **alias** (`fast` / `balanced` / `best` of their provider)
  or by concrete id. `reasoning` is `low | medium | high | default`. An optional
  `native` dict passes vendor parameters through verbatim.
- **Roles** set defaults: `answerer` default, `reviewer` low, `agreement_judge` low,
  `chairman` default. Reviewer and judge calls always use the role default.
- The **ModelResolver** resolves aliases, checks that the backend is supported, and
  maps the level to the registry's native value. If a level cannot be mapped, the
  normal mode downgrades it to `default` with a warning, and the **strict** mode
  (`resolution: strict` / `--strict`) fails.
- **Unregistered models** may be used, but only with `default` reasoning. For `api`
  they also lack a verified price, so such runs are refused (§5).
- Every call's **native parameters actually sent** are recorded in the run metadata.
- `llm-crossjury models list` shows the registry. `llm-crossjury models refresh` diffs
  it against the vendors' model-list endpoints and, by default, **only prints the
  differences**. `--draft <file>` also writes draft entries for models the vendor
  lists but the registry lacks: only `provider` and `model_id` (exactly as returned
  by the official endpoint) are filled, every other field is `null`, and each entry is
  marked as unverified. Drafts fail registry validation until a human completes them.
  `refresh` never edits `models.yaml` (phase 1A).

## 6. Anonymity

- Labels are shuffled per run. The label→member map lives only in the run state and
  is never put in a prompt.
- Every member prompt instructs the model not to reveal its identity, vendor or model.
- Member output (stage-1 answers and Document Review edit reasons) is then scrubbed
  **language-independently**. There are no sentence patterns: the model and vendor
  names Claude, ChatGPT, GPT, Gemini, Grok, Anthropic, OpenAI, Google and xAI (with
  hyphenated version suffixes such as `GPT-x`) are replaced with `[model]`, matched
  **case-insensitively on word boundaries** ("claude", "OPENAI" and "Gpt-4o" match;
  "claudette" does not). Ordinary words equal to a name (e.g. the verb "grok") are
  replaced too, which is an accepted trade-off.
  The count is recorded in the run record.
- If the **original task input** (task text, or the document in Document Review)
  contains any of these names, the scrub is **disabled for that run**, because the task
  is then legitimately about them.
- The order of targets in review prompts is shuffled per reviewer.

## 7. Storage and privacy

- `store_content` defaults to **false**: permanent run records hold metadata and
  content hashes only.
- To allow cross-process resume, checkpoint snapshots of **unfinished** runs include
  the content they need. That content is temporary: it is deleted when the run
  completes, and purged after `checkpoint_retention_days` (default **7**) otherwise.
- Details: ARCHITECTURE §6.

## 8. Backends

- `api`: the official SDK with a key from the environment (`.env`). This is the default.
- `cli` (*experimental*): an official CLI the user is signed in to, under the security
  boundary in ADR 0002. A CLI that cannot meet that boundary is skipped and the reason
  is documented. All four CLIs (`claude`, `codex`, `gemini`, and xAI's Grok Build
  CLI `grok`) are evaluated in phase 1B.
- Members of one run may mix backends.

## 9. Non-goals (v1)

- Web UI and HTTP API (a later phase; `frontend/` is a placeholder).
- Document Review input formats other than Markdown and LaTeX (no PDF, DOCX,
  multi-file LaTeX projects or `\input` resolution). *Amended 2026-11-08:* task
  attachments (PDF, DOCX, images) in Compare and its synthesis upgrade are in scope;
  see ADR 0007.
- Writing edits back into the user's file automatically.
- Tool use, web browsing or code execution by members.
- Multi-turn conversation with the council. One run is one task.
- Fine-tuning, model hosting, local models.
- Hosted service, accounts, telemetry.
- Extracting, proxying or reusing CLI credentials in any form.
- Guaranteeing perfect anonymity (stylistic fingerprints are out of scope).
- Bundled model facts or price data (users maintain `config/models.yaml`).
- Automatic skipping of pipeline stages based on model output.

## 10. Open Questions

### Decided

1. **Final project name.** *Decided:* `llm-crossjury` is the distribution and CLI
   name. The import name is `llm_crossjury`, because Python identifiers cannot contain `-`.
2. **Chair as member.** *Decided:* allowed by default. The chair stays blind to which
   answer is its own. Configurable with `chairman_excluded` (§3.2).
3. **Default topology.** *Decided:* `full` for ≤ 3 answers, `ring` for ≥ 4 (k: see 18) (§3.2).
4. **Early stop.** *Decided:* no automatic skipping. Checkpoint A shows the agreement
   ratio (cheap judge over claims) and the estimated saving, and the user decides.
   The threshold is configurable; its default is set in the evaluation phase.
5. **`--auto`.** *Decided:* explicit opt-in only. Auto decisions are flagged in all
   outputs (§4).
6. **Anonymity.** *Decided:* a prompt instruction plus name scrubbing (§6; see 19).
7. **Budget.** *Decided:* built-in defaults of $5.00 per run, confirmation above
   $1.00, and $15.00 per day, printed at run start (raised from $1.00 / $0.25 / $5.00
   by ADR 0008, sized for the 32k output default; editable in the web UI's Settings). A missing price entry refuses the
   run (§5).
8. **Grok CLI.** *Decided:* not pre-skipped. xAI's official Grok Build CLI (`grok`)
   is evaluated in phase 1B under ADR 0002.
9. **Unknown cost for `cli`.** *Decided:* `allow_unknown_cost: true` is required. The
   ledger records CLI call counts and durations (§5).
10. **`store_content`.** *Decided:* default false. Unfinished-run checkpoints keep
    content temporarily (deleted on completion or after 7 days) (§7).
11. **Reasoning effort.** *Decided:* `low | medium | high | default`, mapped per
    provider. Review and agreement default to `low` (§5).
12. **Review scoring.** *Decided:* no 1–10 scores. A ranking (Borda-aggregated) plus
    an evidence-anchored, severity-tagged issue list (§3.2).
13. **Document size.** *Decided:* chunking by Markdown headings or LaTeX `\section`.
    The hard limit is configurable. The cost estimate is shown before the run (§3.3).
14. **Edit conflicts.** *Decided:* identical or non-overlapping edits are merged
    automatically. Different edits to the same place are disputed. Under `--auto` the
    chair decides and the decision is flagged (§3.3).
15. **Language.** *Decided:* output follows the input, with an `output_language`
    override. The paper preset supports non-English papers. Internal prompts are in
    English (§3.4).

16. **Agreement threshold default.** *Decided:* deferred to the evaluation phase, together
    with the default judge model.
17. **`max_document_tokens` default.** *Decided:* 50,000 estimated tokens.
18. **Ranking under `ring`.** *Decided:* with ≥ 4 members the default is `ring` with
    k = 2. k = 1 stays available as the "issues only, no ranking" mode, which skips
    Borda aggregation (§3.2).
19. **Scrubbing beyond English.** *Decided:* no language-specific sentence patterns.
    Model and vendor names are replaced with `[model]`, and scrubbing is disabled for
    any run whose task input contains those names (§6).
20. **Daily limit and time zones.** *Decided:* the local calendar day.
21. **`--auto` and checkpoint A.** *Decided:* `--auto` never skips peer review.

22. **Reasoning multipliers.** *Decided:* global multipliers (`cost.reasoning_multipliers`,
    provisional `low` 0.5 / `medium` 1.0 / `high` 2.0 / `default` 1.0), which a registry
    entry may override per model and per level. Ledger calibration takes precedence
    once available (§5).
23. **Unregistered `api` models.** *Decided:* agreed. They are allowed with `default`
    reasoning but have no verified price, so `api` runs that need them are refused. In
    practice they are usable on the `cli` backend only. To use one on `api`, add a
    registry entry.
24. **`models refresh` output.** *Decided:* print the diff by default. `--draft <file>`
    writes unverified draft entries (official `model_id` only, all other fields `null`)
    (§5.1).

### Open

_None._
