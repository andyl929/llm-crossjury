"""Full-text index of session turns and large attachments (SQLite FTS5, ADR 0008).

The index lives next to the sessions (``<data dir>/sessions/index.sqlite3``) and holds
conversation text, so the file is created owner-only. It is a cache: everything in it
can be rebuilt from the sessions, so :meth:`MemoryIndex.sync_session` (re)indexes
whatever changed and every failure degrades to "no retrieval" instead of an error.

When the SQLite build has no FTS5, :attr:`MemoryIndex.available` is ``False`` and every
method is a no-op (searches return nothing).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from llm_crossjury.core.memory.turns import index_text
from llm_crossjury.core.sessions.models import ChatTurn, Session

logger = logging.getLogger(__name__)

CHUNK_CHARS = 1_500
"""Characters per indexed chunk."""

CHUNK_OVERLAP = 200
"""Characters shared by neighbouring chunks, so a passage split by a boundary is
still found."""

MAX_QUERY_TERMS = 48
"""At most this many terms of the user's message are used as a search query."""

_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
_CJK_CHAR = re.compile(f"[{_CJK}]")
_WORD = re.compile(r"\w+")
_RUN = re.compile(f"[{_CJK}]+|[^{_CJK}]+")
_MIN_WORD_CHARS = 2

SnippetSource = Literal["turn", "attachment"]


@dataclass(frozen=True)
class Snippet:
    """One search hit."""

    source: SnippetSource
    ref: str
    """Turn id or attachment id."""
    label: str
    """Human-readable origin, e.g. ``turn 3`` or the file name."""
    position: int
    """Turn index (turns) or chunk number (attachments)."""
    text: str


def _normalize(text: str) -> str:
    """Put spaces around CJK characters so the unicode61 tokenizer splits them."""
    return _CJK_CHAR.sub(lambda m: f" {m.group(0)} ", text)


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 ``MATCH`` expression (``""`` = no terms).

    Every term is a double-quoted string built only from word characters, so no FTS5
    operator, column filter or syntax error can come from user input. Latin words of
    two or more characters become terms; runs of CJK characters become two-character
    phrases (single characters when alone). Terms are OR-ed and ranked by bm25.
    """
    terms: dict[str, None] = {}
    for word in _WORD.findall(text.lower()):
        for run in _RUN.findall(word.replace("_", " ")):
            if _CJK_CHAR.match(run):
                pairs = [run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)]
                for pair in pairs:
                    terms.setdefault('"' + " ".join(pair) + '"', None)
            else:
                for part in run.split():
                    if len(part) >= _MIN_WORD_CHARS or part.isdigit():
                        terms.setdefault(f'"{part}"', None)
            if len(terms) >= MAX_QUERY_TERMS:
                break
    return " OR ".join(list(terms)[:MAX_QUERY_TERMS])


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split ``text`` into overlapping chunks, preferring paragraph or line breaks."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(
                text.rfind("\n", start + size // 2, end), text.rfind(" ", start + size // 2, end)
            )
            if cut > start:
                end = cut
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _turn_label(index: int, turn: object) -> str:
    kind = "chat" if isinstance(turn, ChatTurn) else "jury"
    return f"turn {index + 1} ({kind})"


class MemoryIndex:
    """FTS5 index of turns and attachment chunks, per session.

    Args:
        path: Database file (its parent is created owner-only). ``None`` keeps the
            index in memory (tests, or ``save_history: false``).

    Thread-safe: one connection guarded by a lock.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        try:
            self._conn = self._open(path)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("memory index unavailable (%s); retrieval is off", type(exc).__name__)
            self._conn = None

    @staticmethod
    def _open(path: Path | None) -> sqlite3.Connection:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path.exists():
                path.touch(mode=0o600)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
        conn = sqlite3.connect(
            ":memory:" if path is None else str(path), check_same_thread=False, timeout=10
        )
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS docs (
                    session_id TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    PRIMARY KEY (session_id, ref)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                    body,
                    text UNINDEXED,
                    session_id UNINDEXED,
                    ref UNINDEXED,
                    kind UNINDEXED,
                    label UNINDEXED,
                    position UNINDEXED,
                    tokenize = 'unicode61 remove_diacritics 2'
                );
                """
            )
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    @property
    def available(self) -> bool:
        """Whether FTS5 works (otherwise retrieval is skipped)."""
        return self._conn is not None

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection | None]:
        with self._lock:
            conn = self._conn
            if conn is None:
                yield None
                return
            try:
                with conn:
                    yield conn
            except sqlite3.Error as exc:
                logger.warning("memory index error (%s); ignored", type(exc).__name__)

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ writing

    @staticmethod
    def _replace(
        conn: sqlite3.Connection,
        *,
        session_id: str,
        ref: str,
        kind: SnippetSource,
        digest: str,
        rows: Sequence[tuple[str, str, int]],
    ) -> None:
        conn.execute("DELETE FROM chunks WHERE session_id = ? AND ref = ?", (session_id, ref))
        conn.executemany(
            "INSERT INTO chunks (body, text, session_id, ref, kind, label, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (_normalize(text), text, session_id, ref, kind, label, position)
                for text, label, position in rows
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO docs (session_id, ref, kind, digest) VALUES (?, ?, ?, ?)",
            (session_id, ref, kind, digest),
        )

    def sync_session(self, session: Session) -> None:
        """Index every complete turn of ``session`` that is new or changed, and drop
        turns that no longer exist. Cheap when nothing changed."""
        with self._tx() as conn:
            if conn is None:
                return
            known = dict(
                conn.execute(
                    "SELECT ref, digest FROM docs WHERE session_id = ? AND kind = 'turn'",
                    (session.id,),
                ).fetchall()
            )
            current: set[str] = set()
            for index, turn in enumerate(session.turns):
                text = index_text(turn)
                if text is None:
                    continue
                current.add(turn.id)
                digest = _digest(f"{index}\0{text}")
                if known.get(turn.id) == digest:
                    continue
                label = _turn_label(index, turn)
                rows = [(chunk, label, index) for chunk in chunk_text(text)]
                self._replace(
                    conn, session_id=session.id, ref=turn.id, kind="turn", digest=digest, rows=rows
                )
            for ref in set(known) - current:
                conn.execute(
                    "DELETE FROM chunks WHERE session_id = ? AND ref = ?", (session.id, ref)
                )
                conn.execute("DELETE FROM docs WHERE session_id = ? AND ref = ?", (session.id, ref))

    def index_attachment(
        self, session_id: str, attachment_id: str, filename: str, text: str
    ) -> None:
        """Index the text of an attachment too large for the stable prefix (once)."""
        with self._tx() as conn:
            if conn is None:
                return
            row = conn.execute(
                "SELECT digest FROM docs WHERE session_id = ? AND ref = ?",
                (session_id, attachment_id),
            ).fetchone()
            digest = _digest(text)
            if row is not None and row[0] == digest:
                return
            rows = [
                (chunk, f"{filename}, part {i + 1}", i) for i, chunk in enumerate(chunk_text(text))
            ]
            self._replace(
                conn,
                session_id=session_id,
                ref=attachment_id,
                kind="attachment",
                digest=digest,
                rows=rows,
            )

    def delete_session(self, session_id: str) -> None:
        """Remove everything indexed for ``session_id``."""
        with self._tx() as conn:
            if conn is None:
                return
            conn.execute("DELETE FROM chunks WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM docs WHERE session_id = ?", (session_id,))

    def delete_all(self) -> None:
        """Remove every session from the index."""
        with self._tx() as conn:
            if conn is None:
                return
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM docs")

    # ------------------------------------------------------------------ reading

    def search(
        self,
        session_id: str,
        query: str,
        k: int,
        *,
        exclude_refs: Sequence[str] = (),
    ) -> list[Snippet]:
        """The ``k`` best chunks of ``session_id`` for free-text ``query``.

        Args:
            session_id: Session to search.
            query: Any user text; sanitized with :func:`fts_query`.
            k: Maximum number of hits.
            exclude_refs: Turn or attachment ids to leave out (already in context).
        """
        match = fts_query(query)
        if not match or k <= 0:
            return []
        excluded = set(exclude_refs)
        hits: list[Snippet] = []
        rows: list[tuple[str, str, str, str, int]] = []
        with self._tx() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT text, ref, kind, label, position FROM chunks "
                "WHERE chunks MATCH ? AND session_id = ? ORDER BY rank LIMIT ?",
                (match, session_id, k + len(excluded) * 4 + 8),
            ).fetchall()
        for text, ref, kind, label, position in rows:
            if ref in excluded:
                continue
            hits.append(
                Snippet(
                    source="attachment" if kind == "attachment" else "turn",
                    ref=str(ref),
                    label=str(label),
                    position=int(position),
                    text=str(text),
                )
            )
            if len(hits) >= k:
                break
        return hits


_SHARED: dict[Path, MemoryIndex] = {}
_SHARED_LOCK = threading.Lock()


def open_index(path: Path | None) -> MemoryIndex:
    """The process-wide index for ``path`` (one connection per file).

    ``None`` returns a new in-memory index each time.
    """
    if path is None:
        return MemoryIndex(None)
    key = path.expanduser().resolve()
    with _SHARED_LOCK:
        index = _SHARED.get(key)
        if index is None:
            index = MemoryIndex(key)
            _SHARED[key] = index
        return index


__all__ = [
    "CHUNK_CHARS",
    "MemoryIndex",
    "Snippet",
    "chunk_text",
    "fts_query",
    "open_index",
]
