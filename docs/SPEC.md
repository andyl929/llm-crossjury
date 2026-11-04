# llm-crossjury — Product Specification (v1)

Status: draft for review · Phase: design/skeleton

## 1. Purpose

llm-crossjury runs one task across several LLMs (Claude, OpenAI, Gemini, Grok). Each model
answers independently. The answers are then reviewed anonymously by the other
models, and a designated **chair** model synthesizes them. llm-crossjury does not hide
disagreement behind a smooth summary. It shows the points where members disagree
and lets the **user** settle them.

It is built for general professional work: analysis, design decisions, technical
writing, review. Document Review is a built-in mode that turns the same process
into a list of concrete, checkable edits. Academic paper revision is one preset of
that mode.

### Glossary

| Term | Meaning |
|---|---|
| Member | A configured (provider, model, backend) that answers the task |
| Chair | The model that writes the synthesis. It may also be a member |
| Label | Anonymous name of an answer within a run, e.g. `Response B`. It is shuffled per run |
| Claim | An atomic, checkable statement a member extracts from its own answer |
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
5. **US-5 Prune.** After the first round, I want to drop answers that are off-topic
   or clearly wrong, so they don't waste review budget or pollute the synthesis.
6. **US-6 Document review.** I want to submit a Markdown or LaTeX file and get back a
   list of proposed edits (location, original, replacement, reason, category), so I
   stay in control of my text and can accept edits one by one.
7. **US-7 Paper preset.** As an academic, I want a `paper` preset that focuses on
   correctness, clarity, structure, consistency and citations.
8. **US-8 Cost before commitment.** I want an estimate before anything runs and a hard
   budget that stops the run, so I never get surprised by a bill.
9. **US-9 Cost after.** I want a per-run, per-stage, per-provider cost report.
10. **US-10 Use my subscriptions.** I want to optionally use the official CLIs I am
    already signed in to, without llm-crossjury ever touching my credentials.
11. **US-11 Pause and come back.** I want to stop at a checkpoint, close the terminal,
    and resume later with my decision.
12. **US-12 Privacy.** I want to choose whether llm-crossjury stores prompt and response
    text on disk at all.

## 3. Modes

Every mode starts with **Stage 0 — Plan & estimate**:

| | |
|---|---|
| Input | Task text, mode, config (members, chair, topology, budget), optional document and preset |
| Output | `CostEstimate` (range, unpriced calls, assumptions). The user confirms if the high estimate exceeds `confirm_above`. The run is refused if it exceeds `budget.limit` |

Unless stated otherwise, member and chair calls ask for structured output that
follows the schemas in `core/schemas` (`schemas/*.schema.json`).

### 3.1 Compare

| Stage | Input | Output |
|---|---|---|
| 1. Answer | Task | One `Stage1Answer` per member, labelled |
| Report | All answers + ledger | Side-by-side view (answer, claims, caveats), cost per member. Labels are revealed here, because Compare is not anonymous at the reporting step |

There are no checkpoints, no review, and no chair.

### 3.2 Council

| Stage | Input | Output |
|---|---|---|
| 1. Answer | Task | `Stage1Answer` per member, in parallel. Labels are assigned by random shuffle |
| **Checkpoint A — Prune** | Labelled answers (still anonymous to the user by default) | `PruneDecision(drop=[labels])` |
| 2. Peer review | Task + kept answers (labels only) + topology | `PeerReview` per review call: per-target issues, each with a verbatim quote and a severity, then strengths, score rationale and score, then a ranking |
| (early stop) | Stage-1 claims | If claim agreement ≥ the threshold, skip to stage 3 with no reviews |
| 3. Draft synthesis | Task + kept answers + reviews (labels only) | `Synthesis` with `consensus[]`, `disputed[]` (each with `positions[]` per label), `unique[]`, `final_text = null` |
| **Checkpoint B — Adjudicate** | `disputed[]` | `AdjudicationDecision(rulings[])`. Per dispute the user picks one of: adopt a position, custom text, defer to chair, leave open |
| 4. Final synthesis | Draft + rulings | `Synthesis` with `final_text` |
| Report | All artifacts + ledger | Final text, the dispute table with rulings, cost report. Labels are de-anonymized for the user only |

If the draft has no disputes, checkpoint B is satisfied automatically and the run goes straight to stage 4.

### 3.3 Document Review

v1 input formats: **Markdown (`.md`)** and **LaTeX (`.tex`)**, single file, UTF-8.

| Stage | Input | Output |
|---|---|---|
| 0'. Load | File path, preset | `SourceDocument` (text, format, sha256). The document is placed in the stable prompt prefix |
| 1. Edit lists | Document + preset instructions + user instructions | `EditList` per member. Each `Edit` has `location`, `original` (verbatim), `replacement`, `reason`, `category`, `severity`. Edits whose `original` cannot be found are flagged |
| **Checkpoint A — Prune** | Edit lists (by label, with counts per category) | `PruneDecision` |
| 2. Peer review | Document + kept edit lists | `PeerReview`. Issues quote the reviewed edit and use `claim_id` to carry the edit id |
| 3. Draft synthesis | Document + edit lists + reviews | `DocumentSynthesis`: merged, de-duplicated `edits[]` (consensus edits). Conflicting edits go in `synthesis.disputed[]`, unique edits are noted in `synthesis.unique[]` |
| **Checkpoint B — Adjudicate** | Disputed edits | `AdjudicationDecision` |
| 4. Final | Draft + rulings | Final `DocumentSynthesis` (edits + summary) |
| Report | Final edits | Edit table, plus an optional unified diff for the user to apply. **llm-crossjury never overwrites the input file** |

Presets (`paper` in v1) set the instructions, the allowed `EditCategory` values, and the prompt version.

## 4. Human checkpoints

| | A — Prune | B — Adjudicate |
|---|---|---|
| When | After stage 1 | After the draft synthesis, before the final synthesis |
| User sees | Each answer or edit list under its label, plus cost so far and estimated remaining cost | Each disputed point: the question, every position with its label and argument, and the chair's assessment shown separately |
| User returns | Labels to drop (empty keeps all) | One ruling per dispute |
| Validation | At least one answer must remain, and two for peer review. If only one remains, review is skipped | Every dispute id needs a ruling. `adopt_position` needs a label that exists in that dispute |
| Interactive CLI | Prompted inline | Prompted inline |
| `--non-interactive` | Run state is saved and the process exits with code 3, printing the run id. `llm-crossjury resume <id> --decision file.json` continues | Same |
| `--auto` (TBD, see Open Questions) | Keep all | Defer to chair |

Run state is the same whether the run pauses in-process or is resumed later. The
future Web UI will use the same mechanism.

## 5. Cost behavior

- **Before the run:** an estimate range is computed from the call plan, the price
  table, and token heuristics or vendor token counting. The run is refused if the
  high end is over the budget. The user must confirm if it is over `confirm_above`.
- **During the run:** every call is checked against `spent + reserved + call_high`
  before it starts, and is recorded in the ledger as soon as it finishes. A call that
  could exceed the budget is not started. The run stops cleanly: artifacts so far
  are kept, and the report says "stopped: budget".
- **Unknown cost:** `cli` backend calls and calls with unknown usage are counted as
  *unknown*. They are only allowed if `allow_unknown_cost: true`.
- **Prices:** prices come only from `config/pricing.yaml`. The report shows the
  `last_verified` date of each price used and warns about stale entries.

## 6. Anonymity

- Labels are shuffled per run. The label→member map lives only in the run state and
  is never put in a prompt.
- Members and the chair are told they are reviewing "other responses". Vendor names,
  model names and self-identifying phrases are not removed in v1 (see Open
  Questions), but the prompt asks members not to identify themselves.
- The order of targets in review prompts is shuffled per reviewer.

## 7. Backends

- `api`: the official SDK with a key from the environment (`.env`). This is the
  default backend.
- `cli` (*experimental*): an official CLI the user is signed in to, under the security
  boundary in ADR 0002. A CLI that cannot meet that boundary is skipped and the reason
  is documented. Grok CLI is skipped until verified (see the ADR and the Open Questions).
- Members of one run may mix backends.

## 8. Non-goals (v1)

- Web UI and HTTP API (a later phase; `frontend/` is a placeholder).
- Input formats other than Markdown and LaTeX (no PDF, DOCX, multi-file LaTeX
  projects or `\input` resolution).
- Writing edits back into the user's file automatically.
- Tool use, web browsing or code execution by members.
- Multi-turn conversation with the council. One run is one task.
- Fine-tuning, model hosting, local models.
- Hosted service, accounts, telemetry.
- Extracting, proxying or reusing CLI credentials in any form.
- Guaranteeing perfect anonymity (stylistic fingerprints are out of scope).
- Bundled price data (users maintain `pricing.yaml`).

## 9. Open Questions

1. **Final project name.** The distribution name is `llm-crossjury` for now and
   the import name is `council`. What should the final PyPI/GitHub name be? `council`
   is likely taken on PyPI.
2. **Chair as member.** May the chair also be a member? If so, does it see its own
   answer de-anonymized, or does it stay blind?
3. **Default topology and size.** Should the default be `full` for ≤4 members and
   `ring` above that? What is the default `ring_k`?
4. **Early-stop criterion.** How is "agreement" measured on claims (chair-judged,
   embedding similarity, or an exact-match heuristic)? What is the default threshold?
   Should early stop be on by default?
5. **`--auto` mode.** Should there be a fully unattended mode (keep all, defer to
   chair)? Should it be allowed at all, given that adjudication is a core value?
6. **Anonymity scrubbing.** Should llm-crossjury actively remove self-identifying phrases
   ("As Claude…") from answers before review, or only instruct against them?
7. **Default budget.** What should the shipped default `budget.limit` and
   `confirm_above` be? Should a run with no budget configured be refused?
8. **Grok CLI.** Is there an official xAI CLI that can meet ADR 0002? Until this is
   confirmed, Grok is API-only.
9. **Unknown-cost policy for `cli`.** Should `cli` calls be allowed by default (the
   user's subscription pays), or must the user opt in with `allow_unknown_cost`?
10. **`store_content` default.** The proposed default is `false`. That makes paused
    runs resumable only within the same process. Is that acceptable, or should
    checkpoints always store content (encrypted? plain?) while run records do not?
11. **Reasoning effort / thinking settings.** Should llm-crossjury expose per-member
    reasoning-effort settings in v1? They strongly affect cost.
12. **Review scoring scale.** Is a 1–10 score with a written rationale right, or is a
    ranking alone enough?
13. **Document size limit.** What is the maximum document size in v1? Should long
    documents be chunked, or refused above the smallest member's context window?
14. **Edit conflict policy.** When two edits overlap, should the chair always turn
    them into a dispute, or merge them when they are compatible?
15. **Language.** Should prompts and outputs follow the language of the task or
    document automatically? Should the paper preset support non-English papers?
