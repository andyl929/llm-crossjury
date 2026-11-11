"""Text extraction from PDF and DOCX files.

These functions process untrusted input. :func:`build_attachment` runs them in a
separate worker process with a wall-clock limit (see ``isolate.py``); call them
directly only in tests. They never log file content.

Besides the text, extraction decides which derived images a document gets (smart
page images, ADR 0007): the PDF pages to render (scanned pages and figure pages, see
:func:`is_figure_page`) and the DOCX embedded images to keep. Rendering itself
happens in ``pages.py``, in a second worker.
"""

from __future__ import annotations

import io
import re
import warnings
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from llm_crossjury.core.attachments.detect import FileType, detect_file_type, image_size
from llm_crossjury.core.attachments.limits import (
    FIGURE_FULL_COVERAGE,
    FIGURE_MIN_COVERAGE,
    FIGURE_MIN_IMAGE_PX,
    FIGURE_PAGE_MAX_CHARS,
    MAX_DERIVED_BYTES_PER_FILE,
    MAX_DERIVED_IMAGE_BYTES,
    MAX_DERIVED_IMAGE_EDGE_PX,
    MAX_DOCX_IMAGES,
    MAX_PAGE_IMAGES,
    MAX_PDF_PAGES,
    MAX_TEXT_CHARS,
    MIN_EMBEDDED_IMAGE_BYTES,
    MIN_EMBEDDED_IMAGE_PX,
    SCANNED_PAGE_MIN_CHARS,
    AttachmentError,
    AttachmentExtractionError,
    AttachmentTooLargeError,
    UnsupportedAttachmentError,
)
from llm_crossjury.core.providers.base import AttachmentKind

_MAX_LISTED_PAGES = 20

ImageReason = Literal["scanned", "figure", "embedded"]
"""Why a derived image exists: a scanned PDF page, a PDF page with a figure, or an
image embedded in a DOCX."""


@dataclass(frozen=True)
class PageSelection:
    """A PDF page chosen to be rendered as an image."""

    page: int
    """1-based page number."""
    reason: ImageReason


@dataclass(frozen=True)
class ExtractedImage:
    """An image derived from a document (a rendered page or an embedded image)."""

    data: bytes
    media_type: str
    width: int
    height: int
    reason: ImageReason
    page: int | None
    """1-based PDF page number; ``None`` for DOCX images."""
    index: int | None
    """1-based position among the kept DOCX images (the ``[Image i of n]`` marker);
    ``None`` for PDF pages."""


@dataclass(frozen=True)
class Extraction:
    """Text extracted from a document, plus what the user should know about it."""

    text: str
    page_count: int | None
    warnings: tuple[str, ...]
    render_pages: tuple[PageSelection, ...] = ()
    """PDF pages to render as images (at most :data:`MAX_PAGE_IMAGES`, page order)."""
    images: tuple[ExtractedImage, ...] = ()
    """DOCX images kept for members with image input (at most :data:`MAX_DOCX_IMAGES`)."""


def extract_text(media_type: str, data: bytes) -> Extraction:
    """Extract text from a PDF or DOCX and apply :data:`MAX_TEXT_CHARS`.

    Args:
        media_type: ``FileType.PDF`` or ``FileType.DOCX`` value (already detected).
        data: The raw file content.

    Returns:
        The extraction, truncated to :data:`MAX_TEXT_CHARS` with a warning if needed.

    Raises:
        AttachmentError: The file cannot be processed or exceeds a limit.
    """
    if media_type == FileType.PDF.value:
        result = extract_pdf(data)
    elif media_type == FileType.DOCX.value:
        result = extract_docx(data)
    else:
        raise UnsupportedAttachmentError("no text extractor for this file type")
    if len(result.text) <= MAX_TEXT_CHARS:
        return result
    note = (
        f"extracted text was truncated from {len(result.text)} to {MAX_TEXT_CHARS} "
        "characters; text-only members see only the beginning"
    )
    return Extraction(
        text=result.text[:MAX_TEXT_CHARS],
        page_count=result.page_count,
        warnings=(*result.warnings, note),
        render_pages=result.render_pages,
        images=result.images,
    )


# --------------------------------------------------------------------------- tables


def markdown_table(rows: Sequence[Sequence[str | None]]) -> str:
    """Render table rows as a GitHub-style Markdown table.

    The first row is the header. Short rows are padded; ``|`` is escaped and line
    breaks inside cells become spaces.

    Args:
        rows: Table cells, row by row; ``None`` is an empty cell.

    Returns:
        The Markdown table, or ``""`` if every cell is empty.
    """
    cells = [[_cell(c) for c in row] for row in rows if row]
    if not any(any(row) for row in cells):
        return ""
    width = max(len(row) for row in cells)
    cells = [row + [""] * (width - len(row)) for row in cells]
    lines = ["| " + " | ".join(cells[0]) + " |", "|" + "|".join(["---"] * width) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in cells[1:])
    return "\n".join(lines)


def _cell(value: str | None) -> str:
    return " ".join((value or "").split()).replace("|", "\\|")


# --------------------------------------------------------------------------- PDF


def extract_pdf(data: bytes) -> Extraction:
    """Extract page-delimited text (pypdf) and tables as Markdown (pdfplumber).

    Also selects the pages to render as images: pages with almost no text (possibly
    scanned) and figure pages (:func:`is_figure_page`), in page order, at most
    :data:`MAX_PAGE_IMAGES`.

    Args:
        data: The raw PDF.

    Returns:
        Text with one ``--- Page i of n ---`` section per page. Pages with almost no
        text are reported in a warning as possibly scanned.

    Raises:
        AttachmentExtractionError: The PDF is corrupt or password-protected.
        AttachmentTooLargeError: The PDF has more than :data:`MAX_PDF_PAGES` pages.
    """
    from pypdf import PdfReader  # noqa: PLC0415 - heavy import, worker only

    notes: list[str] = []
    with warnings.catch_warnings():
        # pypdf warns on recoverable syntax problems of (untrusted) input files.
        warnings.simplefilter("ignore")
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not _decrypt_empty(reader):
                raise AttachmentExtractionError("the PDF is password-protected")
            page_count = len(reader.pages)
        except AttachmentExtractionError:
            raise
        except Exception as exc:
            raise AttachmentExtractionError(
                f"could not read the PDF ({type(exc).__name__})"
            ) from None
        if page_count > MAX_PDF_PAGES:
            raise AttachmentTooLargeError(
                f"the PDF has {page_count} pages, over the {MAX_PDF_PAGES} page limit"
            )
        texts: list[str] = []
        failed: list[int] = []
        for number, page in enumerate(reader.pages, start=1):
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")
                failed.append(number)
    if failed:
        notes.append(f"text extraction failed on pages {_page_list(failed)}")

    layout = _pdf_layout(data, page_count, notes)

    sections: list[str] = []
    scanned: list[int] = []
    candidates: list[PageSelection] = []
    for index, text in enumerate(texts):
        page_layout = layout[index] if index < len(layout) else _PageLayout((), 0.0)
        chars = len("".join(text.split()))
        if chars < SCANNED_PAGE_MIN_CHARS and not page_layout.tables:
            scanned.append(index + 1)
            candidates.append(PageSelection(index + 1, "scanned"))
        elif is_figure_page(chars, page_layout.image_coverage):
            candidates.append(PageSelection(index + 1, "figure"))
        parts = [f"--- Page {index + 1} of {page_count} ---", text.strip()]
        parts.extend(f"[Table {n}]\n{table}" for n, table in enumerate(page_layout.tables, start=1))
        sections.append("\n\n".join(p for p in parts if p))
    if scanned:
        notes.insert(
            0,
            f"{len(scanned)} of {page_count} pages have little or no extractable text "
            f"and may be scanned images (pages {_page_list(scanned)}); text-only "
            "members will not see their content",
        )
    if len(candidates) > MAX_PAGE_IMAGES:
        skipped = [c.page for c in candidates[MAX_PAGE_IMAGES:]]
        notes.append(
            f"{len(candidates)} pages hold figures or scans; only the first "
            f"{MAX_PAGE_IMAGES} are rendered as images (not rendered: pages "
            f"{_page_list(skipped)})"
        )
    return Extraction(
        text="\n\n".join(sections),
        page_count=page_count,
        warnings=tuple(notes),
        render_pages=tuple(candidates[:MAX_PAGE_IMAGES]),
    )


def is_figure_page(chars: int, image_coverage: float) -> bool:
    """The figure-page heuristic of smart page images (ADR 0007).

    A page is a figure page when its raster images (image XObjects and inline
    images, each at least :data:`FIGURE_MIN_IMAGE_PX` in both source dimensions)
    cover at least :data:`FIGURE_FULL_COVERAGE` of the page area, or at least
    :data:`FIGURE_MIN_COVERAGE` while the page has fewer than
    :data:`FIGURE_PAGE_MAX_CHARS` non-whitespace characters (low text density).
    Vector-only figures (charts drawn with paths) are not detected.

    Args:
        chars: Non-whitespace characters of the page's extracted text.
        image_coverage: Share of the page area covered by its images (0 to 1).

    Returns:
        Whether the page should be rendered as an image.
    """
    if image_coverage >= FIGURE_FULL_COVERAGE:
        return True
    return image_coverage >= FIGURE_MIN_COVERAGE and chars < FIGURE_PAGE_MAX_CHARS


def _decrypt_empty(reader: object) -> bool:
    """Try the empty user password (common for 'protected' but openable PDFs)."""
    try:
        return bool(reader.decrypt(""))  # type: ignore[attr-defined]
    except Exception:
        return False


@dataclass(frozen=True)
class _PageLayout:
    tables: tuple[str, ...]
    image_coverage: float


def _pdf_layout(data: bytes, page_count: int, notes: list[str]) -> list[_PageLayout]:
    """Return the Markdown tables and the image coverage of every page.

    On failure add a note and return ``[]`` (no tables, no figure pages).
    """
    import pdfplumber  # noqa: PLC0415 - heavy import, worker only

    result: list[_PageLayout] = []
    with warnings.catch_warnings():
        # pdfminer warns on malformed input; the text layer is already extracted.
        warnings.simplefilter("ignore")
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages[:page_count]:
                    rendered = (markdown_table(t) for t in page.extract_tables())
                    tables = tuple(t for t in rendered if t)
                    result.append(_PageLayout(tables, _image_coverage(page)))
                    page.close()
        except Exception:
            notes.append(
                "tables and figures could not be detected; tables appear as plain text only"
            )
            return []
    return result


def _image_coverage(page: object) -> float:
    """Share of the page area covered by images of at least FIGURE_MIN_IMAGE_PX."""
    try:
        width = float(page.width)  # type: ignore[attr-defined]
        height = float(page.height)  # type: ignore[attr-defined]
        images = list(page.images)  # type: ignore[attr-defined]
    except Exception:
        return 0.0
    area = width * height
    if area <= 0:
        return 0.0
    covered = 0.0
    for image in images:
        try:
            src_w, src_h = (int(v) for v in image.get("srcsize") or (0, 0))
            if min(src_w, src_h) < FIGURE_MIN_IMAGE_PX:
                continue
            x0, x1 = max(0.0, float(image["x0"])), min(width, float(image["x1"]))
            top, bottom = max(0.0, float(image["top"])), min(height, float(image["bottom"]))
        except (KeyError, TypeError, ValueError):
            continue
        covered += max(0.0, x1 - x0) * max(0.0, bottom - top)
    return min(1.0, covered / area)


def _page_list(pages: list[int]) -> str:
    shown = ", ".join(str(p) for p in pages[:_MAX_LISTED_PAGES])
    return shown + (", ..." if len(pages) > _MAX_LISTED_PAGES else "")


# --------------------------------------------------------------------------- DOCX

_HEADING_STYLE = re.compile(r"^Heading ([1-6])$")

_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_V = "urn:schemas-microsoft-com:vml"
_BLIP = f"{{{_NS_A}}}blip"
_IMAGEDATA = f"{{{_NS_V}}}imagedata"
_R_EMBED = f"{{{_NS_R}}}embed"
_R_ID = f"{{{_NS_R}}}id"

_MARK = "\x00img:{}\x00"
"""Placeholder for an image reference (XML text can never contain NUL)."""
_MARK_RE = re.compile(" ?\x00img:(\\d+)\x00")


def extract_docx(data: bytes) -> Extraction:
    """Extract paragraphs, tables (as Markdown) and embedded images in document order.

    Before parsing, every XML part is checked for a DTD (``<!DOCTYPE`` / ``<!ENTITY``),
    which OOXML never needs, so entity expansion and external entities (XXE) cannot
    occur; python-docx's own parser additionally sets ``resolve_entities=False``.
    The zip-bomb limits are applied earlier, during type detection.

    Embedded images of the body (``a:blip`` and VML ``v:imagedata`` references) are
    kept when they are PNG, JPEG, GIF or WebP, at least
    :data:`MIN_EMBEDDED_IMAGE_BYTES` and :data:`MIN_EMBEDDED_IMAGE_PX`, and within
    the size limits; the first :data:`MAX_DOCX_IMAGES` are kept. Each kept image is
    marked ``[Image i of n]`` in the text where it appears.

    Args:
        data: The raw DOCX (already detected and size-checked).

    Returns:
        The text (headings become ``#`` lines and bullet items ``- `` lines) and
        the kept images.

    Raises:
        AttachmentExtractionError: The file is corrupt or contains a DTD.
    """
    notes: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.filename.lower().endswith((".xml", ".rels")):
                    _check_xml_part(zf.read(info))
    except AttachmentExtractionError:
        raise
    except Exception as exc:
        raise AttachmentExtractionError(f"could not read the DOCX ({type(exc).__name__})") from None

    try:
        blocks, blobs = _docx_blocks(data)
    except Exception as exc:
        raise AttachmentExtractionError(f"could not read the DOCX ({type(exc).__name__})") from None
    images, numbers, skipped = _select_docx_images(blobs)
    total = len(images)

    def marker(match: re.Match[str]) -> str:
        number = numbers.get(int(match.group(1)))
        return f" [Image {number} of {total}]" if number is not None else ""

    lines = [_MARK_RE.sub(marker, block).strip() for block in blocks]
    text = "\n\n".join(line for line in lines if line)
    if images:
        notes.append(
            f"{total} embedded image(s) are sent as images to members that accept "
            "images (marked [Image i of n] in the text); text-only members see only "
            "the markers"
        )
    if skipped:
        notes.append(
            f"{skipped} embedded image(s) were skipped (icons, formats such as EMF/WMF "
            f"that models do not accept, too large, or over the limit of {MAX_DOCX_IMAGES})"
        )
    if not text.strip():
        notes.append("no text was found in the document")
    return Extraction(text=text, page_count=None, warnings=tuple(notes), images=images)


def _check_xml_part(content: bytes) -> None:
    head = content[:4]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in head:
        # Word writes UTF-8; other encodings would hide a DTD from the byte check.
        raise AttachmentExtractionError("the DOCX uses an unsupported XML encoding")
    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise AttachmentExtractionError("the DOCX contains a DTD, which is not allowed")


def _docx_blocks(data: bytes) -> tuple[list[str], list[bytes]]:
    """Text blocks (with image placeholders) and the referenced image blobs, in order."""
    import docx  # noqa: PLC0415 - heavy import, worker only
    from docx.table import Table  # noqa: PLC0415

    document = docx.Document(io.BytesIO(data))
    related = document.part.related_parts
    blobs: list[bytes] = []
    index_of: dict[str, int] = {}

    def marks(element: object) -> str:
        found: list[str] = []
        for node in element.iter():  # type: ignore[attr-defined]
            if node.tag == _BLIP:
                rid = node.get(_R_EMBED)
            elif node.tag == _IMAGEDATA:
                rid = node.get(_R_ID)
            else:
                continue
            part = related.get(rid) if rid else None
            if part is None:
                continue
            key = str(part.partname)
            if key not in index_of:
                index_of[key] = len(blobs)
                blobs.append(bytes(part.blob))
            found.append(_MARK.format(index_of[key]))
        return " ".join(found)

    blocks: list[str] = []
    for item in document.iter_inner_content():
        images = marks(item._element)  # python-docx exposes no public XML access
        if isinstance(item, Table):
            rows = [[cell.text for cell in row.cells] for row in item.rows]
            table = markdown_table(rows)
            blocks.extend(b for b in (table, images) if b)
            continue
        text = item.text.strip()
        if text:
            style = item.style.name if item.style is not None else None
            text = _paragraph_prefix(style or "") + text
        joined = " ".join(p for p in (text, images) if p)
        if joined:
            blocks.append(joined)
    return blocks, blobs


def _select_docx_images(
    blobs: Sequence[bytes],
) -> tuple[tuple[ExtractedImage, ...], dict[int, int], int]:
    """Keep the usable images; return them, blob index -> number, and the skip count."""
    kept: list[ExtractedImage] = []
    numbers: dict[int, int] = {}
    total_bytes = 0
    for position, blob in enumerate(blobs):
        usable = _usable_image(blob)
        if (
            usable is None
            or len(kept) >= MAX_DOCX_IMAGES
            or total_bytes + len(blob) > MAX_DERIVED_BYTES_PER_FILE
        ):
            continue
        file_type, (width, height) = usable
        total_bytes += len(blob)
        numbers[position] = len(kept) + 1
        kept.append(
            ExtractedImage(
                data=blob,
                media_type=file_type.value,
                width=width,
                height=height,
                reason="embedded",
                page=None,
                index=len(kept) + 1,
            )
        )
    return tuple(kept), numbers, len(blobs) - len(kept)


def _usable_image(blob: bytes) -> tuple[FileType, tuple[int, int]] | None:
    """The type and size of an embedded image a vendor accepts, else ``None``."""
    if len(blob) < MIN_EMBEDDED_IMAGE_BYTES or len(blob) > MAX_DERIVED_IMAGE_BYTES:
        return None
    try:
        file_type = detect_file_type(blob)
        if file_type.kind is not AttachmentKind.IMAGE:
            return None
        width, height = image_size(file_type, blob)
    except AttachmentError:
        return None
    if min(width, height) < MIN_EMBEDDED_IMAGE_PX:
        return None
    if max(width, height) > MAX_DERIVED_IMAGE_EDGE_PX:
        return None
    return file_type, (width, height)


def _paragraph_prefix(style: str) -> str:
    if style == "Title":
        return "# "
    match = _HEADING_STYLE.match(style)
    if match:
        return "#" * int(match.group(1)) + " "
    if style.startswith("List Bullet"):
        return "- "
    return ""
