# llm-crossjury

> Status: **design / skeleton phase.** Nothing runs against real models yet.

`llm-crossjury` sends one task to several LLMs (Claude, OpenAI, Gemini, Grok). Each
one answers on its own. Then they review each other's answers without knowing who
wrote which. A *chair* model combines the results, and any point where the models
still **disagree** comes back to you to decide.

It is built for general professional work. It ships with a **Document Review**
mode that returns a list of proposed edits instead of a rewritten document.
Academic paper revision (in any language) is one preset of that mode.

## Modes

| Mode | What you get |
|---|---|
| `compare` | Independent answers side by side, with a cost breakdown |
| `council` | Answers → you see how much they agree, prune weak ones, and may skip review → anonymous peer review (issues and rankings) → chair synthesis → you settle the disputes → final answer |
| `document-review` | Same flow, applied to a Markdown or LaTeX file. The output is an **edit list**: location, original, replacement, reason, category |

```bash
llm-crossjury run "Should we shard by tenant or by region?" --mode council
llm-crossjury run --mode document-review --input paper.tex --preset paper
llm-crossjury cost estimate --mode document-review --input paper.tex
```

## Backends

- **api**: the official SDKs with your own API keys, read from `.env`.
- **cli** (*experimental*): runs the official CLIs you are already signed in to
  (`claude`, `codex`, `gemini`, `grok`) as subprocesses. llm-crossjury never reads
  their credentials. See [ADR 0002](docs/adr/0002-cli-backend-security-boundary.md).

## Cost is a first-class concern

Every run is estimated before it starts and recorded in a ledger while it runs.
The built-in limits are $1.00 per run and $5.00 per day, and anything over $0.25
needs your confirmation. These limits are printed at the start of every run. Model
facts and prices come **only** from the model registry
[`config/models.yaml`](config/models.yaml), where every entry carries a `source_url`
and a `last_verified` date. If an `api` model has no verified price, the run is
refused. Members can name models by alias (`fast` / `balanced` / `best`), and
`llm-crossjury cost estimate` shows exactly which model and reasoning parameters
will be used.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run llm-crossjury --help
```

Read [AGENTS.md](AGENTS.md) before contributing. Design documents live in [docs/](docs/).

## Provenance

This is a clean-room implementation. The general idea of an "LLM council" was
inspired by public discussion of such tools. No code from any existing
implementation was viewed or used. See [ADR 0005](docs/adr/0005-clean-room-and-license.md).

## License

MIT
