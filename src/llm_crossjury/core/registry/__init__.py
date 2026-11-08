"""Model registry and resolver (ADR 0006). ``schema`` is a CONTRACT FILE."""

from llm_crossjury.core.registry.resolver import (
    ModelRegistry,
    ModelResolver,
    RegistryDiff,
    RegistryError,
    ResolutionError,
    ResolutionMode,
    ResolvedModel,
    Role,
    deep_merge,
    resolve_models_path,
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
    "RegistryError",
    "ResolutionError",
    "ResolutionMode",
    "ResolvedModel",
    "Role",
    "deep_merge",
    "resolve_models_path",
]
