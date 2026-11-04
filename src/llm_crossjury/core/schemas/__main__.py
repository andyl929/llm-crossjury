"""``python -m llm_crossjury.core.schemas [DIR]``: regenerate the committed JSON schemas."""

import sys
from pathlib import Path

from llm_crossjury.core.schemas.export import export_all

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("schemas")
for written in export_all(target):
    print(written)
