"""Subprocess scaffolding for the ``cli`` backend.

Every rule here is mandated by ADR 0002; read it before changing anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from llm_crossjury.core.providers.base import (
    Backend,
    Capabilities,
    CompletionRequest,
    CompletionResult,
    ProviderStatus,
)

ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
)
"""Variables passed through to the child. Values are passed, never read or logged.

The child needs its home/config directories to find *its own* login; llm-crossjury never
opens anything under them. ``*_API_KEY`` variables are deliberately absent.
"""


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
    raise NotImplementedError


def build_child_env() -> dict[str, str]:
    """Return a minimal environment built from :data:`ENV_ALLOWLIST` only."""
    raise NotImplementedError


async def run_cli(invocation: CliInvocation) -> tuple[str, str, int]:
    """Run ``invocation`` and return ``(stdout, stderr, returncode)``.

    Must use an argument list (never ``shell=True``), stdin for the prompt, the env
    from :func:`build_child_env`, and kill the whole process tree on timeout.
    Stderr is redacted before it is surfaced anywhere.
    """
    raise NotImplementedError


class CliProviderBase:
    """Common skeleton for CLI-backed providers.

    Usage fields the CLI does not report are returned as ``None`` (unknown).
    Capabilities are conservative until verified against the CLI's documentation.
    """

    name: str = ""
    backend: Backend = Backend.CLI
    command: str = ""
    """Executable name looked up on PATH (e.g. ``"claude"``)."""

    skipped_reason: str | None = None
    """If set, this CLI is not supported in v1 for the documented security reason."""

    def capabilities(self, model: str) -> Capabilities:
        raise NotImplementedError

    def build_invocation(self, request: CompletionRequest, cwd: Path) -> CliInvocation:
        """Translate a request into a safe :class:`CliInvocation` (non-interactive,
        tools disabled, no file writes)."""
        raise NotImplementedError

    def parse_output(self, stdout: str, request: CompletionRequest) -> CompletionResult:
        """Parse CLI stdout into a result. Unknown usage fields become ``None``."""
        raise NotImplementedError

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        raise NotImplementedError

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        raise NotImplementedError

    async def check(self) -> ProviderStatus:
        """Report whether the executable resolves and responds to a version query.

        Must not inspect login state by reading files; at most it may run a vendor-
        documented, token-free status subcommand.
        """
        raise NotImplementedError
