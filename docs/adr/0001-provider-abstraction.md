# ADR 0001: Provider abstraction and structured-output schema convention

- Status: Accepted
- Date: 2026-11-04

## Context

llm-crossjury calls four vendors through two very different backends: official SDKs, and
the official CLIs the user is signed in to. Stages must not care which one they are
talking to. Cost control depends on prompt caching, which only works when calls
share a byte-identical prefix. Usage reporting differs between vendors, and CLIs
often report no usage at all. Every vendor supports a different subset of JSON
Schema for structured output.

## Decision

### Provider protocol (`core/providers/base.py`)

1. **`Provider` is a `typing.Protocol`**, not a base class. It has `name`, `backend`,
   `capabilities(model)`, `complete(request)`, `count_input_tokens(request)` and
   `check()`. `BatchProvider` extends it with `submit_batch`, `poll_batch` and
   `collect_batch`. Adapters, mocks and test doubles satisfy it structurally.
2. **`Prompt = stable_prefix + variable_suffix`.** The split is part of the type, so
   stages have to decide which content is shared and which is per-call. Adapters map
   the boundary onto the vendor's caching mechanism (an explicit breakpoint, or
   automatic prefix caching). `prefix_fingerprint()` lets the estimator and the
   ledger group calls by shared prefix.
3. **`Usage` has five counters**: input, cached_input, cache_write, output, reasoning.
   Each is `int | None`, and **`None` means unknown, never zero**. Cost computation
   returns `None` (unknown) instead of guessing.
4. **Capabilities are declared per model.** There are four flags: `structured_output`,
   `caching`, `batch`, `temperature`. The orchestrator uses only declared
   capabilities and otherwise falls back (format instructions plus validation;
   sequential calls; temperature ignored and recorded as ignored).
5. **Errors never carry secrets.** The error types are `ProviderUnavailableError`,
   `CapabilityNotSupportedError` and `ProviderError`.

### Structured-output schema convention (`core/schemas/`)

The schemas are written in the **strictest common subset** of what the target
vendors accept, so that one canonical schema works everywhere:

1. **Every field is required.** An optional value is expressed as
   **required + nullable** (`T | None` with no default), never by omitting the field.
2. Nullable is allowed **only on scalars** (string, integer, number, boolean). It is
   written as `"type": ["string", "null"]`. It is never allowed on objects, arrays or
   enums. Use an empty list instead of a null array.
3. **Every object has `additionalProperties: false`** (Pydantic `extra="forbid"`).
4. **No `union`, `oneOf`, `anyOf` or `allOf`**, other than the scalar-or-null form in
   rule 2. **No recursion.** No open maps (`dict` fields). No tuples (`prefixItems`).
5. **All `$ref`/`$defs` are inlined.** `title` and `default` keywords are dropped.
6. **No value constraints in the schema** (min/max, length, pattern, format). Range
   and permutation checks are done by Python validators after parsing.
7. **Field order is generation order.** Evidence comes before judgment (quote →
   problem → severity; issues → strengths → rationale → score → ranking).
8. Field docstrings become schema `description`s, so they are written as plain text
   addressed to the model.

`core/schemas/export.py` produces this canonical form and fails on any model that
cannot be expressed in it. `tests/unit/test_schema_conventions.py` enforces the
rules and checks that the committed `schemas/*.schema.json` are current. Vendor
adapters may apply **lossless** dialect transforms (for example, rewriting a nullable
type list into a vendor-specific nullable flag). They may never relax the schema.

## Consequences

- One schema per model, testable without any vendor. Adapters own the dialect quirks.
- Models emit explicit `null`s and empty arrays. This is slightly more verbose, but
  parsing is unambiguous.
- Some natural modelling (polymorphic items, nested optional objects) is off-limits.
  Variants become separate fields or separate models.
- A protocol-based contract keeps adapters decoupled, but `runtime_checkable` only
  checks attribute presence. Correctness comes from mypy and the tests.

## Alternatives considered

- **Abstract base class.** Rejected: it forces inheritance on mocks and third-party adapters.
- **One flat `prompt: str`.** Rejected: caching becomes accidental, and cost estimates
  cannot account for cache hits.
- **A schema per vendor.** Rejected: N× maintenance, and drift between vendors.
- **`Usage` with zeros for unknown.** Rejected: it silently under-reports cost.
