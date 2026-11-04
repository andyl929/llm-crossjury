"""Model registry and resolver (ADR 0006). ``schema`` is a CONTRACT FILE."""

from llm_crossjury.core.registry.resolver import (
    ModelRegistry,
    ModelResolver,
    RegistryDiff,
    ResolutionError,
    ResolutionMode,
    ResolvedModel,
    Role,
)
from llm_crossjury.core.registry.schema import (
    CliFlags,
    EffortMapping,
    ModelEntry,
    ModelRegistryFile,
    Pricing,
    ProviderAliases,
    ReasoningControl,
    ReasoningControlType,
)

__all__ = [
    "CliFlags",
    "EffortMapping",
    "ModelEntry",
    "ModelRegistry",
    "ModelRegistryFile",
    "ModelResolver",
    "Pricing",
    "ProviderAliases",
    "ReasoningControl",
    "ReasoningControlType",
    "RegistryDiff",
    "ResolutionError",
    "ResolutionMode",
    "ResolvedModel",
    "Role",
]
