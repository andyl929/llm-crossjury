# Roadmap: from working MVP to a product people star

- Status: Draft, agreed direction (2026-11-08). Update this file as milestones land.
- Owner: maintainer. Decisions marked **(D-n)** are open until the maintainer resolves them.

## Goal

Make llm-crossjury the tool students and researchers reach for when one model's answer
is not enough: **download it, paste a question or a paper, compare the models side by
side, and get one synthesized answer with the disagreements laid out.** Open source
(MIT), local-first, honest about cost.

Success looks like: a stranger lands on the GitHub page, understands the product in
10 seconds, has it running in 2 minutes, and gets a useful result on the first try.

## Target users and their jobs

| User | Job to be done | What they need from us |
|---|---|---|
| Graduate student / researcher | Check a claim, a proof sketch, a literature summary or a draft against several models | PDF/Word upload, disagreements surfaced, citations of who said what, low cost |
| Undergraduate | Get a reliable explanation; avoid one model's hallucination | Zero setup, subscription mode (no API account), clear UI |
| Developer / power user | Compare models on their own tasks; script it | CLI, config files, exportable results, extensibility |

Most of them have a **subscription, not an API account**. Subscription mode and a
smooth key onboarding are first-class, not afterthoughts.

## Principles

1. **Usable before beautiful, then beautiful.** Each milestone ships something a
   classmate can use.
2. **Honest by default.** Show cost before every run, show which model said what,
   label AI-decided disputes, never hide uncertainty.
3. **Local-first and private.** Keys and files stay on the user's machine; we tell the
   user exactly which vendor receives what.
4. **Reproducible demos.** Every screenshot and animation is generated from a scripted
   demo run with mock providers, so it can be regenerated after every UI change at no
   cost.

## Milestones

### M1: v0.1 "it works for my lab" (finish what is in flight)

- Merge attachments (ADR 0007) and run live smoke tests with real keys and with the
  Claude Code / Codex subscriptions (`scripts/smoke_live.py`, `scripts/smoke_cli.py`).
- Open a PR from `feat/compare-mvp` to `main`; CI green on Ubuntu and Windows.
- Ship prompts as package data (`importlib.resources`) so a wheel install works.
- `pip install llm-crossjury` / `uvx llm-crossjury ui` from PyPI (trusted publishing
  from GitHub Actions; no long-lived tokens).
- Result export: Markdown (and copy buttons) for answers, final synthesis and the
  additional list.
- Local run history (titles, cost, date; content only if `store_content` is on).
- Hallway test: 5 classmates use it on their own question; fix the top issues.

### M1.5: Sessions, chat and memory (in progress, 2026-11-09)

Results survive navigation and closed pages; continue any jury answer as a chat; ask
the jury again mid-conversation (closed loop); layered memory keeps long research
conversations affordable. Budget defaults raised and editable. Design:
[`design/sessions-chat-memory.md`](design/sessions-chat-memory.md), ADR 0008.

| Phase | Scope |
|---|---|
| A | session store, detached tasks, restore on load, app settings (budget, history, memory) |
| B | chat UI: sidebar history, thread, continue buttons, composer, model switch |
| C | memory layer: context layers, summaries, FTS retrieval, case file |
| D | integration: jury inside a conversation, end-to-end tests |

### M1.6: New-user onboarding (queued; confirm scope with the maintainer first)

Guide the first setup end to end: subscription mode (install and sign in to Claude
Code, Codex, Grok CLI; live check), API keys (extend the existing key wizard), and a
very short tutorial (first jury question, continue a chat, ask the jury again).

### M1.7: Small public eval (queued, after M1.6; hard cap $5-10)

Goal: honest evidence that crossjury helps, not a research study.

* Data: GPQA Diamond and SimpleQA, small samples (start with about 50 questions each;
  grow only if the budget allows). Questions are downloaded at run time and never
  committed (GPQA asks that items stay offline).
* Compare: best single member, majority vote, crossjury synthesis. Headline metric is
  flag precision: how often a claim marked disputed or possibly wrong really is wrong,
  versus the error rate of unflagged claims.
* Cost control: run the estimator first and stop above the cap; prefer `fast` and
  `balanced` aliases and subscription CLIs (no API cost; uses plan limits).
* Output: `scripts/eval/` (reproducible), `docs/evals.md` (method, date, model
  versions, cost, limitations, where it does not help). README gets a short "Does it
  help?" section with a 3-4 row table below the fold, only if the results hold up;
  otherwise publish the method and scripts only.

### M2: v0.2 "download and use" (desktop app)

- One-click desktop app for Windows and macOS (Linux AppImage if cheap):
  - **(D-1)** Packaging approach. Recommended: PyInstaller one-folder build plus a
    native window via `pywebview` (the existing FastAPI UI runs inside it, no browser
    tab, no terminal). Alternative: Tauri shell with a Python sidecar (smaller, more
    work).
  - Built in GitHub Actions on each tag; published to GitHub Releases with checksums.
  - **(D-2)** Code signing. Unsigned builds trigger Windows SmartScreen and macOS
    Gatekeeper warnings. macOS notarization needs an Apple Developer account
    (paid, yearly). Recommended: ship unsigned v0.2 with clear instructions, sign once
    there is traction.
  - In-app update check (reads the latest GitHub release; never auto-installs).
- First-run wizard covers both paths: "I have a subscription" (detect `claude` /
  `codex`, show install and sign-in steps) and "I have an API key".

### M3: UI redesign: modern industrial, clean, usable

Design direction: precise, calm, tool-like. Think instrument panel, not marketing page.

- **Design system** (tokens first, one CSS file; light and dark):
  - Neutral graphite/steel palette, one functional accent, semantic colors for
    agree / dispute / error; WCAG AA contrast.
  - Type: a clean grotesk for UI, a monospace for numbers, tokens and costs; tabular
    numerals.
  - 8 px spacing grid, hairline borders, minimal shadows, 4–6 px radii.
  - Motion: short, purposeful (state changes, progress); respects
    `prefers-reduced-motion`.
- **Information architecture:**
  - Left: task, attachments, model picker (provider cards with backend toggle).
  - Center: answers in synchronized columns with diff-friendly layout; collapsible.
  - Right / bottom: synthesis, additional list, cost meter.
  - Persistent cost bar: estimate before, actual after, budget remaining.
- **Usability features:**
  - **Streaming answers** (tokens appear as they arrive). Needs a streaming method
    in the provider contract, so it requires a new ADR and contract change.
  - Keyboard shortcuts (run, estimate, switch columns), command palette.
  - Empty states that teach; inline errors with the fix ("Add key in Settings").
  - Highlight where answers agree or disagree, linked to the agreement groups.
  - Copy, export, and "open in new window" per answer.
- **(D-3)** Frontend stack. Recommended: keep the no-build vanilla setup through M2,
  then move to Svelte + Vite (small runtime, good for a polished app) when the
  redesign starts. Alternative: stay vanilla with a strict component pattern.
- **(D-4)** UI languages. Recommended: an i18n layer with English and Simplified
  Chinese at launch (a large share of the target users), English as the source
  language (project rule) and translations in resource files.
- Validate with a 5-person usability test before and after the redesign.

### M4: README as a product page

Structure (top to bottom):

1. Logo, one-line value proposition, badges (CI, release, license, downloads).
2. **Hero animation**: a 20–30 s loop of the full flow: ask, three answers stream in,
   "AI synthesize", the final answer with the disputed / unique / possibly-wrong list.
3. "Why llm-crossjury": three pains (one model can be confidently wrong; comparing tabs
   by hand is slow; no idea what it costs) and how each is solved.
4. Feature grid with short clips: compare, synthesize, attachments, subscription
   mode, cost control, privacy.
5. Quick start: download buttons (desktop) and one command (`uvx llm-crossjury ui`).
6. How it works: a diagram (Mermaid) of answer → agreement → review → chair, with the
   research basis in one paragraph.
7. Privacy and cost: what leaves the machine, to whom; budget defaults.
8. FAQ, roadmap link, contributing, license; star-history chart.

Production:

- `llm-crossjury ui --demo`: a demo mode with deterministic mock providers and canned
  answers (no keys, no cost).
- A Playwright script drives the demo UI and records WebM/MP4; convert to an optimized
  GIF for the README hero (< 8 MB) and keep MP4 for the site. Regenerate in CI on
  release.
- Screenshots in light and dark; alt text on every image.

### M5: Product site on GitHub Pages

- Style inspired by premium product pages (large type, generous whitespace,
  scroll-driven reveals, full-bleed product shots). Original design and assets only;
  no copied layouts, fonts or imagery from any company.
- Sections mirror the README: hero video, the three pains, feature scenes, "how it
  works", privacy, download.
- Static site in `site/` (recommended: Astro, or plain HTML with CSS scroll-driven
  animations), deployed by GitHub Actions to Pages. Lighthouse ≥ 95, reduced-motion
  fallback, works on phones.
- **(D-5)** Custom domain (optional, later).

### M6: Launch and growth

Launch readiness checklist:

- [ ] Release binaries plus PyPI package; README hero and site live.
- [ ] `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md` (key and file handling),
      issue and PR templates, labelled "good first issue" tasks.
- [ ] Docs: user guide, FAQ, model registry update guide (prices change; make
      community PRs easy and verifiable).
- [ ] A 60–90 s demo video.

Channels (in order): own network and lab groups; Show HN; Reddit (research, grad-school
and LLM communities, following each community's self-promotion rules); X/Twitter;
Product Hunt; Chinese communities (V2EX, Zhihu, Xiaohongshu, Jike); relevant
"awesome" lists. Launch one channel per day, answer every comment and issue quickly,
ship a visible fix within the first week.

Metrics to watch: stars, release downloads, PyPI installs, issues opened/closed,
first-run success (from user reports; no telemetry, per the SPEC non-goals).

## Model lifecycle (future work)

Vendors release, rename, reprice and retire models every few weeks. The registry
(ADR 0006) must keep up without users hand-editing YAML. Planned, in order:

1. **Ship a verified registry.** Real entries (with `source_url` and `last_verified`)
   ship in the package so the app works out of the box; `config/models.local.yaml`
   stays a user override (**D-7**).
2. **`llm-crossjury models refresh`** (already designed, still a stub): diff the
   registry against each vendor's model-list endpoint (api) and CLI listing
   (`grok models`, the Codex models page, Claude Code aliases); `--draft` writes
   unverified entries for a human to complete.
3. **Staleness and retirement in the UI:** warn when a price is older than N days
   (`PricingTable.stale_entries`), hide models the vendor no longer lists, and turn a
   vendor "model not found" error into "this model was retired; pick another".
4. **Registry updates without an app update:** the app can fetch the latest registry
   published with each GitHub release (hash-checked, opt-in, never executes code), so
   a new model or price reaches users the day it is verified.
5. **Maintenance automation:** a scheduled GitHub Action runs the refresh diff and
   opens a PR with drafts; a PR template asks for the official source links; CI
   validates the schema and that every verified entry has its sources.
6. **Subscription aliases:** consider letting cli members pick the CLI's own moving
   aliases (e.g. Claude Code's `sonnet` / `opus`), which follow the vendor's latest
   model automatically; cost is plan usage, so no price is needed.

## Risks

| Risk | Mitigation |
|---|---|
| Vendor prices and models change often | Registry with `source_url`/`last_verified`, `models refresh --draft`, staleness warnings, easy PR workflow |
| Subscription CLIs change flags or terms (e.g. `--bare` default) | Experimental label, re-check per release (ADR 0002), smoke script |
| Users fear cost | Cost shown before every run, conservative budget defaults, demo mode |
| Unsigned desktop builds look unsafe | Checksums, reproducible CI builds, signing when traction justifies it (D-2) |
| Trademark confusion | Use vendor names only to describe compatibility; no vendor logos as our branding |
| Scope creep before launch | Launch after M1–M2 plus README; M3 redesign and M5 site can follow as "v1.0" |

## Decisions needed

- **D-1** Desktop packaging: PyInstaller + pywebview (recommended) or Tauri sidecar.
- **D-2** Code signing now or after traction (recommended: after).
- **D-3** Frontend stack for the redesign: Svelte + Vite (recommended) or vanilla.
- **D-4** UI languages at launch: English + Simplified Chinese (recommended).
- **D-5** Custom domain for the site (optional).
- **D-6** Launch timing: after M1 + M2 + M4 (recommended), with M3/M5 as the v1.0 push.
- **D-7** Ship real, verified model entries in the package (reverses the phase-0
  "examples only" rule and its test; recommended, otherwise a fresh install has no
  usable model).
