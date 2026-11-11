"""Manual live check: smart page images through Compare and a ``cli`` member.

NOT collected by pytest and never run in CI. It builds a two-page PDF whose key fact
(a project codename) appears ONLY inside a raster image on page 2; page 1 holds
ordinary text. The PDF goes through ``build_attachment`` (page selection and
rendering in the isolated workers) and ``run_compare`` with ONE member on the
``cli`` backend (default: the Codex CLI, which reads images but not PDFs), then the
script prints the delivery receipt and the answer. One call through your own
subscription; llm-crossjury never touches the CLI's login (ADR 0002). No API key is
loaded: the run uses only the selected CLI.

Usage::

    uv run python scripts/smoke_page_images.py --live --model gpt-6-luna
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import tempfile
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from llm_crossjury.core.attachments import build_attachment
from llm_crossjury.core.attachments.store import AttachmentStore
from llm_crossjury.core.compare_service import (
    CompareDeps,
    Selection,
    _cli_providers,
    run_compare,
)
from llm_crossjury.core.config import BudgetConfig, CostConfig
from llm_crossjury.core.cost.estimate import CostEstimator
from llm_crossjury.core.cost.ledger import SqliteLedger
from llm_crossjury.core.cost.pricing import PricingTable
from llm_crossjury.core.providers.base import Backend
from llm_crossjury.core.registry.resolver import (
    ModelRegistry,
    ModelResolver,
    ResolutionMode,
    resolve_models_path,
)

CODENAME = "PELICAN-42"
TASK = (
    "According to the attached document, what is the project codename? "
    "Answer with the codename only."
)


def _codename_image() -> tuple[int, int, bytes]:
    """An RGB raster image that shows the codename (no text layer)."""
    image = Image.new("RGB", (900, 300), (245, 245, 235))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=64)
    draw.text((40, 60), "Project codename:", fill=(20, 20, 120), font=font)
    draw.text((40, 160), CODENAME, fill=(160, 20, 20), font=font)
    return image.width, image.height, image.tobytes()


def make_pdf() -> bytes:
    """Page 1: ordinary text; page 2: a caption and the codename as a raster image."""
    width, height, raw = _codename_image()
    pixels = zlib.compress(raw)
    page1 = b"BT /F1 12 Tf 72 720 Td (Quarterly project report. The project is on schedule.) Tj ET"
    page2 = (
        b"BT /F1 12 Tf 72 720 Td (Figure 1: the badge of the project, shown below.) Tj ET\n"
        b"q 468 0 0 156 72 480 cm /Im1 Do Q"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R 6 0 R] /Count 2 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(page1) + page1 + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> /XObject << /Im1 8 0 R >> >> "
        b"/Contents 7 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(page2) + page2 + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n"
        % (width, height, len(pixels))
        + pixels
        + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def _deps(work: Path) -> CompareDeps:
    registry = ModelRegistry.load(resolve_models_path(Path.cwd()))
    pricing = PricingTable(registry)
    budget = BudgetConfig()
    return CompareDeps(
        providers={(p.name, Backend.CLI): p for p in _cli_providers(registry)},
        key_envs={},
        registry=registry,
        resolver=ModelResolver(registry, ResolutionMode.NORMAL),
        pricing=pricing,
        estimator=CostEstimator(
            pricing,
            cache_hit_rate=0.0,
            reasoning_multipliers=dict(CostConfig().reasoning_multipliers),
            currency=budget.currency,
        ),
        ledger=SqliteLedger(work / "ledger.sqlite"),
        budget=budget,
        attachment_store=AttachmentStore(work / "attachments"),
    )


async def _run(args: argparse.Namespace) -> int:
    built = build_attachment("report.pdf", make_pdf())
    print(f"extracted text: {built.attachment.fallback_text!r}")
    print(f"derived images: {[(m.filename, m.reason, m.size) for m in built.meta.derived]}")
    if CODENAME in (built.attachment.fallback_text or ""):
        print("the codename leaked into the text layer; the check would prove nothing")
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps(Path(tmp))
        assert deps.attachment_store is not None
        attachment_id = deps.attachment_store.put(built)
        selection = Selection(provider=args.provider, backend=Backend.CLI, model=args.model)
        result = await run_compare(
            TASK, [selection], deps, attachment_ids=[attachment_id], allow_unknown_cost=True
        )
    member = result.members[0]
    print(f"receipt: {[(r.filename, r.mode, r.images) for r in member.attachments]}")
    print(f"error: {member.error}")
    print(f"answer: {member.text}")
    print(f"uses the image: {CODENAME in (member.text or '')}")
    return 0 if CODENAME in (member.text or "") else 1


def main() -> None:
    """Parse arguments and run the page-image smoke check."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="Required: allow a real CLI call.")
    parser.add_argument("--provider", default="openai", help="CLI provider id (default openai).")
    parser.add_argument("--model", required=True, help="Registry model_id (with cli_flags).")
    args = parser.parse_args()
    if not args.live:
        parser.error("refusing to run without --live")
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
