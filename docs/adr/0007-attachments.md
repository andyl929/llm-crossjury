# ADR 0007: File attachments (PDF, Word, images)

- Status: Accepted
- Date: 2026-11-08
- Amends: SPEC §9 non-goal "Input formats other than Markdown and LaTeX" (task
  attachments are now in scope; Document Review mode is unchanged). Contract change
  CI-5 (`providers/base.py`, `registry/schema.py`), approved by the maintainer.

## Context

Users want to attach files to a Compare task: PDFs, Word documents and images first.
Vendors differ (researched 2026-11-08 from official docs):

- Anthropic, OpenAI and Gemini accept PDFs and images natively on the API surfaces our
  adapters use. xAI accepts images natively; its file input routes through a paid
  server-side retrieval tool (`attachment_search`), which changes the comparison.
- No vendor reads Office files faithfully across all four; local conversion is needed.
- `Message.content` was a plain string, so the frozen contract could not carry files.

## Decision

1. **Contract.** `Message` gains `attachments: tuple[Attachment, ...] = ()` (user
   messages only). `Attachment` holds `kind` (`image` | `pdf` | `document`),
   `media_type`, `filename` (base name only), `sha256`, `data` and `fallback_text`.
   `Capabilities` gains `input_modalities: frozenset[AttachmentKind]` (empty by
   default). The registry `ModelEntry` gains `input_modalities` (`None` = unverified =
   text only), copied from vendor docs like every other model fact (ADR 0006).
2. **Native or fallback.** An adapter sends an attachment natively when its kind is in
   the model's `input_modalities`; otherwise it inlines `fallback_text` in a delimited
   block, or raises `CapabilityNotSupportedError` when there is none (images). Run
   results record, per member, whether each attachment was native or text.
3. **Per kind (v1).**
   - Images: native where declared.
   - PDF: native on Anthropic, OpenAI and Gemini where declared. **xAI uses the
     locally extracted text** (no `attachment_search`), so every member sees comparable
     material and no retrieval fees apply.
   - Word (DOCX): always converted locally to text (paragraphs and tables).
   - `cli` (subscription) members receive `fallback_text` only; images are refused.
4. **Transport: inline only.** No vendor Files API is used in v1, so nothing is stored
   on vendor file services and no cleanup state exists. Total attachment size per run
   is capped at **20 MB** (a product setting), below every vendor's inline limit.
5. **Local extraction.** `pypdf` and `pdfplumber` (PDF text and tables), `python-docx`
   (DOCX). All MIT/BSD. Extraction runs with size, page and time limits. Scanned PDFs
   (near-empty text) are flagged: native members still see the pages, text-only members
   get a warning. AGPL libraries (e.g. PyMuPDF) are excluded (ADR 0005).
6. **Placement and caching.** Attachments go in a user message inside
   `Prompt.stable_prefix`, in a deterministic order (by sha256), so they are cached per
   vendor and reused by later calls of the same run.
7. **Untrusted content.** Prompts state that attachment content is reference data, not
   instructions. Extracted text is wrapped in delimiters with closing tags escaped.
8. **Step 2.** Only the chair receives the attachments; reviewers see the answers only.
   Giving reviewers the attachments is recorded as a later optimization to evaluate.
9. **Privacy.** Before a run, the UI lists every vendor that will receive the files.
10. **Cost.** Input tokens for attachments come from the vendor's token-count endpoint
    where one exists (Anthropic, OpenAI, Gemini); otherwise from a conservative local
    heuristic (pages and image size). Unknown remains unknown, never zero.

## Consequences

- Members may see different representations of the same PDF (pages vs text); this is
  disclosed in the results.
- Charts and images inside Word files are lost in v1.
- Larger prompts: attachments are often the dominant cost, so the estimate shows them
  per file.

## Alternatives considered

- **Text extraction only for everything:** simpler and contract-free, but cannot handle
  images or scanned PDFs, which the maintainer needs.
- **Vendor Files APIs:** support larger files, but leave copies on vendor storage with
  different retention rules (Anthropic's is not ZDR-eligible; xAI keeps files until
  deleted). Deferred.
- **markitdown / docling:** convenient or higher quality, but more dependencies, a
  network-capable `convert()` (markitdown) or separately licensed model weights
  (docling). May be offered later as optional extras.
