# ADR 0005: Clean-room implementation and MIT license

- Status: Accepted
- Date: 2026-11-04

## Context

The idea of an "LLM council" (models answering, cross-reviewing and being
synthesized by a chair) was popularized by an existing public repository. That
repository has **no license**, so its code cannot be reused, adapted or relied on.

## Decision

1. **Clean room.** Contributors (human or AI) must not view, search, clone, or copy
   code from that repository or any fork of it. The design comes only from this
   project's own specification (`docs/SPEC.md`) and general domain knowledge.
2. Any contributor who has studied that code in detail should state so before
   working on the core orchestration.
3. **License: MIT** for all code, documentation and prompt templates in this
   repository.
4. All code, comments and documentation are in English.

## Consequences

- Similar ideas are fine. Similar code obtained from the original is not.
- Third-party dependencies must have licenses compatible with MIT.
