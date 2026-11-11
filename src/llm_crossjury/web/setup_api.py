"""One-click install and sign-in for the subscription CLIs (ADR 0002, amended 2026-11-09).

On an explicit click in the setup wizard or on the Settings page, the app runs the
vendor's own, official install or sign-in command for Claude Code, Codex or Grok Build.
The commands come only from the fixed whitelist :data:`DEFAULT_CATALOG`: the client
names a CLI and an action, never a command, and nothing here runs on its own.

Routes (JSON):

* ``GET /api/setup``: per CLI, the exact install command for this computer (or why
  none is available, e.g. Node.js missing for Codex), the sign-in modes and commands,
  and the job that is running, if any.
* ``POST /api/setup/{cli}/install`` with ``{}``: start the install job.
* ``POST /api/setup/{cli}/login`` with ``{"mode": "browser" | "device"}``: start the
  sign-in job. ``browser`` lets the CLI open the vendor's sign-in page; ``device``
  prints a verification link and code, returned as structured fields.
* ``GET /api/setup/jobs/{id}``: the job's state and a redacted tail of its output.
* ``POST /api/setup/jobs/{id}/cancel``: stop the job (its whole process tree).

Rules: one job per CLI at a time; a hard timeout (install 10 min, sign-in 5 min) kills
the process tree; programs are resolved with ``shutil.which`` on the effective PATH and
started from an argument list (``shell=False``). Where the vendor's installer is a
shell one-liner, the shell gets that fixed literal. The child gets the user's normal
environment minus every ``*_API_KEY`` variable. Output is redacted (tokens, paths, URL
paths and queries) before it is kept; only the device verification link and code are
passed through, because the user must see them. Credentials are never read. When a job
ends, the cached CLI check for that CLI is dropped so the next status is fresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.providers.cli._subprocess import effective_path, kill_process_tree, redact
from llm_crossjury.web.settings_api import DEFAULT_ALLOWED_HOSTS, check_request

SetupKind = Literal["install", "login"]
LoginMode = Literal["browser", "device"]
JobState = Literal["running", "succeeded", "failed", "cancelled", "timed_out"]
Platform = Literal["windows", "unix"]

INSTALL_TIMEOUT_S = 600.0
LOGIN_TIMEOUT_S = 300.0
MAX_TAIL_CHARS = 6000
MAX_TAIL_LINES = 80
MAX_FINISHED_JOBS = 20
MAX_BODY_BYTES = 1024

NODE_URL = "https://nodejs.org/"


# --------------------------------------------------------------------------- catalog


@dataclass(frozen=True)
class SetupCommand:
    """One fixed command line. ``argv[0]`` is a program name resolved on the PATH."""

    argv: tuple[str, ...]
    needs: tuple[str, ...] = ()
    """Other programs the command needs (e.g. ``curl`` for a shell installer)."""

    @property
    def programs(self) -> tuple[str, ...]:
        return (self.argv[0], *self.needs)

    @property
    def display(self) -> str:
        """The command as the user would type it."""
        return " ".join(f'"{a}"' if " " in a else a for a in self.argv)


@dataclass(frozen=True)
class CliSetup:
    """The whitelisted install and sign-in commands of one subscription CLI."""

    product: str
    install: Mapping[Platform, tuple[SetupCommand, ...]]
    """Per platform, the install commands in order of preference."""
    login: Mapping[LoginMode, SetupCommand]
    missing_message: str
    """Shown when no install command can run here (a required program is missing)."""
    missing_url: str | None = None


def _ps(script: str) -> SetupCommand:
    return SetupCommand(("powershell", "-NoProfile", "-NonInteractive", "-Command", script))


def _sh(script: str) -> SetupCommand:
    return SetupCommand(("bash", "-c", script), needs=("curl",))


DEFAULT_CATALOG: dict[str, CliSetup] = {
    # The official commands from each vendor's install docs (same as the README).
    "anthropic": CliSetup(
        product="Claude Code",
        install={
            "windows": (
                SetupCommand(
                    (
                        "winget",
                        "install",
                        "--id",
                        "Anthropic.ClaudeCode",
                        "--exact",
                        "--accept-source-agreements",
                        "--accept-package-agreements",
                    )
                ),
                _ps("irm https://claude.ai/install.ps1 | iex"),
            ),
            "unix": (_sh("curl -fsSL https://claude.ai/install.sh | bash"),),
        },
        login={"browser": SetupCommand(("claude", "auth", "login", "--claudeai"))},
        missing_message="Neither winget nor PowerShell was found. Run the command yourself.",
    ),
    "openai": CliSetup(
        product="Codex CLI",
        install={
            "windows": (SetupCommand(("npm", "install", "-g", "@openai/codex")),),
            "unix": (SetupCommand(("npm", "install", "-g", "@openai/codex")),),
        },
        login={
            "browser": SetupCommand(("codex", "login")),
            "device": SetupCommand(("codex", "login", "--device-auth")),
        },
        missing_message=(
            "Codex installs with npm, which comes with Node.js. Install Node.js first, "
            "then click Check again."
        ),
        missing_url=NODE_URL,
    ),
    "xai": CliSetup(
        product="Grok Build",
        install={
            "windows": (_ps("irm https://x.ai/cli/install.ps1 | iex"),),
            "unix": (_sh("curl -fsSL https://x.ai/cli/install.sh | bash"),),
        },
        login={
            "browser": SetupCommand(("grok", "login", "--oauth")),
            "device": SetupCommand(("grok", "login", "--device-auth")),
        },
        missing_message="PowerShell (Windows) or bash and curl were not found.",
    ),
}
"""The only commands this module ever runs."""


def current_platform() -> Platform:
    return "windows" if sys.platform == "win32" else "unix"


def which_on_path(program: str) -> str | None:
    """``shutil.which`` on the effective PATH (picks up programs installed meanwhile)."""
    return shutil.which(program, path=effective_path())


def setup_env() -> dict[str, str]:
    """The user's environment for their own tools, minus every API key."""
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_API_KEY")}
    env["PATH"] = effective_path()
    return env


# --------------------------------------------------------------------------- output

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_URL = re.compile(r"https?://[^\s\"'<>()\[\]]+")
_CODE = re.compile(r"\b([A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8})+)\b")
_DEVICE_HINT = re.compile(r"device|activate|verif|code|pair", re.IGNORECASE)


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _short_url(url: str) -> str:
    """Scheme and host only: paths and queries can carry one-time secrets."""
    match = re.match(r"(https?)://([^/?#\s]+)", url)
    if match is None:
        return "[link]"
    rest = url[match.end() :]
    return f"{match.group(1)}://{match.group(2)}" + ("/..." if rest.strip("/") else "")


def sanitize_line(line: str, keep_url: str | None = None) -> str:
    """Redact one output line; URLs keep their host only (``keep_url`` stays whole).

    Links are set aside before :func:`redact` (which would treat them as paths) and
    put back afterwards in their shortened form.
    """
    links: list[str] = []

    def hold(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(".,;:")
        links.append(url if keep_url is not None and url == keep_url else _short_url(url))
        return f" LINKMARK{_letters(len(links) - 1)} {match.group(0)[len(url) :]}"

    out = redact(_URL.sub(hold, line), limit=400)
    for index, link in enumerate(links):
        out = out.replace(f"LINKMARK{_letters(index)}", link, 1)
    return out


def _letters(index: int) -> str:
    """A digit-free marker suffix (``A``, ``B``, ... ``AA``), so redaction leaves it."""
    out = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        out = chr(65 + rest) + out
    return out


def parse_device_code(text: str) -> tuple[str, str] | None:
    """Find the verification link and user code a device sign-in prints.

    Returns ``(https link, code)`` or ``None``. Only ``https`` links are accepted; a
    link mentioning device, activate, verify or code wins over others.
    """
    clean = strip_ansi(text)
    urls = [u.rstrip(".,;:") for u in _URL.findall(clean) if u.startswith("https://")]
    if not urls:
        return None
    url = next((u for u in urls if _DEVICE_HINT.search(u)), urls[0])
    codes = [c for c in _CODE.findall(clean) if any(ch.isalpha() for ch in c)]
    if not codes:
        return None
    return url, codes[0]


# --------------------------------------------------------------------------- jobs


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeviceCode(_Frozen):
    url: str
    code: str


class JobView(_Frozen):
    id: str
    cli: str
    product: str
    kind: SetupKind
    mode: LoginMode | None
    state: JobState
    command: str
    exit_code: int | None
    elapsed_s: float
    output: str
    """Redacted tail of the combined output."""
    device: DeviceCode | None
    message: str


@dataclass
class _Job:
    id: str
    cli: str
    product: str
    kind: SetupKind
    mode: LoginMode | None
    command: SetupCommand
    started: float
    state: JobState = "running"
    exit_code: int | None = None
    ended: float | None = None
    lines: list[str] = field(default_factory=list)
    partial: str = ""
    raw: str = ""
    device: DeviceCode | None = None
    message: str = ""
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False

    def view(self) -> JobView:
        end = self.ended if self.ended is not None else time.monotonic()
        keep = self.device.url if self.device else None
        lines = [sanitize_line(line, keep) for line in self.lines[-MAX_TAIL_LINES:]]
        if self.partial.strip():
            lines.append(sanitize_line(self.partial, keep))
        text = "\n".join(line for line in lines if line)
        return JobView(
            id=self.id,
            cli=self.cli,
            product=self.product,
            kind=self.kind,
            mode=self.mode,
            state=self.state,
            command=self.command.display,
            exit_code=self.exit_code,
            elapsed_s=round(end - self.started, 1),
            output=text[-MAX_TAIL_CHARS:],
            device=self.device,
            message=self.message,
        )


class SetupError(Exception):
    """A request the job manager refuses (``code`` and ``status`` for the response)."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


Which = Callable[[str], str | None]


class SetupJobs:
    """Starts, tracks and stops whitelisted install and sign-in jobs.

    Args:
        catalog: The whitelist (default :data:`DEFAULT_CATALOG`).
        which: Resolves a program name to an absolute path, or ``None``.
        env: Builds the child environment.
        platform: Picks the install commands.
        on_finish: Called with the CLI name when a job ends (drops cached checks).
        timeouts: Seconds per kind.
    """

    def __init__(
        self,
        catalog: Mapping[str, CliSetup] | None = None,
        *,
        which: Which = which_on_path,
        env: Callable[[], dict[str, str]] = setup_env,
        platform: Platform | None = None,
        on_finish: Callable[[str], None] | None = None,
        timeouts: Mapping[SetupKind, float] | None = None,
    ) -> None:
        self.catalog = dict(catalog if catalog is not None else DEFAULT_CATALOG)
        self._which = which
        self._env = env
        self.platform: Platform = platform or current_platform()
        self.on_finish = on_finish
        self._timeouts: dict[SetupKind, float] = {
            "install": INSTALL_TIMEOUT_S,
            "login": LOGIN_TIMEOUT_S,
            **(timeouts or {}),
        }
        self._jobs: dict[str, _Job] = {}

    # ------------------------------------------------------------------ queries

    def install_command(self, cli: str) -> SetupCommand | None:
        """The first install command whose programs are all present, or ``None``."""
        for command in self.catalog[cli].install.get(self.platform, ()):
            if all(self._which(p) is not None for p in command.programs):
                return command
        return None

    def running(self, cli: str) -> _Job | None:
        return next((j for j in self._jobs.values() if j.cli == cli and j.state == "running"), None)

    def get(self, job_id: str) -> _Job | None:
        return self._jobs.get(job_id)

    # ------------------------------------------------------------------ actions

    def start(self, cli: str, kind: SetupKind, mode: LoginMode | None = None) -> _Job:
        """Start a job. Raises :class:`SetupError` for anything not whitelisted."""
        setup = self.catalog.get(cli)
        if setup is None:
            raise SetupError(404, "unknown_cli", "Unknown command-line tool.")
        if self.running(cli) is not None:
            raise SetupError(409, "job_running", f"{setup.product} is already busy.")
        if kind == "install":
            command = self.install_command(cli)
            if command is None:
                raise SetupError(409, "missing_program", setup.missing_message)
            mode = None
        else:
            mode = mode or "browser"
            found = setup.login.get(mode)
            if found is None:
                raise SetupError(400, "unsupported_mode", "This sign-in mode is not offered.")
            command = found
        executable = self._which(command.argv[0])
        if executable is None:
            what = setup.product if kind == "login" else command.argv[0]
            raise SetupError(409, "missing_program", f"{what} was not found. Install it first.")
        job = _Job(
            id=secrets.token_hex(16),
            cli=cli,
            product=setup.product,
            kind=kind,
            mode=mode,
            command=command,
            started=time.monotonic(),
        )
        self._jobs[job.id] = job
        self._prune()
        job.task = asyncio.get_running_loop().create_task(self._run(job, executable))
        return job

    def cancel(self, job_id: str) -> _Job | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.state == "running":
            job.cancel_requested = True
            if job.proc is not None:
                kill_process_tree(job.proc)
        return job

    async def shutdown(self) -> None:
        """Stop every running job (server shutdown)."""
        for job in list(self._jobs.values()):
            self.cancel(job.id)
            if job.task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(job.task, timeout=5)

    # ------------------------------------------------------------------ internals

    def _prune(self) -> None:
        done = [j for j in self._jobs.values() if j.state != "running"]
        for job in done[:-MAX_FINISHED_JOBS] if len(done) > MAX_FINISHED_JOBS else []:
            self._jobs.pop(job.id, None)

    def _feed(self, job: _Job, chunk: str) -> None:
        text = strip_ansi(chunk)
        if job.kind == "login" and job.mode == "device" and job.device is None:
            job.raw = (job.raw + text)[-8000:]
            found = parse_device_code(job.raw)
            if found is not None:
                job.device = DeviceCode(url=found[0], code=found[1])
        parts = (job.partial + text).replace("\r\n", "\n").split("\n")
        job.partial = parts.pop()
        for part in parts:
            line = part.split("\r")[-1]  # progress bars redraw with \r: keep the last
            if line.strip():
                job.lines.append(line)
        job.partial = job.partial.split("\r")[-1]
        if len(job.lines) > MAX_TAIL_LINES * 2:
            del job.lines[:-MAX_TAIL_LINES]

    async def _run(self, job: _Job, executable: str) -> None:
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        workdir = tempfile.mkdtemp(prefix="crossjury-setup-")
        try:
            try:
                job.proc = await asyncio.create_subprocess_exec(
                    executable,
                    *job.command.argv[1:],
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=workdir,
                    env=self._env(),
                    **kwargs,
                )
            except OSError as exc:
                job.state = "failed"
                job.message = f"Could not start the command ({type(exc).__name__})."
                return
            try:
                await asyncio.wait_for(self._pump(job), timeout=self._timeouts[job.kind])
            except TimeoutError:
                kill_process_tree(job.proc)
                job.state = "timed_out"
                minutes = round(self._timeouts[job.kind] / 60)
                job.message = f"Stopped after {minutes} min without finishing."
            with contextlib.suppress(Exception):
                await asyncio.wait_for(job.proc.wait(), timeout=15)
            job.exit_code = job.proc.returncode
            if job.state == "running":
                if job.cancel_requested:
                    job.state = "cancelled"
                    job.message = "Cancelled."
                elif job.exit_code == 0:
                    job.state = "succeeded"
                    job.message = "Done."
                else:
                    job.state = "failed"
                    job.message = f"The command ended with exit code {job.exit_code}."
        except asyncio.CancelledError:
            if job.proc is not None:
                kill_process_tree(job.proc)
            job.state = "cancelled"
            job.message = "Cancelled."
            raise
        finally:
            job.ended = time.monotonic()
            shutil.rmtree(workdir, ignore_errors=True)
            if self.on_finish is not None:
                with contextlib.suppress(Exception):
                    self.on_finish(job.cli)

    async def _pump(self, job: _Job) -> None:
        proc = job.proc
        if proc is None or proc.stdout is None:
            return
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            self._feed(job, chunk.decode("utf-8", errors="replace"))
        await proc.wait()


# --------------------------------------------------------------------------- API


class InstallInfo(_Frozen):
    available: bool
    command: str | None
    """The exact command the Install button runs."""
    missing: str | None
    missing_url: str | None


class LoginOption(_Frozen):
    mode: LoginMode
    command: str


class CliSetupInfo(_Frozen):
    name: str
    product: str
    install: InstallInfo
    login: tuple[LoginOption, ...]
    job: JobView | None
    """The running job, if any."""


class SetupInfo(_Frozen):
    enabled: bool
    platform: Platform
    clis: tuple[CliSetupInfo, ...]


class _ErrorBody(_Frozen):
    code: str
    detail: str


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=_ErrorBody(code=code, detail=detail).model_dump()
    )


async def _body(request: Request) -> dict[str, object] | None:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def create_setup_router(
    jobs: SetupJobs | None,
    *,
    allowed_hosts: Sequence[str] = DEFAULT_ALLOWED_HOSTS,
) -> APIRouter:
    """Create the ``/api/setup`` routes. ``jobs=None`` disables them (demo mode)."""
    hosts = frozenset(h.strip("[]").lower() for h in allowed_hosts)
    router = APIRouter()

    def disabled() -> JSONResponse:
        return _error(404, "setup_disabled", "Install and sign-in are not available here.")

    @router.get("/api/setup", response_model=None)
    async def info(request: Request) -> SetupInfo | JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        if jobs is None:
            return SetupInfo(enabled=False, platform=current_platform(), clis=())
        rows: list[CliSetupInfo] = []
        for name, setup in jobs.catalog.items():
            command = await asyncio.to_thread(jobs.install_command, name)
            running = jobs.running(name)
            rows.append(
                CliSetupInfo(
                    name=name,
                    product=setup.product,
                    install=InstallInfo(
                        available=command is not None,
                        command=command.display if command else None,
                        missing=None if command else setup.missing_message,
                        missing_url=None if command else setup.missing_url,
                    ),
                    login=tuple(
                        LoginOption(mode=mode, command=c.display) for mode, c in setup.login.items()
                    ),
                    job=running.view() if running else None,
                )
            )
        return SetupInfo(enabled=True, platform=jobs.platform, clis=tuple(rows))

    async def start(request: Request, cli: str, kind: SetupKind) -> JobView | JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        if jobs is None:
            return disabled()
        body = await _body(request)
        allowed = {"mode"} if kind == "login" else set()
        if body is None or not set(body) <= allowed:
            return _error(400, "invalid_body", 'Send {} or {"mode": "browser"}.')
        mode = body.get("mode", "browser")
        if mode not in ("browser", "device"):
            return _error(400, "unsupported_mode", "Mode must be browser or device.")
        try:
            job = jobs.start(cli, kind, mode if kind == "login" else None)
        except SetupError as exc:
            return _error(exc.status, exc.code, exc.detail)
        return job.view()

    @router.post("/api/setup/{cli}/install", response_model=None)
    async def install(cli: str, request: Request) -> JobView | JSONResponse:
        return await start(request, cli, "install")

    @router.post("/api/setup/{cli}/login", response_model=None)
    async def login(cli: str, request: Request) -> JobView | JSONResponse:
        return await start(request, cli, "login")

    @router.get("/api/setup/jobs/{job_id}", response_model=None)
    async def job(job_id: str, request: Request) -> JobView | JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        found = jobs.get(job_id) if jobs is not None else None
        return found.view() if found is not None else _error(404, "unknown_job", "No such job.")

    @router.post("/api/setup/jobs/{job_id}/cancel", response_model=None)
    async def cancel(job_id: str, request: Request) -> JobView | JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        found = jobs.cancel(job_id) if jobs is not None else None
        return found.view() if found is not None else _error(404, "unknown_job", "No such job.")

    return router


__all__ = [
    "DEFAULT_CATALOG",
    "CliSetup",
    "SetupCommand",
    "SetupError",
    "SetupJobs",
    "create_setup_router",
    "parse_device_code",
    "sanitize_line",
]
