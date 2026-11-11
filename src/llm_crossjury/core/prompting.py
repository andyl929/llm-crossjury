"""Prompt templates (``prompts/<group>/<stage>.v<N>.md``) with named variables.

A template has YAML-ish front matter (``id``, ``version``, ``schema``, ``variables``)
and two sections, ``## stable_prefix`` and ``## variable_suffix`` (see
``prompts/README.md``). Variables are written ``{name}`` and substituted by plain
string replacement of the declared names only, so JSON braces in a template are safe.
The stable prefix becomes one system message unless it is empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from llm_crossjury.core.providers.base import Message, Prompt, Role

_PREFIX_MARKER = "## stable_prefix"
_SUFFIX_MARKER = "## variable_suffix"


class PromptTemplateError(ValueError):
    """A template file is missing, malformed, or rendered with wrong variables."""


@dataclass(frozen=True)
class Template:
    """A parsed template."""

    prompt_id: str
    variables: tuple[str, ...]
    stable_prefix: str
    variable_suffix: str

    def render_text(self, section: str, values: dict[str, str]) -> str:
        """Substitute ``values`` into ``section`` (``"prefix"`` or ``"suffix"``)."""
        text = self.stable_prefix if section == "prefix" else self.variable_suffix
        names = [name for name in self.variables if name in values]
        if not names:
            return text
        # Single pass: a substituted value that itself contains "{other}" is never
        # expanded again (task text is user input).
        pattern = re.compile("|".join(re.escape("{" + name + "}") for name in names))
        return pattern.sub(lambda m: values[m.group(0)[1:-1]], text)

    def render(
        self,
        *,
        prefix_values: dict[str, str] | None = None,
        suffix_values: dict[str, str] | None = None,
    ) -> Prompt:
        """Build a :class:`Prompt`.

        ``prefix_values`` must be identical across calls meant to share a cache entry;
        put per-call values (targets, rulings) in ``suffix_values``.

        Raises:
            PromptTemplateError: if a value names an undeclared variable.
        """
        prefix_values = prefix_values or {}
        suffix_values = suffix_values or {}
        unknown = (set(prefix_values) | set(suffix_values)) - set(self.variables)
        if unknown:
            raise PromptTemplateError(f"{self.prompt_id}: undeclared variables {sorted(unknown)}")
        prefix = self.render_text("prefix", prefix_values).strip()
        suffix = self.render_text("suffix", suffix_values).strip()
        if not suffix:
            raise PromptTemplateError(f"{self.prompt_id}: variable_suffix renders empty")
        return Prompt(
            stable_prefix=(Message(role=Role.SYSTEM, content=prefix),) if prefix else (),
            variable_suffix=(Message(role=Role.USER, content=suffix),),
        )


def prompts_root() -> Path:
    """Locate the repository ``prompts/`` directory above this package.

    Raises:
        PromptTemplateError: if it cannot be found (e.g. a wheel install).
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "prompts"
        if (candidate / "README.md").is_file():
            return candidate
    raise PromptTemplateError("prompts/ directory not found")


def _parse_list(value: str) -> tuple[str, ...]:
    inner = value.strip().removeprefix("[").removesuffix("]")
    return tuple(item.strip() for item in inner.split(",") if item.strip())


@lru_cache(maxsize=32)
def load_template(prompt_id: str, root: Path | None = None) -> Template:
    """Load ``<root>/<prompt_id>.md`` (e.g. ``"synthesis/chair.v1"``).

    Raises:
        PromptTemplateError: if the file is missing or malformed.
    """
    path = (root or prompts_root()) / f"{prompt_id}.md"
    if not path.is_file():
        raise PromptTemplateError(f"prompt template {prompt_id!r} not found")
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():  # noqa: PLR2004
        raise PromptTemplateError(f"{prompt_id}: missing front matter")
    front, body = parts[1], parts[2]
    meta: dict[str, str] = {}
    for line in front.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    if _PREFIX_MARKER not in body or _SUFFIX_MARKER not in body:
        raise PromptTemplateError(f"{prompt_id}: missing stable_prefix or variable_suffix")
    _, _, rest = body.partition(_PREFIX_MARKER)
    prefix, _, suffix = rest.partition(_SUFFIX_MARKER)
    return Template(
        prompt_id=meta.get("id", prompt_id),
        variables=_parse_list(meta.get("variables", "[]")),
        stable_prefix=prefix.strip(),
        variable_suffix=suffix.strip(),
    )
