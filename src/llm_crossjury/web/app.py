"""FastAPI app for the local compare UI.

Serves one static page (vanilla HTML/CSS/JS, no build step) and a small JSON API:

* ``GET /api/providers``: the providers, whether each key is set (never any part of
  its value), the usable models from the registry, and the effective budget.
* ``POST /api/estimate``: the cost range and the budget verdict (confirmation needed,
  or refused) for the selections.
* ``POST /api/run``: runs the comparison and returns every member's result.

The app is meant to listen on localhost. As defense in depth against other web pages
in the user's browser, it checks the ``Host`` header (DNS rebinding), requires JSON
bodies for POST (forcing a CORS preflight, which is never granted), and rejects
cross-origin POSTs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from decimal import Decimal
from importlib import resources
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from llm_crossjury.core.compare_service import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    BudgetRefusedError,
    BudgetStatus,
    CompareDeps,
    CompareResult,
    Selection,
    SelectionError,
    budget_status,
    estimate_compare,
    key_is_set,
    run_compare,
    usable_models,
)
from llm_crossjury.core.cost.pricing import PricingError
from llm_crossjury.core.registry.resolver import ResolutionError

LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
"""Default ``Host`` header values the app accepts."""

PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "xai": "xAI",
}

MAX_TASK_CHARS = 200_000

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_STATIC_FILES: dict[str, str] = {
    "/": "index.html",
    "/app.js": "app.js",
    "/app.css": "app.css",
}
_MEDIA_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}

DepsFactory = Callable[[], CompareDeps]
KeyStatus = Literal["set", "not set", "not required"]


# --------------------------------------------------------------------------- models


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelOption(_Frozen):
    """A selectable model."""

    id: str
    label: str
    aliases: tuple[str, ...]


class ProviderInfo(_Frozen):
    """One provider as shown in the UI."""

    name: str
    label: str
    key_env: str | None
    """Name of the environment variable (never its value)."""
    key_status: KeyStatus
    models: tuple[ModelOption, ...]
    default_model: str | None
    """The ``balanced`` alias target if usable, else the first usable model."""


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


class CompareRequest(_Frozen):
    task: str = Field(max_length=MAX_TASK_CHARS)
    selections: tuple[Selection, ...]


class RunRequest(CompareRequest):
    confirmed: bool = False
    """The user confirmed a run whose high estimate exceeds ``confirm_above``."""


class EstimateLineOut(_Frozen):
    provider: str
    model: str
    cost_low: Decimal | None
    cost_high: Decimal | None


class EstimateResponse(_Frozen):
    currency: str
    total_low: Decimal
    total_high: Decimal
    lines: tuple[EstimateLineOut, ...]
    warnings: tuple[str, ...]
    assumptions: tuple[str, ...]
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


def _provider_info(name: str, deps: CompareDeps) -> ProviderInfo:
    key_env = deps.key_envs.get(name)
    models = usable_models(name, deps)
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
    return ProviderInfo(
        name=name,
        label=PROVIDER_LABELS.get(name, name),
        key_env=key_env,
        key_status=_key_status(key_env),
        models=options,
        default_model=default,
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


# --------------------------------------------------------------------------- app


def create_app(
    deps_factory: DepsFactory,
    *,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
) -> FastAPI:
    """Create the compare UI app.

    Args:
        deps_factory: Builds the compare dependencies. Called once, on first use.
        allowed_hosts: Accepted ``Host`` header names (without port); ``"*"`` accepts
            any host.
    """
    app = FastAPI(
        title="llm-crossjury",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    hosts = frozenset(h.strip("[]").lower() for h in allowed_hosts)
    cache: list[CompareDeps] = []

    def deps() -> CompareDeps:
        if not cache:
            cache.append(deps_factory())
        return cache[0]

    static = resources.files("llm_crossjury.web").joinpath("static")
    pages = {
        path: static.joinpath(name).read_text(encoding="utf-8")
        for path, name in _STATIC_FILES.items()
    }

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = _host_name(request.headers.get("host", "")).lower()
        if "*" not in hosts and host not in hosts:
            return _error(400, "bad_host", "Host not allowed.")
        if request.method == "POST":
            content_type = request.headers.get("content-type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                return _error(415, "unsupported_media_type", "Send JSON.")
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

    @app.exception_handler(SelectionError)
    async def _selection_error(request: Request, exc: SelectionError) -> JSONResponse:
        return _error(400, "invalid_selection", str(exc))

    @app.exception_handler(ResolutionError)
    async def _resolution_error(request: Request, exc: ResolutionError) -> JSONResponse:
        return _error(400, "resolution", f"Model resolution failed: {exc}")

    @app.exception_handler(PricingError)
    async def _pricing_error(request: Request, exc: PricingError) -> JSONResponse:
        return _error(400, "pricing", f"Pricing refused the run: {exc}")

    @app.exception_handler(BudgetRefusedError)
    async def _budget_error(request: Request, exc: BudgetRefusedError) -> JSONResponse:
        return _error(409, "budget_refused", str(exc), exc.status)

    def page(path: str) -> Response:
        name = _STATIC_FILES[path]
        return Response(content=pages[path], media_type=_MEDIA_TYPES[name])

    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        return page("/")

    @app.get("/app.js")
    async def app_js() -> Response:
        return page("/app.js")

    @app.get("/app.css")
    async def app_css() -> Response:
        return page("/app.css")

    @app.get("/api/providers")
    async def providers() -> ProvidersResponse:
        d = deps()
        return ProvidersResponse(
            providers=tuple(_provider_info(name, d) for name in d.providers),
            default_max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            budget=_budget_info(d),
        )

    @app.post("/api/estimate")
    async def estimate(body: CompareRequest) -> EstimateResponse:
        d = deps()
        est = await estimate_compare(body.task, body.selections, d)
        warnings = tuple(w for r in est.resolved for w in r.warnings)
        return EstimateResponse(
            currency=est.currency,
            total_low=est.total_low,
            total_high=est.total_high,
            lines=tuple(
                EstimateLineOut(
                    provider=line.provider,
                    model=line.model,
                    cost_low=line.cost_low,
                    cost_high=line.cost_high,
                )
                for line in est.lines
            ),
            warnings=warnings,
            assumptions=est.assumptions,
            budget=budget_status(est, d),
        )

    @app.post("/api/run", response_model=None)
    async def run(body: RunRequest) -> CompareResult | JSONResponse:
        d = deps()
        status = budget_status(await estimate_compare(body.task, body.selections, d), d)
        if status.refused:
            raise BudgetRefusedError(status.refusal_reason or "Refused by the budget.", status)
        if status.needs_confirmation and not body.confirmed:
            return _error(
                409,
                "confirmation_required",
                f"The high estimate ({status.currency} {status.estimate_high}) exceeds the "
                f"confirmation threshold ({status.currency} {status.confirm_above}).",
                status,
            )
        return await run_compare(body.task, body.selections, d)

    return app


__all__ = ["LOOPBACK_HOSTS", "create_app"]
