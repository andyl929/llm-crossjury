# Architecture Decision Records

| # | Title | Status |
|---|---|---|
| [0001](0001-provider-abstraction.md) | Provider abstraction and structured-output schema convention | Accepted |
| [0002](0002-cli-backend-security-boundary.md) | `cli` backend security boundary and skip conditions | Accepted |
| [0003](0003-cost-control.md) | Cost control strategy | Accepted |
| [0004](0004-document-mode-edit-list.md) | Document Review returns an edit list | Accepted |
| [0005](0005-clean-room-and-license.md) | Clean-room implementation and MIT license | Accepted |
| [0006](0006-model-registry.md) | Model registry, aliases and reasoning resolution | Accepted (supersedes 0001 rule 5) |
| [0007](0007-attachments.md) | File attachments (PDF, Word, images) | Accepted (amends SPEC §9) |

Each ADR has these sections: Status, Context, Decision, Consequences, Alternatives.
Change rules:

- **Small clarifications** (wording, references, defaults that do not change the
  decision) may be made in place, dated in an `Amended:` line.
- **Overturning a decision** (fully or in part) requires a new ADR. The new ADR names
  what it supersedes, and the old ADR gets a `Superseded:` / `Partly superseded:`
  line pointing to it. The old text is not rewritten.
