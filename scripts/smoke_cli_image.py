"""Manual live check: native image input through the ``codex`` CLI (EXPERIMENTAL).

NOT collected by pytest and never run in CI. It generates a 32x32 solid-colour PNG,
attaches it to ONE tiny prompt ("What colour is this image? One word.") and runs it
through the Codex CLI you are signed in to (your own subscription), then prints the
text, the reported usage and the wall-clock duration. llm-crossjury never touches the
CLI's login: it only launches the executable (ADR 0002).

The model must be registered with ``cli_flags`` in the model registry
(``config/models.local.yaml`` or ``LLM_CROSSJURY_MODELS``).

Usage::

    uv run python scripts/smoke_cli_image.py --live --model <model-id>
    uv run python scripts/smoke_cli_image.py --live --model <model-id> --colour blue
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import struct
import sys
import time
import zlib
from pathlib import Path

from llm_crossjury.core.providers.base import (
    Attachment,
    AttachmentKind,
    CompletionRequest,
    Message,
    Prompt,
    ProviderError,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.providers.cli import CodexCliProvider
from llm_crossjury.core.registry import ModelRegistry, RegistryError, resolve_models_path

COLOURS: dict[str, tuple[int, int, int]] = {
    "red": (220, 20, 20),
    "green": (20, 180, 20),
    "blue": (20, 40, 220),
    "yellow": (240, 220, 20),
}


def solid_png(rgb: tuple[int, int, int], size: int = 32) -> bytes:
    """Return a ``size`` x ``size`` 8-bit RGB PNG filled with ``rgb``."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgb) * size
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


def _request(model: str, png: bytes) -> CompletionRequest:
    image = Attachment(
        kind=AttachmentKind.IMAGE,
        media_type="image/png",
        filename="swatch.png",
        sha256=hashlib.sha256(png).hexdigest(),
        data=png,
        fallback_text=None,
    )
    return CompletionRequest(
        model=model,
        prompt=Prompt(
            stable_prefix=(
                Message(role=Role.USER, content="Image attached.", attachments=(image,)),
            ),
            variable_suffix=(
                Message(role=Role.USER, content="What colour is this image? One word."),
            ),
        ),
        response_schema=None,
        temperature=None,
        max_output_tokens=None,
        reasoning=ReasoningEffort.DEFAULT,
        native_params={},
        cache_prefix=False,
        timeout_s=180.0,
        tags={},
    )


async def _run(args: argparse.Namespace) -> int:
    try:
        registry = ModelRegistry.load(resolve_models_path(Path.cwd()))
    except RegistryError as exc:
        print(f"registry error: {exc}", file=sys.stderr)
        return 2
    provider = CodexCliProvider(registry=registry)
    caps = provider.capabilities(args.model)
    print(f"input_modalities: {sorted(caps.input_modalities)}")
    if not args.yes:
        print(
            "This sends one tiny prompt with a 32x32 image through your own Codex "
            "subscription. Cost is unknown (counts toward your plan's usage limits)."
        )
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    start = time.monotonic()
    try:
        result = await provider.complete(_request(args.model, solid_png(COLOURS[args.colour])))
    except ProviderError as exc:
        print(f"error after {time.monotonic() - start:.1f}s: {exc}", file=sys.stderr)
        return 1
    print(f"sent colour: {args.colour}")
    print(f"usage: {result.usage.model_dump()}")
    print(f"duration: {result.latency_s:.2f}s" if result.latency_s else "duration: unknown")
    print(f"text: {result.text}")
    return 0


def main() -> None:
    """Parse arguments and run the image smoke check."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="Required: allow a real CLI call.")
    parser.add_argument("--model", required=True, help="Registry model_id (with cli_flags).")
    parser.add_argument("--colour", choices=sorted(COLOURS), default="red")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()
    if not args.live:
        parser.error("refusing to run without --live")
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
