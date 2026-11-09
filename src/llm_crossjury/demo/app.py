"""Demo mode: the real compare UI, wired to simulated providers in a temporary data dir.

``llm-crossjury ui --demo`` serves :func:`create_demo_app`. Everything except the
providers is real: the shipped model registry, the resolver, the pricing table, the
estimator, the SQLite ledger, the attachment store, the synthesis stages and the web
API. Only the provider calls are simulated (:class:`~llm_crossjury.demo.providers.DemoProvider`).

Isolation:

* All state (ledger, attachments, sessions, app settings, settings key store,
  onboarding state) lives under the ``data_dir`` the caller passes, normally a fresh
  temporary directory.
* No ``.env``, key file or settings key store of the user is read: the settings page
  looks in an empty directory under ``data_dir``, and the providers need no key.
* No network call and no subprocess: the simulated providers answer in-process, the
  key "Test" button answers without contacting a vendor, and the subscription CLIs are
  never executed.

Every HTML page gets a "Demo mode" banner (injected by middleware, so the static files
stay untouched), and ``GET /api/providers`` reports ``demo: true``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response

from llm_crossjury.core.app_settings import SETTINGS_NAME, AppSettingsStore
from llm_crossjury.core.attachments.store import AttachmentStore
from llm_crossjury.core.compare_service import CompareDeps, _cli_providers
from llm_crossjury.core.config import BudgetConfig, CostConfig
from llm_crossjury.core.cost.estimate import CostEstimator
from llm_crossjury.core.cost.ledger import SqliteLedger
from llm_crossjury.core.cost.pricing import PricingTable
from llm_crossjury.core.key_check import KeyTestResult
from llm_crossjury.core.providers.base import Backend, Provider
from llm_crossjury.core.registry.resolver import ModelRegistry, ModelResolver, ResolutionMode
from llm_crossjury.core.sessions.interfaces import AppSettings, BudgetSettings
from llm_crossjury.demo.providers import DemoProvider
from llm_crossjury.web import LOOPBACK_HOSTS, create_app
from llm_crossjury.web.app import CLI_PRODUCTS

REGISTRY_RELATIVE = Path("config") / "models.yaml"
"""The shipped registry, relative to the project root."""

BANNER_ID = "demo-banner"
BANNER_TEXT = (
    "Demo mode: the models are simulated and the answers are canned. "
    "No API keys, no network calls, no cost."
)
_BANNER_HTML = (
    f'<div id="{BANNER_ID}" class="demo-banner" role="status">'
    f"<strong>Demo mode.</strong> {BANNER_TEXT.removeprefix('Demo mode: ')}</div>"
)
_BANNER_CSS = """\
.demo-banner {
  padding: 6px 16px;
  font: 13px/1.4 system-ui, sans-serif;
  text-align: center;
  background: #fff4cc;
  color: #4a3b00;
  border-bottom: 1px solid #e6d27a;
}
@media (prefers-color-scheme: dark) {
  .demo-banner { background: #3a3000; color: #ffe9a3; border-bottom-color: #6b5a10; }
}
"""
DEMO_CSS_PATH = "/demo.css"
_PAGES_WITH_OWN_BADGE = frozenset({"/", "/index.html"})
"""Pages whose UI reads ``demo`` from ``/api/providers`` and shows its own badge."""


def shipped_registry_path() -> Path:
    """Locate ``config/models.yaml`` above this package (the shipped, verified registry).

    The user's ``models.local.yaml`` and ``LLM_CROSSJURY_MODELS`` are ignored on purpose,
    so the demo looks the same on every machine.

    Raises:
        FileNotFoundError: if the registry is not found (for example in a wheel install).
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / REGISTRY_RELATIVE
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{REGISTRY_RELATIVE} not found above the package")


def _api_adapters(registry: ModelRegistry) -> tuple[Provider, ...]:
    # Constructed only for capabilities(): constructing an adapter reads no key and
    # makes no call (see compare_service.build_default_deps).
    from llm_crossjury.core.providers.api import (  # noqa: PLC0415
        AnthropicApiProvider,
        GeminiApiProvider,
        OpenAIApiProvider,
        XaiApiProvider,
    )

    return (
        AnthropicApiProvider(registry=registry),
        OpenAIApiProvider(registry=registry),
        GeminiApiProvider(registry=registry),
        XaiApiProvider(registry=registry),
    )


def build_demo_deps(data_dir: Path, *, latency_scale: float = 1.0) -> CompareDeps:
    """Compare dependencies with simulated providers and state under ``data_dir``.

    Every provider is offered on ``api``; Anthropic, OpenAI and xAI also on ``cli``
    (subscription), as in a real install. No key is required (``key_envs`` is empty).

    Args:
        data_dir: Directory for the ledger and the attachment store (created if needed).
        latency_scale: Multiplies the simulated delays (``0`` for tests).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    registry = ModelRegistry.load(shipped_registry_path())
    pricing = PricingTable(registry)
    # Demo spend is simulated: generous limits so no confirmation or refusal interrupts
    # the walkthrough (the real defaults still apply to normal runs).
    budget = BudgetConfig(
        run_limit=Decimal("100"), confirm_above=Decimal("100"), daily_limit=Decimal("1000")
    )
    estimator = CostEstimator(
        pricing,
        cache_hit_rate=0.0,
        reasoning_multipliers=dict(CostConfig().reasoning_multipliers),
        currency=budget.currency,
    )
    providers: dict[tuple[str, Backend], Provider] = {}
    for adapter in _api_adapters(registry):
        providers[(adapter.name, Backend.API)] = DemoProvider(
            adapter.name, Backend.API, capabilities_from=adapter, latency_scale=latency_scale
        )
    for cli in _cli_providers(registry):
        product = CLI_PRODUCTS.get(cli.name)
        providers[(cli.name, Backend.CLI)] = DemoProvider(
            cli.name,
            Backend.CLI,
            capabilities_from=cli,
            latency_scale=latency_scale,
            product=product.product if product is not None else None,
        )
    return CompareDeps(
        providers=providers,
        key_envs={},
        registry=registry,
        resolver=ModelResolver(registry, ResolutionMode.NORMAL),
        pricing=pricing,
        estimator=estimator,
        ledger=SqliteLedger(data_dir / "ledger.sqlite3"),
        budget=budget,
        attachment_store=AttachmentStore(data_dir / "attachments"),
    )


async def demo_key_tester(provider: str) -> KeyTestResult:
    """Answer the settings page's key test without contacting any vendor."""
    return KeyTestResult(
        provider=provider,
        status="error",
        message="Demo mode does not test keys. Run llm-crossjury ui without --demo to test one.",
    )


def inject_banner(html: str) -> str:
    """Add the demo stylesheet and banner to an HTML page (unchanged if not HTML-like)."""
    link = f'<link rel="stylesheet" href="{DEMO_CSS_PATH}">'
    if "</head>" in html:
        html = html.replace("</head>", f"  {link}\n</head>", 1)
    if "<body>" in html:
        html = html.replace("<body>", f"<body>\n  {_BANNER_HTML}", 1)
    return html


def _add_banner(app: FastAPI) -> None:
    @app.get(DEMO_CSS_PATH)
    async def demo_css() -> Response:
        return Response(content=_BANNER_CSS, media_type="text/css; charset=utf-8")

    @app.middleware("http")
    async def banner(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path in _PAGES_WITH_OWN_BADGE:
            return response  # the compare page shows its own demo badge (#demo-badge)
        if not response.headers.get("content-type", "").startswith("text/html"):
            return response
        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is None:
            return response
        chunks = [
            chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
            async for chunk in body_iterator
        ]
        html = inject_banner(b"".join(chunks).decode("utf-8"))
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(content=html, status_code=response.status_code, headers=headers)


def create_demo_app(
    data_dir: Path,
    *,
    latency_scale: float = 1.0,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
) -> FastAPI:
    """Create the compare UI in demo mode, with all state under ``data_dir``.

    Args:
        data_dir: An empty directory owned by this app (normally a temporary one).
        latency_scale: Multiplies the simulated delays (``0`` for tests).
        allowed_hosts: Accepted ``Host`` header names, as for
            :func:`~llm_crossjury.web.create_app`.
    """
    deps = build_demo_deps(data_dir, latency_scale=latency_scale)
    config = data_dir / "config"
    keys_base = data_dir / "keys"
    for directory in (config, keys_base):
        directory.mkdir(parents=True, exist_ok=True)
    ui_state = config / "ui-state.json"
    # No key is set in a demo: skip the first-run wizard, which would otherwise open.
    ui_state.write_text(json.dumps({"onboarding_dismissed": True}) + "\n", encoding="utf-8")
    # Sessions and app settings live under data_dir too, never in the user's real data
    # or config directory. The demo's default budget is the generous simulated one.
    settings = AppSettingsStore(
        config / SETTINGS_NAME,
        defaults=AppSettings(
            budget=BudgetSettings(
                run_limit=deps.budget.run_limit,
                confirm_above=deps.budget.confirm_above,
                daily_limit=deps.budget.daily_limit,
            )
        ),
    )
    app = create_app(
        lambda: deps,
        allowed_hosts=allowed_hosts,
        key_base_dir=keys_base,
        key_store_path=config / "keys.env",
        ui_state_path=ui_state,
        key_tester=demo_key_tester,
        demo=True,
        sessions_path=data_dir / "sessions",
        settings_store=settings,
    )
    _add_banner(app)
    return app


__all__ = [
    "BANNER_ID",
    "BANNER_TEXT",
    "build_demo_deps",
    "create_demo_app",
    "demo_key_tester",
    "inject_banner",
    "shipped_registry_path",
]
