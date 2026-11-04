"""Provider layer.

``base`` is a CONTRACT FILE (see AGENTS.md). Concrete adapters live in ``api/``
(official SDKs) and ``cli/`` (official, locally signed-in CLIs; experimental).
``mock`` is a deterministic in-process provider for tests.
"""

from llm_crossjury.core.providers.base import (
    Backend,
    BatchProvider,
    Capabilities,
    CompletionRequest,
    CompletionResult,
    Message,
    Prompt,
    Provider,
    ProviderStatus,
    Role,
    Usage,
)

__all__ = [
    "Backend",
    "BatchProvider",
    "Capabilities",
    "CompletionRequest",
    "CompletionResult",
    "Message",
    "Prompt",
    "Provider",
    "ProviderStatus",
    "Role",
    "Usage",
]
