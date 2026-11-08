"""Text extraction from PDF and DOCX files.

These functions process untrusted input. :func:`build_attachment` runs them in a
separate worker process with a wall-clock limit (see ``isolate.py``); call them
directly only in tests. They never log file content.
"""

from __future__ import annotations

import io
import re
import warnings
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass

from llm_crossjury.core.attachments.detect import FileType
from llm_crossjury.core.attachments.limits import (
    MAX_PDF_PAGES,
    MAX_TEXT_CHARS,
    SCANNED_PAGE_MIN_CHARS,
    AttachmentExtractionError,
    AttachmentTooLargeError,
    UnsupportedAttachmentError,
)

_MAX_LISTED_PAGES = 20


@dataclass(frozen=True)
class Extraction:
    """Text extracted from a document, plus what the user should know about it."""

    text: str
    page_count: int | None
    warnings: tuple[str, ...]


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

    tables = _pdf_tables(data, page_count, notes)

    sections: list[str] = []
    scanned: list[int] = []
    for index, text in enumerate(texts):
        page_tables = tables[index] if index < len(tables) else []
        if len("".join(text.split())) < SCANNED_PAGE_MIN_CHARS and not page_tables:
            scanned.append(index + 1)
        parts = [f"--- Page {index + 1} of {page_count} ---", text.strip()]
        parts.extend(f"[Table {n}]\n{table}" for n, table in enumerate(page_tables, start=1))
        sections.append("\n\n".join(p for p in parts if p))
    if scanned:
        notes.insert(
            0,
            f"{len(scanned)} of {page_count} pages have little or no extractable text "
            f"and may be scanned images (pages {_page_list(scanned)}); text-only "
            "members will not see their content",
        )
    return Extraction(text="\n\n".join(sections), page_count=page_count, warnings=tuple(notes))


def _decrypt_empty(reader: object) -> bool:
    """Try the empty user password (common for 'protected' but openable PDFs)."""
    try:
        return bool(reader.decrypt(""))  # type: ignore[attr-defined]
    except Exception:
        return False


def _pdf_tables(data: bytes, page_count: int, notes: list[str]) -> list[list[str]]:
    """Return Markdown tables per page; on failure add a note and return ``[]``."""
    import pdfplumber  # noqa: PLC0415 - heavy import, worker only

    result: list[list[str]] = []
    with warnings.catch_warnings():
        # pdfminer warns on malformed input; the text layer is already extracted.
        warnings.simplefilter("ignore")
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages[:page_count]:
                    rendered = (markdown_table(t) for t in page.extract_tables())
                    result.append([t for t in rendered if t])
                    page.close()
        except Exception:
            notes.append("tables could not be extracted; they appear as plain text only")
            return []
    return result


def _page_list(pages: list[int]) -> str:
    shown = ", ".join(str(p) for p in pages[:_MAX_LISTED_PAGES])
    return shown + (", ..." if len(pages) > _MAX_LISTED_PAGES else "")


# --------------------------------------------------------------------------- DOCX

_HEADING_STYLE = re.compile(r"^Heading ([1-6])$")


def extract_docx(data: bytes) -> Extraction:
    """Extract paragraphs and tables (as Markdown) in document order.

    Before parsing, every XML part is checked for a DTD (``<!DOCTYPE`` / ``<!ENTITY``),
    which OOXML never needs, so entity expansion and external entities (XXE) cannot
    occur; python-docx's own parser additionally sets ``resolve_entities=False``.
    The zip-bomb limits are applied earlier, during type detection.

    Args:
        data: The raw DOCX (already detected and size-checked).

    Returns:
        The text; headings become ``#`` lines and bullet items ``- `` lines. Embedded
        images are dropped with a warning.

    Raises:
        AttachmentExtractionError: The file is corrupt or contains a DTD.
    """
    notes: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            media = 0
            for info in zf.infolist():
                name = info.filename.lower()
                if name.startswith("word/media/"):
                    media += 1
                if name.endswith((".xml", ".rels")):
                    _check_xml_part(zf.read(info))
    except AttachmentExtractionError:
        raise
    except Exception as exc:
        raise AttachmentExtractionError(f"could not read the DOCX ({type(exc).__name__})") from None
    if media:
        notes.append(f"{media} embedded image(s) were dropped; only text and tables are used")

    try:
        blocks = _docx_blocks(data)
    except Exception as exc:
        raise AttachmentExtractionError(f"could not read the DOCX ({type(exc).__name__})") from None
    text = "\n\n".join(blocks)
    if not text.strip():
        notes.append("no text was found in the document")
    return Extraction(text=text, page_count=None, warnings=tuple(notes))


def _check_xml_part(content: bytes) -> None:
    head = content[:4]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in head:
        # Word writes UTF-8; other encodings would hide a DTD from the byte check.
        raise AttachmentExtractionError("the DOCX uses an unsupported XML encoding")
    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise AttachmentExtractionError("the DOCX contains a DTD, which is not allowed")


def _docx_blocks(data: bytes) -> list[str]:
    import docx  # noqa: PLC0415 - heavy import, worker only
    from docx.table import Table  # noqa: PLC0415

    document = docx.Document(io.BytesIO(data))
    blocks: list[str] = []
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            rows = [[cell.text for cell in row.cells] for row in item.rows]
            table = markdown_table(rows)
            if table:
                blocks.append(table)
            continue
        text = item.text.strip()
        if not text:
            continue
        style = item.style.name if item.style is not None else None
        blocks.append(_paragraph_prefix(style or "") + text)
    return blocks


def _paragraph_prefix(style: str) -> str:
    if style == "Title":
        return "# "
    match = _HEADING_STYLE.match(style)
    if match:
        return "#" * int(match.group(1)) + " "
    if style.startswith("List Bullet"):
        return "- "
    return ""
