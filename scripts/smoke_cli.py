"""Manual live smoke check for one ``cli`` backend provider (EXPERIMENTAL).

NOT collected by pytest and never run in CI. It runs ONE tiny prompt through the
vendor CLI you are signed in to (your own subscription), then prints the text, the
reported usage and the wall-clock duration. llm-crossjury never touches the CLI's
login: it only launches the executable (ADR 0002).

The model must be registered with ``cli_flags`` in the model registry
(``config/models.local.yaml`` or ``LLM_CROSSJURY_MODELS``).

Usage::

    uv run python scripts/smoke_cli.py --live --provider claude --model <model-id>
    uv run python scripts/smoke_cli.py --live --provider claude --model <model-id> --schema
    uv run python scripts/smoke_cli.py --live --provider claude --check-only
    uv run python scripts/smoke_cli.py --live --provider claude --model <model-id> \\
        --attach tiny.png --question "What colour is this image? One word."

``--attach`` sends one image or PDF the way a Compare run does (ADR 0007): natively
when the model's capabilities declare its kind, otherwise as extracted text.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from llm_crossjury.core.attachments import build_attachment
from llm_crossjury.core.attachments.prompt import with_attachments
from llm_crossjury.core.providers.base import (
    Attachment,
    CompletionRequest,
    Message,
    Prompt,
    ProviderError,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.providers.cli import (
    ClaudeCliProvider,
    CodexCliProvider,
    GeminiCliProvider,
    GrokCliProvider,
)
from llm_crossjury.core.providers.cli._subprocess import CliProviderBase
from llm_crossjury.core.registry import ModelRegistry, RegistryError, resolve_models_path

PROVIDERS: dict[str, type[CliProviderBase]] = {
    "claude": ClaudeCliProvider,
    "codex": CodexCliProvider,
    "gemini": GeminiCliProvider,
    "grok": GrokCliProvider,
}

DEFAULT_QUESTION = "Reply with the word: pong"

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _request(
    model: str,
    *,
    schema: bool,
    reasoning: ReasoningEffort,
    attachment: Attachment | None = None,
    question: str = DEFAULT_QUESTION,
) -> CompletionRequest:
    prompt = Prompt(
        stable_prefix=(Message(role=Role.SYSTEM, content="You answer in one short line."),),
        variable_suffix=(Message(role=Role.USER, content=question),),
    )
    if attachment is not None:
        prompt = with_attachments(prompt, [attachment])
    return CompletionRequest(
        model=model,
        prompt=prompt,
        response_schema=SCHEMA if schema else None,
        temperature=None,
        max_output_tokens=None,
        reasoning=reasoning,
        native_params={},
        cache_prefix=False,
        timeout_s=180.0,
        tags={},
    )


async def _run(args: argparse.Namespace) -> int:
    registry = None
    if not args.check_only:
        try:
            registry = ModelRegistry.load(resolve_models_path(Path.cwd()))
        except RegistryError as exc:
            print(f"registry error: {exc}", file=sys.stderr)
            return 2
    provider = PROVIDERS[args.provider](registry=registry)

    status = await provider.check()
    print(f"check: available={status.available} version={status.version} ({status.detail})")
    if args.check_only or not status.available:
        return 0 if status.available else 1

    attachment = None
    if args.attach:
        path = Path(args.attach)
        attachment = build_attachment(path.name, path.read_bytes()).attachment
        print(f"attachment: {attachment.filename} ({attachment.kind}, {len(attachment.data)} B)")
    request = _request(
        args.model,
        schema=args.schema,
        reasoning=ReasoningEffort(args.reasoning),
        attachment=attachment,
        question=args.question or DEFAULT_QUESTION,
    )
    caps = provider.capabilities(args.model)
    print(f"capabilities: {caps.model_dump()}")
    if not args.yes:
        print(
            "This sends one tiny prompt through your own CLI subscription. "
            "Cost is unknown (counts toward your plan's usage limits)."
        )
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    start = time.monotonic()
    try:
        result = await provider.complete(request)
    except ProviderError as exc:
        print(f"error after {time.monotonic() - start:.1f}s: {exc}", file=sys.stderr)
        return 1
    print(f"model: {result.model}")
    print(f"finish_reason: {result.finish_reason}")
    print(f"usage: {result.usage.model_dump()}")
    print(f"duration: {result.latency_s:.2f}s" if result.latency_s else "duration: unknown")
    print("text:")
    print(result.text)
    return 0


def main() -> None:
    """Parse arguments and run the smoke check."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="Required: allow a real CLI call.")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    parser.add_argument("--model", help="Registry model_id (with cli_flags).")
    parser.add_argument(
        "--reasoning", choices=[e.value for e in ReasoningEffort], default="default"
    )
    parser.add_argument("--schema", action="store_true", help="Request a tiny JSON schema.")
    parser.add_argument("--attach", help="Path of one image or PDF to attach.")
    parser.add_argument("--question", help="The user question (default: a fixed tiny prompt).")
    parser.add_argument("--check-only", action="store_true", help="Only run the version check.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()
    if not args.live:
        parser.error("refusing to run without --live")
    if not args.check_only and not args.model:
        parser.error("--model is required unless --check-only is given")
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
