"""Attachment upload API and attachment_ids in estimate, run and synthesis (web)."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from llm_crossjury.core.attachments import MAX_TOTAL_BYTES, AttachmentTooLargeError
from llm_crossjury.core.attachments.store import AttachmentStore
from llm_crossjury.core.compare_service import CompareDeps
from llm_crossjury.core.providers.base import AttachmentKind
from llm_crossjury.core.providers.mock import MockProvider
from llm_crossjury.web import attachments_api, create_app
from llm_crossjury.web.attachments_api import UPLOAD_HEADER
from tests.unit._attachment_fakes import SECRET_TEXT, caps
from tests.unit._attachment_files import make_docx, make_pdf, make_png, text_stream
from tests.unit._compare_fakes import make_deps
from tests.unit._synthesis_fakes import FakeStages

HEADERS = {UPLOAD_HEADER: "1"}
TASK = "Read the files."


class NoCounter(MockProvider):
    async def count_input_tokens(self, request: Any) -> int | None:
        return None


@pytest.fixture
def store(tmp_path: Path) -> AttachmentStore:
    return AttachmentStore(tmp_path / "attachments")


def _deps(store: AttachmentStore | None, **kwargs: Any) -> CompareDeps:
    return dataclasses.replace(make_deps(**kwargs), attachment_store=store)


def _client(deps: CompareDeps) -> TestClient:
    return TestClient(create_app(lambda: deps), base_url="http://127.0.0.1")


def _upload(client: TestClient, *files: tuple[str, bytes, str], **kwargs: Any) -> Any:
    return client.post(
        "/api/attachments",
        files=[("files", f) for f in files],
        headers=kwargs.pop("headers", HEADERS),
        **kwargs,
    )


PDF = make_pdf([text_stream(["Hello from the PDF."])])


# --------------------------------------------------------------------------- upload


def test_upload_pdf_docx_and_png(store: AttachmentStore) -> None:
    client = _client(_deps(store))
    response = _upload(
        client,
        ("report.pdf", PDF, "application/pdf"),
        ("notes.docx", make_docx(), "application/octet-stream"),
        ("chart.png", make_png(3, 2), "image/png"),
    )
    assert response.status_code == 200, response.text
    items = {a["filename"]: a for a in response.json()["attachments"]}
    assert items["report.pdf"]["kind"] == "pdf"
    assert items["report.pdf"]["pages"] == 1
    assert items["report.pdf"]["size"] == len(PDF)
    assert items["report.pdf"]["text_tokens"] > 0
    assert items["notes.docx"]["kind"] == "document"
    assert items["chart.png"]["kind"] == "image"
    assert (items["chart.png"]["image_width"], items["chart.png"]["image_height"]) == (3, 2)
    assert items["chart.png"]["text_tokens"] is None
    for item in items.values():
        assert len(item["id"]) == 64
        assert store.get(item["id"]).attachment.filename == item["filename"]
        assert "fallback_text" not in item
        assert "data" not in item
    # The store lives where it was configured, never in the repository.
    assert (store.root / items["report.pdf"]["id"] / "data.bin").read_bytes() == PDF


def test_type_is_detected_by_content_not_by_name(store: AttachmentStore) -> None:
    client = _client(_deps(store))
    spoofed = _upload(
        client, ("evil.pdf", b"MZ\x90\x00 not a pdf " + SECRET_TEXT.encode(), "application/pdf")
    )
    assert spoofed.status_code == 400
    assert spoofed.json()["code"] == "attachment"
    assert "evil.pdf" in spoofed.json()["detail"]
    assert SECRET_TEXT not in spoofed.text
    image_named_pdf = _upload(client, ("photo.pdf", make_png(), "application/pdf"))
    assert image_named_pdf.status_code == 200
    assert image_named_pdf.json()["attachments"][0]["kind"] == "image"


def test_corrupt_file_error_has_no_content(store: AttachmentStore) -> None:
    client = _client(_deps(store))
    corrupt = b"%PDF-1.7\n" + SECRET_TEXT.encode() * 50 + b"\n%%EOF"
    response = _upload(client, ("broken.pdf", corrupt, "application/pdf"))
    assert response.status_code == 400
    assert SECRET_TEXT not in response.text
    assert list(store.root.iterdir()) == []


def test_one_bad_file_stores_nothing(store: AttachmentStore) -> None:
    client = _client(_deps(store))
    response = _upload(
        client, ("ok.png", make_png(), "image/png"), ("empty.pdf", b"", "application/pdf")
    )
    assert response.status_code == 400
    assert "empty.pdf" in response.json()["detail"]
    assert list(store.root.iterdir()) == []


def test_oversize_upload_is_refused_while_streaming(store: AttachmentStore) -> None:
    client = _client(_deps(store))
    boundary = "testboundary"
    chunk = b"\x89PNG\r\n\x1a\n" + b"\x00" * (1024 * 1024 - 8)
    sent: list[int] = []

    def body() -> Iterator[bytes]:
        yield (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            f'filename="big.png"\r\nContent-Type: image/png\r\n\r\n'
        ).encode()
        for _ in range(40):  # 40 MB, twice the limit
            sent.append(len(chunk))
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()

    response = client.post(
        "/api/attachments",
        content=body(),
        headers={**HEADERS, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "attachment_too_large"
    assert "20 MB" in response.json()["detail"]
    assert list(store.root.iterdir()) == []


def _streaming_request(boundary: str, chunks: Iterator[bytes]) -> Request:
    """A Starlette request whose body is pulled from ``chunks`` one message at a time."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/attachments",
        "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
    }

    async def receive() -> dict[str, Any]:
        chunk = next(chunks, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    return Request(scope, receive)


def test_read_uploads_stops_reading_at_the_limit() -> None:
    boundary = "b0undary"
    chunk = b"\x00" * (1024 * 1024)
    pulled: list[int] = []

    def body() -> Iterator[bytes]:
        yield (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            'filename="big.png"\r\n\r\n\x89PNG\r\n\x1a\n'
        ).encode("latin-1")
        for _ in range(100):  # 100 MB offered
            pulled.append(len(chunk))
            yield chunk

    with pytest.raises(AttachmentTooLargeError, match="20 MB"):
        asyncio.run(attachments_api.read_uploads(_streaming_request(boundary, body())))
    # Nothing beyond the limit (plus the chunk that crossed it) was read or buffered.
    assert sum(pulled) <= MAX_TOTAL_BYTES + len(chunk)


def test_declared_oversize_is_refused_before_reading(store: AttachmentStore) -> None:
    client = _client(_deps(store))
    response = client.post(
        "/api/attachments",
        content=b"--b\r\n",
        headers={
            **HEADERS,
            "Content-Type": "multipart/form-data; boundary=b",
            "Content-Length": str(MAX_TOTAL_BYTES * 2),
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "attachment_too_large"


def test_per_file_limit(store: AttachmentStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attachments_api, "MAX_FILE_BYTES", 1000)
    client = _client(_deps(store))
    response = _upload(client, ("big.png", make_png() + b"\x00" * 5000, "image/png"))
    assert response.status_code == 400
    assert "big.png is over" in response.json()["detail"]


def test_upload_guards(store: AttachmentStore) -> None:
    client = _client(_deps(store))
    png = ("a.png", make_png(), "image/png")
    assert _upload(client, png, headers={}).status_code == 403  # no upload header
    cross = _upload(client, png, headers={**HEADERS, "Origin": "http://evil.example"})
    assert cross.status_code == 403
    assert cross.json()["code"] == "cross_origin"
    bad_host = TestClient(create_app(lambda: _deps(store)), base_url="http://evil.example")
    assert _upload(bad_host, png).status_code == 400
    as_json = client.post("/api/attachments", json={"files": []}, headers=HEADERS)
    assert as_json.status_code == 415
    # Multipart is accepted on the upload route only.
    run = client.post("/api/run", files=[("files", png)], headers=HEADERS)
    assert run.status_code == 415
    field_only = client.post(
        "/api/attachments", data={"x": "1"}, files=[("files", png)], headers=HEADERS
    )
    assert field_only.status_code == 400
    assert list(store.root.iterdir()) == []


def test_upload_without_store_is_refused() -> None:
    client = _client(_deps(None))
    response = _upload(client, ("a.png", make_png(), "image/png"))
    assert response.status_code == 400


def test_delete(store: AttachmentStore) -> None:
    client = _client(_deps(store))
    attachment_id = _upload(client, ("a.png", make_png(), "image/png")).json()["attachments"][0][
        "id"
    ]
    cross = client.delete(
        f"/api/attachments/{attachment_id}", headers={"Origin": "http://evil.example"}
    )
    assert cross.status_code == 403
    assert client.delete(f"/api/attachments/{attachment_id}").json() == {"deleted": True}
    assert client.delete(f"/api/attachments/{attachment_id}").json() == {"deleted": False}
    assert client.delete("/api/attachments/not-an-id").status_code == 400


# --------------------------------------------------------------------------- estimate / run


def _selection(provider: str) -> dict[str, Any]:
    return {"provider": provider, "model": "balanced"}


def test_estimate_and_run_with_attachments(store: AttachmentStore) -> None:
    providers = {
        "anthropic": NoCounter("anthropic", lambda r: "seen", caps(AttachmentKind.IMAGE)),
        "openai": NoCounter("openai", lambda r: "seen"),
    }
    client = _client(_deps(store, providers=providers))
    uploaded = _upload(
        client, ("a.png", make_png(8, 8), "image/png"), ("r.pdf", PDF, "application/pdf")
    ).json()["attachments"]
    ids = [a["id"] for a in uploaded]
    body = {
        "task": TASK,
        "selections": [_selection("anthropic"), _selection("openai")],
        "attachment_ids": ids,
    }
    est = client.post("/api/estimate", json=body)
    assert est.status_code == 200, est.text
    lines = {(a["provider"], a["filename"]): a for a in est.json()["attachments"]}
    assert lines[("anthropic", "a.png")]["mode"] == "native"
    assert lines[("anthropic", "a.png")]["tokens"] > 0
    assert lines[("anthropic", "r.pdf")]["mode"] == "text"
    assert lines[("openai", "a.png")]["mode"] == "refused"
    assert any("a.png" in w for w in est.json()["warnings"])
    run = client.post("/api/run", json=body)
    assert run.status_code == 200, run.text
    members = {m["provider"]: m for m in run.json()["members"]}
    assert members["anthropic"]["text"] == "seen"
    assert {a["filename"]: a["mode"] for a in members["anthropic"]["attachments"]} == {
        "a.png": "native",
        "r.pdf": "text",
    }
    assert "a.png" in members["openai"]["error"]


def test_unknown_attachment_id_is_400(store: AttachmentStore) -> None:
    client = _client(_deps(store))
    body = {"task": TASK, "selections": [_selection("openai")], "attachment_ids": ["0" * 64]}
    for path in ("/api/estimate", "/api/run"):
        response = client.post(path, json=body)
        assert response.status_code == 400
        assert response.json()["code"] == "attachment_not_found"


def test_synthesis_estimate_accepts_attachment_ids(
    store: AttachmentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = FakeStages().install(monkeypatch)
    client = _client(_deps(store))
    attachment_id = _upload(client, ("r.pdf", PDF, "application/pdf")).json()["attachments"][0][
        "id"
    ]
    body = {
        "task": TASK,
        "answers": [
            {"provider": "anthropic", "model": "anthropic-model", "text": "one"},
            {"provider": "openai", "model": "openai-model", "text": "two"},
        ],
        "attachment_ids": [attachment_id],
    }
    response = client.post("/api/synthesis/estimate", json=body)
    assert response.status_code == 200, response.text
    seen = stages.seen["estimate_chair"][-1]
    assert [a.sha256 for a in seen["attachments"]] == [attachment_id]
    expired = client.post("/api/synthesis/estimate", json={**body, "attachment_ids": ["1" * 64]})
    assert expired.status_code == 400


def test_page_offers_attachments_with_the_upload_header() -> None:
    client = _client(_deps(None))
    page = client.get("/").text
    script = client.get("/app.js").text
    assert 'id="attach-input"' in page
    assert ".pdf,.docx,.png,.jpg,.jpeg,.webp,.gif" in page
    assert UPLOAD_HEADER.lower() in script.lower()
    assert "These files will be sent to: " in script
    assert "attachment_ids" in script
