"""Temporary attachment store: round trip, expiry, ids, damage and permissions."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from llm_crossjury.core.attachments.store import (
    ATTACHMENT_TTL_S,
    AttachmentNotFoundError,
    AttachmentStore,
    is_attachment_id,
)
from llm_crossjury.core.providers.base import AttachmentKind
from tests.unit._attachment_fakes import built


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_put_and_get_round_trip(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "att")
    item = built(AttachmentKind.PDF, name="report.pdf", warnings=("page 2 may be scanned",))
    attachment_id = store.put(item)
    assert attachment_id == item.attachment.sha256
    assert is_attachment_id(attachment_id)
    assert store.get(attachment_id) == item
    image = built(AttachmentKind.IMAGE, name="x.png", payload=b"\x89PNG\r\n\x1a\n\xff\x00binary")
    assert store.get(store.put(image)) == image


def test_entries_expire_after_ttl(tmp_path: Path) -> None:
    clock = Clock()
    store = AttachmentStore(tmp_path, clock=clock)
    old = store.put(built(AttachmentKind.PDF, name="old.pdf"))
    clock.now += ATTACHMENT_TTL_S - 1
    assert store.get(old).attachment.filename == "old.pdf"
    clock.now += 2
    with pytest.raises(AttachmentNotFoundError, match="expired"):
        store.get(old)
    assert not (tmp_path / old).exists()


def test_upload_purges_expired_entries(tmp_path: Path) -> None:
    clock = Clock()
    store = AttachmentStore(tmp_path, clock=clock)
    old = store.put(built(AttachmentKind.PDF, name="old.pdf"))
    clock.now += ATTACHMENT_TTL_S + 1
    store.put(built(AttachmentKind.DOCUMENT, name="new.docx"))
    assert not (tmp_path / old).exists()


def test_startup_purges_expired_entries(tmp_path: Path) -> None:
    clock = Clock()
    old = AttachmentStore(tmp_path, clock=clock).put(built(AttachmentKind.PDF, name="a.pdf"))
    second = AttachmentStore(tmp_path, clock=clock).put(built(AttachmentKind.PDF, name="b.pdf"))
    clock.now += ATTACHMENT_TTL_S / 2
    AttachmentStore(tmp_path, clock=clock)  # nothing expired yet
    assert (tmp_path / old).exists()
    clock.now += ATTACHMENT_TTL_S / 2 + 1
    AttachmentStore(tmp_path, clock=clock)  # "start-up" of a new process
    assert not (tmp_path / old).exists()
    assert not (tmp_path / second).exists()
    (tmp_path / "unrelated.txt").write_text("kept", encoding="utf-8")
    AttachmentStore(tmp_path, clock=clock)
    assert (tmp_path / "unrelated.txt").exists()


def test_reupload_restarts_the_lifetime(tmp_path: Path) -> None:
    clock = Clock()
    store = AttachmentStore(tmp_path, clock=clock)
    item = built(AttachmentKind.PDF, name="a.pdf")
    store.put(item)
    clock.now += ATTACHMENT_TTL_S - 10
    store.put(item)
    clock.now += 20
    assert store.get(item.attachment.sha256) == item


@pytest.mark.parametrize("bad", ["", "../../etc/passwd", "A" * 64, "0" * 63, "..", "g" * 64])
def test_invalid_ids_are_rejected(tmp_path: Path, bad: str) -> None:
    store = AttachmentStore(tmp_path)
    with pytest.raises(AttachmentNotFoundError, match="invalid attachment id"):
        store.get(bad)
    with pytest.raises(AttachmentNotFoundError):
        store.delete(bad)


def test_unknown_id_and_delete(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    with pytest.raises(AttachmentNotFoundError, match="not found"):
        store.get("0" * 64)
    attachment_id = store.put(built(AttachmentKind.PDF, name="a.pdf"))
    assert store.delete(attachment_id) is True
    assert store.delete(attachment_id) is False
    with pytest.raises(AttachmentNotFoundError):
        store.get(attachment_id)


def test_damaged_entry_is_deleted(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    attachment_id = store.put(built(AttachmentKind.PDF, name="a.pdf"))
    (tmp_path / attachment_id / "data.bin").write_bytes(b"tampered")
    with pytest.raises(AttachmentNotFoundError) as info:
        store.get(attachment_id)
    assert "tampered" not in str(info.value)
    assert not (tmp_path / attachment_id).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_entries_are_owner_only(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "att")
    attachment_id = store.put(built(AttachmentKind.PDF, name="a.pdf"))
    entry = tmp_path / "att" / attachment_id
    assert stat.S_IMODE(os.stat(tmp_path / "att").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(entry).st_mode) == 0o700
    for name in ("data.bin", "meta.json"):
        assert stat.S_IMODE(os.stat(entry / name).st_mode) == 0o600
