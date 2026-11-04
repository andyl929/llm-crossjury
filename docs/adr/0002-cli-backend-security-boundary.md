# ADR 0002: `cli` backend security boundary and skip conditions

- Status: Accepted (the backend itself is *experimental*)
- Date: 2026-11-04

## Context

Many users pay for vendor subscriptions that come with an official CLI (`claude`,
`codex`, `gemini`, and possibly `grok`). Using those CLIs as members saves
API spend. These CLIs are agentic tools, though. They can read files, run commands
and edit code, and they keep OAuth tokens or cookies in the user's profile. llm-crossjury
must get model output from them **without** becoming a credential-handling tool and
**without** giving a model agentic access to the user's machine.

## Decision

### What llm-crossjury does

1. **Subprocess only.** llm-crossjury launches the vendor's own executable and reads its
   stdout. It never talks to the vendor's backend directly with the CLI's identity.
2. **Resolve the executable with `shutil.which`** and call `subprocess` with the
   absolute path and an **argument list**. `shell=True` is never used.
   - **Windows:** CLIs installed via npm are often `.cmd`/`.bat` shims. `which`
     resolves them through `PATHEXT`, so the call goes to a concrete file. Windows
     runs batch files through `cmd.exe`, which re-parses arguments, and argument
     escaping for batch files is a known injection risk. So the argv contains **only
     fixed, llm-crossjury-controlled flags**. The prompt and every other user- or
     model-derived string go over **stdin** (or a temp file llm-crossjury writes, if the
     CLI cannot read stdin). If a CLI can only take the prompt in argv, it is skipped
     on Windows.
3. **Prompt on stdin, never in argv.** This also keeps prompts out of process
   listings and shell history.
4. **Minimal environment.** The child gets only an allowlist (`PATH`, home/profile
   directories, `SYSTEMROOT`, temp, locale). The values are passed through unread so
   the CLI can find *its own* login. `*_API_KEY` variables are never forwarded.
5. **Empty working directory.** Each call runs in a fresh temp directory. The
   document under review is sent in the prompt, not as a file path.
6. **Tools and writes disabled.** The CLI runs in its non-interactive mode, with
   tool use, file writes, command execution and network tools disabled through the
   CLI's documented flags or settings, and in a read-only sandbox mode where one is
   offered.
7. **Bounded execution.** There is a hard timeout that kills the process tree, and a
   stdout byte cap. Exceeding the cap is an error; output is never truncated.
8. **Redaction.** Stderr and error messages pass through the redactor before they
   are shown or logged.
9. **Usage reported honestly.** Fields the CLI does not report are `None`. Cost is
   unknown unless the user opts in (SPEC §5).

### What llm-crossjury never does

- It never reads, parses, copies, caches, logs or stores OAuth tokens, refresh tokens,
  cookies, keychain entries, or any file in a CLI's config or credential directory.
- It never checks login state by inspecting files. `providers check` may only run a
  vendor-documented status or version subcommand that spends no tokens.
- It never runs an interactive login on the user's behalf, and never automates a
  browser login.
- It never sends a CLI's identity to any process other than that CLI.

### Skip conditions

A CLI is **not supported in v1** if any of the following holds. The reason is then
recorded in its adapter's `skipped_reason` and in this ADR's table:

| # | Condition |
|---|---|
| S1 | It is not an official CLI published by the model vendor |
| S2 | It cannot run non-interactively with output on stdout |
| S3 | Tool use, file writes or command execution cannot be reliably disabled |
| S4 | The prompt cannot be passed without argv (stdin or file), for the Windows rule above |
| S5 | It requires llm-crossjury to read or handle credentials in order to work |
| S6 | The vendor's terms prohibit this kind of automated use |
| S7 | Its output cannot be told apart from interactive or decorative output reliably enough to parse |

### Status per CLI (v1)

| CLI | Status | Notes |
|---|---|---|
| `claude` | Planned | Each condition to be verified against current official docs during implementation |
| `codex` | Planned | Same |
| `gemini` | Planned | Same |
| `grok` | **Skipped (pending S1)** | It has not been verified that an official xAI CLI exists and meets S2–S7. Grok stays available through the `api` backend. Revisit when verified (SPEC Open Questions) |

The statuses above are design intent. Each "Planned" entry must be re-checked
against the vendor's current documentation before its adapter is implemented, and
moved to "Skipped" with a reason if any condition fails.

## Consequences

- The `cli` backend gives weaker guarantees on cost and usage. It is labelled
  experimental in the CLI and the docs.
- Some CLIs may end up skipped on Windows only (S4).
- llm-crossjury's attack surface stays limited to launching a known executable with fixed flags.

## Alternatives considered

- **Reusing CLI tokens with direct HTTP calls.** Rejected: this violates the
  no-credential rule and most likely the vendors' terms.
- **`shell=True` for `.cmd` shims.** Rejected: it opens shell injection.
- **Passing prompts as argv with escaping.** Rejected on Windows, because batch-file
  argument parsing cannot be escaped reliably.
