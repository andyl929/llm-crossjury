# Contributing to llm-crossjury

Issues and pull requests are welcome. Before you start:

- Read [AGENTS.md](AGENTS.md). In short: no hard-coded model facts or prices (they live
  in [`config/models.yaml`](config/models.yaml) with a source link and a date), no
  credential handling, and tests for every change.
- Design documents and decision records are in [`docs/`](docs/). A change to a frozen
  contract goes through [`docs/contract-issues.md`](docs/contract-issues.md) first.

## Development setup

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

`uv run llm-crossjury ui --demo` starts the app with simulated models: no keys, no
network calls, no cost.

## Clean room

This is a clean-room implementation: no code from other "LLM council" projects was
viewed or used ([ADR 0005](docs/adr/0005-clean-room-and-license.md)). Please keep it
that way in your contributions.
