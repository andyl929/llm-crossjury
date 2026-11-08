"""Placing attachments in prompts, and deciding how each member receives them (ADR 0007).

Layout (shared by Compare and the synthesis chair): the system message of the stable
prefix gains :data:`~llm_crossjury.core.attachments.render.ATTACHMENT_NOTICE`, and ONE
user message follows it, still inside the stable prefix, holding the attachments in
:func:`~llm_crossjury.core.attachments.render.sort_attachments` order. The adapters
render that message natively or as fallback text.

The ``cli`` (subscription) adapters ignore ``Message.attachments``, so their prompts
are rewritten by :func:`inline_attachments`: the fallback blocks become part of the
message text, and images (no text form) are refused.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.attachments.build import AttachmentMeta, BuiltAttachment
from llm_crossjury.core.attachments.render import (
    ATTACHMENT_NOTICE,
    fallback_block,
    sort_attachments,
)
from llm_crossjury.core.attachments.tokens import estimate_attachment_tokens
from llm_crossjury.core.providers.base import (
    Attachment,
    AttachmentKind,
    Backend,
    CapabilityNotSupportedError,
    Message,
    Prompt,
    Role,
)

ATTACHMENTS_HEADER = "Files attached by the user:"
"""First line of the user message that carries the attachments."""

DeliveryMode = Literal["native", "text", "refused"]
"""How one member receives one attachment."""


class AttachmentReceipt(BaseModel):
    """How one member receives (or would receive) one attachment, for the results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    """The attachment id (sha256)."""
    filename: str
    kind: AttachmentKind
    mode: DeliveryMode
    warnings: tuple[str, ...]
    """Extraction warnings that matter for this member (text mode only)."""


def delivery_mode(
    att: Attachment, input_modalities: frozenset[AttachmentKind], backend: Backend
) -> DeliveryMode:
    """Decide how a member receives ``att``.

    ``cli`` members get text only. Other members get the file natively when its kind
    is in ``input_modalities`` (never for ``DOCUMENT`` in v1), else its text; an
    attachment without text (an image) is refused.
    """
    if (
        backend is not Backend.CLI
        and att.kind is not AttachmentKind.DOCUMENT
        and att.kind in input_modalities
    ):
        return "native"
    return "text" if att.fallback_text is not None else "refused"


def receipts(
    built: Sequence[BuiltAttachment], input_modalities: frozenset[AttachmentKind], backend: Backend
) -> tuple[AttachmentReceipt, ...]:
    """Per-attachment delivery of one member, in the given order."""
    out: list[AttachmentReceipt] = []
    for item in built:
        att = item.attachment
        mode = delivery_mode(att, input_modalities, backend)
        out.append(
            AttachmentReceipt(
                id=att.sha256,
                filename=att.filename,
                kind=att.kind,
                mode=mode,
                warnings=item.meta.warnings if mode == "text" else (),
            )
        )
    return tuple(out)


def refusal_message(
    refused: Sequence[AttachmentReceipt], *, provider: str, model: str, backend: Backend
) -> str:
    """User-facing reason why a member cannot receive some attachments.

    Names the files and the model only, never file content.
    """
    names = ", ".join(r.filename for r in refused)
    if backend is Backend.CLI:
        return (
            f"Subscription (CLI) models receive attached files as extracted text only, and "
            f"images have no text form ({names}). Remove the image(s) or use an API key "
            "for this provider."
        )
    return (
        f"{provider}/{model} does not accept image attachments ({names}). Remove the "
        "image(s) or pick a model that accepts images."
    )


def attachments_message(attachments: Iterable[Attachment]) -> Message:
    """The user message that carries the attachments (sorted), for the stable prefix."""
    ordered = sort_attachments(attachments)
    lines = [ATTACHMENTS_HEADER, *(f"- {a.filename} ({a.media_type})" for a in ordered)]
    return Message(role=Role.USER, content="\n".join(lines), attachments=ordered)


def with_attachments(prompt: Prompt, attachments: Sequence[Attachment]) -> Prompt:
    """Add the notice and the attachment message to ``prompt``'s stable prefix.

    The notice is appended to the first system message (one is created when the
    prefix has none); the attachment message follows the system messages. With no
    attachments, ``prompt`` is returned unchanged.
    """
    if not attachments:
        return prompt
    prefix = list(prompt.stable_prefix)
    systems = [m for m in prefix if m.role is Role.SYSTEM]
    rest = [m for m in prefix if m.role is not Role.SYSTEM]
    if systems:
        first = systems[0]
        systems[0] = Message(role=Role.SYSTEM, content=f"{first.content}\n\n{ATTACHMENT_NOTICE}")
    else:
        systems = [Message(role=Role.SYSTEM, content=ATTACHMENT_NOTICE)]
    return Prompt(
        stable_prefix=(*systems, attachments_message(attachments), *rest),
        variable_suffix=prompt.variable_suffix,
    )


def inline_attachments(prompt: Prompt) -> Prompt:
    """Rewrite ``prompt`` for a text-only (``cli``) member.

    Every message's attachments are replaced by their fallback blocks in the
    message text, so nothing is silently dropped by an adapter that ignores
    ``Message.attachments``.

    Raises:
        CapabilityNotSupportedError: an attachment has no text form (an image).
    """

    def convert(message: Message) -> Message:
        if not message.attachments:
            return message
        missing = [a.filename for a in message.attachments if a.fallback_text is None]
        if missing:
            raise CapabilityNotSupportedError(
                "subscription (CLI) models receive attached files as extracted text only; "
                f"images cannot be sent ({', '.join(missing)})"
            )
        blocks = "\n\n".join(fallback_block(a) for a in message.attachments)
        return Message(role=message.role, content=f"{message.content}\n\n{blocks}")

    return Prompt(
        stable_prefix=tuple(convert(m) for m in prompt.stable_prefix),
        variable_suffix=tuple(convert(m) for m in prompt.variable_suffix),
    )


def attachment_tokens(
    built: Sequence[BuiltAttachment],
    *,
    provider: str,
    input_modalities: frozenset[AttachmentKind],
    backend: Backend,
) -> dict[str, int | None]:
    """Local token estimate per attachment id for one member (``None`` = refused)."""
    out: dict[str, int | None] = {}
    for item in built:
        att = item.attachment
        mode = delivery_mode(att, input_modalities, backend)
        out[att.sha256] = (
            None
            if mode == "refused"
            else estimate_attachment_tokens(att, item.meta, provider, mode == "native")
        )
    return out


def prompt_attachment_tokens(
    prompt: Prompt,
    metas: Mapping[str, AttachmentMeta],
    *,
    provider: str,
    input_modalities: frozenset[AttachmentKind],
    backend: Backend,
) -> int:
    """Local token estimate of every attachment carried by ``prompt``'s messages.

    Attachments without metadata in ``metas`` are counted by their text form (or
    not at all when they have none: such a call is refused by the adapter anyway).
    """
    total = 0
    for message in (*prompt.stable_prefix, *prompt.variable_suffix):
        for att in message.attachments:
            mode = delivery_mode(att, input_modalities, backend)
            meta = metas.get(att.sha256)
            if mode == "refused":
                continue
            if meta is None:
                if att.fallback_text is not None:
                    total += estimate_attachment_tokens(
                        att, _TEXT_ONLY_META, provider, native=False
                    )
                continue
            total += estimate_attachment_tokens(att, meta, provider, mode == "native")
    return total


_TEXT_ONLY_META = AttachmentMeta(
    page_count=None,
    image_size=None,
    warnings=(),
    extractor="unknown",
    extractor_version="unknown",
    text_tokens=None,
)


__all__ = [
    "ATTACHMENTS_HEADER",
    "AttachmentReceipt",
    "DeliveryMode",
    "attachment_tokens",
    "attachments_message",
    "delivery_mode",
    "inline_attachments",
    "prompt_attachment_tokens",
    "receipts",
    "refusal_message",
    "with_attachments",
]
