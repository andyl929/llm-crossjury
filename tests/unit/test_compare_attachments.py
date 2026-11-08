"""Compare mode with attachments: prompt layout, per-member delivery, estimates."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from llm_crossjury.core.attachments.prompt import ATTACHMENTS_HEADER
from llm_crossjury.core.attachments.render import (
    ATTACHMENT_NOTICE,
    fallback_block,
    sort_attachments,
)
from llm_crossjury.core.attachments.store import AttachmentNotFoundError
from llm_crossjury.core.attachments.tokens import estimate_attachment_tokens
from llm_crossjury.core.compare_service import (
    CompareDeps,
    Selection,
    estimate_compare_details,
    load_attachments,
    prepare_calls,
    run_compare,
)
from llm_crossjury.core.providers.base import (
    AttachmentKind,
    Backend,
    CompletionRequest,
    Role,
)
from llm_crossjury.core.providers.mock import MockProvider
from tests.unit._attachment_fakes import MemoryStore, built, caps
from tests.unit._compare_fakes import FakeCliProvider, make_deps

TASK = "Summarize the attached files."
PDF = built(AttachmentKind.PDF, name="report.pdf", warnings=("page 2 may be scanned",))
DOCX = built(AttachmentKind.DOCUMENT, name="notes.docx")
PNG = built(AttachmentKind.IMAGE, name="chart.png")


class CountingProvider(MockProvider):
    """A mock provider whose token counter returns ``count`` (``None`` = no counter)."""

    def __init__(self, name: str, count: int | None, *kinds: AttachmentKind) -> None:
        super().__init__(name, lambda req: f"{name} answer", caps(*kinds))
        self.count = count
        self.counted: list[CompletionRequest] = []

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        self.counted.append(request)
        return self.count


def _deps(store: MemoryStore, **kwargs: Any) -> CompareDeps:
    return dataclasses.replace(make_deps(**kwargs), attachment_store=store)


def _store(*items: Any) -> tuple[MemoryStore, list[str]]:
    store = MemoryStore()
    return store, store.add(*items)


# --------------------------------------------------------------------------- layout


def test_prompt_layout_puts_sorted_attachments_in_the_prefix() -> None:
    store, ids = _store(PDF, DOCX, PNG)
    deps = _deps(
        store, providers={"anthropic": CountingProvider("anthropic", None, *AttachmentKind)}
    )
    built_items = load_attachments(list(reversed(ids)), deps)
    call = prepare_calls(
        TASK, [Selection(provider="anthropic", model="balanced")], deps, attachments=built_items
    )[0]
    prompt = call.request.prompt
    system, files = prompt.stable_prefix
    assert system.role is Role.SYSTEM
    assert system.content.endswith(ATTACHMENT_NOTICE)
    assert files.role is Role.USER
    assert files.content.startswith(ATTACHMENTS_HEADER)
    expected = sort_attachments([PDF.attachment, DOCX.attachment, PNG.attachment])
    assert files.attachments == expected
    assert [line.split(" (")[0][2:] for line in files.content.splitlines()[1:]] == [
        a.filename for a in expected
    ]
    assert len(prompt.variable_suffix) == 1
    assert prompt.variable_suffix[0].content == TASK
    assert not prompt.variable_suffix[0].attachments


def test_prefix_is_identical_for_any_upload_order() -> None:
    store, ids = _store(PDF, DOCX)
    deps = _deps(store)
    sel = [Selection(provider="openai", model="balanced")]
    a = prepare_calls(TASK, sel, deps, attachments=load_attachments(ids, deps))[0]
    b = prepare_calls(TASK, sel, deps, attachments=load_attachments(ids[::-1], deps))[0]
    assert a.request.prompt.stable_prefix == b.request.prompt.stable_prefix
    assert a.plan.prefix_fingerprint == b.plan.prefix_fingerprint


def test_no_attachments_keeps_the_old_prompt() -> None:
    call = prepare_calls(TASK, [Selection(provider="openai", model="balanced")], make_deps())[0]
    assert len(call.request.prompt.stable_prefix) == 1
    assert ATTACHMENT_NOTICE not in call.request.prompt.stable_prefix[0].content
    assert call.attachments == ()


def test_unknown_or_expired_ids_are_refused() -> None:
    deps = _deps(MemoryStore())
    with pytest.raises(AttachmentNotFoundError):
        load_attachments(["0" * 64], deps)
    with pytest.raises(AttachmentNotFoundError):
        load_attachments(["0" * 64], make_deps())  # no store configured


def test_total_size_is_checked() -> None:
    big_a = built(AttachmentKind.PDF, name="a.pdf", payload=b"a" * (11 * 1024 * 1024))
    big_b = built(AttachmentKind.PDF, name="b.pdf", payload=b"b" * (11 * 1024 * 1024))
    store, ids = _store(big_a, big_b)
    with pytest.raises(ValueError, match="20 MB"):
        load_attachments(ids, _deps(store))


# --------------------------------------------------------------------------- delivery


def _run(deps: CompareDeps, ids: list[str], selections: list[Selection]) -> Any:
    return asyncio.run(
        run_compare(TASK, selections, deps, attachment_ids=ids, allow_unknown_cost=True)
    )


def test_native_text_and_refused_per_member() -> None:
    store, ids = _store(PDF, DOCX, PNG)
    vision = CountingProvider("anthropic", None, AttachmentKind.IMAGE, AttachmentKind.PDF)
    text_only = CountingProvider("openai", None)
    images_only = CountingProvider("gemini", None, AttachmentKind.IMAGE)
    deps = _deps(store, providers={"anthropic": vision, "openai": text_only, "gemini": images_only})
    result = _run(
        deps,
        ids,
        [Selection(provider=p, model="balanced") for p in ("anthropic", "openai", "gemini")],
    )
    by = {m.provider: m for m in result.members}
    modes = {p: {r.filename: r.mode for r in m.attachments} for p, m in by.items()}
    assert modes["anthropic"] == {
        "report.pdf": "native",
        "notes.docx": "text",
        "chart.png": "native",
    }
    assert modes["gemini"] == {"report.pdf": "text", "notes.docx": "text", "chart.png": "native"}
    assert modes["openai"]["chart.png"] == "refused"
    # The text-only member fails with a clear error and is never called.
    assert by["openai"].text is None
    assert "chart.png" in (by["openai"].error or "")
    assert "does not accept image" in (by["openai"].error or "")
    assert text_only.requests == []
    # The others keep running and receive the files in the prefix.
    assert by["anthropic"].error is None
    assert by["gemini"].error is None
    assert vision.requests[0].prompt.stable_prefix[1].attachments
    # Extraction warnings show only where the text is used.
    warn = {p: {r.filename: r.warnings for r in m.attachments} for p, m in by.items()}
    assert warn["gemini"]["report.pdf"] == ("page 2 may be scanned",)
    assert warn["anthropic"]["report.pdf"] == ()


def test_cli_member_gets_inlined_text_without_attachments() -> None:
    store, ids = _store(PDF, DOCX)
    cli = FakeCliProvider("anthropic")
    deps = _deps(store, cli_providers={"anthropic": cli})
    result = _run(
        deps, ids, [Selection(provider="anthropic", backend=Backend.CLI, model="anthropic-cli")]
    )
    member = result.members[0]
    assert member.error is None
    assert {r.mode for r in member.attachments} == {"text"}
    prompt = cli.requests[0].prompt
    assert all(not m.attachments for m in (*prompt.stable_prefix, *prompt.variable_suffix))
    files = prompt.stable_prefix[1]
    assert files.role is Role.USER
    assert files.content.startswith(ATTACHMENTS_HEADER)
    for item in sort_attachments([PDF.attachment, DOCX.attachment]):
        assert fallback_block(item) in files.content
    assert files.content.index('report.pdf"') > 0
    assert prompt.stable_prefix[0].content.endswith(ATTACHMENT_NOTICE)
    assert prompt.variable_suffix[0].content == TASK


def test_cli_member_refuses_images_and_others_continue() -> None:
    store, ids = _store(PDF, PNG)
    cli = FakeCliProvider("anthropic")
    api = CountingProvider("gemini", None, AttachmentKind.IMAGE, AttachmentKind.PDF)
    deps = _deps(store, providers={"gemini": api}, cli_providers={"anthropic": cli})
    result = _run(
        deps,
        ids,
        [
            Selection(provider="anthropic", backend=Backend.CLI, model="anthropic-cli"),
            Selection(provider="gemini", model="balanced"),
        ],
    )
    by = {m.provider: m for m in result.members}
    assert "Subscription (CLI)" in (by["anthropic"].error or "")
    assert "chart.png" in (by["anthropic"].error or "")
    assert cli.requests == []
    assert by["gemini"].error is None
    assert by["gemini"].text == "gemini answer"


def test_errors_never_contain_file_content() -> None:
    store, ids = _store(PDF, PNG)
    deps = _deps(
        store,
        providers={"openai": CountingProvider("openai", None)},
        cli_providers={"anthropic": FakeCliProvider("anthropic")},
    )
    result = _run(
        deps,
        ids,
        [
            Selection(provider="openai", model="balanced"),
            Selection(provider="anthropic", backend=Backend.CLI, model="anthropic-cli"),
        ],
    )
    for member in result.members:
        assert member.error is not None
        assert "TOP-SECRET" not in member.error


# --------------------------------------------------------------------------- estimate


def test_estimate_uses_local_attachment_tokens_without_a_vendor_counter() -> None:
    store, ids = _store(PDF, PNG)
    provider = CountingProvider("anthropic", None, AttachmentKind.IMAGE, AttachmentKind.PDF)
    deps = _deps(store, providers={"anthropic": provider})
    sel = [Selection(provider="anthropic", model="balanced")]
    base = asyncio.run(estimate_compare_details(TASK, sel, deps)).estimate
    details = asyncio.run(estimate_compare_details(TASK, sel, deps, attachment_ids=ids))
    expected = {
        PDF.attachment.filename: estimate_attachment_tokens(
            PDF.attachment, PDF.meta, "anthropic", True
        ),
        PNG.attachment.filename: estimate_attachment_tokens(
            PNG.attachment, PNG.meta, "anthropic", True
        ),
    }
    assert {line.filename: line.tokens for line in details.attachments} == expected
    assert all(line.mode == "native" and not line.vendor_counted for line in details.attachments)
    extra = details.estimate.lines[0].input_tokens - base.lines[0].input_tokens
    assert extra >= sum(expected.values())
    assert provider.counted  # asked, but it had no counter


def test_estimate_prefers_the_vendor_count() -> None:
    store, ids = _store(PDF)
    provider = CountingProvider("openai", 123_456, AttachmentKind.PDF)
    deps = _deps(store, providers={"openai": provider})
    sel = [Selection(provider="openai", model="balanced")]
    details = asyncio.run(estimate_compare_details(TASK, sel, deps, attachment_ids=ids))
    assert details.estimate.lines[0].input_tokens == 123_456
    assert details.attachments[0].vendor_counted is True
    assert provider.counted[0].prompt.stable_prefix[1].attachments == (PDF.attachment,)
    # The run reuses the cached count instead of counting again.
    asyncio.run(run_compare(TASK, sel, deps, attachment_ids=ids))
    assert len(provider.counted) == 1


def test_estimate_without_attachments_never_calls_the_counter() -> None:
    provider = CountingProvider("openai", 99)
    deps = _deps(MemoryStore(), providers={"openai": provider})
    asyncio.run(
        estimate_compare_details(TASK, [Selection(provider="openai", model="balanced")], deps)
    )
    assert provider.counted == []


def test_estimate_lists_refusals_and_cli_text_lines() -> None:
    store, ids = _store(DOCX, PNG)
    deps = _deps(store, cli_providers={"openai": FakeCliProvider("openai")})
    sel = [Selection(provider="openai", backend=Backend.CLI, model="openai-cli")]
    details = asyncio.run(estimate_compare_details(TASK, sel, deps, attachment_ids=ids))
    modes = {line.filename: (line.mode, line.tokens) for line in details.attachments}
    assert modes["chart.png"] == ("refused", None)
    assert modes["notes.docx"][0] == "text"
    assert (modes["notes.docx"][1] or 0) > 0
    assert len(details.refusals) == 1
    assert "chart.png" in details.refusals[0]
