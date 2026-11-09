"""Builders for small attachment fixture files (no binaries in the repository)."""

from __future__ import annotations

import io
import random
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


# --------------------------------------------------------------------------- images


def noise(width: int, height: int, seed: int = 0) -> bytes:
    """Deterministic random 8-bit gray pixels (compress badly, like a photo)."""
    return random.Random(seed).randbytes(width * height)  # noqa: S311 - test pixels


def make_noise_png(width: int = 64, height: int = 64, seed: int = 0) -> bytes:
    """A grayscale PNG of random pixels (64x64 is about 4 KB)."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    pixels = noise(width, height, seed)
    raw = b"".join(b"\x00" + pixels[y * width : (y + 1) * width] for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_bmp(width: int = 64, height: int = 64) -> bytes:
    """A 24-bit BMP (a format vendors do not accept)."""
    row = (width * 3 + 3) & ~3
    pixels = b"".join(
        bytes((x * 4 % 256, y * 4 % 256, 128)) for y in range(height) for x in range(width)
    )
    rows = b"".join(
        pixels[y * width * 3 : (y + 1) * width * 3].ljust(row, b"\x00") for y in range(height)
    )
    header = struct.pack("<2sIHHI", b"BM", 54 + len(rows), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(rows), 2835, 2835, 0, 0)
    return header + info + rows


def make_docx_with_images(images: list[bytes]) -> bytes:
    """A DOCX with ``Paragraph i.`` followed by picture ``i`` for every image."""
    document = docx.Document()
    document.add_heading("Figures", level=1)
    for number, image in enumerate(images, start=1):
        document.add_paragraph(f"Paragraph {number}.")
        document.add_picture(io.BytesIO(image))
    document.add_paragraph("The end.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


ImagePlacement = tuple[int, int, int, int, int, int]
"""``(source width, source height, x, y, width, height)``; position and size in points."""


def make_image_pdf(pages: list[tuple[list[str], ImagePlacement | None]]) -> bytes:
    """A Letter-size PDF; each page has text lines and optionally one gray image."""
    objects: list[bytes] = [
        b"",  # catalog, filled in below
        b"",  # page tree, filled in below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids: list[int] = []
    for number, (lines, image) in enumerate(pages):
        stream = text_stream(lines) if lines else b""
        resources = "/Font << /F1 3 0 R >>"
        if image is not None:
            src_w, src_h, x, y, w, h = image
            data = zlib.compress(noise(src_w, src_h, seed=number))
            objects.append(
                (
                    f"<< /Type /XObject /Subtype /Image /Width {src_w} /Height {src_h} "
                    f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
                    f"/Length {len(data)} >>\nstream\n"
                ).encode()
                + data
                + b"\nendstream"
            )
            resources += f" /XObject << /Im1 {len(objects)} 0 R >>"
            stream += f"\nq {w} 0 0 {h} {x} {y} cm /Im1 Do Q".encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content = len(objects)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << {resources} >> /Contents {content} 0 R >>"
            ).encode()
        )
        kids.append(len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    ).encode()
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


FULL_FIGURE: ImagePlacement = (200, 150, 81, 300, 450, 340)
"""A figure covering about 32% of a Letter page."""
SMALL_FIGURE: ImagePlacement = (120, 90, 100, 100, 200, 150)
"""A figure covering about 6% of the page (a figure only on a page with little text)."""
LOGO: ImagePlacement = (64, 64, 500, 740, 30, 30)
"""A small logo (well under 1% of the page): never a figure."""
SCAN: ImagePlacement = (300, 400, 0, 0, 612, 792)
"""A full-page raster image without text: a scanned page."""
