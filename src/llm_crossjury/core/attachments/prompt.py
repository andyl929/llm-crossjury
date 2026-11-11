"""Placing attachments in prompts, and deciding how each member receives them (ADR 0007).

Layout (shared by Compare and the synthesis chair): the system message of the stable
prefix gains :data:`~llm_crossjury.core.attachments.render.ATTACHMENT_NOTICE`, and ONE
user message follows it, still inside the stable prefix, holding the attachments in
:func:`~llm_crossjury.core.attachments.render.sort_attachments` order. The adapters
render that message natively or as fallback text.

Delivery rule, per member and attachment (:func:`receipts`):

* the attachment's kind is in the member's ``input_modalities`` -> the original only
  (``native``);
* otherwise, it has ``fallback_text`` -> the text block (``text``), plus its derived
  images (rendered PDF pages, DOCX images) when the member reads images
  (``text+images``, see :func:`add_derived_images`);
* otherwise (an image the member cannot read) -> ``refused``.

``cli`` (subscription) prompts are then rewritten by :func:`inline_attachments`:
attachments whose kind the CLI adapter declares in ``Capabilities.input_modalities``
stay on the message (the adapter sends them natively), the others become fallback
blocks in the message text, and an attachment with neither form (an image) is
refused. CLI adapters that declare no modalities therefore receive text only and
never see an attachment.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.attachments.build import (
    AttachmentMeta,
    BuiltAttachment,
    DerivedImageMeta,
)
from llm_crossjury.core.attachments.limits import MAX_TOTAL_BYTES
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

DERIVED_HEADER = (
    "Images taken from these files (pages with figures or scans, or images embedded "
    "in a document), each attached right after its file:"
)
"""Heading of the list of derived images added by :func:`add_derived_images`."""

DeliveryMode = Literal["native", "text", "text+images", "refused"]
"""How one member receives one attachment."""

BaseMode = Literal["native", "text", "refused"]
""":func:`delivery_mode` before derived images are considered."""


class AttachmentReceipt(BaseModel):
    """How one member receives (or would receive) one attachment, for the results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    """The attachment id (sha256)."""
    filename: str
    kind: AttachmentKind
    mode: DeliveryMode
    warnings: tuple[str, ...]
    """Extraction warnings that matter for this member (text modes only)."""
    images: int = 0
    """Derived images sent with the text (``text+images`` only): page images of a
    PDF, or embedded images of a DOCX."""


def delivery_mode(
    att: Attachment, input_modalities: frozenset[AttachmentKind], backend: Backend
) -> BaseMode:
    """Decide how a member receives ``att`` itself (without derived images).

    A member gets the file natively when its kind is in ``input_modalities`` (never
    for ``DOCUMENT`` in v1), else its text; an attachment without text (an image) is
    refused. The rule is the same on both backends: a ``cli`` provider declares only
    the kinds its adapter really delivers (``backend`` is kept for the callers and
    only changes the wording of :func:`refusal_message`).
    """
    del backend  # same rule on both backends (see the docstring)
    if att.kind is not AttachmentKind.DOCUMENT and att.kind in input_modalities:
        return "native"
    return "text" if att.fallback_text is not None else "refused"


def select_derived_images(
    attachments: Sequence[Attachment],
    derived: Mapping[str, Sequence[Attachment]],
    input_modalities: frozenset[AttachmentKind],
    *,
    max_bytes: int | None = None,
) -> dict[str, tuple[Attachment, ...]]:
    """The derived images one member receives, per source attachment id.

    Only attachments delivered as ``text`` get their derived images, and only when
    the member reads images. A member never receives more than ``max_bytes`` of
    binary data: the native originals count first, then derived images are added in
    attachment order until the next one would exceed the limit.

    Args:
        attachments: The member's attachments, in prompt order.
        derived: Source attachment id -> its derived images (in order).
        input_modalities: The member's native input kinds.
        max_bytes: Binary data limit per member (default :data:`MAX_TOTAL_BYTES`).

    Returns:
        Source id -> the derived images to send (non-empty tuples only).
    """
    if AttachmentKind.IMAGE not in input_modalities:
        return {}
    modes = [(a, delivery_mode(a, input_modalities, Backend.API)) for a in attachments]
    limit = MAX_TOTAL_BYTES if max_bytes is None else max_bytes
    used = sum(len(a.data) for a, mode in modes if mode == "native")
    out: dict[str, tuple[Attachment, ...]] = {}
    for att, mode in modes:
        images = derived.get(att.sha256, ())
        if mode != "text" or not images or att.sha256 in out:
            continue
        chosen: list[Attachment] = []
        for image in images:
            if used + len(image.data) > limit:
                break
            used += len(image.data)
            chosen.append(image)
        if chosen:
            out[att.sha256] = tuple(chosen)
    return out


def derived_map(built: Iterable[BuiltAttachment]) -> dict[str, tuple[Attachment, ...]]:
    """Source attachment id -> its derived images, for :func:`add_derived_images`."""
    return {b.attachment.sha256: b.derived_images for b in built if b.derived_images}


def receipts(
    built: Sequence[BuiltAttachment], input_modalities: frozenset[AttachmentKind], backend: Backend
) -> tuple[AttachmentReceipt, ...]:
    """Per-attachment delivery of one member, in the given order."""
    selected = select_derived_images(
        [b.attachment for b in built], derived_map(built), input_modalities
    )
    out: list[AttachmentReceipt] = []
    for item in built:
        att = item.attachment
        mode: DeliveryMode = delivery_mode(att, input_modalities, backend)
        images = len(selected.get(att.sha256, ()))
        warnings: tuple[str, ...] = ()
        if mode == "text":
            warnings = item.meta.warnings
            left_out = (
                len(item.derived_images) - images if AttachmentKind.IMAGE in input_modalities else 0
            )
            if left_out:
                warnings = (
                    *warnings,
                    f"{left_out} derived image(s) were left out to stay within the "
                    "request size limit",
                )
            if images:
                mode = "text+images"
        out.append(
            AttachmentReceipt(
                id=att.sha256,
                filename=att.filename,
                kind=att.kind,
                mode=mode,
                warnings=warnings,
                images=images,
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
            f"The subscription (CLI) model {provider}/{model} receives attached files as "
            f"extracted text only here, and images have no text form ({names}). Remove the "
            "image(s), pick a CLI model that accepts images, or use an API key for this "
            "provider."
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


def add_derived_images(
    prompt: Prompt,
    derived: Mapping[str, Sequence[Attachment]],
    input_modalities: frozenset[AttachmentKind],
) -> Prompt:
    """Add the derived images one member receives (smart page images, ADR 0007).

    Each selected image (:func:`select_derived_images`) is placed right after its
    source attachment on the same message, and the message text lists the images
    by their generated names. Apply this before :func:`inline_attachments`. With
    nothing to add, ``prompt`` is returned unchanged.
    """
    originals = [a for m in (*prompt.stable_prefix, *prompt.variable_suffix) for a in m.attachments]
    selected = select_derived_images(originals, derived, input_modalities)
    if not selected:
        return prompt

    def expand(message: Message) -> Message:
        added = [img for a in message.attachments for img in selected.get(a.sha256, ())]
        if not added:
            return message
        attachments = tuple(
            item for a in message.attachments for item in (a, *selected.get(a.sha256, ()))
        )
        lines = [DERIVED_HEADER, *(f"- {img.filename} ({img.media_type})" for img in added)]
        content = "\n\n".join(p for p in (message.content, "\n".join(lines)) if p)
        return Message(role=message.role, content=content, attachments=attachments)

    return Prompt(
        stable_prefix=tuple(expand(m) for m in prompt.stable_prefix),
        variable_suffix=tuple(expand(m) for m in prompt.variable_suffix),
    )


def inline_attachments(
    prompt: Prompt, input_modalities: frozenset[AttachmentKind] = frozenset()
) -> Prompt:
    """Rewrite ``prompt`` for a ``cli`` member.

    Attachments the member takes natively (see :func:`delivery_mode`) stay on their
    message; every other attachment is replaced by its fallback block in the message
    text, so nothing is silently dropped by an adapter that only reads text. With
    no ``input_modalities`` every attachment becomes text.

    Raises:
        CapabilityNotSupportedError: an attachment is neither native nor has a text
            form (an image for a CLI model without image input).
    """

    def convert(message: Message) -> Message:
        if not message.attachments:
            return message
        modes = [(a, delivery_mode(a, input_modalities, Backend.CLI)) for a in message.attachments]
        missing = [a.filename for a, mode in modes if mode == "refused"]
        if missing:
            raise CapabilityNotSupportedError(
                "this subscription (CLI) model receives attached files as extracted text "
                f"only, and images have no text form ({', '.join(missing)})"
            )
        native = tuple(a for a, mode in modes if mode == "native")
        blocks = "\n\n".join(fallback_block(a) for a, mode in modes if mode == "text")
        content = f"{message.content}\n\n{blocks}" if blocks else message.content
        return Message(role=message.role, content=content, attachments=native)

    return Prompt(
        stable_prefix=tuple(convert(m) for m in prompt.stable_prefix),
        variable_suffix=tuple(convert(m) for m in prompt.variable_suffix),
    )


def derived_image_meta(info: DerivedImageMeta) -> AttachmentMeta:
    """The :class:`AttachmentMeta` of a derived image, for token estimates."""
    return AttachmentMeta(
        page_count=None,
        image_size=(info.width, info.height),
        warnings=(),
        extractor="derived",
        extractor_version="1",
        text_tokens=None,
    )


def attachment_metas(built: Iterable[BuiltAttachment]) -> dict[str, AttachmentMeta]:
    """Attachment id -> metadata, derived images included (for prompt estimates)."""
    out: dict[str, AttachmentMeta] = {}
    for item in built:
        out[item.attachment.sha256] = item.meta
        for info in item.meta.derived:
            out.setdefault(info.sha256, derived_image_meta(info))
    return out


def attachment_tokens(
    built: Sequence[BuiltAttachment],
    *,
    provider: str,
    input_modalities: frozenset[AttachmentKind],
    backend: Backend,
) -> dict[str, int | None]:
    """Local token estimate per attachment id for one member (``None`` = refused).

    In ``text+images`` mode the estimate is the text block plus every derived image
    the member receives, each estimated as a native image.
    """
    selected = select_derived_images(
        [b.attachment for b in built], derived_map(built), input_modalities
    )
    out: dict[str, int | None] = {}
    for item in built:
        att = item.attachment
        mode = delivery_mode(att, input_modalities, backend)
        if mode == "refused":
            out[att.sha256] = None
            continue
        tokens = estimate_attachment_tokens(att, item.meta, provider, mode == "native")
        images = selected.get(att.sha256, ())
        for info in item.meta.derived[: len(images)]:
            tokens += estimate_attachment_tokens(
                _image_stub(info), derived_image_meta(info), provider, native=True
            )
        out[att.sha256] = tokens
    return out


def _image_stub(info: DerivedImageMeta) -> Attachment:
    """An image attachment without data, enough for :func:`estimate_attachment_tokens`."""
    return Attachment(
        kind=AttachmentKind.IMAGE,
        media_type=info.media_type,
        filename=info.filename,
        sha256=info.sha256,
        data=b"",
        fallback_text=None,
    )


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
    Derived images need their metadata in ``metas`` (see :func:`attachment_metas`).
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
    "DERIVED_HEADER",
    "AttachmentReceipt",
    "BaseMode",
    "DeliveryMode",
    "add_derived_images",
    "attachment_metas",
    "attachment_tokens",
    "attachments_message",
    "delivery_mode",
    "derived_image_meta",
    "derived_map",
    "inline_attachments",
    "prompt_attachment_tokens",
    "receipts",
    "refusal_message",
    "select_derived_images",
    "with_attachments",
]
