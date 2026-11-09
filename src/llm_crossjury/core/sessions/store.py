"""Local store for web UI sessions (ADR 0008).

Layout under the data directory, never inside the repository::

    <data dir>/sessions/<id>.json    one session (owner-only, written atomically)

The files hold prompt and response text, so the directory is created ``0o700`` and the
files ``0o600`` where the platform supports POSIX permissions (on Windows the user
profile's ACLs apply). With ``persist=False`` (``AppSettings.save_history`` off) new
and changed sessions live in memory only and are lost when the server stops; files
written earlier stay on disk (and stay readable) until the user deletes them.

When the store is created (server start-up), any turn still marked ``pending`` on disk
becomes an error, since the task that would have finished it died with the old
process. A damaged file is skipped in the history list and reported as not found.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from llm_crossjury.core.sessions.interfaces import SessionNotFoundError
from llm_crossjury.core.sessions.models import (
    JuryTurn,
    Session,
    SessionSummary,
    Turn,
    TurnStatus,
    summarize,
)

SESSION_ID_PATTERN = r"^[0-9a-f]{32}$"
"""A session or turn id: 32 lowercase hex digits (a uuid4 hex)."""

INTERRUPTED_ERROR = "Interrupted: the server stopped before this finished."

_ID_RE = re.compile(SESSION_ID_PATTERN)
_SUFFIX = ".json"
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def is_session_id(value: str) -> bool:
    """Return whether ``value`` has the form of a session or turn id (32 hex digits)."""
    return bool(_ID_RE.match(value))


def new_id() -> str:
    """A fresh session id."""
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class _Cached:
    """What the history list needs from one file, reused while the file is unchanged."""

    __slots__ = ("attachment_ids", "stamp", "summary")

    def __init__(
        self, stamp: tuple[int, int], summary: SessionSummary, attachment_ids: frozenset[str]
    ) -> None:
        self.stamp = stamp
        self.summary = summary
        self.attachment_ids = attachment_ids


def _attachment_ids(session: Session) -> frozenset[str]:
    return frozenset(a for turn in session.turns for a in turn.attachment_ids)


def _interrupted(turn: Turn) -> Turn:
    """``turn`` with unfinished work marked as interrupted (unchanged if none)."""
    changes: dict[str, object] = {}
    if turn.status is TurnStatus.PENDING:
        changes["status"] = TurnStatus.ERROR
        changes["error"] = INTERRUPTED_ERROR
    if isinstance(turn, JuryTurn) and "pending" in turn.synthesis:
        synthesis = dict(turn.synthesis)
        stage = synthesis.pop("pending")
        if isinstance(stage, str) and stage:
            synthesis[f"{stage}_error"] = INTERRUPTED_ERROR
        changes["synthesis"] = synthesis
    return turn.model_copy(update=changes) if changes else turn


class SessionStore:
    """Sessions by id, as JSON files under ``root`` (see the module docstring).

    Implements :class:`~llm_crossjury.core.sessions.interfaces.SessionStoreLike`. One
    server process owns the directory. Every method is synchronous and quick enough to
    call from the event loop (one small file per call; the history list caches per
    file).

    Args:
        root: Directory of the session files (created owner-only if missing), or
            ``None`` for a store that never touches the disk.
        persist: Write new and changed sessions to disk (``False``: memory only).
    """

    def __init__(self, root: Path | None, *, persist: bool = True) -> None:
        self._root = root
        self._persist = persist and root is not None
        self._memory: dict[str, Session] = {}
        """Sessions not (or not yet) written to disk; they win over a file."""
        self._cache: dict[str, _Cached] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        if root is not None:
            _mkdir_private(root)
            self._recover()

    # ------------------------------------------------------------------ properties

    @property
    def root(self) -> Path | None:
        """The session directory (``None`` = memory only)."""
        return self._root

    @property
    def persist(self) -> bool:
        """Whether new and changed sessions are written to disk."""
        return self._persist

    def set_persist(self, persist: bool) -> None:
        """Turn saving on or off (``AppSettings.save_history``).

        Turning it off never deletes files. Turning it on writes the sessions kept in
        memory meanwhile.
        """
        if persist and self._root is None:
            return
        self._persist = persist
        if persist:
            for session_id in list(self._memory):
                with self._lock(session_id):
                    session = self._memory.pop(session_id, None)
                    if session is not None:
                        self._write(session)

    # ------------------------------------------------------------------ SessionStoreLike

    def create(self, title: str = "") -> Session:
        """Create, store and return an empty session."""
        now = _now()
        session = Session(id=new_id(), title=title, created_at=now, updated_at=now)
        self._save(session)
        return session

    def get(self, session_id: str) -> Session:
        """Return a session.

        Raises:
            SessionNotFoundError: unknown or malformed id, or a damaged file.
        """
        if not is_session_id(session_id):
            raise SessionNotFoundError(session_id)
        session = self._memory.get(session_id)
        if session is not None:
            return session
        loaded = self._read(session_id)
        if loaded is None:
            raise SessionNotFoundError(session_id)
        return loaded

    def list(self) -> list[SessionSummary]:
        """Every readable session's history row, newest ``updated_at`` first."""
        rows: dict[str, SessionSummary] = {}
        for session_id in self._file_ids():
            cached = self._cached(session_id)
            if cached is not None:
                rows[session_id] = cached.summary
        for session_id, session in list(self._memory.items()):
            rows[session_id] = summarize(session)
        return sorted(rows.values(), key=lambda r: r.updated_at, reverse=True)

    def update(self, session_id: str, fn: Callable[[Session], Session]) -> Session:
        """Apply ``fn`` to the latest version under the session's lock and save it.

        ``updated_at`` is set to now. ``fn`` must not change the id.

        Raises:
            SessionNotFoundError: no such session.
            ValueError: ``fn`` changed the session id.
        """
        with self._lock(session_id):
            current = self.get(session_id)
            changed = fn(current)
            if changed.id != current.id:
                raise ValueError("a session update must not change the session id")
            changed = changed.model_copy(update={"updated_at": max(_now(), current.updated_at)})
            self._save(changed)
            return changed

    def delete(self, session_id: str) -> bool:
        """Delete a session (memory and file). Returns whether one existed."""
        if not is_session_id(session_id):
            return False
        with self._lock(session_id):
            existed = self._memory.pop(session_id, None) is not None
            self._cache.pop(session_id, None)
            path = self._path(session_id)
            if path is not None and path.exists():
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                existed = True
        with self._locks_guard:
            self._locks.pop(session_id, None)
        return existed

    def delete_all(self) -> int:
        """Delete every session (including damaged files). Returns how many."""
        ids = set(self._memory) | set(self._file_ids())
        return sum(1 for session_id in ids if self.delete(session_id))

    def attachment_ids(self) -> frozenset[str]:
        """Every attachment id referenced by a stored session (kept past the TTL)."""
        found: set[str] = set()
        for session_id in self._file_ids():
            cached = self._cached(session_id)
            if cached is not None:
                found |= cached.attachment_ids
        for session in list(self._memory.values()):
            found |= _attachment_ids(session)
        return frozenset(found)

    # ------------------------------------------------------------------ internals

    def _lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = self._locks[session_id] = threading.Lock()
            return lock

    def _path(self, session_id: str) -> Path | None:
        if self._root is None or not is_session_id(session_id):
            return None
        return self._root / f"{session_id}{_SUFFIX}"

    def _file_ids(self) -> Iterator[str]:
        if self._root is None:
            return
        try:
            children = list(self._root.iterdir())
        except OSError:
            return
        for child in children:
            if child.suffix == _SUFFIX and is_session_id(child.stem) and child.is_file():
                yield child.stem

    def _read(self, session_id: str) -> Session | None:
        path = self._path(session_id)
        if path is None:
            return None
        try:
            return Session.model_validate_json(path.read_bytes())
        except (OSError, ValueError, ValidationError):
            return None

    def _cached(self, session_id: str) -> _Cached | None:
        path = self._path(session_id)
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(session_id)
        if cached is not None and cached.stamp == stamp:
            return cached
        session = self._read(session_id)
        if session is None or session.id != session_id:
            self._cache.pop(session_id, None)
            return None
        cached = _Cached(stamp, summarize(session), _attachment_ids(session))
        self._cache[session_id] = cached
        return cached

    def _save(self, session: Session) -> None:
        if self._persist:
            self._write(session)
            self._memory.pop(session.id, None)
        else:
            self._memory[session.id] = session

    def _write(self, session: Session) -> None:
        path = self._path(session.id)
        if path is None:  # pragma: no cover - persist is never on without a root
            self._memory[session.id] = session
            return
        _write_private(path, session.model_dump_json().encode("utf-8"))
        self._cache.pop(session.id, None)

    def _recover(self) -> None:
        """Mark work left pending by a previous server process as interrupted."""
        for session_id in list(self._file_ids()):
            session = self._read(session_id)
            if session is None or session.id != session_id:
                continue
            turns = tuple(_interrupted(t) for t in session.turns)
            if turns != session.turns:
                with contextlib.suppress(OSError):
                    self._write(session.model_copy(update={"turns": turns}))


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(_DIR_MODE)


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` atomically to an owner-only file."""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        with contextlib.suppress(OSError):
            tmp.chmod(_FILE_MODE)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def stored_attachment_ids(root: Path) -> frozenset[str]:
    """Attachment ids referenced by the session files under ``root`` (read-only).

    For wiring the attachment store's purge before a :class:`SessionStore` exists.
    """
    found: set[str] = set()
    try:
        children = list(root.iterdir())
    except OSError:
        return frozenset()
    for child in children:
        if child.suffix != _SUFFIX or not is_session_id(child.stem):
            continue
        try:
            data = json.loads(child.read_bytes())
            turns = data.get("turns", []) if isinstance(data, dict) else []
            for turn in turns:
                ids = turn.get("attachment_ids", []) if isinstance(turn, dict) else []
                found.update(i for i in ids if isinstance(i, str))
        except (OSError, ValueError):
            continue
    return frozenset(found)


__all__ = [
    "INTERRUPTED_ERROR",
    "SESSION_ID_PATTERN",
    "SessionStore",
    "is_session_id",
    "new_id",
    "stored_attachment_ids",
]
