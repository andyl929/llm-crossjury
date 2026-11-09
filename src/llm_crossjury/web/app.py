"""FastAPI app for the local compare UI.

Serves one static page (vanilla HTML/CSS/JS, no build step) and a small JSON API:

* ``GET /api/providers``: the providers, whether each key is set (never any part of
  its value), the usable models per backend (``api`` = API key, ``cli`` =
  "subscription", the vendor CLI the user signed in to), the CLI status from each
  CLI provider's ``check()`` (run concurrently on request, cached briefly, never at
  startup; ``?refresh=true`` re-checks), and the effective budget.
* ``POST /api/estimate``: the cost range of the priced calls, the number of CLI calls
  (unknown cost), per-attachment token lines and the budget verdict (confirmation
  needed, or refused).
* ``POST /api/run``: runs the comparison and returns every member's result, including
  how each member received each attachment. A run with CLI members needs
  ``allow_unknown_cost`` (409 ``consent_required`` otherwise).
* ``POST /api/attachments`` (multipart) and ``DELETE /api/attachments/{id}``: upload
  and remove task attachments (see :mod:`llm_crossjury.web.attachments_api`); estimate,
  run and synthesis requests refer to them by ``attachment_ids``.
* ``POST /api/synthesis/{estimate,agreement,finish,resynthesize-estimate,resynthesize}``:
  the stateless AI synthesis upgrade of a finished comparison (see
  :mod:`llm_crossjury.core.synthesis_service`). The client posts the step-1 material
  and earlier stage outputs back on every call.
* Sessions (``/api/sessions*``) and app settings (``/api/settings/app``): see
  :mod:`llm_crossjury.web.sessions_api`. With ``?session=<id>&turn=<turn id>``, the
  estimate, run and step-2 endpoints record their work in that session and run it
  detached from the request (see :mod:`llm_crossjury.web.session_hooks`, ADR 0008).

The effective budget of every estimate, run and budget line is ``AppSettings.budget``
(Settings page), which replaces the limits of the injected ``BudgetConfig``.

The app is meant to listen on localhost. As defense in depth against other web pages
in the user's browser, it checks the ``Host`` header (DNS rebinding), requires JSON
bodies for POST (forcing a CORS preflight, which is never granted; the upload route
alone accepts multipart and requires a custom header instead), and rejects
cross-origin POSTs and DELETEs.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.routing import Match, Route
from starlette.types import Scope

from llm_crossjury.core.app_settings import (
    AppSettingsStore,
    default_settings_path,
    effective_budget,
)
from llm_crossjury.core.attachments import AttachmentError, AttachmentTooLargeError
from llm_crossjury.core.attachments.store import AttachmentNotFoundError, AttachmentStore
from llm_crossjury.core.compare_service import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_ATTACHMENTS,
    AttachmentEstimateLine,
    BudgetRefusedError,
    BudgetStatus,
    CompareDeps,
    CompareResult,
    ConsentRequiredError,
    Selection,
    SelectionError,
    budget_status,
    estimate_compare,
    estimate_compare_details,
    key_is_set,
    provider_names,
    run_compare,
    usable_models,
)
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.cost.estimate import CostEstimate
from llm_crossjury.core.cost.pricing import PricingError
from llm_crossjury.core.providers.base import Backend, ProviderStatus
from llm_crossjury.core.registry.resolver import ResolutionError
from llm_crossjury.core.sessions.interfaces import CaseFileBuilder, TaskRunnerLike
from llm_crossjury.core.sessions.store import SessionStore
from llm_crossjury.core.sessions.tasks import TaskRunner
from llm_crossjury.core.storage import DataDirError, resolve_data_paths
from llm_crossjury.core.synthesis_service import (
    AgreementStepResult,
    FinishRequest,
    FinishStepResult,
    ResynthesisRequest,
    ResynthesisResult,
    SynthesisEstimate,
    SynthesisRequest,
    SynthesisStageError,
    estimate_resynthesis,
    estimate_synthesis,
    run_agreement_step,
    run_finish_step,
    run_resynthesis,
)
from llm_crossjury.web import session_hooks as hooks
from llm_crossjury.web.attachments_api import UPLOAD_PATH, create_attachments_router
from llm_crossjury.web.session_hooks import AppContext
from llm_crossjury.web.sessions_api import create_sessions_router
from llm_crossjury.web.settings_api import KeyTester, create_settings_router

LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
"""Default ``Host`` header values the app accepts."""

PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "xai": "xAI",
}

CLI_CHECK_TTL_S = 30.0
"""How long a CLI ``check()`` result is reused before it is run again."""

CLI_CHECK_TIMEOUT_S = 15.0
"""A CLI check that takes longer is reported as not responding."""

MAX_STATUS_DETAIL_CHARS = 300


@dataclass(frozen=True)
class CliProduct:
    """Display text for a provider's subscription backend (not model facts)."""

    product: str
    command: str
    experimental: bool
    install: tuple[str, ...]
    """Official install commands, one per platform or method."""
    sign_in: str


CLI_PRODUCTS: dict[str, CliProduct] = {
    "anthropic": CliProduct(
        product="Claude Code",
        command="claude",
        experimental=False,
        install=(
            "macOS/Linux/WSL: curl -fsSL https://claude.ai/install.sh | bash",
            "Windows PowerShell: irm https://claude.ai/install.ps1 | iex",
            "Windows (winget): winget install Anthropic.ClaudeCode",
        ),
        sign_in="Run claude once in a terminal and sign in with your Claude account.",
    ),
    "openai": CliProduct(
        product="Codex CLI",
        command="codex",
        experimental=False,
        install=("npm: npm install -g @openai/codex",),
        sign_in="Run codex login in a terminal and choose Sign in with ChatGPT.",
    ),
    "xai": CliProduct(
        product="Grok Build",
        command="grok",
        experimental=True,
        install=(
            "macOS/Linux/WSL: curl -fsSL https://x.ai/cli/install.sh | bash",
            "Windows PowerShell: irm https://x.ai/cli/install.ps1 | iex",
        ),
        sign_in="Run grok once in a terminal and sign in with your xAI account.",
    ),
}
"""Subscription backends offered in the UI. Install and sign-in text is shown verbatim."""

SUBSCRIPTION_UNAVAILABLE: dict[str, str] = {
    "gemini": "Gemini requires an API key (Google AI Studio).",
}
"""Providers without a subscription option, and why (Google forbids third-party use of
consumer plans)."""

CONSENT_TEXT = (
    "I understand: cost is unknown, calls count against my plan's usage limits, "
    "and this mode is experimental"
)

MAX_TASK_CHARS = 200_000
MAX_ANSWER_CHARS = 400_000
MAX_INSTRUCTION_CHARS = 20_000

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

STATIC_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*\.(js|css|svg|html)$"
"""Files of the static package directory served at ``/<name>`` (no subdirectories)."""

_STATIC_NAME_RE = re.compile(STATIC_NAME_PATTERN)
_MEDIA_TYPES = {
    "html": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "svg": "image/svg+xml",
}

DepsFactory = Callable[[], CompareDeps]
RouterFactory = Callable[[AppContext], APIRouter]
"""Builds an extra router from the app's shared objects (see ``create_app``)."""
KeyStatus = Literal["set", "not set", "not required"]


# --------------------------------------------------------------------------- models


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelOption(_Frozen):
    """A selectable model."""

    id: str
    label: str
    aliases: tuple[str, ...]


class CliStatus(_Frozen):
    """The subscription (``cli``) backend of one provider."""

    product: str
    command: str
    experimental: bool
    available: bool
    """The CLI was found and answered its version/status check."""
    detail: str
    """Check result (redacted, never credentials)."""
    version: str | None
    install: tuple[str, ...]
    sign_in: str
    models: tuple[ModelOption, ...]
    """Registry entries that list the ``cli`` backend and have ``cli_flags``."""
    default_model: str | None


class ProviderInfo(_Frozen):
    """One provider as shown in the UI."""

    name: str
    label: str
    key_env: str | None
    """Name of the environment variable (never its value)."""
    key_status: KeyStatus
    models: tuple[ModelOption, ...]
    """Usable ``api`` models (verified and priced)."""
    default_model: str | None
    """The ``balanced`` alias target if usable, else the first usable model."""
    cli: CliStatus | None
    """The subscription backend, or ``None`` if not offered for this provider."""
    subscription_note: str | None
    """Why no subscription backend is offered, when there is a reason to show."""


class BudgetInfo(_Frozen):
    """The effective budget and today's spend."""

    currency: str
    run_limit: Decimal
    confirm_above: Decimal
    daily_limit: Decimal
    spent_today: Decimal
    daily_remaining: Decimal


class ProvidersResponse(_Frozen):
    providers: tuple[ProviderInfo, ...]
    default_max_output_tokens: int
    budget: BudgetInfo
    consent_text: str
    """The consent statement the UI shows next to the checkbox for CLI members."""
    demo: bool = False
    """The app runs in demo mode: simulated models, canned answers, no keys, no cost."""


class CompareRequest(_Frozen):
    task: str = Field(max_length=MAX_TASK_CHARS)
    selections: tuple[Selection, ...]
    attachment_ids: tuple[str, ...] = Field(default=(), max_length=MAX_ATTACHMENTS)
    """Uploaded attachments (``POST /api/attachments``) sent to every member."""


class RunRequest(CompareRequest):
    confirmed: bool = False
    """The user confirmed a run whose high estimate exceeds ``confirm_above``."""
    allow_unknown_cost: bool = False
    """The user ticked the consent box for subscription (CLI) members."""


class EstimateLineOut(_Frozen):
    provider: str
    backend: Backend
    model: str
    calls: int
    cost_low: Decimal | None
    cost_high: Decimal | None
    """``None`` for ``cli`` calls (plan usage, unknown cost)."""


class EstimateResponse(_Frozen):
    currency: str
    total_low: Decimal
    total_high: Decimal
    """Range of the priced (``api``) calls only."""
    cli_calls: int
    lines: tuple[EstimateLineOut, ...]
    attachments: tuple[AttachmentEstimateLine, ...] = ()
    """Per member and attachment: delivery mode and estimated input tokens."""
    warnings: tuple[str, ...]
    assumptions: tuple[str, ...]
    budget: BudgetStatus


class SynthesisStartRequest(SynthesisRequest):
    """Agreement step input; ``confirmed`` as for :class:`RunRequest`."""

    confirmed: bool = False


class ResynthesizeRequest(ResynthesisRequest):
    """Re-synthesis input; ``confirmed`` as for :class:`RunRequest`."""

    confirmed: bool = False


class ResynthesisEstimateResponse(_Frozen):
    estimate: CostEstimate
    budget: BudgetStatus


class ErrorResponse(_Frozen):
    code: str
    detail: str
    budget: BudgetStatus | None = None


# --------------------------------------------------------------------------- helpers


def _error(status: int, code: str, detail: str, budget: BudgetStatus | None = None) -> JSONResponse:
    body = ErrorResponse(code=code, detail=detail, budget=budget)
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def _host_name(host_header: str) -> str:
    """Strip the port from a ``Host`` header (handles ``[::1]:8765``)."""
    if host_header.startswith("["):
        return host_header[1 : host_header.find("]")] if "]" in host_header else host_header
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def _key_status(key_env: str | None) -> KeyStatus:
    if key_env is None:
        return "not required"
    return "set" if key_is_set(key_env) else "not set"


def _model_options(
    name: str, deps: CompareDeps, backend: Backend
) -> tuple[tuple[ModelOption, ...], str | None]:
    models = usable_models(name, deps, backend)
    options = tuple(
        ModelOption(
            id=m.model_id,
            label=f"{m.model_id} ({', '.join(m.aliases)})" if m.aliases else m.model_id,
            aliases=m.aliases,
        )
        for m in models
    )
    default = next((m.model_id for m in models if "balanced" in m.aliases), None)
    if default is None and options:
        default = options[0].id
    return options, default


def _redact_detail(text: str, deps: CompareDeps) -> str:
    """Remove any set API key value from a status text and bound its length."""
    for env in deps.key_envs.values():
        if key_is_set(env):
            secret = os.environ.get(env, "").strip()
            text = text.replace(secret, "[redacted]")
    if len(text) > MAX_STATUS_DETAIL_CHARS:
        text = text[: MAX_STATUS_DETAIL_CHARS - 3] + "..."
    return text


def _cli_status(name: str, deps: CompareDeps, status: ProviderStatus | None) -> CliStatus | None:
    product = CLI_PRODUCTS.get(name)
    if product is None or (name, Backend.CLI) not in deps.providers:
        return None
    options, default = _model_options(name, deps, Backend.CLI)
    available = status is not None and status.available
    detail = "Not checked yet." if status is None else _redact_detail(status.detail, deps)
    version = None if status is None or status.version is None else status.version[:80]
    return CliStatus(
        product=product.product,
        command=product.command,
        experimental=product.experimental,
        available=available,
        detail=detail,
        version=version,
        install=product.install,
        sign_in=product.sign_in,
        models=options,
        default_model=default,
    )


def _provider_info(
    name: str, deps: CompareDeps, cli_checks: dict[str, ProviderStatus]
) -> ProviderInfo:
    key_env = deps.key_envs.get(name)
    options, default = _model_options(name, deps, Backend.API)
    cli = _cli_status(name, deps, cli_checks.get(name))
    return ProviderInfo(
        name=name,
        label=PROVIDER_LABELS.get(name, name),
        key_env=key_env,
        key_status=_key_status(key_env),
        models=options,
        default_model=default,
        cli=cli,
        subscription_note=None if cli is not None else SUBSCRIPTION_UNAVAILABLE.get(name),
    )


class CliCheckCache:
    """Runs CLI ``check()`` calls concurrently and reuses results for a short time.

    Checks run only when the provider list is requested, never at startup. A check
    that raises or times out is reported as unavailable with the exception type only.
    """

    def __init__(self, ttl_s: float = CLI_CHECK_TTL_S) -> None:
        self._ttl = ttl_s
        self._results: dict[str, tuple[float, ProviderStatus]] = {}
        self._lock = asyncio.Lock()

    async def statuses(self, deps: CompareDeps, *, refresh: bool) -> dict[str, ProviderStatus]:
        """Return ``provider -> status`` for every ``cli`` provider in ``deps``."""
        names = [name for name, backend in deps.providers if backend is Backend.CLI]
        async with self._lock:
            now = time.monotonic()
            stale = [
                n
                for n in names
                if refresh or n not in self._results or now - self._results[n][0] > self._ttl
            ]
            if stale:
                fresh = await asyncio.gather(*(self._check(n, deps) for n in stale))
                done = time.monotonic()
                for n, status in zip(stale, fresh, strict=True):
                    self._results[n] = (done, status)
            return {n: self._results[n][1] for n in names}

    @staticmethod
    async def _check(name: str, deps: CompareDeps) -> ProviderStatus:
        provider = deps.providers[(name, Backend.CLI)]
        try:
            return await asyncio.wait_for(provider.check(), timeout=CLI_CHECK_TIMEOUT_S)
        except TimeoutError:
            detail = "The CLI did not respond to its version check in time."
        except Exception as exc:  # a broken adapter must not break the provider list
            # Only the type name: an exception message is not guaranteed secret-free.
            detail = f"The CLI check failed ({type(exc).__name__})."
        return ProviderStatus(
            provider=name, backend=Backend.CLI, available=False, detail=detail, version=None
        )


def _budget_info(deps: CompareDeps) -> BudgetInfo:
    budget = deps.budget
    spent = deps.ledger.spent_on(date.today())
    return BudgetInfo(
        currency=budget.currency,
        run_limit=budget.run_limit,
        confirm_above=budget.confirm_above,
        daily_limit=budget.daily_limit,
        spent_today=spent,
        daily_remaining=max(Decimal(0), budget.daily_limit - spent),
    )


def _check_synthesis_size(req: SynthesisRequest) -> None:
    if len(req.task) > MAX_TASK_CHARS:
        raise SelectionError(f"The task is longer than {MAX_TASK_CHARS} characters.")
    if len(req.extra_instructions or "") > MAX_INSTRUCTION_CHARS:
        raise SelectionError(
            f"The extra instructions are longer than {MAX_INSTRUCTION_CHARS} characters."
        )
    if any(len(a.text) > MAX_ANSWER_CHARS for a in req.answers):
        raise SelectionError(f"An answer is longer than {MAX_ANSWER_CHARS} characters.")


def _consent_error(exc: ConsentRequiredError, status: BudgetStatus | None = None) -> JSONResponse:
    return _error(409, "consent_required", str(exc), status)


def _confirmation_error(status: BudgetStatus) -> JSONResponse:
    return _error(
        409,
        "confirmation_required",
        f"The high estimate ({status.currency} {status.estimate_high}) exceeds the "
        f"confirmation threshold ({status.currency} {status.confirm_above}).",
        status,
    )


# --------------------------------------------------------------------------- app


def _add_synthesis_routes(app: FastAPI, ctx: AppContext) -> None:
    """Register the ``/api/synthesis/*`` endpoints (stateless step 2, session hooks)."""
    deps = ctx.deps

    def target(session: str | None, turn: str | None) -> JSONResponse | None:
        if session is None and turn is None:
            return None
        return hooks.check_stage_target(ctx, session, turn)

    @app.post("/api/synthesis/estimate")
    async def synthesis_estimate(body: SynthesisRequest) -> SynthesisEstimate:
        _check_synthesis_size(body)
        return estimate_synthesis(body, deps())

    @app.post("/api/synthesis/agreement", response_model=None)
    async def synthesis_agreement(
        body: SynthesisStartRequest, session: str | None = None, turn: str | None = None
    ) -> AgreementStepResult | JSONResponse:
        _check_synthesis_size(body)
        if (bad := target(session, turn)) is not None:
            return bad
        d = deps()
        status = estimate_synthesis(body, d).budget
        if status.cli_calls and not body.allow_unknown_cost:
            return _consent_error(ConsentRequiredError(status.cli_calls), status)
        if status.needs_confirmation and not body.confirmed:
            return _confirmation_error(status)
        if session is None or turn is None:
            return await run_agreement_step(body, d)
        result: AgreementStepResult = await hooks.run_stage(
            ctx, session, turn, "agreement", run_agreement_step(body, d)
        )
        return result

    @app.post("/api/synthesis/finish", response_model=None)
    async def synthesis_finish(
        body: FinishRequest, session: str | None = None, turn: str | None = None
    ) -> FinishStepResult | JSONResponse:
        _check_synthesis_size(body)
        if (bad := target(session, turn)) is not None:
            return bad
        if session is None or turn is None:
            return await run_finish_step(body, deps())
        result: FinishStepResult = await hooks.run_stage(
            ctx, session, turn, "finish", run_finish_step(body, deps())
        )
        return result

    @app.post("/api/synthesis/resynthesize-estimate")
    async def synthesis_resynthesize_estimate(
        body: ResynthesisRequest,
    ) -> ResynthesisEstimateResponse:
        _check_synthesis_size(body)
        d = deps()
        est = estimate_resynthesis(body, d)
        return ResynthesisEstimateResponse(estimate=est, budget=budget_status(est, d))

    @app.post("/api/synthesis/resynthesize", response_model=None)
    async def synthesis_resynthesize(
        body: ResynthesizeRequest, session: str | None = None, turn: str | None = None
    ) -> ResynthesisResult | JSONResponse:
        _check_synthesis_size(body)
        if (bad := target(session, turn)) is not None:
            return bad
        d = deps()
        status = budget_status(estimate_resynthesis(body, d), d)
        if status.cli_calls and not body.allow_unknown_cost:
            return _consent_error(ConsentRequiredError(status.cli_calls), status)
        if status.needs_confirmation and not body.confirmed:
            return _confirmation_error(status)
        if session is None or turn is None:
            return await run_resynthesis(body, d)
        result: ResynthesisResult = await hooks.run_stage(
            ctx, session, turn, "resynthesize", run_resynthesis(body, d)
        )
        return result


def _add_error_handlers(app: FastAPI) -> None:
    """Map service errors to JSON error responses."""

    @app.exception_handler(SelectionError)
    async def _selection_error(request: Request, exc: SelectionError) -> JSONResponse:
        return _error(400, "invalid_selection", str(exc))

    @app.exception_handler(ResolutionError)
    async def _resolution_error(request: Request, exc: ResolutionError) -> JSONResponse:
        return _error(400, "resolution", f"Model resolution failed: {exc}")

    @app.exception_handler(PricingError)
    async def _pricing_error(request: Request, exc: PricingError) -> JSONResponse:
        return _error(400, "pricing", f"Pricing refused the run: {exc}")

    @app.exception_handler(ConsentRequiredError)
    async def _consent_required(request: Request, exc: ConsentRequiredError) -> JSONResponse:
        return _consent_error(exc)

    @app.exception_handler(BudgetRefusedError)
    async def _budget_error(request: Request, exc: BudgetRefusedError) -> JSONResponse:
        return _error(409, "budget_refused", str(exc), exc.status)

    @app.exception_handler(AttachmentError)
    async def _attachment_error(request: Request, exc: AttachmentError) -> JSONResponse:
        # Attachment messages never contain file content (see core.attachments.limits).
        if isinstance(exc, AttachmentNotFoundError):
            return _error(400, "attachment_not_found", f"{exc}.")
        code = "attachment_too_large" if isinstance(exc, AttachmentTooLargeError) else "attachment"
        return _error(400, code, f"Attachment refused: {exc}.")

    @app.exception_handler(SynthesisStageError)
    async def _stage_error(request: Request, exc: SynthesisStageError) -> JSONResponse:
        status = 409 if exc.code == "budget_stopped" else 502
        return _error(status, exc.code, str(exc))


def _add_guard(app: FastAPI, hosts: frozenset[str]) -> None:
    """Host allow-list, JSON-only POST bodies, cross-origin refusal, security headers."""

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = _host_name(request.headers.get("host", "")).lower()
        if "*" not in hosts and host not in hosts:
            return _error(400, "bad_host", "Host not allowed.")
        if request.method in {"POST", "DELETE"}:
            content_type = request.headers.get("content-type", "")
            media = content_type.split(";", 1)[0].strip().lower()
            # Only the upload route takes multipart (it requires its own header).
            expected = (
                "multipart/form-data" if request.url.path == UPLOAD_PATH else "application/json"
            )
            if request.method == "POST" and media != expected:
                hint = "Send JSON." if expected == "application/json" else f"Send {expected}."
                return _error(415, "unsupported_media_type", hint)
            origin = request.headers.get("origin")
            if (
                origin is not None
                and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()
            ):
                return _error(403, "cross_origin", "Cross-origin requests are not allowed.")
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


def _add_compare_routes(
    app: FastAPI, ctx: AppContext, files: Mapping[str, tuple[str, str]], *, demo: bool
) -> None:
    """Register ``/``, ``/api/providers``, ``/api/estimate`` and ``/api/run``."""
    deps = ctx.deps
    cli_checks = CliCheckCache()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        text, media = files["index.html"]
        return Response(content=text, media_type=media)

    @app.get("/api/providers")
    async def providers(refresh: bool = False) -> ProvidersResponse:
        d = deps()
        checks = await cli_checks.statuses(d, refresh=refresh)
        return ProvidersResponse(
            providers=tuple(_provider_info(name, d, checks) for name in provider_names(d)),
            default_max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            budget=_budget_info(d),
            consent_text=CONSENT_TEXT,
            demo=demo,
        )

    @app.post("/api/estimate", response_model=None)
    async def estimate(
        body: CompareRequest, session: str | None = None
    ) -> EstimateResponse | JSONResponse:
        task = body.task
        if session is not None:
            found = hooks.get_session(ctx, session)
            if isinstance(found, JSONResponse):
                return found
            task = hooks.with_context(task, await hooks.case_file_for(ctx, found, task))
        d = deps()
        details = await estimate_compare_details(
            task, body.selections, d, attachment_ids=body.attachment_ids
        )
        est = details.estimate
        warnings = tuple(w for r in est.resolved for w in r.warnings) + details.refusals
        status = budget_status(est, d)
        return EstimateResponse(
            currency=est.currency or d.budget.currency,
            total_low=est.total_low,
            total_high=est.total_high,
            cli_calls=status.cli_calls,
            lines=tuple(
                EstimateLineOut(
                    provider=line.provider,
                    backend=line.backend,
                    model=line.model,
                    calls=line.calls,
                    cost_low=line.cost_low,
                    cost_high=line.cost_high,
                )
                for line in est.lines
            ),
            attachments=details.attachments,
            warnings=warnings,
            assumptions=est.assumptions,
            budget=status,
        )

    @app.post("/api/run", response_model=None)
    async def run(
        body: RunRequest, session: str | None = None, turn: str | None = None
    ) -> CompareResult | JSONResponse:
        task = body.task
        case_file: str | None = None
        hooked = session is not None or turn is not None
        if hooked:
            found = hooks.get_session(ctx, session)
            if isinstance(found, JSONResponse):
                return found
            if (bad := hooks.check_new_turn(found, turn)) is not None:
                return bad
            case_file = await hooks.case_file_for(ctx, found, task)
            task = hooks.with_context(task, case_file)
        d = deps()
        status = budget_status(
            await estimate_compare(task, body.selections, d, attachment_ids=body.attachment_ids),
            d,
        )
        if status.cli_calls and not body.allow_unknown_cost:
            return _consent_error(ConsentRequiredError(status.cli_calls), status)
        if status.refused:
            raise BudgetRefusedError(status.refusal_reason or "Refused by the budget.", status)
        if status.needs_confirmation and not body.confirmed:
            return _confirmation_error(status)
        work = run_compare(
            task,
            body.selections,
            d,
            allow_unknown_cost=body.allow_unknown_cost,
            attachment_ids=body.attachment_ids,
        )
        if session is None or turn is None:
            return await work
        return await hooks.run_jury_turn(
            ctx,
            session,
            turn,
            task=body.task,
            case_file=case_file,
            selections=body.selections,
            attachment_ids=body.attachment_ids,
            work=work,
        )


class _StaticFileRoute(Route):
    """``GET /<name>`` for the files directly inside the static package directory.

    Matches only names in ``files`` (read once, at app creation), so other routes
    added later (e.g. ``/demo.css``) still match their own paths.
    """

    def __init__(self, files: Mapping[str, tuple[str, str]]) -> None:
        self._files = dict(files)
        super().__init__("/{name}", self._serve, methods=["GET"], include_in_schema=False)

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        match, child = super().matches(scope)
        if match is Match.NONE or child.get("path_params", {}).get("name") not in self._files:
            return Match.NONE, {}
        return match, child

    async def _serve(self, request: Request) -> Response:
        text, media = self._files[request.path_params["name"]]
        return Response(content=text, media_type=media)


def _static_files() -> dict[str, tuple[str, str]]:
    """``name -> (text, media type)`` for every servable file of the static package."""
    static = resources.files("llm_crossjury.web").joinpath("static")
    files: dict[str, tuple[str, str]] = {}
    for entry in static.iterdir():
        match = _STATIC_NAME_RE.match(entry.name)
        if match is not None and entry.is_file():
            files[entry.name] = (entry.read_text(encoding="utf-8"), _MEDIA_TYPES[match.group(1)])
    return files


def _default_sessions_root() -> Path | None:
    try:
        return resolve_data_paths().sessions
    except DataDirError:
        return None  # an unusable data dir: sessions stay in memory


def create_app(
    deps_factory: DepsFactory,
    *,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
    key_base_dir: Path | None = None,
    key_store_path: Path | None = None,
    ui_state_path: Path | None = None,
    key_tester: KeyTester | None = None,
    demo: bool = False,
    sessions_path: Path | None = None,
    session_store: SessionStore | None = None,
    settings_path: Path | None = None,
    settings_store: AppSettingsStore | None = None,
    task_runner: TaskRunnerLike | None = None,
    case_file_builder: CaseFileBuilder | None = None,
    extra_routers: Sequence[RouterFactory] = (),
) -> FastAPI:
    """Create the compare UI app.

    Args:
        deps_factory: Builds the compare dependencies. Called once, on first use. Its
            budget limits are replaced by ``AppSettings.budget``.
        allowed_hosts: Accepted ``Host`` header names (without port); ``"*"`` accepts
            any host.
        key_base_dir: Directory holding ``.env`` and the key files, for the settings
            page (default: the current directory).
        key_store_path: Settings key store override (default: the user config dir).
        ui_state_path: Onboarding state file override (default: the user config dir).
        key_tester: Key check override for the settings page (default: a real check).
        demo: Report demo mode in ``GET /api/providers`` (``demo: true``).
        sessions_path: Session directory override (default: ``<data dir>/sessions``).
        session_store: Session store override (wins over ``sessions_path``).
        settings_path: App settings file override (default:
            ``<config dir>/settings.json``).
        settings_store: App settings store override (wins over ``settings_path``).
        task_runner: Detached task runner override.
        case_file_builder: Conversation context for a jury question asked
            mid-session (``None`` = no case file). Can also be set later on
            ``app.state.crossjury.case_file_builder``.
        extra_routers: Router factories called with the :class:`AppContext` and
            included before the static files route.
    """
    app = FastAPI(
        title="llm-crossjury",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    hosts = frozenset(h.strip("[]").lower() for h in allowed_hosts)
    cache: list[CompareDeps] = []
    effective: list[tuple[object, CompareDeps]] = []

    settings = (
        settings_store
        if settings_store is not None
        else AppSettingsStore(
            settings_path if settings_path is not None else default_settings_path()
        )
    )
    if session_store is None:
        root = sessions_path if sessions_path is not None else _default_sessions_root()
        session_store = SessionStore(root, persist=settings.get().save_history)
    sessions = session_store
    settings.subscribe(lambda s: sessions.set_persist(s.save_history))

    def base_deps() -> CompareDeps:
        if not cache:
            built = deps_factory()
            if isinstance(built.attachment_store, AttachmentStore):
                built.attachment_store.set_keep(sessions.attachment_ids)
            cache.append(built)
        return cache[0]

    def deps() -> CompareDeps:
        base = base_deps()
        current = settings.get()
        if effective and effective[0][0] is current:
            return effective[0][1]
        budget = effective_budget(base.budget, current.budget)
        result = base if budget == base.budget else replace(base, budget=budget)
        effective[:] = [(current, result)]
        return result

    ctx = AppContext(
        deps=deps,
        sessions=sessions,
        tasks=task_runner if task_runner is not None else TaskRunner(),
        settings=settings,
        allowed_hosts=tuple(allowed_hosts),
        case_file_builder=case_file_builder,
    )
    app.state.crossjury = ctx

    files = _static_files()
    _add_guard(app, hosts)
    _add_error_handlers(app)
    _add_compare_routes(app, ctx, files, demo=demo)

    _add_synthesis_routes(app, ctx)
    app.include_router(create_attachments_router(lambda: deps().attachment_store))
    app.include_router(
        create_settings_router(
            base_dir=key_base_dir if key_base_dir is not None else Path.cwd(),
            allowed_hosts=allowed_hosts,
            store_path=key_store_path,
            ui_state_path=ui_state_path,
            tester=key_tester,
            budget_source=lambda: effective_budget(BudgetConfig(), settings.get().budget),
        )
    )
    app.include_router(
        create_sessions_router(store=sessions, settings=settings, allowed_hosts=allowed_hosts)
    )
    for factory in extra_routers:
        app.include_router(factory(ctx))
    app.router.routes.append(_StaticFileRoute(files))

    return app


__all__ = ["LOOPBACK_HOSTS", "STATIC_NAME_PATTERN", "AppContext", "RouterFactory", "create_app"]
