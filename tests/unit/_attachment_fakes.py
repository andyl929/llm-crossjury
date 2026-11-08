"""Built attachments and an in-memory store for service tests (no extraction worker)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from llm_crossjury.core.attachments.build import AttachmentMeta, BuiltAttachment
from llm_crossjury.core.attachments.store import AttachmentNotFoundError
from llm_crossjury.core.providers.base import Attachment, AttachmentKind, Capabilities

SECRET_TEXT = "TOP-SECRET-FILE-CONTENT-4711"  # noqa: S105 - test marker, not a password
"""Text inside the fake files; must never appear in error responses."""


def built(
    kind: AttachmentKind,
    *,
    name: str | None = None,
    payload: bytes | None = None,
    text: str | None = None,
    warnings: tuple[str, ...] = (),
) -> BuiltAttachment:
    """A built attachment with plausible metadata for ``kind``."""
    media = {
        AttachmentKind.PDF: "application/pdf",
        AttachmentKind.IMAGE: "image/png",
        AttachmentKind.DOCUMENT: (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    }[kind]
    data = payload if payload is not None else f"{kind.value}:{name}:{SECRET_TEXT}".encode()
    fallback = None if kind is AttachmentKind.IMAGE else (text or f"{SECRET_TEXT} in {name}")
    attachment = Attachment(
        kind=kind,
        media_type=media,
        filename=name or f"file.{kind.value}",
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        fallback_text=fallback,
    )
    meta = AttachmentMeta(
        page_count=2 if kind is AttachmentKind.PDF else None,
        image_size=(64, 64) if kind is AttachmentKind.IMAGE else None,
        warnings=warnings,
        extractor="test",
        extractor_version="1",
        text_tokens=None if fallback is None else 10,
    )
    return BuiltAttachment(attachment=attachment, meta=meta)


@dataclass
class MemoryStore:
    """An :class:`AttachmentStoreLike` backed by a dict."""

    items: dict[str, BuiltAttachment] = field(default_factory=dict)

    def add(self, *items: BuiltAttachment) -> list[str]:
        for item in items:
            self.items[item.attachment.sha256] = item
        return [item.attachment.sha256 for item in items]

    def put(self, item: BuiltAttachment) -> str:
        self.items[item.attachment.sha256] = item
        return item.attachment.sha256

    def delete(self, attachment_id: str) -> bool:
        return self.items.pop(attachment_id, None) is not None

    def get(self, attachment_id: str) -> BuiltAttachment:
        try:
            return self.items[attachment_id]
        except KeyError:
            raise AttachmentNotFoundError(
                f"attachment {attachment_id[:12]} was not found or has expired"
            ) from None


def caps(*kinds: AttachmentKind) -> Capabilities:
    """Mock capabilities with the given native input modalities."""
    return Capabilities(
        structured_output=True,
        caching=False,
        batch=False,
        temperature=True,
        reasoning=False,
        input_modalities=frozenset(kinds),
    )
