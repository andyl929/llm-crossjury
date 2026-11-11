"""Subprocess scaffolding for the ``cli`` backend.

Every rule here is mandated by ADR 0002; read it before changing anything.

In short: the vendor CLI is resolved with :func:`shutil.which` and started with an
argument list (never a shell), the prompt goes over stdin, the child sees only an
allowlisted environment and an empty temporary working directory, the whole process
tree is killed on timeout, stdout is capped (overflow is an error, never truncation),
and stderr is redacted before it can reach any message. llm-crossjury never reads,
copies or stores anything from a CLI's config or credential directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_crossjury.core.providers.api._common import ModelLookup, utc_now
from llm_crossjury.core.providers.base import (
    Backend,
    Capabilities,
    CompletionRequest,
    CompletionResult,
    Message,
    ProviderError,
    ProviderStatus,
    ProviderUnavailableError,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.registry.schema import ModelEntry, ReasoningControlType

ENV_ALLOWLIST: tuple[str, ...] = (
    # Executable lookup: the CLI (or its Node.js shim) must find its own runtime.
    "PATH",
    # POSIX home: CLIs locate their own config/login directory relative to it.
    "HOME",
    # macOS/Linux user name: keychain/secret-service lookups by the CLI are keyed on it.
    "USER",
    "LOGNAME",
    # Windows profile directories: where Windows CLIs keep their own login/config.
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    # Windows fallback for the home directory: Node.js os.homedir() and several
    # runtimes build it from HOMEDRIVE+HOMEPATH when USERPROFILE is unavailable.
    "HOMEDRIVE",
    "HOMEPATH",
    # Windows system root: required by the C runtime, Winsock and cmd.exe.
    "SYSTEMROOT",
    # Windows executable extensions: npm .cmd shims and Node.js child lookups need it.
    "PATHEXT",
    # Windows command interpreter: .cmd/.bat shims are executed through %ComSpec%.
    "COMSPEC",
    # Windows machine-wide data: vendor-managed (enterprise) policy files live here;
    # without it a CLI cannot apply its administrator's managed settings.
    "PROGRAMDATA",
    # Temporary directories: CLIs write scratch files there.
    "TEMP",
    "TMP",
    "TMPDIR",
    # Locale: keeps CLI output encoding (UTF-8) stable.
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    # XDG base directories (Linux): CLIs following the spec keep config, data, cache
    # and runtime sockets under these.
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    # Linux session bus: a CLI that stores its login in the desktop keyring
    # (secret service) reaches it over D-Bus. The address is passed, never used here.
    "DBUS_SESSION_BUS_ADDRESS",
    # Grok Build CLI home override: a directory path (not a secret) that tells the
    # CLI where its own config/login lives, for users who relocated it.
    "GROK_HOME",
)
"""Variables passed through to the child. Values are passed, never read or logged.

The child needs its home/config directories to find *its own* login; llm-crossjury never
opens anything under them. ``*_API_KEY`` variables are deliberately absent (and are
filtered again by :func:`build_child_env` even if an adapter lists one by mistake).
"""

DEFAULT_TIMEOUT_S = 600.0
"""Hard wall-clock limit when the request does not set ``timeout_s``."""

DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
"""Default stdout cap."""

MAX_STDERR_BYTES = 64 * 1024
"""Stderr is only diagnostic: bytes beyond this are discarded (never surfaced)."""

CHECK_TIMEOUT_S = 30.0
"""Timeout of the ``--version`` run in :meth:`CliProviderBase.check`."""

_MAX_DETAIL_CHARS = 300
_MIN_TOKEN_LEN = 20
_READ_CHUNK = 65536
_REDACTED = "[REDACTED]"

_SAFE_ARG = re.compile(r"^[A-Za-z0-9._:/@=+\[\]-]+$")
"""Registry-derived argv tokens may only use these characters. This keeps cmd.exe
metacharacters (``& | < > ^ % "`` and whitespace) out of argv for ``.cmd`` shims."""

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bearer/basic credentials and authorization headers.
    re.compile(r"(?i)\b(bearer|basic)\s+\S+"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+"),
    # key=value / key: value pairs whose name suggests a secret.
    re.compile(
        r"(?i)\b[\w.-]*(api[_-]?key|token|secret|password|passwd|cookie|credential)"
        r"[\w.-]*\s*[:=]\s*\S+"
    ),
    # Vendor-style key prefixes (sk-..., xai-..., AIza..., ghp_..., etc.).
    # Anything following them up to whitespace is dropped, masked forms included.
    re.compile(r"\b(sk|xai|ghp|gho|ghu|ghs|glpat|AIza|ya29)[-_.]\S*"),
    # Masked key fragments such as "abcd1234...wxyz9" or "abcd…wxyz" or "ab***yz",
    # as printed by some CLIs' status commands.
    re.compile(r"\S*[A-Za-z0-9](?:…|\.{3,}|\*{3,})[A-Za-z0-9]\S*"),
    # JWT-like dotted base64 triples.
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),
    # Filesystem paths (may name credential files or reveal profile directories).
    re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s\\/\"']+){2,}[\\/]?"),
    re.compile(r"~[\\/]\S*"),
)
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_+/=-]*\d[A-Za-z0-9_+/=-]*", re.ASCII)
"""Any run of token-alphabet characters that contains a digit; redacted if long."""


def redact(text: str, *, limit: int = _MAX_DETAIL_CHARS) -> str:
    """Strip token-like substrings and paths from ``text`` and keep it short.

    Used for every piece of CLI output (stderr, error envelopes, version strings)
    before it can appear in an exception, a status detail or a log line.

    Args:
        text: Raw text from the child process.
        limit: Maximum number of characters returned (an ellipsis marks the cut).

    Returns:
        A single-line, redacted, length-limited string.
    """
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    out = _LONG_TOKEN.sub(_redact_long_token, out)
    out = " ".join(out.split())
    if len(out) > limit:
        out = out[:limit] + "..."
    return out


def _redact_long_token(match: re.Match[str]) -> str:
    token = match.group(0)
    return _REDACTED if len(token) >= _MIN_TOKEN_LEN else token


@dataclass(frozen=True)
class CliInvocation:
    """A fully resolved, shell-free command line.

    Attributes:
        executable: Absolute path from :func:`resolve_executable` (on Windows this may
            be a ``.cmd``/``.bat`` shim).
        args: Fixed, non-user-controlled flags. The prompt is NEVER placed in argv.
        stdin_text: The prompt, delivered on stdin.
        cwd: An empty temporary working directory.
        timeout_s: Hard wall-clock limit; the process tree is killed on expiry.
        max_output_bytes: Stdout cap; output beyond it is an error, not truncation.
    """

    executable: Path
    args: tuple[str, ...]
    stdin_text: str
    cwd: Path
    timeout_s: float
    max_output_bytes: int


def resolve_executable(command: str) -> Path:
    """Resolve ``command`` to an absolute path with :func:`shutil.which`.

    On Windows, vendor CLIs installed through npm are commonly ``.cmd`` shims; they
    are resolved here so that ``subprocess`` is never invoked with ``shell=True``.

    Raises:
        llm_crossjury.core.providers.base.ProviderUnavailableError: if not found.
    """
    if not command:
        raise ProviderUnavailableError("no CLI command configured")
    found = shutil.which(command)
    if found is None and sys.platform == "win32":
        # The process may have inherited a stale PATH (e.g. an editor terminal opened
        # before the CLI was installed); retry with the current persistent PATH.
        found = shutil.which(command, path=effective_path())
    if found is None:
        raise ProviderUnavailableError(f"'{command}' CLI not found on PATH")
    return Path(found).absolute()


def _registry_path() -> str:
    """Return the persistent machine + user PATH from the Windows registry ("" elsewhere).

    Only the ``Path`` values under the standard Environment keys are read; nothing
    else (ADR 0002 forbids touching CLI credential or config locations).
    """
    if sys.platform != "win32":
        return ""
    import winreg  # noqa: PLC0415 - Windows only

    parts: list[str] = []
    keys = (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    )
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if isinstance(value, str) and value:
            parts.append(os.path.expandvars(value))
    return os.pathsep.join(parts)


def effective_path() -> str:
    """The process PATH, extended on Windows with persistent entries it lacks."""
    current = os.environ.get("PATH", "")
    extra = _registry_path()
    if not extra:
        return current
    seen = {p.strip().lower() for p in current.split(os.pathsep) if p.strip()}
    added = [p for p in extra.split(os.pathsep) if p.strip() and p.strip().lower() not in seen]
    return os.pathsep.join([current, *added]) if added else current


def is_batch_shim(executable: Path) -> bool:
    """Return ``True`` for Windows ``.cmd``/``.bat`` files (re-parsed by cmd.exe).

    Only fixed, llm-crossjury-controlled flags may go into their argv (ADR 0002).
    """
    return executable.suffix.lower() in (".cmd", ".bat")


def _is_api_key_name(name: str) -> bool:
    return name.upper().endswith("_API_KEY")


def build_child_env(
    extra: Sequence[str] = (), overrides: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return a minimal environment built from :data:`ENV_ALLOWLIST` only.

    Args:
        extra: Additional variable names a specific adapter needs (for example a
            vendor's documented config-directory override). Names ending in
            ``_API_KEY`` are dropped even if listed.
        overrides: Fixed, adapter-defined values merged in after the allowlist
            (for example a flag that turns a CLI feature off).

    Returns:
        A new dict; allowlisted values are copied unread from :data:`os.environ`.

    Raises:
        ProviderError: if ``overrides`` names an ``*_API_KEY`` variable.
    """
    env: dict[str, str] = {}
    for name in (*ENV_ALLOWLIST, *extra):
        if _is_api_key_name(name):
            continue
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    if "PATH" in env:
        # CLI shims start further programs (e.g. node for npm-installed CLIs).
        env["PATH"] = effective_path()
    for name, value in (overrides or {}).items():
        if _is_api_key_name(name):
            raise ProviderError(f"refusing to set {name} for a CLI child process (ADR 0002)")
        env[name] = value
    return env


@contextlib.contextmanager
def temp_workspace() -> Iterator[Path]:
    """Create a fresh, empty working directory and remove it (with its parent) after.

    Layout: ``<root>/cwd`` is yielded and stays empty; adapters that must hand the
    CLI a file (for example a system prompt) write it to :func:`inputs_dir`, a
    sibling of the working directory, so the cwd itself stays empty.

    Yields:
        The empty working directory.
    """
    root = Path(tempfile.mkdtemp(prefix="llm-crossjury-cli-"))
    try:
        cwd = root / "cwd"
        cwd.mkdir()
        yield cwd
    finally:
        shutil.rmtree(root, ignore_errors=True)


def inputs_dir(cwd: Path) -> Path:
    """Return (and create) the directory for llm-crossjury-written input files.

    It is a sibling of ``cwd`` inside the :func:`temp_workspace` root, so it is
    removed together with the working directory.
    """
    path = cwd.parent / "inputs"
    path.mkdir(exist_ok=True)
    return path


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc`` and all of its descendants. Best effort, never raises."""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        taskkill = shutil.which("taskkill")
        if taskkill is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(  # noqa: S603 - absolute path from which, fixed argv
                    [taskkill, "/T", "/F", "/PID", str(proc.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc`` and all of its descendants. Best effort, never raises.

    The process must have been started in its own process group (Windows) or session
    (POSIX), as :func:`run_cli` does.
    """
    _kill_tree(proc)


async def _feed_stdin(proc: asyncio.subprocess.Process, data: bytes) -> None:
    stream = proc.stdin
    if stream is None:
        return
    try:
        stream.write(data)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the child exited without reading all input; its exit code tells why
    finally:
        with contextlib.suppress(OSError):
            stream.close()


class _OutputCapExceededError(Exception):
    def __init__(self, cap: int) -> None:
        super().__init__(cap)
        self.cap = cap


async def _read_capped(stream: asyncio.StreamReader | None, cap: int) -> bytes:
    """Read all of ``stream``; raise once more than ``cap`` bytes arrive."""
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > cap:
            raise _OutputCapExceededError(cap)
        chunks.append(chunk)


async def _read_discarding(stream: asyncio.StreamReader | None, cap: int) -> bytes:
    """Read all of ``stream``, keeping at most ``cap`` bytes (stderr only)."""
    if stream is None:
        return b""
    kept = bytearray()
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            return bytes(kept)
        room = cap - len(kept)
        if room > 0:
            kept += chunk[:room]


async def run_cli(
    invocation: CliInvocation,
    *,
    extra_env: Sequence[str] = (),
    env_overrides: Mapping[str, str] | None = None,
) -> tuple[str, str, int]:
    """Run ``invocation`` and return ``(stdout, stderr, returncode)``.

    Must use an argument list (never ``shell=True``), stdin for the prompt, the env
    from :func:`build_child_env`, and kill the whole process tree on timeout.
    Stderr is redacted before it is surfaced anywhere: the returned ``stderr`` is
    already redacted and length-limited.

    Args:
        invocation: The command line to run.
        extra_env: Extra allowlisted variable names (see :func:`build_child_env`).
        env_overrides: Fixed values merged in after the allowlist (no ``*_API_KEY``).

    The child runs with ``cwd=invocation.cwd``. An empty ``stdin_text`` is fine:
    stdin is then closed immediately (for CLIs that read the prompt from a file in
    the cwd).

    Raises:
        ProviderUnavailableError: the executable cannot be started.
        ProviderError: timeout (the process tree is killed), or more than
            ``max_output_bytes`` on stdout (the output is rejected, not truncated).
    """
    env = build_child_env(extra_env, env_overrides)
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    name = invocation.executable.stem
    try:
        proc = await asyncio.create_subprocess_exec(
            str(invocation.executable),
            *invocation.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(invocation.cwd),
            env=env,
            **kwargs,
        )
    except OSError as exc:
        raise ProviderUnavailableError(f"could not start '{name}': {type(exc).__name__}") from None

    async def communicate() -> tuple[bytes, bytes, int]:
        feeder = asyncio.create_task(_feed_stdin(proc, invocation.stdin_text.encode("utf-8")))
        readers = (
            asyncio.create_task(_read_capped(proc.stdout, invocation.max_output_bytes)),
            asyncio.create_task(_read_discarding(proc.stderr, MAX_STDERR_BYTES)),
        )
        try:
            out, err = await asyncio.gather(*readers)
            code = await proc.wait()
        finally:
            # gather() does not cancel the other reader when one fails (output cap):
            # a reader left running would make the reaping communicate() below fail
            # and leak the process transport.
            for task in (feeder, *readers):
                task.cancel()
            await asyncio.gather(feeder, *readers, return_exceptions=True)
        return out, err, code

    try:
        out, err, code = await asyncio.wait_for(communicate(), timeout=invocation.timeout_s)
    except TimeoutError:
        await _kill_and_reap(proc)
        raise ProviderError(
            f"'{name}' timed out after {invocation.timeout_s:g}s; process tree killed"
        ) from None
    except _OutputCapExceededError as exc:
        await _kill_and_reap(proc)
        raise ProviderError(
            f"'{name}' wrote more than {exc.cap} bytes to stdout; output rejected"
        ) from None
    except BaseException:
        await _kill_and_reap(proc)
        raise
    stdout = out.decode("utf-8", errors="replace")
    stderr = redact(err.decode("utf-8", errors="replace"))
    return stdout, stderr, code


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    await asyncio.to_thread(_kill_tree, proc)
    # Drain what is left in the pipes (the process is dead, so this is bounded) so
    # the transports see EOF and close; discard it unread.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.communicate(), timeout=10)
    # Reap even if draining failed, so the transport does not outlive the event loop.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5)


def render_transcript(messages: Sequence[Message]) -> str:
    """Render non-system messages as plain text for a CLI's stdin.

    A single user message is sent verbatim. Anything else (multi-turn context) is
    rendered as labelled blocks, one per message, so the model sees the turns in
    order. System messages are rejected: they belong in the system prompt.

    Raises:
        ProviderError: if a system message is passed.
    """
    if any(m.role is Role.SYSTEM for m in messages):
        raise ProviderError("system messages cannot be sent on stdin")
    if len(messages) == 1 and messages[0].role is Role.USER:
        return messages[0].content
    return "\n\n".join(f"[{m.role.value}]\n{m.content}" for m in messages)


class CliProviderBase:
    """Common skeleton for CLI-backed providers.

    Usage fields the CLI does not report are returned as ``None`` (unknown).
    Capabilities are conservative until verified against the CLI's documentation.

    Subclasses set ``name`` and ``command`` and implement :meth:`build_invocation`
    and :meth:`parse_output`; :meth:`complete`, :meth:`capabilities`,
    :meth:`count_input_tokens` and :meth:`check` work as-is.

    Args:
        registry: Model registry; argv values (model and reasoning flags) come only
            from its ``cli_flags``. ``None`` means every model is unknown, so
            :meth:`registry_argv` refuses to build a command line.
    """

    name: str = ""
    backend: Backend = Backend.CLI
    command: str = ""
    """Executable name looked up on PATH (e.g. ``"claude"``)."""

    skipped_reason: str | None = None
    """If set, this CLI is not supported in v1 for the documented security reason."""

    native_schema: bool = False
    """The CLI documents a flag that constrains output to a JSON schema."""

    extra_env: tuple[str, ...] = ()
    """Vendor-documented variables (beyond :data:`ENV_ALLOWLIST`) the CLI needs to find
    its own login. Never an ``*_API_KEY``."""

    default_timeout_s: float = DEFAULT_TIMEOUT_S
    """Used when ``request.timeout_s`` is ``None``."""

    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    """Stdout cap for completions."""

    def __init__(self, *, registry: ModelLookup | None = None) -> None:
        self._registry = registry

    # ------------------------------------------------------------------ registry

    def _entry(self, model: str) -> ModelEntry | None:
        if self._registry is None:
            return None
        return self._registry.get(self.name, model)

    def registry_argv(self, request: CompletionRequest) -> tuple[str, ...]:
        """Model and reasoning argv tokens from the registry entry's ``cli_flags``.

        ``{model}`` is filled with ``request.model``; ``{value}`` with the entry's
        native value for ``request.reasoning`` (nothing for ``default``). Each
        template token stays exactly one argv token (plain replacement, as in the
        resolver).

        Raises:
            ProviderError: no entry or no ``cli_flags`` (argv values must come from
                the registry), a non-default reasoning level the entry cannot map,
                or a token with characters outside the safe set.
        """
        entry = self._entry(request.model)
        if entry is None or entry.cli_flags is None:
            raise ProviderError(
                f"{self.name}/{request.model} has no registry cli_flags; the cli backend "
                "only passes registry-defined flags"
            )
        flags = entry.cli_flags
        tokens: list[str] = []
        if flags.model is not None:
            tokens += [t.replace("{model}", request.model) for t in flags.model]
        if request.reasoning is not ReasoningEffort.DEFAULT:
            value = self._native_reasoning(entry, request.reasoning)
            if value is None or flags.reasoning is None:
                raise ProviderError(
                    f"reasoning '{request.reasoning}' cannot be set via the CLI for "
                    f"{self.name}/{request.model}"
                )
            tokens += [t.replace("{value}", value) for t in flags.reasoning]
        for token in tokens:
            if not _SAFE_ARG.match(token):
                raise ProviderError(
                    f"registry cli_flags for {self.name}/{request.model} produce an unsafe "
                    "argv token"
                )
        return tuple(tokens)

    @staticmethod
    def _native_reasoning(entry: ModelEntry, level: ReasoningEffort) -> str | None:
        mapping = entry.reasoning_control.mapping
        if mapping is None:
            return None
        value = {
            ReasoningEffort.LOW: mapping.low,
            ReasoningEffort.MEDIUM: mapping.medium,
            ReasoningEffort.HIGH: mapping.high,
        }.get(level)
        return None if value is None else str(value)

    # ------------------------------------------------------------- capabilities

    def native_schema_usable(self) -> bool:
        """Whether the schema flag may be used on this machine (subclass hook)."""
        return self.native_schema

    def capabilities(self, model: str) -> Capabilities:
        """Capabilities of ``model`` on this CLI, from the registry entry.

        ``structured_output`` only if the adapter declares native schema support
        (and it is usable here) AND the entry says ``structured_output: true``.
        ``reasoning`` only if the control type is ``levels``/``budget`` and the entry
        has a ``cli_flags.reasoning`` template. Caching, batch and temperature are
        never offered: CLIs expose none of them.
        """
        entry = self._entry(model)
        if entry is None:
            return Capabilities(
                structured_output=False,
                caching=False,
                batch=False,
                temperature=False,
                reasoning=False,
            )
        reasoning = (
            entry.reasoning_control.type
            in (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET)
            and entry.cli_flags is not None
            and entry.cli_flags.reasoning is not None
        )
        return Capabilities(
            structured_output=entry.structured_output is True and self.native_schema_usable(),
            caching=False,
            batch=False,
            temperature=False,
            reasoning=reasoning,
        )

    # --------------------------------------------------------- subclass hooks

    def build_invocation(self, request: CompletionRequest, cwd: Path) -> CliInvocation:
        """Translate a request into a safe :class:`CliInvocation` (non-interactive,
        tools disabled, no file writes)."""
        raise NotImplementedError

    def parse_output(self, stdout: str, request: CompletionRequest) -> CompletionResult:
        """Parse CLI stdout into a result. Unknown usage fields become ``None``."""
        raise NotImplementedError

    def child_env_overrides(self) -> dict[str, str]:
        """Fixed environment values to set for the child after the allowlist.

        Never an ``*_API_KEY`` name (rejected by :func:`build_child_env`).
        """
        return {}

    def describe_failure(self, stdout: str, stderr: str) -> str:
        """Short, redacted reason for a non-zero exit (subclasses may parse stdout).

        Args:
            stdout: Raw stdout of the failed run.
            stderr: Already redacted stderr.
        """
        return stderr

    # ------------------------------------------------------------------- calls

    def resolve(self) -> Path:
        """Resolve this provider's executable (see :func:`resolve_executable`).

        Raises:
            ProviderUnavailableError: skipped CLI, or not found on PATH.
        """
        if self.skipped_reason is not None:
            raise ProviderUnavailableError(
                f"{self.command} CLI is not supported: {self.skipped_reason}"
            )
        return resolve_executable(self.command)

    def timeout_for(self, request: CompletionRequest) -> float:
        """The hard timeout for ``request``."""
        return request.timeout_s if request.timeout_s else self.default_timeout_s

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run one completion through the CLI.

        Resolve the executable, create an empty temp cwd, build the invocation, run
        it, turn a non-zero exit into a redacted :class:`ProviderError`, and parse the
        output. ``started_at`` and ``latency_s`` (wall clock of the subprocess) are
        filled here.

        Raises:
            ProviderUnavailableError: CLI skipped or not found.
            ProviderError: missing registry flags, timeout, output cap, non-zero exit
                or unparseable output.
        """
        self.resolve()
        started_at = utc_now()
        start = time.monotonic()
        with temp_workspace() as cwd:
            invocation = self.build_invocation(request, cwd)
            stdout, stderr, code = await run_cli(
                invocation, extra_env=self.extra_env, env_overrides=self.child_env_overrides()
            )
        latency = time.monotonic() - start
        if code != 0:
            detail = redact(self.describe_failure(stdout, stderr))
            message = f"{self.command} exited with code {code}"
            raise ProviderError(f"{message}: {detail}" if detail else message)
        try:
            result = self.parse_output(stdout, request)
        except ProviderError as exc:
            # Error envelopes on stdout are vendor text: redact like stderr.
            raise type(exc)(redact(str(exc))) from None
        return result.model_copy(update={"started_at": started_at, "latency_s": latency})

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        """CLIs offer no token counting; always ``None`` (unknown)."""
        return None

    async def check(self) -> ProviderStatus:
        """Report whether the executable resolves and responds to a version query.

        Must not inspect login state by reading files; at most it may run a vendor-
        documented, token-free status subcommand. This default runs only
        ``<command> --version`` and never reports the executable's path.
        """
        try:
            executable = self.resolve()
        except ProviderUnavailableError as exc:
            return self._status(False, str(exc), None)
        try:
            with temp_workspace() as cwd:
                stdout, stderr, code = await run_cli(
                    CliInvocation(
                        executable=executable,
                        args=("--version",),
                        stdin_text="",
                        cwd=cwd,
                        timeout_s=CHECK_TIMEOUT_S,
                        max_output_bytes=64 * 1024,
                    ),
                    extra_env=self.extra_env,
                    env_overrides=self.child_env_overrides(),
                )
        except ProviderError as exc:
            return self._status(False, f"{self.command} --version failed: {exc}", None)
        if code != 0:
            detail = f"{self.command} --version exited with code {code}"
            return self._status(False, f"{detail}: {stderr}" if stderr else detail, None)
        lines = [line for line in stdout.splitlines() if line.strip()]
        version = redact(lines[0], limit=80) if lines else None
        return await self._login_report(executable, version)

    async def _login_report(self, executable: Path, version: str | None) -> ProviderStatus:
        """Combine the version result with the optional login status check."""
        found = f"{self.command} CLI found ({version or 'version unknown'})"
        if self.login_status_args is None:
            return self._status(True, f"{found}; login not checked", version)
        signed_in = await self._login_status(executable)
        if signed_in is None:
            return self._status(True, f"{found}; login status unknown", version)
        if not signed_in:
            hint = f"run '{self.command}' once in a terminal and sign in"
            return self._status(False, f"{found} but not signed in: {hint}", version)
        return self._status(True, f"{found}; signed in", version)

    login_status_args: tuple[str, ...] | None = None
    """A vendor-documented, token-free login status subcommand (e.g. ``("auth",
    "status")``). Only its exit code is used (0 = signed in); its output is discarded
    unread, because it can describe the account or a key fragment (ADR 0002)."""

    async def _login_status(self, executable: Path) -> bool | None:
        """Run :attr:`login_status_args`; ``None`` if it could not be run."""
        if self.login_status_args is None:
            return None
        try:
            with temp_workspace() as cwd:
                _stdout, _stderr, code = await run_cli(
                    CliInvocation(
                        executable=executable,
                        args=self.login_status_args,
                        stdin_text="",
                        cwd=cwd,
                        timeout_s=CHECK_TIMEOUT_S,
                        max_output_bytes=64 * 1024,
                    ),
                    extra_env=self.extra_env,
                    env_overrides=self.child_env_overrides(),
                )
        except ProviderError:
            return None
        return code == 0

    def _status(self, available: bool, detail: str, version: str | None) -> ProviderStatus:
        return ProviderStatus(
            provider=self.name,
            backend=self.backend,
            available=available,
            detail=detail,
            version=version,
        )
