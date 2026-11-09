# ADR 0008: Sessions, chat continuation and layered memory

## Status

Accepted (2026-11-09). Partly supersedes ADR 0003's default limits and the "content is
not stored by default" rule of SPEC section 6 for the web UI's sessions.

## Context

The web UI lost questions and results when the user navigated away, and in-flight
runs were cancelled with the page. Users also want to keep talking to one model after
a jury round and to send later questions back to the jury. Long research
conversations need a context strategy that does not resend the full transcript to
every juror.

## Decision

1. The home page works on **sessions**: ordered chat turns and jury turns
   (`core/sessions/models.py`).
2. Sessions are **saved locally by default** (`<data dir>/sessions`, owner-only, never
   in the repository), like a chat web app's history. Users can delete one session,
   clear all, or turn saving off (`save_history: false`, memory only).
3. Run-type work runs as **server tasks detached from the request** and writes its
   result into the session, so closing or leaving the page loses nothing.
4. Every call in a session gets a **layered context** (stable prefix, summary and
   facts, FTS-retrieved snippets, recent turns verbatim) within a token budget. A jury
   question mid-session sends each juror the same compact case file, not the
   transcript.
5. Summaries are written by a **memory model**: user-chosen, else a `fast` model on a
   signed-in subscription CLI, else the cheapest keyed `fast` API model. Memory calls are
   budgeted and recorded like any other call.
6. Budget defaults become $5.00 per run, $15.00 per day, confirmation above $1.00, and
   are editable in Settings.

Details: `docs/design/sessions-chat-memory.md`.

## Consequences

* Prompt and response text is now on disk by default for web sessions. The data
  directory check (never inside a git working tree) applies, and Settings offers
  delete and opt-out.
* A server restart interrupts running work; such turns are marked as errors.
* Retrieval uses SQLite FTS5 (standard library); no vector database dependency.

## Alternatives

* **Browser storage only** (`sessionStorage`): fixes navigation but not cancelled
  runs, and cannot hold large results reliably. Rejected.
* **Vendor-side conversation state** (provider sessions, `--resume`): differs per
  vendor, is unavailable or unsafe for the CLI backend (ADR 0002), and cannot be shared
  across models. Rejected.
* **Full transcript every call**: simplest, but cost grows quadratically and multiplies
  by the number of jurors. Rejected.
