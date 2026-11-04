"""Manual live smoke check: one minimal call per configured provider.

NOT collected by pytest. Makes real, billed calls when implemented.

Usage (once implemented)::

    uv run python scripts/smoke_live.py --live --config council.yaml
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Required: allow real calls.")
    parser.add_argument("--config", default="council.yaml")
    args = parser.parse_args()
    if not args.live:
        parser.error("refusing to run without --live")
    raise NotImplementedError("live smoke check is implementation-phase work")


if __name__ == "__main__":
    main()
