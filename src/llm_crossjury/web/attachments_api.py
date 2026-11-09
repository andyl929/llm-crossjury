"""Attachment upload API (ADR 0007).

* ``POST /api/attachments``: ``multipart/form-data`` with one or more files. Each file
  is checked by magic bytes and its text is extracted in an isolated worker
  (:func:`~llm_crossjury.core.attachments.build_attachment`, run in a thread); the
  result is kept in the temporary :class:`~llm_crossjury.core.attachments.store.
  AttachmentStore` and its metadata is returned. The id is the file's sha256.
* ``DELETE /api/attachments/{id}``: removes a stored file.

The body is parsed as a stream: the request is refused as soon as a file, the files
together, or the whole body exceed their limits, before anything beyond the limit is
buffered. This route is the only one that accepts a non-JSON body; because a
multipart POST needs no CORS preflight, it additionally requires the
:data:`UPLOAD_HEADER` request header, which a cross-origin page can only send after
a preflight that is never granted. Error messages never contain file content.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from llm_crossjury.core.attachments import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    AttachmentError,
    AttachmentTooLargeError,
    BuiltAttachment,
    build_attachment,
    sanitize_filename,
)
from llm_crossjury.core.attachments.build import DerivedImageMeta
from llm_crossjury.core.attachments.store import AttachmentNotFoundError
from llm_crossjury.core.compare_service import MAX_ATTACHMENTS, AttachmentStoreLike
from llm_crossjury.core.providers.base import AttachmentKind

UPLOAD_PATH = "/api/attachments"
UPLOAD_HEADER = "x-crossjury-upload"
"""Required request header of an upload (any non-empty value)."""

MAX_BODY_OVERHEAD = 256 * 1024
"""Multipart framing allowed on top of :data:`MAX_TOTAL_BYTES` (headers, boundaries)."""

MAX_BODY_BYTES = MAX_TOTAL_BYTES + MAX_BODY_OVERHEAD
_MB = 1024 * 1024


class AttachmentInfo(BaseModel):
    """What the UI shows about an uploaded file (never its content)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    """The sha256 of the file; send it back in ``attachment_ids``."""
    filename: str
    kind: AttachmentKind
    media_type: str
    size: int
    """Bytes."""
    pages: int | None
    image_width: int | None
    image_height: int | None
    warnings: tuple[str, ...]
    text_tokens: int | None
    """Local token estimate of the extracted text (``None`` for images)."""
    derived: tuple[DerivedImageMeta, ...] = ()
    """Images derived from the file (rendered PDF pages, DOCX images) for members
    that read images but not the file itself."""


class UploadResponse(BaseModel):
    """Result of an upload: one entry per file, in upload order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attachments: tuple[AttachmentInfo, ...]


class DeleteResponse(BaseModel):
    """Result of a delete."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deleted: bool


def attachment_info(built: BuiltAttachment) -> AttachmentInfo:
    """Metadata of a built attachment for the UI."""
    att, meta = built.attachment, built.meta
    width, height = meta.image_size if meta.image_size is not None else (None, None)
    return AttachmentInfo(
        id=att.sha256,
        filename=att.filename,
        kind=att.kind,
        media_type=att.media_type,
        size=len(att.data),
        pages=meta.page_count,
        image_width=width,
        image_height=height,
        warnings=meta.warnings,
        text_tokens=meta.text_tokens,
        derived=meta.derived,
    )


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "detail": detail})


# --------------------------------------------------------------------------- parsing


@dataclass
class _Upload:
    filename: str
    data: bytearray = field(default_factory=bytearray)


@dataclass
class _PartCollector:
    """Collects file parts from :class:`MultipartParser` callbacks, enforcing limits."""

    files: list[_Upload] = field(default_factory=list)
    error: AttachmentError | None = None
    _field: bytearray = field(default_factory=bytearray)
    _value: bytearray = field(default_factory=bytearray)
    _headers: dict[str, bytes] = field(default_factory=dict)
    _current: _Upload | None = None
    _total: int = 0

    def _fail(self, error: AttachmentError) -> None:
        if self.error is None:
            self.error = error

    def on_part_begin(self) -> None:
        self._headers = {}
        self._current = None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[bytes(self._field).decode("latin-1").strip().lower()] = bytes(self._value)
        self._field.clear()
        self._value.clear()

    def on_headers_finished(self) -> None:
        _, params = parse_options_header(self._headers.get("content-disposition", b""))
        raw = params.get(b"filename")
        if raw is None:
            self._fail(AttachmentError("send files only (unexpected form field)"))
            return
        if len(self.files) >= MAX_ATTACHMENTS:
            self._fail(AttachmentError(f"attach at most {MAX_ATTACHMENTS} files per upload"))
            return
        # Browsers send the name as raw UTF-8; it is reduced to a safe base name.
        self._current = _Upload(filename=raw.decode("utf-8", errors="replace"))
        self.files.append(self._current)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self.error is not None or self._current is None:
            return
        size = end - start
        self._total += size
        current = self._current
        if len(current.data) + size > MAX_FILE_BYTES:
            name = sanitize_filename(current.filename)
            self._fail(
                AttachmentTooLargeError(f"{name} is over the {MAX_FILE_BYTES // _MB} MB limit")
            )
            return
        if self._total > MAX_TOTAL_BYTES:
            self._fail(
                AttachmentTooLargeError(
                    f"the files together are over the {MAX_TOTAL_BYTES // _MB} MB limit"
                )
            )
            return
        current.data += data[start:end]

    def on_part_end(self) -> None:
        self._current = None


async def read_uploads(request: Request) -> list[_Upload]:
    """Stream-parse a multipart body into its files, enforcing every size limit.

    Raises:
        AttachmentError: malformed body, unexpected fields, or a limit exceeded (the
            rest of the body is not read).
    """
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise AttachmentError("send the files as multipart/form-data")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise AttachmentTooLargeError(
            f"the upload is over the {MAX_TOTAL_BYTES // _MB} MB limit for all files"
        )
    collector = _PartCollector()
    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": collector.on_part_begin,
            "on_part_data": collector.on_part_data,
            "on_part_end": collector.on_part_end,
            "on_header_field": collector.on_header_field,
            "on_header_value": collector.on_header_value,
            "on_header_end": collector.on_header_end,
            "on_headers_finished": collector.on_headers_finished,
        },
    )
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_BODY_BYTES:
                raise AttachmentTooLargeError(
                    f"the upload is over the {MAX_TOTAL_BYTES // _MB} MB limit for all files"
                )
            parser.write(chunk)
            if collector.error is not None:
                raise collector.error
        parser.finalize()
    except MultipartParseError:
        raise AttachmentError("the upload is not valid multipart/form-data") from None
    if collector.error is not None:
        raise collector.error
    if not collector.files:
        raise AttachmentError("no file was sent")
    return collector.files


# --------------------------------------------------------------------------- router


def create_attachments_router(store: Callable[[], AttachmentStoreLike | None]) -> APIRouter:
    """Create the upload router.

    Args:
        store: Returns the attachment store (``None`` = uploads unavailable).
    """
    router = APIRouter()

    @router.post(UPLOAD_PATH, response_model=None)
    async def upload(request: Request) -> UploadResponse | JSONResponse:
        if not request.headers.get(UPLOAD_HEADER, "").strip():
            return _error(403, "missing_upload_header", "Uploads need the upload header.")
        target = store()
        if target is None:
            return _error(400, "attachment", "Attachments are not available in this setup.")
        try:
            uploads = await read_uploads(request)
            built: list[BuiltAttachment] = []
            for item in uploads:
                try:
                    built.append(
                        await asyncio.to_thread(build_attachment, item.filename, bytes(item.data))
                    )
                except AttachmentError as exc:
                    name = sanitize_filename(item.filename)
                    raise type(exc)(f"{name}: {exc}") from None
            for ready in built:
                await asyncio.to_thread(target.put, ready)
        except AttachmentTooLargeError as exc:
            return _error(400, "attachment_too_large", f"Upload refused: {exc}.")
        except AttachmentError as exc:
            return _error(400, "attachment", f"Upload refused: {exc}.")
        return UploadResponse(attachments=tuple(attachment_info(b) for b in built))

    @router.delete(UPLOAD_PATH + "/{attachment_id}", response_model=None)
    async def delete(attachment_id: str) -> DeleteResponse | JSONResponse:
        target = store()
        if target is None:
            return _error(400, "attachment", "Attachments are not available in this setup.")
        try:
            deleted = await asyncio.to_thread(target.delete, attachment_id)
        except AttachmentNotFoundError as exc:
            return _error(400, "attachment_not_found", f"{exc}.")
        return DeleteResponse(deleted=bool(deleted))

    return router


__all__ = [
    "MAX_BODY_BYTES",
    "UPLOAD_HEADER",
    "UPLOAD_PATH",
    "AttachmentInfo",
    "UploadResponse",
    "attachment_info",
    "create_attachments_router",
    "read_uploads",
]
