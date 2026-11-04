"""``cli`` backend (EXPERIMENTAL): official vendor CLIs the user is signed in to.

Security boundary: ``docs/adr/0002-cli-backend-security-boundary.md``. In short,
council only launches the CLI as a subprocess, sends the prompt on stdin, reads stdout,
and never reads, copies, or stores OAuth tokens, cookies, or credential files.
"""

from council.core.providers.cli.claude_code import ClaudeCliProvider
from council.core.providers.cli.codex import CodexCliProvider
from council.core.providers.cli.gemini_cli import GeminiCliProvider
from council.core.providers.cli.grok_cli import GrokCliProvider

__all__ = ["ClaudeCliProvider", "CodexCliProvider", "GeminiCliProvider", "GrokCliProvider"]
