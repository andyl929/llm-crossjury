"""Settings API and pages: API-key management and the first-run onboarding wizard.

Routes (all JSON unless noted):

* ``GET /settings`` (HTML), ``/settings.js``, ``/settings.css``: the Settings -> API
  Keys page. ``/settings?onboarding=1`` opens the onboarding wizard.
* ``GET /api/keys``: per provider, set / not set and the source (environment, ``.env``,
  key file, settings), never any part of a key.
* ``PUT /api/keys/{provider}`` with ``{"key": "..."}``: save in the settings store.
* ``DELETE /api/keys/{provider}``: delete the key saved in settings (other sources are
  never touched; the UI explains how to remove them).
* ``POST /api/keys/{provider}/test``: list the vendor's models with the current key.
* ``GET /api/onboarding``: whether to show the wizard, plus its static content.
* ``POST /api/onboarding/dismiss`` with ``{"dismissed": true|false}``.

Guards: the same as the main app (Host allow-list, JSON bodies, cross-origin rejection),
checked again here so the router is safe even on its own. The ``PUT`` body is parsed by
hand so that no validation error can ever echo the submitted key.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from llm_crossjury.core import key_check
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.key_check import KeyTestResult
from llm_crossjury.core.keys import (
    PROVIDER_KEY_ENVS,
    InvalidKeyError,
    KeyInfo,
    KeySource,
    KeyStoreError,
    config_dir,
    delete_key,
    key_file_path,
    key_sources,
    key_store_path,
    save_key,
)

DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
MAX_BODY_BYTES = 8192
UI_STATE_NAME = "ui-state.json"

KeyTester = Callable[[str], Awaitable[KeyTestResult]]


class _Vendor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    key_page_url: str
    key_page_label: str


VENDORS: dict[str, _Vendor] = {
    # Verified 2026-11-08 against each vendor's official docs.
    "anthropic": _Vendor(
        label="Anthropic",
        key_page_url="https://platform.claude.com/settings/keys",
        key_page_label="Claude Console: API keys",
    ),
    "openai": _Vendor(
        label="OpenAI",
        key_page_url="https://platform.openai.com/settings/organization/api-keys",
        key_page_label="OpenAI Platform: API keys",
    ),
    "gemini": _Vendor(
        label="Google Gemini",
        key_page_url="https://aistudio.google.com/apikey",
        key_page_label="Google AI Studio: API keys",
    ),
    "xai": _Vendor(
        label="xAI",
        key_page_url="https://console.x.ai/team/default/api-keys",
        key_page_label="xAI Console: API keys",
    ),
}

SOURCE_LABELS: dict[KeySource, str] = {
    "environment": "Environment variable",
    "dotenv": ".env file",
    "key_file": "Key file",
    "settings": "Saved in settings",
}

_STATIC: dict[str, tuple[str, str]] = {
    "/settings": ("settings.html", "text/html; charset=utf-8"),
    "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/settings.css": ("settings.css", "text/css; charset=utf-8"),
}


# --------------------------------------------------------------------------- models


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KeyRow(_Frozen):
    """One provider's key status. Never contains any part of the key."""

    name: str
    label: str
    env_var: str
    status: Literal["set", "not set"]
    source: KeySource | None
    source_label: str | None
    stored: bool
    """A key is saved in settings (``DELETE`` is possible)."""
    overridden: bool
    """A saved key exists but a higher source provides the key in use."""
    how_to_remove: str | None
    key_page_url: str
    key_page_label: str


class KeysResponse(_Frozen):
    providers: tuple[KeyRow, ...]
    store_path: str | None
    """Location of the settings key store (``None`` if unusable)."""
    store_note: str


class SaveResponse(_Frozen):
    provider: KeyRow
    applied: bool
    """The saved key is now the one in use."""
    message: str


class OnboardingProvider(_Frozen):
    name: str
    label: str
    env_var: str
    key_page_url: str
    key_page_label: str


class OnboardingBudget(_Frozen):
    """Effective budget defaults (read-only)."""

    currency: str
    run_limit: Decimal
    confirm_above: Decimal
    daily_limit: Decimal


class OnboardingResponse(_Frozen):
    show: bool
    """No provider has a key from any source and the wizard was not dismissed."""
    any_key_set: bool
    dismissed: bool
    subscription_mode: Literal["coming_soon"]
    providers: tuple[OnboardingProvider, ...]
    budget: OnboardingBudget


class DismissResponse(_Frozen):
    dismissed: bool


class ErrorResponse(_Frozen):
    code: str
    detail: str


# --------------------------------------------------------------------------- helpers


def _error(status: int, code: str, detail: str) -> JSONResponse:
    body = ErrorResponse(code=code, detail=detail)
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def _host_name(host_header: str) -> str:
    """Strip the port from a ``Host`` header (handles ``[::1]:8765``)."""
    if host_header.startswith("["):
        return host_header[1 : host_header.find("]")] if "]" in host_header else host_header
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def check_request(request: Request, hosts: frozenset[str]) -> JSONResponse | None:
    """Apply the app's request guards; return an error response or ``None``.

    Host allow-list (DNS rebinding), JSON-only bodies for ``POST``/``PUT`` (forcing a
    CORS preflight that is never granted), and rejection of cross-origin writes.
    """
    host = _host_name(request.headers.get("host", "")).lower()
    if "*" not in hosts and host not in hosts:
        return _error(400, "bad_host", "Host not allowed.")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.method != "DELETE":
            content_type = request.headers.get("content-type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                return _error(415, "unsupported_media_type", "Send JSON.")
        origin = request.headers.get("origin")
        if (
            origin is not None
            and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()
        ):
            return _error(403, "cross_origin", "Cross-origin requests are not allowed.")
    return None


async def _json_object(request: Request) -> dict[str, object] | None:
    """Read a small JSON object body, or ``None`` if it is not one."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _how_to_remove(info: KeyInfo, base_dir: Path) -> str | None:
    if info.source == "environment":
        return (
            f"Set by the environment variable {info.env_var} in the shell or system "
            "settings that started this app. Remove it there, then restart the app."
        )
    if info.source == "dotenv":
        return (
            f"Read from the {info.env_var} line in {base_dir / '.env'}. Delete that line, "
            "then restart the app."
        )
    if info.source == "key_file":
        return (
            f"Read from the key file {key_file_path(base_dir, info.provider)}. Delete the "
            "file, then restart the app."
        )
    if info.source == "settings":
        return "Saved in settings. Use Delete to remove it."
    return None


def _row(info: KeyInfo, base_dir: Path) -> KeyRow:
    vendor = VENDORS[info.provider]
    return KeyRow(
        name=info.provider,
        label=vendor.label,
        env_var=info.env_var,
        status="set" if info.is_set else "not set",
        source=info.source,
        source_label=SOURCE_LABELS[info.source] if info.source else None,
        stored=info.stored,
        overridden=info.stored and info.source not in (None, "settings"),
        how_to_remove=_how_to_remove(info, base_dir),
        key_page_url=vendor.key_page_url,
        key_page_label=vendor.key_page_label,
    )


def _read_dismissed(path: Path | None) -> bool:
    if path is None or not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("onboarding_dismissed") is True


def _write_dismissed(path: Path, dismissed: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"onboarding_dismissed": dismissed}) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- router


@dataclass(frozen=True)
class _Context:
    base_dir: Path
    hosts: frozenset[str]
    store_path: Path | None
    ui_state_path: Path | None
    tester: KeyTester
    budget: BudgetConfig

    def store(self) -> Path:
        return self.store_path if self.store_path is not None else key_store_path()

    def ui_state(self) -> Path | None:
        if self.ui_state_path is not None:
            return self.ui_state_path
        try:
            return config_dir() / UI_STATE_NAME
        except KeyStoreError:
            return None

    def rows(self) -> tuple[KeyRow, ...]:
        infos = key_sources(self.base_dir, PROVIDER_KEY_ENVS, store_path=self.store())
        return tuple(_row(info, self.base_dir) for info in infos.values())


def _add_page_routes(router: APIRouter, ctx: _Context) -> None:
    static = resources.files("llm_crossjury.web").joinpath("static")
    pages = {
        path: (static.joinpath(name).read_text(encoding="utf-8"), media)
        for path, (name, media) in _STATIC.items()
    }

    def page(path: str) -> Callable[[Request], Awaitable[Response]]:
        async def serve(request: Request) -> Response:
            if (refused := check_request(request, ctx.hosts)) is not None:
                return refused
            text, media = pages[path]
            return Response(content=text, media_type=media)

        return serve

    for path in _STATIC:
        router.add_api_route(path, page(path), methods=["GET"], include_in_schema=False)


def _add_key_routes(router: APIRouter, ctx: _Context) -> None:
    @router.get("/api/keys", response_model=None)
    async def list_keys(request: Request) -> KeysResponse | JSONResponse:
        if (refused := check_request(request, ctx.hosts)) is not None:
            return refused
        try:
            location: str | None = str(ctx.store())
            providers = await asyncio.to_thread(ctx.rows)
        except KeyStoreError as exc:
            return _error(500, "key_store", str(exc))
        return KeysResponse(
            providers=providers,
            store_path=location,
            store_note=(
                "Keys saved here are stored in this file, readable only by your user "
                "account (file mode 0600 on macOS/Linux; your user profile's permissions "
                "on Windows). They are never shown again after saving."
            ),
        )

    @router.put("/api/keys/{provider}", response_model=None)
    async def put_key(provider: str, request: Request) -> SaveResponse | JSONResponse:
        if (refused := check_request(request, ctx.hosts)) is not None:
            return refused
        if provider not in PROVIDER_KEY_ENVS:
            return _error(404, "unknown_provider", "Unknown provider.")
        body = await _json_object(request)
        value = body.get("key") if body is not None else None
        if body is None or set(body) != {"key"} or not isinstance(value, str):
            return _error(400, "invalid_body", 'Send {"key": "<your API key>"}.')
        try:
            info = await asyncio.to_thread(
                save_key, provider, value, base_dir=ctx.base_dir, store_path=ctx.store()
            )
        except InvalidKeyError as exc:
            return _error(400, "invalid_key", str(exc))
        except KeyStoreError as exc:
            return _error(500, "key_store", str(exc))
        row = _row(info, ctx.base_dir)
        applied = info.source == "settings"
        message = (
            "Saved. The key is in use now."
            if applied
            else (
                f"Saved, but the key in use comes from: {row.source_label}. That source "
                "takes precedence over settings."
            )
        )
        return SaveResponse(provider=row, applied=applied, message=message)

    @router.delete("/api/keys/{provider}", response_model=None)
    async def remove_key(provider: str, request: Request) -> KeyRow | JSONResponse:
        if (refused := check_request(request, ctx.hosts)) is not None:
            return refused
        if provider not in PROVIDER_KEY_ENVS:
            return _error(404, "unknown_provider", "Unknown provider.")
        try:
            deleted = await asyncio.to_thread(
                delete_key, provider, base_dir=ctx.base_dir, store_path=ctx.store()
            )
            current = await asyncio.to_thread(ctx.rows)
        except KeyStoreError as exc:
            return _error(500, "key_store", str(exc))
        row = next(r for r in current if r.name == provider)
        if not deleted:
            return _error(
                409,
                "not_in_settings",
                "No key is saved in settings for this provider. "
                + (row.how_to_remove or "Nothing to delete."),
            )
        return row

    @router.post("/api/keys/{provider}/test", response_model=None)
    async def test_provider_key(provider: str, request: Request) -> KeyTestResult | JSONResponse:
        if (refused := check_request(request, ctx.hosts)) is not None:
            return refused
        if provider not in PROVIDER_KEY_ENVS:
            return _error(404, "unknown_provider", "Unknown provider.")
        try:
            return await ctx.tester(provider)
        except Exception:  # never let an SDK message (or a key) reach the client
            return KeyTestResult(provider=provider, status="error", message="The check failed.")


def _add_onboarding_routes(router: APIRouter, ctx: _Context) -> None:
    @router.get("/api/onboarding", response_model=None)
    async def onboarding(request: Request) -> OnboardingResponse | JSONResponse:
        if (refused := check_request(request, ctx.hosts)) is not None:
            return refused
        try:
            infos = await asyncio.to_thread(ctx.rows)
        except KeyStoreError as exc:
            return _error(500, "key_store", str(exc))
        any_set = any(r.status == "set" for r in infos)
        dismissed = await asyncio.to_thread(_read_dismissed, ctx.ui_state())
        return OnboardingResponse(
            show=not any_set and not dismissed,
            any_key_set=any_set,
            dismissed=dismissed,
            subscription_mode="coming_soon",
            providers=tuple(
                OnboardingProvider(
                    name=r.name,
                    label=r.label,
                    env_var=r.env_var,
                    key_page_url=r.key_page_url,
                    key_page_label=r.key_page_label,
                )
                for r in infos
            ),
            budget=OnboardingBudget(
                currency=ctx.budget.currency,
                run_limit=ctx.budget.run_limit,
                confirm_above=ctx.budget.confirm_above,
                daily_limit=ctx.budget.daily_limit,
            ),
        )

    @router.post("/api/onboarding/dismiss", response_model=None)
    async def dismiss(request: Request) -> DismissResponse | JSONResponse:
        if (refused := check_request(request, ctx.hosts)) is not None:
            return refused
        body = await _json_object(request)
        flag = body.get("dismissed", True) if body is not None else None
        if not isinstance(flag, bool):
            return _error(400, "invalid_body", 'Send {"dismissed": true}.')
        path = ctx.ui_state()
        if path is None:
            return _error(500, "config_dir", "The config directory is not usable.")
        try:
            await asyncio.to_thread(_write_dismissed, path, flag)
        except OSError as exc:
            return _error(500, "config_dir", f"Cannot save the setting: {exc.strerror}")
        return DismissResponse(dismissed=flag)


def create_settings_router(
    *,
    base_dir: Path,
    allowed_hosts: Sequence[str] = DEFAULT_ALLOWED_HOSTS,
    store_path: Path | None = None,
    ui_state_path: Path | None = None,
    tester: KeyTester | None = None,
    budget: BudgetConfig | None = None,
) -> APIRouter:
    """Create the settings router (key management, onboarding, settings page).

    Args:
        base_dir: Directory holding ``.env`` and the ``.<provider>.key`` files.
        allowed_hosts: Accepted ``Host`` names (without port); ``"*"`` accepts any.
        store_path: Key store override (default: ``<config dir>/keys.env``).
        ui_state_path: Onboarding state file override (default:
            ``<config dir>/ui-state.json``).
        tester: Key check override (default: :func:`key_check.check_key`).
        budget: Budget shown in the wizard (default: :class:`BudgetConfig` defaults).
    """
    ctx = _Context(
        base_dir=base_dir,
        hosts=frozenset(h.strip("[]").lower() for h in allowed_hosts),
        store_path=store_path,
        ui_state_path=ui_state_path,
        tester=tester if tester is not None else key_check.check_key,
        budget=budget if budget is not None else BudgetConfig(),
    )
    router = APIRouter()
    _add_page_routes(router, ctx)
    _add_key_routes(router, ctx)
    _add_onboarding_routes(router, ctx)
    return router


__all__ = ["VENDORS", "check_request", "create_settings_router"]
