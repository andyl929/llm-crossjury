# Does crossjury help? A small public eval (M1.7)

> **Status: results TBD.** The harness is ready and a 6-question pilot has run (see
> *Pilot* below). Every number marked **TBD** is filled in only after the full run.
> Pilot numbers are about cost and time only. They are not evidence of accuracy.

This is a small, honest check. It is not a research study. With 50 questions per
dataset, one accuracy point is two questions, so the 95% confidence interval on any
accuracy is roughly ±14 points. Treat differences smaller than that as noise.

## What we measure

crossjury sends one question to several models (step 1, "members"). A chair model then
runs the synthesis (step 2): an agreement judge, a peer review, and the chair. The
chair writes one final answer and lists points that are **disputed**. Under the final
answer, reviewers' serious issues are listed as **possibly wrong**.

The question is whether this helps compared with simpler options:

| System | Definition |
|---|---|
| Each member | The member's own answer. |
| Best single member | The member with the highest accuracy on that dataset. This is chosen after the fact, so it is an optimistic baseline. |
| Majority vote | GPQA: the most common letter among the members; a tie for first place counts as wrong. SimpleQA: correct only if more than half of all members are graded correct (free-text answers cannot be grouped without another model call; a strict majority of correct grades means a vote over answer meanings picks the gold answer). A failed member counts as wrong. |
| crossjury synthesis | The chair's `final_text`. If the synthesis fails, it counts as wrong. |

**Headline metric: flag precision.** For each question with a synthesis, the question is
*flagged* when the synthesis output shows any disputed point (`additional.disputed`) or
any possibly-wrong item (`additional.possibly_wrong`, reviewer issues with severity
critical or major). This is what the UI shows under the final answer. We report:

- **coverage**: the share of questions flagged;
- **error rate of flagged answers**: the share of flagged synthesis answers that are wrong;
- **error rate of unflagged answers**: the same for unflagged answers;
- **errors caught**: the share of all wrong synthesis answers that were flagged.

A useful flag has a much higher error rate when it is set than when it is not. The
report also gives four alternative flag rules (chair disputes only, review issues only,
agreement-stage disputed groups, agreement ratio below 0.8).

## Method

- **Data** (downloaded at run time into the user cache directory; never committed):
  - GPQA Diamond (198 questions), `gpqa_diamond.csv` from `dataset.zip` in
    [idavidrein/gpqa](https://github.com/idavidrein/gpqa). The zip password is read
    from that repository's README at run time. GPQA asks that its items are not
    revealed online, so this page and the repository contain only aggregate numbers,
    record ids and question hashes.
  - SimpleQA (4,326 questions),
    `https://openaipublic.blob.core.windows.net/simple-evals/simple_qa_test_set.csv`.
- **Sampling**: 50 questions per dataset, seed 7. Items are sorted by id, shuffled with
  the seed, and the first N are taken, so a smaller N is a prefix of a larger one.
- **GPQA format**: the four options are shuffled per question under the seed. The
  prompt is the multiple-choice template of
  [openai/simple-evals](https://github.com/openai/simple-evals) (`common.py`, MIT
  License): the model ends with `Answer: <letter>`. The letter is parsed from the last
  `Answer: X` in the response (Markdown emphasis allowed); this applies to members and
  to the synthesis alike. Scoring is exact letter match.
- **SimpleQA format**: the bare question, as in simple-evals. Each member answer and the
  synthesis answer are graded separately as correct / incorrect / not attempted by an
  LLM grader using the official SimpleQA grader prompt, verbatim (`GRADER_TEMPLATE` in
  `simpleqa_eval.py` of openai/simple-evals, commit `652c89d`, MIT License; SimpleQA by
  Wei et al., https://cdn.openai.com/papers/simpleqa.pdf). The grader sees no
  information about which system wrote the answer. "Error" for flag precision means
  "not correct" (incorrect or not attempted).
- **Runs**: everything goes through the product's own code
  (`compare_service.run_compare`, then `synthesis_service.run_agreement_step` and
  `run_finish_step` with peer review always on, review tier `economy`, no extra
  instructions). Members get the product's compare prompt (`prompts/compare/answer.v1.md`).
  Member output cap 16,000 tokens (the product default is 32,000; lowered so the
  worst-case estimate fits the budget cap). The synthesis label seed is fixed per
  question.
- **Harness**: `scripts/eval/run.py` (runs, resumable JSONL), `scripts/eval/report.py`
  (tables and JSON summary). Unit tests: `tests/unit/test_eval_harness.py`.

### Models and backends

| Role | Provider | Model | Backend |
|---|---|---|---|
| Member | Anthropic | claude-sonnet-5-5 | cli (Claude Code subscription) |
| Member | OpenAI | gpt-6.1-sol | cli (Codex subscription) |
| Member | xAI | grok-4.7 | cli (Grok subscription) |
| Member | Google | gemini-3.8-flash | api (billed) |
| Chair | Anthropic | claude-sonnet-5-5 | cli |
| Agreement judge | Anthropic | `fast` alias (claude-haiku-5-5) | cli |
| Reviewers (economy) | each member's provider | its `fast` alias where reachable on the member's backend, else the member model | as the member |
| SimpleQA grader | Anthropic | claude-sonnet-5-5 | cli |

Notes: xAI's `balanced` model (grok-4.3) is API-only and no xAI key was used, so the
xAI member is grok-4.7 through the Grok CLI. The grader is from the same vendor as one
member and the chair; this can bias grading toward that vendor's phrasing.

CLI versions are recorded in the run manifest (`manifest-*.json` in the output
directory). Full run CLI versions: **TBD**.

## Results

Run date: **TBD**. Sample: **TBD** GPQA, **TBD** SimpleQA questions scored.

### GPQA Diamond (accuracy)

| System | Accuracy |
|---|---:|
| claude-sonnet-5-5 (cli) | TBD |
| gpt-6.1-sol (cli) | TBD |
| grok-4.7 (cli) | TBD |
| gemini-3.8-flash (api) | TBD |
| Best single member | TBD |
| Majority vote | TBD |
| crossjury synthesis | TBD |

### SimpleQA (accuracy = share graded correct)

| System | Accuracy |
|---|---:|
| claude-sonnet-5-5 (cli) | TBD |
| gpt-6.1-sol (cli) | TBD |
| grok-4.7 (cli) | TBD |
| gemini-3.8-flash (api) | TBD |
| Best single member | TBD |
| Majority vote | TBD |
| crossjury synthesis | TBD |

### Flag precision (headline)

| Dataset | Coverage (% flagged) | Error rate, flagged | Error rate, unflagged | Errors caught |
|---|---:|---:|---:|---:|
| GPQA Diamond | TBD | TBD | TBD | TBD |
| SimpleQA | TBD | TBD | TBD | TBD |

### Cost and time

| | GPQA | SimpleQA |
|---|---:|---:|
| API spend (USD, Gemini only) | TBD | TBD |
| CLI (subscription) calls | TBD | TBD |
| Wall time per question (median) | TBD | TBD |

## Pilot (cost and time only)

Run on 2026-11-11 with N=3 per dataset (6 questions), the default configuration and
CLI versions Claude Code 2.1.317, codex-cli 0.167.0, grok 1.0.77. Six of six
questions completed: no member, synthesis or grader failures, no timeouts, and the
Claude CLI returned valid structured output for the judge and the chair every time.
Accuracy on 6 questions means nothing, so it is not reported here.

| | GPQA (3) | SimpleQA (3) |
|---|---:|---:|
| API spend per question (USD, Gemini member + reviewer) | 0.0062 | 0.0045 |
| CLI calls per question | 8 | 13 (incl. 5 grader calls) |
| Wall time per question (median / max) | 109 s / 675 s | 79 s / 363 s |

Total pilot spend: USD 0.032 (the pre-run estimate was USD 0.13 - 0.43; the estimate
is deliberately pessimistic). The slowest step is the grok-4.7 member through the Grok
CLI: 280 s and 573 s on two questions, with up to 25,000 output tokens. The per-call
timeout is 900 s, so a hard GPQA item can time out there; such a member counts as
failed, and `--retry-failures` reruns the question.

Projection for the full run (50 + 50, concurrency 2), from the pilot means: about
USD 0.55 of API spend (the pre-run high estimate is USD 7.11, below the USD 8 cap),
about 1,050 CLI calls, and roughly 3 to 5 hours of wall time, driven by the slow Grok
tail.

An early observation to check in the full run: on GPQA the agreement ratio was low
(0.14 - 0.5) even when all four members chose the same letter, because the agreement
judge compares reasoning segments, not just the final letter. The "agreement ratio
below 0.8" flag may therefore flag almost every GPQA question.

## Where crossjury does not help

**TBD after the full run.** This section will list, with counts: questions where the
synthesis was wrong but the majority or the best member was right; flag rules that do
not separate right from wrong answers; and the cost and latency overhead against a
single member.

## Limitations

- Small samples (50 per dataset): wide confidence intervals; no significance testing.
- "Best single member" is picked after seeing the results (optimistic baseline).
- SimpleQA majority vote is approximated from the grades (see the table above).
- One grader model, from the same vendor as a member and the chair.
- Subscription CLIs run with the vendor's own defaults (reasoning level, system
  prompt additions); their model versions can change without notice.
- Each question is run once; model sampling noise is not measured.
- Flags are judged at question level: a flag counts as "right" when the final answer
  is wrong, even if the flagged point is not the reason it is wrong.
- SimpleQA asks for short facts without web access; it measures recall and hedging,
  not reasoning. GPQA measures graduate-level science reasoning in multiple-choice form.
  Neither covers open-ended tasks, which is where crossjury is meant to be used.

## Reproduce

```bash
uv sync
uv run python scripts/eval/run.py                       # download + estimate only
uv run python scripts/eval/run.py --live --yes          # full run: 50 + 50, cap USD 8
uv run python scripts/eval/report.py --out-dir "<output dir printed by run.py>"
```

Run it from the repository root (keys load as `build_default_deps(Path.cwd())` loads
them; `--keys-dir DIR` loads key files from another directory too). The output
directory defaults to `<user data dir>/llm-crossjury/evals/m17-n<N>-seed<seed>/`.
A rerun skips questions already scored; `--retry-failures` reruns those where a member
or the synthesis failed. `--max-usd` (default 8) is a hard cap on priced spend: the run
refuses to start when the high estimate exceeds it and stops mid-run before any call
that could pass it. Concurrency defaults to 2 questions at a time.
