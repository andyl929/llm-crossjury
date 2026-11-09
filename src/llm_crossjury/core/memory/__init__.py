"""Session memory (ADR 0008): layered context, search index, summaries, case files.

* :mod:`.context`: the four-layer prompt for one call within a token budget.
* :mod:`.index`: SQLite FTS5 index of turns and large attachments.
* :mod:`.summarizer`: rolling summary and pinned facts, written by the memory model.
* :mod:`.picker`: which model writes the summaries.
* :mod:`.case_file`: the context a jury question mid-session sends to every juror.
* :mod:`.turns`: how stored turns are shown to a model.
"""

from __future__ import annotations

from llm_crossjury.core.memory.case_file import (
    CASE_FILE_MAX_TOKENS,
    build_case_file_sync,
    make_case_file_builder,
)
from llm_crossjury.core.memory.context import (
    BuiltContext,
    ContextBreakdown,
    ContextOverflowError,
    ContextTarget,
    build_context,
)
from llm_crossjury.core.memory.index import MemoryIndex, Snippet, open_index
from llm_crossjury.core.memory.picker import MemoryModel, pick_memory_model
from llm_crossjury.core.memory.summarizer import MEMORY_STAGE, update_memory

__all__ = [
    "CASE_FILE_MAX_TOKENS",
    "MEMORY_STAGE",
    "BuiltContext",
    "ContextBreakdown",
    "ContextOverflowError",
    "ContextTarget",
    "MemoryIndex",
    "MemoryModel",
    "Snippet",
    "build_case_file_sync",
    "build_context",
    "make_case_file_builder",
    "open_index",
    "pick_memory_model",
    "update_memory",
]
