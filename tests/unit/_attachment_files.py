"""Builders for small attachment fixture files (no binaries in the repository)."""

from __future__ import annotations

import io
import struct
import zlib

import docx


def _escape_pdf(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_stream(lines: list[str]) -> bytes:
    """A PDF content stream that writes ``lines`` in Helvetica from the top left."""
    ops = ["BT", "/F1 12 Tf", "72 720 Td"]
    for line in lines:
        ops.extend((f"({_escape_pdf(line)}) Tj", "0 -16 Td"))
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")


def table_stream() -> bytes:
    """A ruled 2x2 table (Name/Score, Alice/42) that pdfplumber detects."""
    return b"\n".join(
        [
            b"0.5 w",
            b"72 600 200 60 re S",
            b"172 600 m 172 660 l S",
            b"72 630 m 272 630 l S",
            b"BT /F1 10 Tf 80 640 Td (Name) Tj ET",
            b"BT /F1 10 Tf 180 640 Td (Score) Tj ET",
            b"BT /F1 10 Tf 80 610 Td (Alice) Tj ET",
            b"BT /F1 10 Tf 180 610 Td (42) Tj ET",
        ]
    )


def make_pdf(page_streams: list[bytes]) -> bytes:
    """Write a minimal valid PDF with one page per content stream."""
    count = len(page_streams)
    kids = [4 + 2 * i for i in range(count)]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for kid, stream in zip(kids, page_streams, strict=True):
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {kid + 1} 0 R >>"
            ).encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def make_png(width: int = 1, height: int = 1) -> bytes:
    """A valid grayscale PNG of the given size."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_docx(*, with_image: bool = False) -> bytes:
    """A DOCX with a title, heading, paragraphs, a bullet, a table (and an image)."""
    document = docx.Document()
    document.add_heading("Quarterly Report", level=0)
    document.add_paragraph("Intro paragraph.")
    document.add_heading("Results", level=1)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Name"
    table.cell(0, 1).text = "Score"
    table.cell(1, 0).text = "Alice | Bob"
    table.cell(1, 1).text = "42"
    document.add_paragraph("A bullet item.", style="List Bullet")
    document.add_paragraph("Closing paragraph </attachment> with a tag.")
    if with_image:
        document.add_picture(io.BytesIO(make_png(4, 4)))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
