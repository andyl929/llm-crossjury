# UI v2: design plan and audit

Scope: the local web UI (`src/llm_crossjury/web/static/`): the Compare page, the
Settings page and the first-run wizard. The redesign keeps every API call and
response contract unchanged; it only changes markup, styles and client-side behaviour.

Brief (maintainer): "Modern industrial feel, clean and minimal, with genuinely good
usability." It is a work tool for students and researchers, and the first impression
should make people want to star it.

Method: Anthropic's `frontend-design` skill (two-pass plan, AI-default tells, one bold
element, copy as design) and the UI/UX Pro Max priority rules (audit below).

## 1. Subject

- **Product:** a bench for cross-examining AI answers. One question goes to up to four
  models; their answers sit side by side; then the models review each other
  anonymously and a chair model writes the final answer plus a list of what it left
  out or doubts.
- **Audience:** students and researchers at a desk, often with a PDF or a screenshot,
  who care about what each answer costs.
- **Primary job:** compare answers and reach one trustworthy answer, without surprise
  costs.
- **Vernacular:** the jury (deliberation, verdict, dissent) and the instrument bench
  (readouts, signal lamps, control strip, DIN lettering on equipment). The brief's
  "industrial" points at the second; the product's name points at the first. The UI
  uses the bench for its look and the jury only in its structure (answers, then a
  verdict with recorded dissent), not as decoration.

## 2. Design plan, pass 1

**Colour (light / dark)**

| Name | Light | Dark | Role |
|---|---|---|---|
| Graphite | `#1E2226` | `#E3E6E9` | Text; also the control strip's fill in both themes (`#262B30` / `#2A2F35`) |
| Steel | `#58626B` | `#9BA6B0` | Secondary text, idle lamps, meta |
| Concrete | `#E6E8E9` | `#1B1F23` | Page background |
| Panel | `#F7F8F8` | `#24292E` | Work surfaces (rack, columns, synthesis) |
| Signal | `#F5B800` | `#F5B800` | The one bold colour: primary action fill and the cost gauge |
| Cobalt | `#2450B8` | `#8DB0FF` | Links, focus ring, selected state |

Status colours are semantic and always paired with an icon or a word: ok `#2E7D4F` /
`#6CCB8F`, warn `#94570A` / `#F2B35A`, error `#B42318` / `#FF8F85`.

**Type**

- *Display and readouts:* Bahnschrift (Windows' DIN 1451 face), then DIN Alternate
  (macOS), then the system sans. Used for the wordmark, section titles, column
  headers and every number (tabular figures).
- *Body:* the system UI face (Segoe UI Variable, San Francisco, Roboto), 16 px / 1.55.
  Answers are reading text, so they get the most neutral face available.
- *Code:* Cascadia Mono, SF Mono, Consolas, the system monospace. Code only, never
  labels.
- Scale (about 1.2): 12, 13, 14, 16, 19, 23, 28 px. Weights: 400 body, 600 labels
  and titles. Sentence case everywhere.

**Layout concept: a bench with a control strip.** The page is one vertical flow of
the real sequence (question, models, answers, synthesis). A graphite control strip is
pinned to the bottom edge: it holds the cost readouts, the budget gauge and the
primary action, so cost is in view before every click.

```
Desktop (1440)
+------------------------------------------------------------------------------+
| [mark] crossjury    Compare  Settings                         [theme]        |
+------------------------------------------------------------------------------+
| Question                                  | Models                           |
| +---------------------------------------+ | o Claude   [API|Sub] [model   v] |
| |                                       | | o ChatGPT  [API|Sub] [model   v] |
| |                                       | | o Gemini   [API    ] [model   v] |
| +---------------------------------------+ | o Grok     [API|Sub] [model   v] |
| [Attach files]  file.pdf 1.2 MB  x        | Estimate lines (after Estimate)  |
+------------------------------------------------------------------------------+
| Answers                [Sync scroll] [Export Markdown]                        |
| +----------------+ +----------------+ +----------------+                      |
| | Claude  model  | | ChatGPT model  | | Gemini  model  |  each column scrolls |
| | $0.01  2.1 s   | | ...            | | ...            |  on its own; collapse|
| | answer text    | |                | |                |  / full width / copy |
| +----------------+ +----------------+ +----------------+                      |
+------------------------------------------------------------------------------+
| Synthesis   Agreement -> Review -> Chair                                      |
| Final answer (72ch)                         | Agreement gauge, ranking, cost  |
| Additional list: disputed | unique | possibly wrong     [Regenerate]         |
+------------------------------------------------------------------------------+
|#### Estimate $0.01-$0.12 | Last run $0.03 | Today [||||......] $0.40 of $5 |  |
|####                            Ctrl+Enter  [Estimate cost] [Ask 3 models]    |
+------------------------------------------------------------------------------+

Phone (390)
+----------------------------+
| [mark] crossjury  [=] [th] |
| Question                   |
| [textarea]                 |
| [Attach files]             |
| Models (rack rows stack)   |
| Answers (stacked, each     |
|  collapsible)              |
| Synthesis                  |
|############################|
| Est. $0.01-0.12   [Ask 3]  |
+----------------------------+
```

Alignment: everything left aligned on a 4 px grid (8 / 12 / 16 / 24 / 32 spacing).
Numbers in readouts are right aligned within their cell.

**Principles**

1. *Cost is always in view.* The control strip shows the estimate before, the actual
   after, the budget left and the subscription calls. No action spends money without
   a number next to it.
2. *Compare, don't scroll.* Answers are parallel columns with their own scroll, an
   optional synchronized scroll, and per-column collapse, full width and copy.
3. *One bold thing.* Signal yellow appears only on the primary action and the budget
   gauge. Everything else is graphite, steel and hairlines.
4. *State is explicit.* Lamps plus words for provider status; every disabled button
   says why next to it; every error says how to fix it.
5. *Quiet surfaces.* Hairline borders, two radii (3 px controls, 6 px surfaces), no
   drop shadows except the strip's edge, no gradients.

## 3. Review against the brief (pass 2)

Working a similar prompt ("modern minimal AI comparison tool") through from scratch
lands on: a near-black page with one acid accent, rounded identical cards with soft
shadows, an all-caps tracked label above each section, a monospace face for
numbers, and meta strings joined with middle dots. The v1 UI already had two of
those (uppercase section labels, middle-dot meta strings). Changes made:

- **Accent.** First draft used safety orange. Orange-on-grey sits next to the
  terracotta default (`#D97757`) and next to the warning colour. Changed to signal
  yellow (the RAL 1003 family used on machine controls), which only works as a
  *fill* with graphite text: that forces it to stay on the primary action and the
  gauge, which is exactly the one-place boldness the brief wants.
- **Dark theme base.** First draft `#111418` reads as the "tinted near-black" tell
  and is harsh for long reading. Changed to graphite `#1B1F23` with panels one step
  lighter, so dark mode is a dimmed bench, not a void.
- **Provider cards.** First draft: a grid of four rounded cards (the SaaS-card kit).
  Changed to a *rack*: four rows of equal height, each with a signal lamp, name,
  backend switch and model select, separated by hairlines inside one panel. Rows read
  as equipment modules and compare better than cards.
- **Labels.** v1 used uppercase tracked section labels. Changed to sentence-case
  Bahnschrift titles; the type face carries the industrial tone, not the casing.
- **Meta strings.** v1 joined meta with `·`. Changed to discrete readout cells
  separated by space and a hairline; numbers in tabular figures.
- **Monospace for numbers.** Rejected: numbers use Bahnschrift with tabular figures;
  monospace is for code only.
- **Numbering.** Kept "Step 1 / Step 2" because the product really is a sequence and
  the README uses the same words. The synthesis progress is also a real sequence
  (agreement, review, chair), so it is shown as a stepper.
- **Emoji icons** for the additional list (scales, bulb, cross) replaced by one inline
  SVG icon set (1.75 px stroke, round caps), from a sprite in the page.

## 4. Implementation notes

- **Fonts:** no bundled font. Bahnschrift ships with Windows 10+ and DIN Alternate
  with macOS; Linux falls back to the system sans. Nothing is fetched, which keeps the
  strict CSP (`default-src 'self'`) and the wheel small. No font licence is needed.
- **Icons:** an inline `<svg>` sprite in each page (the HTML parser gives it the SVG
  namespace, so no namespace URL is written anywhere); JS clones `<use>` references.
- **Themes:** `theme.js` (new, loaded in `<head>` so there is no flash) applies a
  stored choice (`system`, `light`, `dark`) from `localStorage` inside try/catch, and
  wires the toggle on both pages. Settings code (`settings.js`) still never touches
  storage. Tokens live on `:root`, redefined for dark under
  `@media (prefers-color-scheme: dark)` with `:root:not([data-theme="light"])` and
  again under `:root[data-theme="dark"]`.
- **Markdown:** the safe renderer builds DOM nodes only (no HTML sinks). It supports
  headings, paragraphs, bold, italic, inline code, fenced code blocks (with a copy
  button), ordered and unordered lists (up to three levels), block quotes, rules,
  pipe tables and links. Links are allowed only for `http:` and `https:` URLs (parsed
  with `URL`), open in a new tab with `rel="noopener noreferrer"`, and show their
  target in a tooltip; anything else stays text.
- **Export:** "Export Markdown" builds the run (question, attachments, answers, final
  answer, additional list, costs) as text and downloads it through a `Blob` URL.
- **New routes:** `/theme.js` and `/favicon.svg` (static files, same headers).
- **Demo mode:** when `GET /api/providers` returns `demo: true`, the top bar shows
  "Demo mode: simulated answers, no API calls".
- **Keeping work:** once there are answers, Settings links inside the page open in a
  new tab, leaving the page asks first (`beforeunload`), and coming back to the tab
  refreshes key status without losing the selection.

## 5. Critique log

- *Pass 1 (screenshots):* a stray `null` text node under every answer (fixed); disabled
  primary buttons turned muddy olive at 45 % opacity (now a neutral disabled style);
  position stance and argument ran together (now two lines); the synthesis intro
  floated to the right (now under the title); summary markers sat outside the panel
  padding; the empty-state numerals left a gap; the phone strip was three rows tall
  (now a two-row grid, and phones get no Ctrl+Enter hints).
- *Pass 2:* steppers wrapped with a dangling connector on phones (connectors hidden
  below 720 px); the strip's disabled button picked up the light hairline colour (the
  strip now scopes its own `--rule`); table cells in narrow columns broke mid-word
  (cells now wrap at word boundaries and the table scrolls); "Add a xAI key" copy;
  four answers did not fit one row at 1200 px (column minimum 300 to 260 px); the
  demo badge overflowed the phone top bar (it takes its own row there).

## 6. Audit (UI/UX Pro Max, priority order)

P = pass, D = deliberate deviation (reason given), N/A = not applicable. No failures
remain open.

### 1. Accessibility (critical)

| Rule | Result | Notes |
|---|---|---|
| color-contrast | P | Light: graphite/panel 15.0, steel/panel 5.9, steel/concrete 5.1, cobalt/panel 6.8, ok 4.7, warn 5.4, error 6.2. Dark: 11.7, 5.9, 6.8, 7.4, 7.9, 6.7. Strip text 11.4, strip muted 6.3. Placeholders use steel. |
| focus-states | P | 2 px cobalt outline with offset; light cobalt inside the strip; the segmented switch shows focus on its label. |
| alt-text | N/A | No content images; icons are decorative (`aria-hidden`). |
| aria-labels | P | Every icon-only button has `aria-label` and `title`. |
| icon-context | P | Icons next to text are hidden; icon controls expose state (`aria-pressed`, `aria-expanded`). |
| keyboard-nav | P | DOM order matches visual order; native controls only. Checked by tabbing over CDP. |
| form-labels | P | Every field is labelled (the task's label is its heading; model selects name the provider). |
| skip-links | P | First tab stop on both pages. |
| heading-hierarchy | P | h1 (visually hidden), h2 panels, h3 answer titles and final answer, h4 for Markdown headings and side blocks. |
| color-not-only | P | Lamps, tags, severities and test results always carry a word; errors carry an icon. |
| dynamic-type | P | No fixed heights on text; text wraps instead of truncating. |
| reduced-motion | P | Stops spinners, pulses, transitions and smooth scrolling. |
| voiceover-sr | P | Polite live regions (action hint, status lines, toast), `role=alert` errors, hidden "running"/"skipped" text in the stepper. |
| escape-routes | P | Wizard Skip and Back; confirm dialogs can be cancelled. |
| keyboard-shortcuts | P | Only Ctrl/Cmd+Enter; browser and assistive shortcuts untouched. |
| focus-not-obscured | P | `scroll-padding-bottom` and body padding reserve the strip's height. |
| focus-appearance | P | 2 px outline at 6.8:1 against panels. |
| dragging-alternative | P | File drop also has "Attach files" and paste. |
| web-target-size | P | Smallest pointer target 28 px; 44 px on coarse pointers. |
| consistent-help | P | Help text sits in the same place on both pages. |
| redundant-entry | P | Selections and options survive backend switches and re-renders. |
| accessible-authentication | P | Key fields accept paste. |
| auto-rotation-controls | N/A | No carousels. |
| contextual-live-badge-updates | P | "3 of 4 selected" and the action hint update as whole phrases. |

### 2. Touch and interaction (critical)

| Rule | Result | Notes |
|---|---|---|
| touch-target-size, touch-spacing | P | 44 px targets on coarse pointers, 8 px gaps. |
| hover-vs-tap | P | Nothing depends on hover. |
| loading-buttons | P | Estimate, Ask, AI synthesize and Regenerate show a spinner and disable while busy. |
| error-feedback | P | Errors appear next to their cause (answer, estimate, synthesis, key row). |
| cursor-pointer, tap-delay, press-feedback | P | Pointer cursor, `touch-action: manipulation`, 1 px press offset. |
| no-precision-required | P | Whole rows are labels for checkboxes and radios. |
| gesture-alternative | P | Every action has a visible control. |
| gesture-conflicts, standard/system gestures, swipe, drag threshold, haptics | N/A | No custom gestures. |
| safe-area-awareness | P | No edge-hugging targets. |

### 3. Performance (high)

| Rule | Result | Notes |
|---|---|---|
| font-loading, font-preload | P | System fonts only: nothing to download. |
| critical-css | P | One stylesheet in head; `theme.js` runs before paint (no theme flash). |
| third-party-scripts | P | None (CSP `default-src 'self'`). |
| content-jumping | P | Strip space reserved; pending columns keep their header and skeleton. |
| progressive-loading | P | Skeleton lines plus elapsed seconds per member. |
| debounce-throttle, reduce-reflows | P | Synthesis estimate debounced; passive scroll listeners; sync scroll guarded per frame. |
| network-fallback | P | A lost server says to check the terminal and retry. |
| images, lazy loading, bundle splitting, virtualization | N/A | No raster images; one small script per page; at most four columns. |

### 4. Style selection (high)

| Rule | Result | Notes |
|---|---|---|
| style-match, color-palette-from-product | P | Calm instrument bench: graphite, steel, one signal yellow. |
| consistency, dark-mode-pairing | P | Both pages share tokens and components; themes designed together. |
| no-emoji-icons, icon-style-consistent | P | v1 emoji replaced by one SVG set (24 px grid, 1.75 px stroke). |
| effects-match-style, elevation-consistent | P | Two radii, no gradients, one raised surface (the strip). |
| system-controls | P | Native checkboxes, radios, selects and confirm dialogs. |
| state-clarity | P | Distinct hover, pressed, selected and disabled states. |
| blur-purpose | N/A | No blur. |
| primary-action | D | One primary per working area: Ask (strip) for step 1; AI synthesize, then Regenerate, inside the synthesis panel. |

### 5. Layout and responsive (high)

| Rule | Result | Notes |
|---|---|---|
| viewport-meta | P | Zoom allowed. |
| breakpoints | P | 720 / 1024 / 1180 px; checked at 360, 390, 820, 1200 and 1440. |
| readable-font-size, line-length-control | P | 16 px body; Markdown 78ch, final answer 72ch. |
| horizontal-scroll | P | No page overflow at 360 px (checked by script); wide tables and code scroll inside their block. |
| spacing-scale, container-width, z-index | P | 4 px grid; 1600 / 920 px max widths; z 40 strip, 50 toast, 100 skip link. |
| fixed-element-offset, viewport-units | P | Strip offset reserved; `100dvh`. |
| scroll-behavior | D | Answer columns scroll inside the page on purpose, for side-by-side comparison; `overscroll-behavior: contain` and each column is a focusable region. |
| content-priority, visual-hierarchy, label overflow | P | Phones keep estimate and actions; tags wrap; nothing is truncated. |

### 6. Typography and colour (medium)

| Rule | Result | Notes |
|---|---|---|
| line-height, font-scale, weight-hierarchy | P | 1.55 body; 12 to 28 px scale; 600 titles, 400 body. |
| font-pairing | P | DIN-style display with the system text face. |
| color-semantic, color-dark-mode, accessible pairs | P | Tokens only in components; tonal dark variants; contrast above. |
| number-tabular | P | All readouts, costs, tokens and counters. |
| letter-spacing | P | Default tracking, no all-caps. |
| truncation, long tokens, heading balance | P | Wrapping, `overflow-wrap: anywhere` for ids and URLs, `text-wrap: balance`. |

### 7. Animation (medium)

| Rule | Result | Notes |
|---|---|---|
| duration tokens, consistency, easing | P | 120 ms state, 180 ms toast, deceleration curve. |
| transform-performance, layout-shift-avoid | P | Only colour, opacity and transform animate. |
| excessive-motion, motion-meaning | P | No entrance animations; motion answers actions only. |
| interruptible, no-blocking, cancellable transitions | P | Nothing blocks input; stale estimate responses are dropped by sequence number. |
| reduced-motion | P | See accessibility. |
| page transitions, modals, parallax, stagger | N/A | None used. |

### 8. Forms and feedback (medium)

| Rule | Result | Notes |
|---|---|---|
| input-labels, helper text | P | Visible labels (the inline key form gained one); persistent help under task, attachments and key field. |
| error-placement, error-clarity, error-recovery | P | Errors sit next to the cause and say how to fix it: key errors link to Settings, budget errors say what to change, CLI errors point to "Install and sign in". |
| submit-feedback, success-feedback, toast-dismiss | P | Spinner, then result line or toast (3 s, `role=status`). |
| ime-composition-enter | P | Composition guard before the shortcut; explicit buttons exist. |
| empty-states | P | The answers area teaches the three steps; synthesis says why it is unavailable. |
| disabled-states | P | Neutral fill, `disabled`, `not-allowed`, and the reason beside it. |
| confirmation-dialogs, destructive-emphasis | P | Over-threshold runs and key deletion ask first; delete uses the error colour. |
| progressive-disclosure | P | Model options, CLI help and cost breakdown start collapsed. |
| multi-step-progress, focus-management | P | Wizard stepper with Back, focus to each step heading; skip decision focuses "Skip review". |
| form-autosave, sheet-dismiss-confirm | P | Leaving with answers asks first; in-page Settings links open a new tab. |
| password-toggle | D | Not offered: keys are never shown in the page (security rule). |
| autofill-support | D | `autocomplete="off"` on keys so browsers do not store them. |
| inline-validation, error-summary, undo | N/A | No multi-field forms; the only destructive action confirms first. |

### 9. Navigation (high)

| Rule | Result | Notes |
|---|---|---|
| nav-state-active, consistency, persistent nav | P | Same top bar on both pages, current page underlined with `aria-current`. |
| back-behavior, state-preservation | P | See forms: work is protected when leaving. |
| deep-linking | P | `/settings`, `/settings?onboarding=1`. |
| empty-nav-state | P | Disabled providers explain why and link to the fix. |
| bottom nav, drawers, breadcrumbs | N/A | Two pages. |

### 10. Charts and data (low)

| Rule | Result | Notes |
|---|---|---|
| screen-reader-summary, direct-labeling | P | Agreement bar has an `aria-label`; percentages and scores are printed. |
| export-option | P | Markdown export of the whole run. |
| other chart rules | N/A | Only a budget gauge and an agreement bar. |

## 7. Screenshots

`docs/design/screenshots/<view>-<desktop|mobile>-<light|dark>.png`, at 1440x900 and
390x844, taken with headless Edge against fake providers and stages (real registry
and prices). Views: `empty`, `estimate`, `results` (three answers), `synthesis` (final
answer), `additional` (additional list), `settings`, `wizard`.
