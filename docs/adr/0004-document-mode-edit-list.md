# ADR 0004: Document Review returns an edit list, not a rewrite

- Status: Accepted
- Date: 2026-11-04

## Context

Users of Document Review (for example, authors revising a paper) need to improve
their document while staying its author. A model can return either a fully
rewritten document or a list of discrete proposed edits.

## Decision

Document Review returns an **`EditList`**. Each `Edit` has `location` (section, lines,
occurrence), verbatim `original`, `replacement`, `reason`, `category` and `severity`.
The chair merges the members' lists into a `DocumentSynthesis`, and conflicting edits
become **disputes** for checkpoint B. llm-crossjury can render accepted edits as a unified
diff, but it never overwrites the input file.

## Rationale

1. **The user keeps control.** Each change can be accepted or rejected on its own,
   with its reason in view. A rewrite forces all-or-nothing, or a manual diff.
2. **Nothing changes without a trace.** Full rewrites tend to drift in meaning, drop
   content, and "smooth" carefully chosen wording. An edit list makes every change
   explicit and reviewable.
3. **Edits can be checked mechanically.** `original` must exist verbatim in the source
   (`validate_edits`). Hallucinated or misplaced changes can be detected. A rewrite
   cannot be checked this way.
4. **Edits can be compared across models.** Edit lists from different members can be
   aligned by location and `original`, which makes consensus, disputes and unique
   edits well-defined. Comparing four rewrites is a fuzzy diffing problem.
5. **Peer review gets concrete.** Reviewers critique specific edits ("e4 changes the
   claim's meaning"), not a whole new text.
6. **Lower cost.** Output tokens scale with the number of changes, not with the size
   of the document. The document sits once in the cached stable prefix.
7. **Markup safety.** LaTeX and Markdown constructs outside the edited spans are
   never touched, so a rewrite cannot break macros, labels or references elsewhere.

## Consequences

- Large structural reorganizations are awkward to express as edits. v1 handles them
  as `structure` edits with a descriptive `replacement`, or as a recommendation in
  `summary`.
- `original` must be quoted exactly. Prompts must stress this, and validation must
  flag misses.
- Overlapping edits need a conflict policy (SPEC Open Questions).

## Alternatives considered

- **Full rewrite plus a computed diff.** Rejected: the diff shows *what* changed but
  not *why*, and it mixes intentional and accidental changes.
- **Inline comments only (no replacements).** Rejected: less actionable. Edits can
  still carry an empty-change suggestion through `reason` when no concrete
  replacement is appropriate.
