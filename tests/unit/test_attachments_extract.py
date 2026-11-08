"""PDF and DOCX text extraction (run in-process here; isolation is tested separately)."""

from __future__ import annotations

import io
import warnings
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter

from llm_crossjury.core.attachments import extract
from llm_crossjury.core.attachments.detect import FileType
from llm_crossjury.core.attachments.extract import (
    extract_docx,
    extract_pdf,
    extract_text,
    markdown_table,
)
from llm_crossjury.core.attachments.limits import (
    AttachmentExtractionError,
    AttachmentTooLargeError,
    UnsupportedAttachmentError,
)
from tests.unit._attachment_files import make_docx, make_pdf, table_stream, text_stream

LONG = "This page has a proper text layer with enough characters."


def test_markdown_table() -> None:
    table = markdown_table([["a", "b|c"], ["multi\nline", None, "extra"]])
    assert table == "| a | b\\|c |  |\n|---|---|---|\n| multi line |  | extra |"
    assert markdown_table([[None, ""], []]) == ""


# --------------------------------------------------------------------------- PDF


def test_pdf_pages_and_tables() -> None:
    data = make_pdf([text_stream([LONG, "Second line"]), table_stream()])
    result = extract_pdf(data)
    assert result.page_count == 2
    assert result.warnings == ()
    text = result.text
    assert text.startswith("--- Page 1 of 2 ---\n\n" + LONG)
    assert "Second line" in text
    assert "--- Page 2 of 2 ---" in text
    assert "[Table 1]\n| Name | Score |\n|---|---|\n| Alice | 42 |" in text
    assert text.index("Page 1 of 2") < text.index("Page 2 of 2") < text.index("[Table 1]")


def test_pdf_scanned_pages_warn() -> None:
    data = make_pdf([text_stream([LONG]), b"", text_stream(["tiny"])])
    result = extract_pdf(data)
    assert len(result.warnings) == 1
    assert "2 of 3 pages" in result.warnings[0]
    assert "pages 2, 3" in result.warnings[0]
    assert "--- Page 2 of 3 ---" in result.text


def test_pdf_page_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "MAX_PDF_PAGES", 2)
    with pytest.raises(AttachmentTooLargeError, match="3 pages"):
        extract_pdf(make_pdf([b"", b"", b""]))


def test_pdf_corrupt() -> None:
    with pytest.raises(AttachmentExtractionError, match="could not read the PDF"):
        extract_pdf(b"%PDF-1.4\nthis is not a pdf body")


def test_pdf_password_protected() -> None:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf([text_stream([LONG])]))))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        writer.encrypt("secret", algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(AttachmentExtractionError, match="password-protected"):
        extract_pdf(buffer.getvalue())


# --------------------------------------------------------------------------- DOCX


def test_docx_document_order_and_markdown() -> None:
    result = extract_docx(make_docx())
    assert result.page_count is None
    assert result.warnings == ()
    assert result.text.split("\n\n") == [
        "# Quarterly Report",
        "Intro paragraph.",
        "# Results",
        "| Name | Score |\n|---|---|\n| Alice \\| Bob | 42 |",
        "- A bullet item.",
        "Closing paragraph </attachment> with a tag.",
    ]


def test_docx_images_dropped_with_warning() -> None:
    result = extract_docx(make_docx(with_image=True))
    assert any("1 embedded image(s) were dropped" in w for w in result.warnings)
    assert "Closing paragraph" in result.text


def _replace_member(data: bytes, name: str, body: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info.filename, body if info.filename == name else source.read(info))
    return buffer.getvalue()


XXE = (
    b'<?xml version="1.0"?>\n<!DOCTYPE w [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n'
    b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    b"<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>"
)


@pytest.mark.parametrize("member", ["word/document.xml", "[Content_Types].xml", "_rels/.rels"])
def test_docx_rejects_dtd(member: str) -> None:
    data = _replace_member(make_docx(), member, XXE)
    with pytest.raises(AttachmentExtractionError, match="DTD"):
        extract_docx(data)


def test_docx_rejects_utf16_xml() -> None:
    data = _replace_member(make_docx(), "word/document.xml", XXE.decode().encode("utf-16"))
    with pytest.raises(AttachmentExtractionError, match="encoding"):
        extract_docx(data)


def test_docx_corrupt_document_part() -> None:
    data = _replace_member(make_docx(), "word/document.xml", b"<not-closed")
    with pytest.raises(AttachmentExtractionError, match="could not read the DOCX"):
        extract_docx(data)


# --------------------------------------------------------------------------- dispatch


def test_extract_text_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "MAX_TEXT_CHARS", 30)
    result = extract_text(FileType.DOCX.value, make_docx())
    assert len(result.text) == 30
    assert "truncated" in result.warnings[-1]


def test_extract_text_rejects_images() -> None:
    with pytest.raises(UnsupportedAttachmentError):
        extract_text(FileType.PNG.value, b"\x89PNG")
