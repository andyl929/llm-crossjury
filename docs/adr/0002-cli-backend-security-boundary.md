# ADR 0002: `cli` backend security boundary and skip conditions

- Status: Accepted (the backend itself is *experimental*)
- Date: 2026-11-04
- Amended: 2026-11-04 (Grok Build CLI no longer pre-skipped)
- Amended: 2026-11-08 (status per CLI after implementation; SIWC rejected)
- Amended: 2026-11-08 (native images/PDFs for Claude Code over stdin)

## Context

Many users pay for vendor subscriptions that come with an official CLI (`claude`,
`codex`, `gemini`, and xAI's Grok Build CLI `grok`). Using those CLIs as members saves
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
   unknown, so a `cli` member is only allowed with an explicit
   `allow_unknown_cost: true`. The ledger still records the number of CLI calls and
   their wall-clock duration (SPEC §5).
10. **The CLI's own login only.** Some CLIs also accept an API key from the
   environment (the Grok Build CLI accepts `XAI_API_KEY`). Because `*_API_KEY` is never
   forwarded (rule 4), the `cli` backend always runs on the user's own CLI login. API
   keys are used only by the `api` backend.

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

*Amended 2026-11-08* after implementation against the vendors' current docs (sources
are cited in each adapter's docstring).

| CLI | Status | Notes |
|---|---|---|
| `claude` | **Supported (experimental)** | `claude -p --output-format json --safe-mode`, tools off (`--tools ""`, MCP denied, `--strict-mcp-config`, `dontAsk`), prompt on stdin, system prompt from a temp file. Anthropic permits "an end user … signing in to the unmodified Claude Code binary with their own Claude subscription" (code.claude.com/docs/en/legal-and-compliance). **Risk:** the headless docs say `--bare`, which does not read the OAuth login, "will become the default for `-p` in a future release"; re-check on every release |
| `codex` | **Supported (experimental)** | `codex exec - --json`, read-only sandbox, `approval_policy=never`, shell/web/apps/plugins/hooks/memories off, user config ignored, any tool event rejected. OpenAI documents that `codex exec` reuses the saved CLI login |
| `grok` | **Experimental (terms unverified)** | Grok Build headless mode with `--prompt-file` (no stdin; S4 met via a temp file), `--verbatim`, tool allow/deny lists, `--sandbox strict` (not enforced on Windows), `GROK_MEMORY=0`. xAI's legal pages could not be retrieved automatically; the maintainer must read them before release. Labelled experimental in the UI, and usable only once registry entries with verified `cli_flags` exist (none are configured yet) |
| `gemini` | **Skipped (S6)** | Gemini CLI stopped serving Google AI Pro/Ultra and free accounts on 2026-06-18 (google-gemini/gemini-cli discussion 27274); the replacement, Antigravity CLI, states that wrapping it in third-party applications "is not supported under consumer plans" (antigravity.google/docs/faq). Gemini users use the `api` backend |
| `agy` | **Skipped (S6)** | As above |

**Rejected alternative:** OpenAI "Sign in with ChatGPT" for third-party local apps is
OpenAI's sanctioned program for this use case, but it requires the host application to
run OAuth and store and refresh tokens, which violates "What llm-crossjury never does".

**Attachments:** `cli` members receive attachments as extracted text, except the
kinds their adapter declares natively (ADR 0007). *Amended 2026-11-08:* Claude
Code receives images and PDFs as base64 `image`/`document` blocks in one
stream-json user message on **stdin** (`--input-format stream-json
--output-format stream-json --verbose`). The data never enters argv, no file
path is written and no tool is needed, so every rule above still holds; a stream
that shows a tool call other than `StructuredOutput` is rejected.

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
