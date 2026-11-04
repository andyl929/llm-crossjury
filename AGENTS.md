# AGENTS.md: working rules for contributors (human and AI)

Read this file, `docs/SPEC.md` and `docs/ARCHITECTURE.md` before you change anything.

## 1. Hard rules

1. **Clean room.** Do not view, search, clone or copy code from karpathy/llm-council
   or any fork of it. Work only from this repository's docs (ADR 0005).
2. **No credentials.** No code may read, extract, copy, cache, log or store OAuth
   tokens, cookies, keychain entries, or CLI credential files (ADR 0002).
3. **Never print secrets.** No output (CLI, logs, exceptions, test output, reports,
   commit messages) may contain an API key or any part of one: no prefix, suffix or
   length. Report presence as "set"/"not set" only.
4. **No hard-coded prices.** Prices come only from `config/pricing.yaml`, and every
   entry needs `source_url` and `last_verified`. Never fill a price from memory.
5. **Contract files are frozen**: `src/council/core/schemas/**` and
   `src/council/core/providers/base.py`. Do not modify them. If a contract is wrong
   or insufficient, add an entry to `docs/contract-issues.md`, work around it outside
   the contract if you can, and report it under *Decisions needed*.
6. **No global installs** (pip/npm/brew/winget/choco/system packages, global uv
   tools) without the maintainer's explicit consent. Project dependencies go through
   `uv add` only.
7. **Never `git push`**, and never create remotes, tags or releases. Never rewrite
   published history.
8. **English only** for code, comments, docs, prompts and commit messages.

## 2. Toolchain

```bash
uv sync                              # create/update .venv from uv.lock
uv run pytest                        # all tests (network blocked)
uv run ruff check . && uv run ruff format --check .
uv run mypy                          # strict
uv run python -m council.core.schemas schemas   # regenerate JSON schemas
```

All four checks (pytest, ruff check, ruff format --check, mypy) must pass before
every commit.

## 3. Coding standards

- Python ≥ 3.11 (`.python-version` pins 3.12 for development). `from __future__ import annotations`.
- Full type hints. `mypy --strict` clean. No `Any` in public signatures unless the
  data really is untyped JSON.
- Pydantic v2 for data crossing a boundary. Data models are frozen and use
  `extra="forbid"`.
- Structured-output schemas follow ADR 0001 (all fields required, nullable scalars
  only, no unions/recursion/dicts). The schema test enforces this.
- Money is `Decimal`, never `float`. Every amount carries its currency.
- Async for I/O (`asyncio`). Never block the event loop.
- Subprocesses: an argument list, an absolute path from `shutil.which`, no
  `shell=True`, prompts on stdin (ADR 0002).
- Docstrings: Google style on every public symbol.
- Keep `core` free of CLI/UI concerns. `core` never imports `council.cli`.
- Run data goes to the platformdirs user data dir, never into the repository.

## 4. Commits

- **Conventional Commits**: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`,
  `build:`, `ci:`, with an optional scope (`feat(cost): ...`).
- **Small steps**: one logical change per commit. Every commit passes all checks.
- Do not commit `.env`, run data, or generated files other than `schemas/*.schema.json`.

## 5. Tests

- Every change ships with tests. Bug fixes start with a failing test.
- **No real network in tests.** pytest-socket blocks every host except loopback. Use
  `MockProvider` or recorded fixtures. Never use real keys: an autouse fixture
  removes them from the environment.
- Tests that need a vendor SDK must mock it at the adapter boundary.
- Live checks belong in `scripts/` (manual only, requiring `--live`), never in `tests/`.
- Warnings are errors.

## 6. End-of-task report

Finish every task with exactly this structure:

```
## Done
- <what was completed, with file paths>
- Checks: pytest <result>, ruff <result>, mypy <result>

## Deviations
- <anything done differently from the request or the docs, and why> (or "None")

## Decisions needed
- <questions for the maintainer, each with a recommended option> (or "None")
```
