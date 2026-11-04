"""``python -m llm_crossjury.core.schemas [DIR]``: regenerate the committed JSON schemas.

Writes the structured-output schemas to ``DIR`` and the configuration-file schemas
(model registry, run config) to ``DIR/config``.
"""

import sys
from pathlib import Path

from llm_crossjury.core.config_export import export_config_schemas
from llm_crossjury.core.schemas.export import export_all

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("schemas")
for written in [*export_all(target), *export_config_schemas(target / "config")]:
    print(written)
