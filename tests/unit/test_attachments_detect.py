"""Magic-byte detection, image header parsing and file name sanitizing."""

from __future__ import annotations

import io
import struct
import zipfile

import pytest
from PIL import Image

from llm_crossjury.core.attachments import detect
from llm_crossjury.core.attachments.detect import (
    FileType,
    detect_file_type,
    image_size,
    sanitize_filename,
)
from llm_crossjury.core.attachments.limits import (
    MAX_FILENAME_CHARS,
    AttachmentExtractionError,
    AttachmentTooLargeError,
    UnsupportedAttachmentError,
)
from llm_crossjury.core.providers.base import AttachmentKind
from tests.unit._attachment_files import make_docx, make_pdf, make_png, text_stream


def _pillow(fmt: str, size: tuple[int, int], mode: str = "RGB", **kwargs: object) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size).save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buffer.getvalue()


# --------------------------------------------------------------------------- types


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (make_png(), FileType.PNG),
        (_pillow("JPEG", (3, 2)), FileType.JPEG),
        (_pillow("GIF", (3, 2), mode="P"), FileType.GIF),
        (_pillow("WEBP", (3, 2)), FileType.WEBP),
        (make_pdf([text_stream(["x"])]), FileType.PDF),
        (b"junk\n%PDF-1.7\n", FileType.PDF),
        (make_docx(), FileType.DOCX),
    ],
)
def test_detects_by_magic_bytes(data: bytes, expected: FileType) -> None:
    assert detect_file_type(data) is expected


def test_kinds() -> None:
    assert FileType.PDF.kind is AttachmentKind.PDF
    assert FileType.DOCX.kind is AttachmentKind.DOCUMENT
    assert FileType.WEBP.kind is AttachmentKind.IMAGE


@pytest.mark.parametrize(
    "data",
    [
        b"plain text pretending to be a pdf",
        b"MZ\x90\x00 executable",
        _zip({"xl/workbook.xml": b"<x/>"}),  # XLSX, not DOCX
        b"PK\x03\x04 not really a zip",
        b"RIFF\x00\x00\x00\x00WAVE",
        b"\x00" * 64,
    ],
)
def test_rejects_unsupported(data: bytes) -> None:
    with pytest.raises(UnsupportedAttachmentError, match="supported types"):
        detect_file_type(data)


def test_docx_member_count_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect, "MAX_ZIP_MEMBERS", 3)
    data = _zip({"word/document.xml": b"<x/>", "a": b"", "b": b"", "c": b""})
    with pytest.raises(AttachmentTooLargeError, match="too many parts"):
        detect_file_type(data)


def test_docx_uncompressed_size_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect, "MAX_ZIP_UNCOMPRESSED_BYTES", 10_000)
    data = _zip({"word/document.xml": b"\x00" * 20_000})
    assert len(data) < 1_000  # highly compressible: what a zip bomb looks like
    with pytest.raises(AttachmentTooLargeError, match="expands to"):
        detect_file_type(data)


# --------------------------------------------------------------------------- images


@pytest.mark.parametrize(
    ("file_type", "data"),
    [
        (FileType.PNG, make_png(37, 21)),
        (FileType.JPEG, _pillow("JPEG", (37, 21))),
        (FileType.JPEG, _pillow("JPEG", (37, 21), progressive=True)),
        (FileType.GIF, _pillow("GIF", (37, 21), mode="P")),
        (FileType.WEBP, _pillow("WEBP", (37, 21))),  # lossy VP8
        (FileType.WEBP, _pillow("WEBP", (37, 21), lossless=True)),  # VP8L
        (FileType.WEBP, _pillow("WEBP", (37, 21), mode="RGBA", exif=b"Exif\x00\x00")),
    ],
)
def test_image_size(file_type: FileType, data: bytes) -> None:
    assert image_size(file_type, data) == (37, 21)


def test_webp_vp8x_large_canvas() -> None:
    header = b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X" + struct.pack("<I", 10)
    body = b"\x00" * 4 + (5000 - 1).to_bytes(3, "little") + (3000 - 1).to_bytes(3, "little")
    assert image_size(FileType.WEBP, header + body) == (5000, 3000)


@pytest.mark.parametrize(
    ("file_type", "data"),
    [
        (FileType.PNG, b"\x89PNG\r\n\x1a\n"),
        (FileType.PNG, make_png()[:20]),
        (FileType.JPEG, b"\xff\xd8\xff\xe0\x00"),
        (FileType.JPEG, b"\xff\xd8\xff\xda\x00\x02"),
        (FileType.GIF, b"GIF89a\x00\x00\x00\x00"),
        (FileType.WEBP, b"RIFF\x00\x00\x00\x00WEBPXXXX"),
    ],
)
def test_malformed_image_header(file_type: FileType, data: bytes) -> None:
    with pytest.raises(AttachmentExtractionError, match="image header"):
        image_size(file_type, data)


# --------------------------------------------------------------------------- names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\secret.docx", "secret.docx"),
        ("a\x00b\x1f\nc.png", "abc.png"),
        ("evil\u202egnp.exe", "evilgnp.exe"),  # bidi override removed
        ('say "hi" <x>.pdf', "say _hi_ _x_.pdf"),
        ("..", "attachment"),
        ("", "attachment"),
        ("dir/", "attachment"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_caps_length_keeping_extension() -> None:
    name = sanitize_filename("x" * 500 + ".pdf")
    assert len(name) == MAX_FILENAME_CHARS
    assert name.endswith(".pdf")
    assert len(sanitize_filename("y" * 500)) == MAX_FILENAME_CHARS
