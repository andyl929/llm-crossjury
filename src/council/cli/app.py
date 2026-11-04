"""``council`` command skeleton. Every command body is a placeholder.

Output rules (AGENTS.md): never print API keys, tokens, or credential paths; cost
figures always show their currency and whether any part is unknown.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from council.core.modes import Mode
from council.core.providers.base import Backend

app = typer.Typer(
    name="council",
    help="Ask several LLMs, have them review each other anonymously, and synthesize.",
    no_args_is_help=True,
)
cost_app = typer.Typer(help="Estimate and report costs.", no_args_is_help=True)
providers_app = typer.Typer(help="Inspect configured providers.", no_args_is_help=True)
app.add_typer(cost_app, name="cost")
app.add_typer(providers_app, name="providers")

ConfigOpt = Annotated[
    Path | None,
    typer.Option(
        "--config", "-c", help="Config file (default: $COUNCIL_CONFIG or ./council.yaml)."
    ),
]
ModeOpt = Annotated[Mode, typer.Option("--mode", "-m", help="Run mode.")]
InputOpt = Annotated[
    Path | None,
    typer.Option("--input", "-i", help="Document for document-review (.md or .tex)."),
]
PresetOpt = Annotated[
    str | None, typer.Option("--preset", help="Document Review preset, e.g. 'paper'.")
]
BudgetOpt = Annotated[
    str | None,
    typer.Option("--budget", help="Override budget limit, as a decimal in the config currency."),
]


@app.command()
def run(
    task: Annotated[str | None, typer.Argument(help="Task text; use '-' to read stdin.")] = None,
    mode: ModeOpt = Mode.COUNCIL,
    input_file: InputOpt = None,
    preset: PresetOpt = None,
    config: ConfigOpt = None,
    budget: BudgetOpt = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the cost confirmation prompt.")
    ] = False,
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            help="At a checkpoint, save the run and exit instead of prompting.",
        ),
    ] = False,
) -> None:
    """Estimate, confirm, then run a task in the chosen mode."""
    raise NotImplementedError


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Id of a paused run.")],
    decision: Annotated[
        Path | None,
        typer.Option("--decision", "-d", help="JSON file with the checkpoint decision."),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Resume a run paused at a checkpoint (prompts for the decision if not given)."""
    raise NotImplementedError


@cost_app.command("estimate")
def cost_estimate(
    task: Annotated[str | None, typer.Argument(help="Task text; use '-' to read stdin.")] = None,
    mode: ModeOpt = Mode.COUNCIL,
    input_file: InputOpt = None,
    preset: PresetOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Show the estimated cost range of a run without making any model call."""
    raise NotImplementedError


@cost_app.command("report")
def cost_report(
    run_id: Annotated[str | None, typer.Argument(help="Run id; omit for recent runs.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of recent runs.")] = 10,
) -> None:
    """Show recorded spend from the ledger."""
    raise NotImplementedError


@providers_app.command("check")
def providers_check(
    backend: Annotated[
        Backend | None, typer.Option("--backend", "-b", help="Only check this backend.")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Check that configured providers are reachable. Spends no tokens; prints no secrets."""
    raise NotImplementedError


def main() -> None:
    """Console-script entry point."""
    app()
