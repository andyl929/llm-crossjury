"""Turn an eval's ``results.jsonl`` into a Markdown table and a JSON summary.

Offline: reads the results only. Writes ``report.md`` and ``summary.json`` next to
the results and prints the Markdown. Neither contains question or answer text.

Usage::

    uv run python scripts/eval/report.py --out-dir <eval output dir> [--project-n 50]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from crossjury_eval.runner import read_records
from crossjury_eval.summary import summarize, to_markdown


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True, help="Eval output directory.")
    parser.add_argument(
        "--project-n", type=int, default=None, help="Project cost/time to N per dataset."
    )
    parser.add_argument("--concurrency", type=int, default=2, help="For the time projection.")
    args = parser.parse_args(argv)
    records = read_records(args.out_dir / "results.jsonl")
    if not records:
        print(f"no results in {args.out_dir}", file=sys.stderr)
        return 1
    summary = summarize(records, project_n=args.project_n, concurrency=args.concurrency)
    markdown = to_markdown(summary)
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (args.out_dir / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
