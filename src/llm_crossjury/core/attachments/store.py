"""Temporary on-disk store for uploaded attachments (ADR 0007).

The web UI uploads files once and then refers to them by id (the sha256 of the file)
in estimate, run and synthesis requests. The store lives under the data directory,
never inside the repository::

    <data dir>/attachments/<sha256>/data.bin    the raw file
    <data dir>/attachments/<sha256>/meta.json   name, type, extracted text, metadata
    <data dir>/attachments/<sha256>/image-<i>.bin  derived image i (page images, DOCX
                                                  images; listed in the metadata)

Directories are created owner-only (``0o700``) and files ``0o600`` where the platform
supports POSIX permissions (on Windows the user profile's ACLs apply). Entries older
than :data:`ATTACHMENT_TTL_S` are deleted when the store is created (app start-up),
on every upload, and when an expired entry is requested.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from llm_crossjury.core.attachments.build import (
    AttachmentMeta,
    BuiltAttachment,
    DerivedImageMeta,
)
from llm_crossjury.core.attachments.limits import AttachmentError
from llm_crossjury.core.providers.base import Attachment, AttachmentKind

ATTACHMENT_TTL_S = 24 * 60 * 60
"""Uploaded attachments are deleted 24 hours after their (last) upload."""

ATTACHMENT_ID_PATTERN = r"^[0-9a-f]{64}$"
"""An attachment id is the lowercase hex sha256 of the file."""

_ID_RE = re.compile(ATTACHMENT_ID_PATTERN)
_DATA = "data.bin"
_META = "meta.json"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_FORMAT = 1


class AttachmentNotFoundError(AttachmentError):
    """The attachment id is unknown, expired or its entry is unreadable."""


def is_attachment_id(value: str) -> bool:
    """Return whether ``value`` has the form of an attachment id (64 hex digits)."""
    return bool(_ID_RE.match(value))


def _not_found(attachment_id: str) -> AttachmentNotFoundError:
    return AttachmentNotFoundError(
        f"attachment {attachment_id[:12]} was not found or has expired; attach the file again"
    )


class AttachmentStore:
    """Stores built attachments by sha256 for :data:`ATTACHMENT_TTL_S`.

    Not safe for concurrent writers in different processes; one UI process owns it.
    Blocking (file I/O): async callers should use ``asyncio.to_thread`` for large
    files.

    Args:
        root: Directory of the store (created owner-only if missing).
        ttl_s: Lifetime of an entry in seconds.
        clock: Returns the current time in seconds since the epoch (tests inject one).
    """

    def __init__(
        self,
        root: Path,
        *,
        ttl_s: float = ATTACHMENT_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = root
        self._ttl = ttl_s
        self._clock = clock
        _mkdir_private(root)
        self.purge_expired()

    @property
    def root(self) -> Path:
        """The store directory."""
        return self._root

    def _entry(self, attachment_id: str) -> Path:
        if not is_attachment_id(attachment_id):
            raise AttachmentNotFoundError("invalid attachment id")
        return self._root / attachment_id

    def put(self, built: BuiltAttachment) -> str:
        """Store ``built`` (replacing an entry of the same file) and return its id.

        Expired entries are purged first. The entry's lifetime restarts.
        """
        self.purge_expired()
        att = built.attachment
        entry = self._entry(att.sha256)
        _mkdir_private(entry)
        meta = {
            "format": _FORMAT,
            "created_at": self._clock(),
            "filename": att.filename,
            "kind": att.kind.value,
            "media_type": att.media_type,
            "fallback_text": att.fallback_text,
            "meta": built.meta.model_dump(mode="json"),
        }
        for index, image in enumerate(built.derived_images):
            _write_private(entry / _image_name(index), image.data)
        _write_private(entry / _DATA, att.data)
        _write_private(entry / _META, json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        return att.sha256

    def get(self, attachment_id: str) -> BuiltAttachment:
        """Load an attachment by id.

        Raises:
            AttachmentNotFoundError: unknown, malformed or expired id, or a damaged
                entry (deleted). The message names at most the id prefix.
        """
        entry = self._entry(attachment_id)
        try:
            meta = json.loads((entry / _META).read_text(encoding="utf-8"))
            data = (entry / _DATA).read_bytes()
        except (OSError, ValueError):
            raise _not_found(attachment_id) from None
        try:
            created = float(meta["created_at"])
            if self._expired(created):
                self.delete(attachment_id)
                raise _not_found(attachment_id)
            if hashlib.sha256(data).hexdigest() != attachment_id:
                raise ValueError("digest mismatch")
            attachment = Attachment(
                kind=AttachmentKind(meta["kind"]),
                media_type=str(meta["media_type"]),
                filename=str(meta["filename"]),
                sha256=attachment_id,
                data=data,
                fallback_text=meta["fallback_text"],
            )
            loaded = AttachmentMeta.model_validate(meta["meta"])
            return BuiltAttachment(
                attachment=attachment,
                meta=loaded,
                derived_images=tuple(
                    _load_image(entry, index, info) for index, info in enumerate(loaded.derived)
                ),
            )
        except (KeyError, TypeError, ValueError, ValidationError, OSError):
            self.delete(attachment_id)
            raise _not_found(attachment_id) from None

    def delete(self, attachment_id: str) -> bool:
        """Delete an entry. Returns whether one existed.

        Raises:
            AttachmentNotFoundError: the id is malformed.
        """
        entry = self._entry(attachment_id)
        if not entry.exists():
            return False
        shutil.rmtree(entry, ignore_errors=True)
        return True

    def purge_expired(self) -> int:
        """Delete every entry older than the TTL (and unreadable entries).

        Returns:
            The number of deleted entries.
        """
        removed = 0
        try:
            children = list(self._root.iterdir())
        except OSError:
            return 0
        for child in children:
            if not child.is_dir() or not is_attachment_id(child.name):
                continue
            try:
                created = float(json.loads((child / _META).read_text("utf-8"))["created_at"])
            except (OSError, ValueError, KeyError, TypeError):
                # A half-written entry: fall back to the directory's modification time.
                try:
                    created = child.stat().st_mtime
                except OSError:
                    continue
            if self._expired(created):
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed

    def _expired(self, created: float) -> bool:
        return self._clock() - created >= self._ttl


def _image_name(index: int) -> str:
    return f"image-{index}.bin"


def _load_image(entry: Path, index: int, info: DerivedImageMeta) -> Attachment:
    """Load derived image ``index`` and check it against its recorded sha256."""
    data = (entry / _image_name(index)).read_bytes()
    if hashlib.sha256(data).hexdigest() != info.sha256:
        raise ValueError("digest mismatch")
    return Attachment(
        kind=AttachmentKind.IMAGE,
        media_type=info.media_type,
        filename=info.filename,
        sha256=info.sha256,
        data=data,
        fallback_text=None,
    )


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(_DIR_MODE)


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` atomically to an owner-only file."""
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    with contextlib.suppress(OSError):
        tmp.chmod(_FILE_MODE)
    tmp.replace(path)


__all__ = [
    "ATTACHMENT_ID_PATTERN",
    "ATTACHMENT_TTL_S",
    "AttachmentNotFoundError",
    "AttachmentStore",
    "is_attachment_id",
]
