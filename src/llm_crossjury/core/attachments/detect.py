"""File type detection, image header parsing and file name sanitizing.

Everything here is cheap and runs in the calling process: it reads only fixed-size
headers (and, for DOCX, the zip central directory), never decompresses or parses
document content.
"""

from __future__ import annotations

import io
import struct
import unicodedata
import zipfile
from enum import StrEnum

from llm_crossjury.core.attachments.limits import (
    MAX_FILENAME_CHARS,
    MAX_ZIP_MEMBERS,
    MAX_ZIP_UNCOMPRESSED_BYTES,
    AttachmentExtractionError,
    AttachmentTooLargeError,
    UnsupportedAttachmentError,
)
from llm_crossjury.core.providers.base import AttachmentKind


class FileType(StrEnum):
    """A supported file type, identified by magic bytes."""

    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"
    WEBP = "image/webp"
    GIF = "image/gif"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    @property
    def kind(self) -> AttachmentKind:
        """The contract kind for this file type."""
        if self is FileType.PDF:
            return AttachmentKind.PDF
        if self is FileType.DOCX:
            return AttachmentKind.DOCUMENT
        return AttachmentKind.IMAGE


_PDF_HEADER_WINDOW = 1024
"""PDF readers accept ``%PDF-`` anywhere in the first 1024 bytes."""

_DOCX_MAIN_PART = "word/document.xml"

SUPPORTED_TYPES_TEXT = "PDF, PNG, JPEG, WebP, GIF, DOCX"


def detect_file_type(data: bytes) -> FileType:
    """Identify ``data`` by its magic bytes (never by file name).

    Args:
        data: The raw file content.

    Returns:
        The detected file type.

    Raises:
        UnsupportedAttachmentError: The content is not a supported type.
        AttachmentTooLargeError: A DOCX exceeds the zip member/size limits.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileType.PNG
    if data.startswith(b"\xff\xd8\xff"):
        return FileType.JPEG
    if data.startswith((b"GIF87a", b"GIF89a")):
        return FileType.GIF
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return FileType.WEBP
    if b"%PDF-" in data[:_PDF_HEADER_WINDOW]:
        return FileType.PDF
    if data.startswith(b"PK\x03\x04") and _is_docx(data):
        return FileType.DOCX
    raise UnsupportedAttachmentError(
        f"unsupported file type; supported types are {SUPPORTED_TYPES_TEXT}"
    )


def _is_docx(data: bytes) -> bool:
    """Return whether ``data`` is a zip holding ``word/document.xml``.

    Also applies the zip-bomb guards, using the sizes declared in the central
    directory (nothing is decompressed here). ``zipfile`` stops reading a member at
    its declared size, so the declared total bounds the real work.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return False
    if _DOCX_MAIN_PART not in {info.filename for info in infos}:
        return False
    if len(infos) > MAX_ZIP_MEMBERS:
        raise AttachmentTooLargeError(f"DOCX has too many parts ({len(infos)} > {MAX_ZIP_MEMBERS})")
    total = sum(info.file_size for info in infos)
    if total > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise AttachmentTooLargeError(
            f"DOCX expands to {total} bytes, over the {MAX_ZIP_UNCOMPRESSED_BYTES} byte limit"
        )
    return True


# --------------------------------------------------------------------------- images


def image_size(file_type: FileType, data: bytes) -> tuple[int, int]:
    """Read an image's width and height from its header.

    Args:
        file_type: One of the image file types.
        data: The raw file content.

    Returns:
        ``(width, height)`` in pixels, both positive.

    Raises:
        AttachmentExtractionError: The header is truncated or malformed.
    """
    try:
        if file_type is FileType.PNG:
            size = _png_size(data)
        elif file_type is FileType.GIF:
            size = _gif_size(data)
        elif file_type is FileType.WEBP:
            size = _webp_size(data)
        elif file_type is FileType.JPEG:
            size = _jpeg_size(data)
        else:
            raise ValueError(file_type)
    except (struct.error, ValueError, IndexError):
        size = None
    if size is None or size[0] <= 0 or size[1] <= 0:
        raise AttachmentExtractionError(f"could not read the {file_type.name} image header")
    return size


def _png_size(data: bytes) -> tuple[int, int] | None:
    # Signature (8), IHDR length (4), "IHDR" (4), width (4), height (4); big-endian.
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _gif_size(data: bytes) -> tuple[int, int]:
    # Logical screen descriptor right after the 6-byte signature; little-endian.
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X":
        # Extended format: 24-bit canvas width-1 and height-1 at offsets 24 and 27.
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8L":
        # Lossless: signature byte 0x2f at 20, then 14-bit width-1 and height-1.
        if data[20] != 0x2F:  # noqa: PLR2004
            return None
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        # Lossy: frame tag (3) + start code 9d 01 2a at 23, then 14-bit width/height.
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    return None


# SOF markers carrying frame dimensions: C0-CF except DHT (C4), JPG (C8), DAC (CC).
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})  # TEM, RST0-7: no length


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    pos = 2
    end = len(data)
    while pos + 4 <= end:
        if data[pos] != 0xFF:  # noqa: PLR2004
            return None
        marker = data[pos + 1]
        if marker == 0xFF:  # noqa: PLR2004 - fill byte
            pos += 1
            continue
        if marker in _JPEG_STANDALONE:
            pos += 2
            continue
        if marker in {0xD9, 0xDA}:  # EOI or SOS before any SOF
            return None
        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        if marker in _JPEG_SOF:
            # Segment: length (2), precision (1), height (2), width (2).
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return width, height
        if length < 2:  # noqa: PLR2004
            return None
        pos += 2 + length
    return None


# --------------------------------------------------------------------------- names

_UNSAFE_NAME_CHARS = str.maketrans({c: "_" for c in '"<>:*?|'})
_DEFAULT_NAME = "attachment"


def sanitize_filename(filename: str) -> str:
    """Reduce ``filename`` to a safe base name.

    Drops any directory part (``/`` or ``\\``), control and format characters
    (including bidi overrides), replaces characters that are unsafe in file names or
    in the ``name="..."`` attribute of :func:`fallback_block`, and caps the length at
    :data:`MAX_FILENAME_CHARS` while keeping a short extension.

    Args:
        filename: The name supplied by the user's browser or CLI.

    Returns:
        A non-empty base name.
    """
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch) not in {"Cc", "Cf"})
    base = unicodedata.normalize("NFC", base).translate(_UNSAFE_NAME_CHARS).strip(" .")
    if not base:
        return _DEFAULT_NAME
    if len(base) > MAX_FILENAME_CHARS:
        stem, dot, ext = base.rpartition(".")
        max_ext = 10
        if dot and stem and len(ext) <= max_ext:
            base = stem[: MAX_FILENAME_CHARS - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            base = base[:MAX_FILENAME_CHARS].rstrip(" .")
    return base or _DEFAULT_NAME
