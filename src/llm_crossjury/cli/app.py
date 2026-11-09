"""``llm-crossjury`` command skeleton. Every command body is a placeholder.

Output rules (AGENTS.md): never print API keys, tokens, or credential paths; cost
figures always show their currency and whether any part is unknown.
"""

from __future__ import annotations

import os
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from llm_crossjury.core.compare_service import build_default_deps
from llm_crossjury.core.keys import PROVIDER_KEY_ENVS
from llm_crossjury.core.modes import Mode
from llm_crossjury.core.providers.base import Backend
from llm_crossjury.web import LOOPBACK_HOSTS, create_app

app = typer.Typer(
    name="llm-crossjury",
    help="Ask several LLMs, have them review each other anonymously, and synthesize.",
    no_args_is_help=True,
)
cost_app = typer.Typer(help="Estimate and report costs.", no_args_is_help=True)
providers_app = typer.Typer(help="Inspect configured providers.", no_args_is_help=True)
models_app = typer.Typer(help="Inspect the model registry.", no_args_is_help=True)
app.add_typer(cost_app, name="cost")
app.add_typer(providers_app, name="providers")
app.add_typer(models_app, name="models")

ConfigOpt = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Config file (default: $LLM_CROSSJURY_CONFIG or ./llm-crossjury.yaml).",
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
StrictOpt = Annotated[
    bool,
    typer.Option(
        "--strict",
        help="Fail if a member's reasoning level is unsupported (default: downgrade + warn).",
    ),
]
BudgetOpt = Annotated[
    str | None,
    typer.Option("--budget", help="Override the per-run limit (decimal, config currency)."),
]


@app.command()
def run(
    task: Annotated[str | None, typer.Argument(help="Task text; use '-' to read stdin.")] = None,
    mode: ModeOpt = Mode.COUNCIL,
    input_file: InputOpt = None,
    preset: PresetOpt = None,
    config: ConfigOpt = None,
    budget: BudgetOpt = None,
    strict: StrictOpt = False,
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
    auto: Annotated[
        bool,
        typer.Option(
            "--auto",
            help="Decide checkpoints automatically (keep all, defer disputes to the chair). "
            "Every auto-decided item is flagged in the output.",
        ),
    ] = False,
    output_language: Annotated[
        str | None,
        typer.Option("--output-language", help="BCP 47 tag; default follows the input language."),
    ] = None,
) -> None:
    """Estimate, confirm, then run a task in the chosen mode.

    Always prints the effective budget (run limit, confirmation threshold, daily
    limit and today's spend), then each member's resolved model, backend and native
    reasoning parameters, then the estimate.
    """
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
    strict: StrictOpt = False,
) -> None:
    """Show resolved models and the estimated cost range without making any model call."""
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


@models_app.command("list")
def models_list(
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Only this provider.")
    ] = None,
) -> None:
    """List registry entries and aliases with backends, reasoning control, pricing
    status and last_verified dates. Reads config/models.yaml only; no network."""
    raise NotImplementedError


@models_app.command("refresh")
def models_refresh(
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Only this provider.")
    ] = None,
    draft: Annotated[
        Path | None,
        typer.Option(
            "--draft",
            help="Also write unverified draft entries (model_id only) for new models "
            "to this file. Never overwrites an existing file.",
        ),
    ] = None,
) -> None:
    """Compare the registry with each vendor's model-list endpoint and print the
    differences. Never edits config/models.yaml and spends no tokens."""
    raise NotImplementedError


_WILDCARD_HOSTS = ("0.0.0.0", "::")  # noqa: S104 - compared against, never bound here
_BROWSER_DELAY_S = 1.0


@app.command()
def ui(
    host: Annotated[
        str, typer.Option("--host", help="Interface to listen on. Keep the default (localhost).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8765,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open the browser.")
    ] = False,
    demo: Annotated[
        bool,
        typer.Option(
            "--demo",
            help="Simulated models and canned answers: no keys, no network calls, no cost. "
            "All state goes to a temporary directory that is deleted on exit.",
        ),
    ] = False,
) -> None:
    """Start the local compare web UI and open it in the browser.

    Loads API keys from ./.env (never printed) and the model registry from config/.
    With --demo, reads neither: see llm_crossjury.demo.
    """
    if host in _WILDCARD_HOSTS:
        allowed: tuple[str, ...] = ("*",)
    else:
        allowed = (*LOOPBACK_HOSTS, host)
    if host not in LOOPBACK_HOSTS:
        typer.echo(
            f"Warning: listening on {host}, not localhost. Anyone who can reach this "
            "address can spend your API budget.",
            err=True,
        )
    shown = "127.0.0.1" if host in _WILDCARD_HOSTS else host
    url = f"http://[{shown}]:{port}/" if ":" in shown else f"http://{shown}:{port}/"
    if demo:
        _serve_demo(host, port, allowed, url, open_browser=not no_browser)
        return
    deps = build_default_deps(Path.cwd())
    web_app = create_app(lambda: deps, allowed_hosts=allowed)
    _serve(web_app, host, port, url, open_browser=not no_browser)


def _serve(web_app: object, host: str, port: int, url: str, *, open_browser: bool) -> None:
    typer.echo(f"llm-crossjury UI: {url} (Ctrl+C to stop)")
    if open_browser:
        timer = threading.Timer(_BROWSER_DELAY_S, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    uvicorn.run(web_app, host=host, port=port, log_level="warning")  # type: ignore[arg-type]


def _serve_demo(
    host: str, port: int, allowed: tuple[str, ...], url: str, *, open_browser: bool
) -> None:
    from llm_crossjury.demo import create_demo_app  # noqa: PLC0415 - only for --demo

    # The demo process never needs an API key: drop any from its own environment so
    # nothing real can show up on the settings page. Values are never read.
    for env in PROVIDER_KEY_ENVS.values():
        os.environ.pop(env, None)
    with tempfile.TemporaryDirectory(
        prefix="llm-crossjury-demo-", ignore_cleanup_errors=True
    ) as tmp:
        web_app = create_demo_app(Path(tmp), allowed_hosts=allowed)
        typer.echo(
            "Demo mode: simulated models and canned answers. No API keys, no network "
            "calls, no cost."
        )
        _serve(web_app, host, port, url, open_browser=open_browser)


def main() -> None:
    """Console-script entry point."""
    app()
