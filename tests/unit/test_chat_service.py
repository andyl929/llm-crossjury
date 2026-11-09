"""Chat service: estimate and run one message with fake api and cli providers."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from llm_crossjury.core.attachments.store import AttachmentNotFoundError
from llm_crossjury.core.chat_service import (
    CHAT_STAGE,
    ChatRequest,
    deps_with_settings,
    estimate_chat,
    run_chat,
)
from llm_crossjury.core.compare_service import (
    BudgetRefusedError,
    CompareDeps,
    ConsentRequiredError,
    SelectionError,
)
from llm_crossjury.core.providers.base import (
    AttachmentKind,
    Backend,
    ProviderError,
    Role,
)
from llm_crossjury.core.sessions.interfaces import AppSettings, BudgetSettings
from llm_crossjury.core.sessions.models import (
    ChatTurn,
    ContinuationRef,
    ModelRef,
    Session,
    TurnStatus,
)
from tests.unit._attachment_fakes import MemoryStore, built
from tests.unit._compare_fakes import (
    CLI_PROVIDERS,
    FIXED_CALL_COST,
    PER_OUTPUT_TOKEN,
    FakeCliProvider,
    FakeLedger,
    echo_provider,
    make_deps,
    raising_provider,
)
from tests.unit._session_fakes import NOW, chat_turn, jury_turn, make_session, new_id

SETTINGS = AppSettings()
ANSWERS = (
    ("openai", "openai-model", "OpenAI answer."),
    ("gemini", "gemini-model", "Gemini answer."),
)


def _req(**kwargs: object) -> ChatRequest:
    data: dict[str, object] = {
        "message": "And in Rust?",
        "model": {"provider": "openai", "model": "balanced"},
    }
    data.update(kwargs)
    return ChatRequest.model_validate(data)


def _pending(req: ChatRequest) -> ChatTurn:
    return ChatTurn(
        id=new_id(),
        created_at=NOW,
        message=req.message,
        model=req.model,
        attachment_ids=req.attachment_ids,
        continued_from=req.continued_from,
    )


def _with_pending(session: Session, turn: ChatTurn) -> Session:
    return session.model_copy(update={"turns": (*session.turns, turn)})


def _run(session: Session, req: ChatRequest, deps: CompareDeps) -> ChatTurn:
    turn = _pending(req)
    return asyncio.run(
        run_chat(_with_pending(session, turn), req, deps, settings=SETTINGS, turn=turn)
    )


def _history() -> Session:
    jury = jury_turn("How to sort?", ANSWERS)
    return make_session([jury, chat_turn("Why?", "Because.")])


# --------------------------------------------------------------------------- estimate


def test_estimate_happy_path() -> None:
    deps = make_deps()
    est = estimate_chat(_history(), _req(), deps, settings=SETTINGS)
    assert est.model == "openai-model"
    assert est.max_output_tokens == 4096  # default 32000 clamped to the registry max_output
    assert est.total_high == 4096 * PER_OUTPUT_TOKEN
    assert est.total_low == (4096 // 8) * PER_OUTPUT_TOKEN
    assert est.cli_calls == 0
    assert not est.budget.refused
    assert est.context_budget_tokens == SETTINGS.context_budget_tokens
    assert est.context.recent > 0
    assert est.context.message > 0
    assert est.context_tokens == est.context.total
    assert est.verbatim_turns == 2


def test_estimate_uses_the_requested_output_cap() -> None:
    est = estimate_chat(make_session(), _req(max_output_tokens=100), make_deps(), settings=SETTINGS)
    assert est.max_output_tokens == 100
    assert est.total_high == 100 * PER_OUTPUT_TOKEN


def test_estimate_refuses_bad_requests() -> None:
    deps = make_deps()
    with pytest.raises(SelectionError):
        estimate_chat(make_session(), _req(message="  "), deps, settings=SETTINGS)
    with pytest.raises(SelectionError):
        estimate_chat(
            make_session(), _req(model={"provider": "nope", "model": "m"}), deps, settings=SETTINGS
        )
    with pytest.raises(SelectionError):
        estimate_chat(
            make_session(),
            _req(model={"provider": "openai", "backend": "cli", "model": "openai-cli"}),
            deps,
            settings=SETTINGS,
        )
    ref = {"turn_id": new_id(), "source": "member", "member_index": 0}
    with pytest.raises(SelectionError):
        estimate_chat(_history(), _req(continued_from=ref), deps, settings=SETTINGS)
    session = _history()
    bad_index = {"turn_id": session.turns[0].id, "source": "member", "member_index": 9}
    with pytest.raises(SelectionError):
        estimate_chat(session, _req(continued_from=bad_index), deps, settings=SETTINGS)


def test_settings_budget_replaces_the_config_budget() -> None:
    settings = AppSettings(
        budget=BudgetSettings(
            run_limit=Decimal("0.01"), confirm_above=Decimal(0), daily_limit=Decimal(1)
        )
    )
    deps = deps_with_settings(make_deps(), settings)
    est = estimate_chat(make_session(), _req(), deps, settings=settings)
    assert est.budget.run_limit == Decimal("0.01")
    assert est.budget.refused


# --------------------------------------------------------------------------- run


def test_run_api_happy_path_records_the_call() -> None:
    ledger = FakeLedger()
    openai = echo_provider("openai")
    deps = make_deps(providers={"openai": openai}, ledger=ledger)
    session = _history()
    turn = _run(session, _req(), deps)
    assert turn.status is TurnStatus.DONE
    assert turn.error is None
    assert turn.reply is not None
    assert turn.resolved_model == "openai-model"
    assert turn.cost == FIXED_CALL_COST
    assert turn.currency == "USD"
    assert turn.context_tokens is not None
    assert turn.context_tokens > 0
    assert [e.stage for e in ledger.entries] == [CHAT_STAGE]
    request = openai.requests[0]
    assert request.cache_prefix is True
    assert request.max_output_tokens == 4096
    suffix = [(m.role, m.content) for m in request.prompt.variable_suffix]
    assert suffix[0] == (Role.USER, "How to sort?")
    assert suffix[1] == (Role.ASSISTANT, "OpenAI answer.")  # its own step-1 answer
    assert suffix[-1] == (Role.USER, "And in Rust?")


def test_run_continuation_of_another_member() -> None:
    openai = echo_provider("openai")
    deps = make_deps(providers={"openai": openai})
    session = _history()
    ref = ContinuationRef(turn_id=session.turns[0].id, source="member", member_index=1)
    turn = _run(session, _req(continued_from=ref.model_dump()), deps)
    assert turn.status is TurnStatus.DONE
    contents = [m.content for m in openai.requests[0].prompt.variable_suffix]
    assert any(c.endswith("Gemini answer.") for c in contents)


def test_run_cli_needs_consent_and_has_unknown_cost() -> None:
    clis = {p: FakeCliProvider(p) for p in CLI_PROVIDERS}
    ledger = FakeLedger()
    deps = make_deps(cli_providers=clis, ledger=ledger)
    model = {"provider": "anthropic", "backend": "cli", "model": "anthropic-cli"}
    with pytest.raises(ConsentRequiredError):
        _run(_history(), _req(model=model), deps)
    assert not clis["anthropic"].requests
    turn = _run(_history(), _req(model=model, allow_unknown_cost=True), deps)
    assert turn.status is TurnStatus.DONE
    assert turn.reply == "anthropic cli answer"
    assert turn.cost is None
    assert ledger.entries[0].cost is None
    assert ledger.entries[0].backend is Backend.CLI
    # Stateless: the whole transcript is in the prompt the CLI receives.
    contents = [m.content for m in clis["anthropic"].requests[0].prompt.variable_suffix]
    assert "How to sort?" in contents


def test_run_writes_provider_failures_into_the_turn() -> None:
    deps = make_deps(providers={"openai": raising_provider("openai", ProviderError("vendor 503"))})
    turn = _run(_history(), _req(), deps)
    assert turn.status is TurnStatus.ERROR
    assert turn.error == "vendor 503"
    assert turn.reply is None
    crash = make_deps(providers={"openai": raising_provider("openai", RuntimeError("sk-secret"))})
    turn = _run(_history(), _req(), crash)
    assert turn.status is TurnStatus.ERROR
    assert turn.error == "Unexpected error (RuntimeError)."


def test_run_without_key_is_an_error_turn() -> None:
    deps = make_deps(key_envs={"openai": "OPENAI_API_KEY"})
    turn = _run(make_session(), _req(), deps)
    assert turn.status is TurnStatus.ERROR
    assert turn.error is not None
    assert "OPENAI_API_KEY is not set" in turn.error


def test_run_refused_by_the_budget_makes_no_call() -> None:
    openai = echo_provider("openai")
    settings = AppSettings(
        budget=BudgetSettings(run_limit=Decimal("0.001"), confirm_above=Decimal(0))
    )
    deps = deps_with_settings(make_deps(providers={"openai": openai}), settings)
    with pytest.raises(BudgetRefusedError):
        _run(make_session(), _req(), deps)
    assert not openai.requests


def test_run_empty_reply_is_an_error() -> None:
    from llm_crossjury.core.providers.mock import MockProvider  # noqa: PLC0415

    deps = make_deps(providers={"openai": MockProvider("openai", lambda req: "  ")})
    turn = _run(make_session(), _req(), deps)
    assert turn.status is TurnStatus.ERROR
    assert turn.error is not None
    assert "empty reply" in turn.error


# --------------------------------------------------------------------------- attachments


def _store_deps(store: MemoryStore, **kwargs: object) -> CompareDeps:
    from dataclasses import replace  # noqa: PLC0415

    return replace(make_deps(**kwargs), attachment_store=store)  # type: ignore[arg-type]


def test_attachments_of_the_session_and_message_are_in_the_prefix() -> None:
    store = MemoryStore()
    old = built(AttachmentKind.DOCUMENT, name="old.docx", text="Old notes.")
    new = built(AttachmentKind.DOCUMENT, name="new.docx", text="New notes.")
    old_id, new_id_ = store.add(old, new)
    openai = echo_provider("openai")
    deps = _store_deps(store, providers={"openai": openai})
    session = make_session([chat_turn("see file", "ok", attachment_ids=(old_id,))])
    turn = _run(session, _req(attachment_ids=[new_id_]), deps)
    assert turn.status is TurnStatus.DONE
    prefix = openai.requests[0].prompt.stable_prefix
    names = sorted(a.filename for m in prefix for a in m.attachments)
    assert names == ["new.docx", "old.docx"]


def test_unknown_attachment_and_refused_image() -> None:
    store = MemoryStore()
    image = built(AttachmentKind.IMAGE, name="pic.png")
    (image_id,) = store.add(image)
    clis = {p: FakeCliProvider(p) for p in CLI_PROVIDERS}
    deps = _store_deps(store, cli_providers=clis)
    with pytest.raises(AttachmentNotFoundError):
        estimate_chat(make_session(), _req(attachment_ids=["f" * 64]), deps, settings=SETTINGS)
    model = ModelRef(provider="anthropic", backend=Backend.CLI, model="anthropic-cli")
    with pytest.raises(SelectionError):
        estimate_chat(
            make_session(),
            _req(model=model.model_dump(), attachment_ids=[image_id]),
            deps,
            settings=SETTINGS,
        )
