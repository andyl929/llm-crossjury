# Prompt templates

Prompts are versioned files, not strings in code.

## Layout

```
prompts/<mode-or-shared>/<stage>.v<N>.md
```

- A version is **immutable once released**. To change a prompt, add `v<N+1>` and
  update the reference in configuration or the preset. Run records store the prompt id
  (for example `council/peer_review.v1`) so results stay reproducible.
- Each file starts with front matter: `id`, `version`, `schema` (the structured
  output model, if any), and `variables`.
- Each template has two sections, `## stable_prefix` and `## variable_suffix`, which
  map onto `Prompt.stable_prefix` / `Prompt.variable_suffix`
  ([ADR 0001](../docs/adr/0001-provider-abstraction.md)). Nothing that varies per
  call (anonymous labels, run ids, dates) may appear in the stable prefix.

## Status

Every file here is a **placeholder**. The prompt text gets written in the
implementation phase.
