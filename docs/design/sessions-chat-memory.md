# Sessions, chat and memory (design for M1.5)

Status: approved by the maintainer 2026-11-09. Decision record: ADR 0008.
Shared contract (code): `src/llm_crossjury/core/sessions/models.py` and
`src/llm_crossjury/core/sessions/interfaces.py`.

## Problems this solves

1. The home page keeps everything in browser memory. Opening Settings and coming
   back loses the question and the results; closing the page mid-run also throws away
   paid work, because the request is cancelled with the page.
2. After step 1 or step 2 a user wants to keep talking: with the model whose answer
   they liked, or with the chair about the synthesis. Later in that conversation they
   want to hand one question back to the jury, and then keep chatting. Chat and jury
   form a loop.
3. Long research and development conversations must not resend everything on every
   call, and must not multiply that cost by the number of jurors.

## Concepts

* **Session**: one conversation, shown as one thread on the home page and one row in
  the history sidebar. An ordered list of turns.
* **Jury turn**: one crossjury round (step 1 answers, optional step 2 stages). The
  same UI as today, rendered as a card inside the thread.
* **Chat turn**: one user message answered by one model, rendered as message bubbles.
* **Continuation**: a chat turn started from a "Continue" button on a jury turn. It
  records which member answer (or the synthesis) it continues.

## Persistence and detached work

* Sessions are saved under `<data dir>/sessions/<id>.json` (owner-only, atomic
  writes), never inside the repository. Setting `save_history: false` keeps them in
  memory only (lost when the server stops).
* Every run-type request carries `?session=<id>&turn=<turn id>`. The browser generates
  the turn id (32 lowercase hex). The server writes the pending turn first, runs the
  work as a task that survives the request (`TaskRunnerLike`), and writes the result or
  the error into the turn when it finishes, whether or not the page is still open.
* The page restores the last session on load (`#s=<id>` in the URL, else the last
  session id in `localStorage`) and polls `GET /api/sessions/{id}` every 2 s while any
  turn is pending.
* When the server starts, any turn still `pending` on disk becomes `error`
  ("Interrupted: the server stopped before this finished.").
* Attachments referenced by a stored session are kept past the 24 h upload TTL.

## HTTP API

Errors keep the existing JSON error shape and codes (`consent_required`,
`confirmation_required`, budget refusal, ...). Pre-checks (consent, confirmation,
budget) run before a turn is written: a 409 leaves the session unchanged.

### Sessions

| Method and path | Body | Returns |
|---|---|---|
| `GET /api/sessions` | | `{"sessions": [SessionSummary]}`, newest `updated_at` first |
| `POST /api/sessions` | `{"title"?: str}` | `Session` |
| `GET /api/sessions/{id}` | | `Session` (404 `session_not_found`) |
| `POST /api/sessions/{id}/title` | `{"title": str}` | `SessionSummary` |
| `POST /api/sessions/{id}/turns/{turn}/synthesis-settings` | `{"settings": object}` | `{"ok": true}` |
| `DELETE /api/sessions/{id}` | | `{"deleted": bool}` |
| `DELETE /api/sessions` | | `{"deleted": int}` |

### Session hooks on the existing endpoints

* `POST /api/estimate?session=S` and `POST /api/run?session=S&turn=T`: when session S
  already has turns, the server asks the `CaseFileBuilder` for a case file and the
  members receive
  `"<conversation_context>\n{case_file}\n</conversation_context>\n\n{task}"` instead of
  `task` (the estimate uses the same text). `run` appends `JuryTurn(id=T, task=task,
  case_file=..., selections, attachment_ids)` as pending, then stores `step1` and
  `status` when done. Without query parameters both endpoints behave exactly as before.
* `POST /api/synthesis/{agreement,finish,resynthesize}?session=S&turn=T`: while running,
  `turn.synthesis["pending"] = "<stage>"`; on success `turn.synthesis["<stage>"] =` the
  JSON result; on failure `turn.synthesis["<stage>_error"] = "<message>"`; `pending`
  is removed in both cases. Starting a stage clears `synthesis_stale`. A stage is
  refused (409 `turn_busy`) while a member of the turn is being run again.

### Progressive step 1 and per-member retry (CI-7)

* While `run` is in flight, each member is stored in `turn.members_done[index]` as soon
  as it finishes (index into `selections`). The page that started the run polls the
  session every second and takes over only `members_done`; a reloaded page uses the
  normal 2 s polling. `step1` is still written once, when every member is done.
* `POST /api/run/member?session=S&turn=T` with `{"index", "allow_unknown_cost",
  "confirmed"}` runs `selections[index]` again with the same task, case file and
  attachments. Pre-checks as for `run` (consent, budget refusal, confirmation); then
  `index` is added to `retrying`, the call runs detached, the new member replaces
  `step1.members[index]` and `members_done[index]`, the step-1 totals are recomputed,
  `index` leaves `retrying`, and `synthesis_stale` is set if step 2 has any result. A
  failed call keeps the old member. Returns `{"index", "member", "turn"}`. Errors: 400
  `invalid_member_index`, 404 for an unknown session or turn, 409 `turn_busy` (step 1
  pending, the member already re-running, or a step-2 stage running), 409 `no_answers`
  (step 1 failed as a whole).
* `POST /api/run/member/estimate?session=S&turn=T` with `{"index"}` returns the usual
  estimate response for that one call.

### Chat

`ChatRequest`:

```json
{
  "message": "str",
  "model": {"provider": "gemini", "backend": "api", "model": "gemini-4-argon"},
  "attachment_ids": [],
  "max_output_tokens": null,
  "continued_from": {"turn_id": "...", "source": "member", "member_index": 2},
  "allow_unknown_cost": false,
  "confirmed": false
}
```

| Method and path | Returns |
|---|---|
| `POST /api/chat/estimate?session=S` | `ChatEstimate`: cost range, `budget` (`BudgetStatus`), `context_tokens`, `context` breakdown (`prefix`, `summary`, `retrieved`, `recent`, `message` tokens), `context_budget_tokens`, `cli_calls` |
| `POST /api/chat?session=S&turn=T` | the finished `ChatTurn` (also stored in the session) |
| `GET /api/sessions/{id}/memory` | `MemoryState` |

### App settings

| Method and path | Body | Returns |
|---|---|---|
| `GET /api/settings/app` | | `{"settings": AppSettings, "defaults": AppSettings}` |
| `PUT /api/settings/app` | `AppSettings` | `{"settings": AppSettings}` |

Settings live in `<user config dir>/llm-crossjury/settings.json`. The budget in
`AppSettings` replaces `BudgetConfig`'s limits for every estimate, run and budget line.
New built-in defaults (general purpose, sized for the 32k output default): $5.00 per
run, $15.00 per day, confirmation above $1.00.

## Memory layer (`core.memory`)

Modelled on claude-mem's two ideas: compress raw history into structured notes, and
disclose progressively (a short index first, full text only on demand). Every model
call in a session gets a context assembled in four layers, within
`context_budget_tokens` (and within the model's own context window minus its output
cap):

| Layer | Content | Notes |
|---|---|---|
| 0 Stable prefix | system prompt, attachments of the session | identical across turns, so vendor prompt caching applies (`Prompt.stable_prefix`) |
| 1 Summary | `MemoryState.summary` + `facts` | covers turns `< summarized_through` |
| 2 Retrieved | top-k snippets from older turns and attachment chunks, SQLite FTS5 (`<data dir>/sessions/index.sqlite3`), queried with the new message | only when layer 3 does not already cover them |
| 3 Recent | the last turns verbatim, newest kept first | at least the last 2 exchanges |

**Turns as messages.** A chat turn is a user message plus an assistant reply. A jury
turn becomes, for the target model: the question as a user message, then (a) the
model's own step-1 answer as the assistant message if it was a member, else (b) the
synthesis final text if present, else (c) a compact digest of the member answers,
labelled by model. The continuation that started a chat is always in layer 3.

**Summaries.** After a turn finishes, when the verbatim history outside layer 3
exceeds about 25% of the context budget, the memory model folds the oldest unsummarized
turns into `summary` and extracts `facts` (decisions, constraints, conclusions, open
questions). This runs in the background and never blocks a reply. Memory model:
`AppSettings.memory_model`, else automatic: a `fast` alias on a signed-in subscription
CLI (no extra cost), else the cheapest priced `fast` model with a key. Memory calls go
through the ledger under stage `memory`.

**Case file** (jury question mid-session): summary + facts + top retrieved snippets +
the last exchange, capped at 8,000 tokens. Every juror receives the same case file, so
the context cost does not multiply by the full transcript length.

**Large attachments.** An attachment that fits the budget stays whole in layer 0 (cached).
One that does not is chunked into the FTS index and served through layer 2.

## UI

* Left sidebar: new conversation, history list (title, time, pending dot), rename,
  delete. Collapsible; a drawer on narrow screens.
* Thread: jury turns as cards (today's step 1 and step 2 UI), chat turns as bubbles
  (user right, model left with a model badge, Markdown, copy).
* "Continue with <model>" on every step-1 member card and "Continue with chair" on a
  synthesis result: switches the composer to chat with that model and records the
  continuation.
* Composer at the bottom: message box, attach, paste images, model picker and two send
  actions: Send (chat with the picked model) and Ask the jury (the jury panel, with
  today's model selection). An empty session starts in jury mode.
* A context meter under the composer (from `/api/chat/estimate`).
* Settings page gains Budget, History (save toggle, clear all) and Memory (memory model,
  context budget) sections.

Streaming replies are out of scope (needs a provider contract change); a typing
indicator shows instead.
