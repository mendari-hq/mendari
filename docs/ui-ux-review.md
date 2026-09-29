# Dashboard UI/UX review and redesign plan

_Status: proposal, awaiting approval. Reviewed 2026-09-29. No application code was changed._

Scope: the developer dashboard served at `/` (`src/openhealth/dashboard/`). The API, the data
models and every computation stay as they are. Every number the redesign shows comes from the
existing endpoints (`/api/packs/{id}`, `/api/stats`, `/api/review`, `/api/audit`, `/api/evals`,
`/api/feedback`, `/api/packs/{id}/examples`). No mock or placeholder data.

---

## 1. Current-state summary

**Stack.** FastAPI serves three static files: `index.html` (37 lines), `style.css` (224) and
`app.js` (747). The app is vanilla JavaScript with a hash router (`#overview`, `#review`, …),
string-template rendering into `<main id="view">`, and no framework, build step or runtime
dependency. The only chart is a hand-written SVG (the reliability diagram). Styling uses CSS
custom properties with a `prefers-color-scheme: dark` override, so a dark theme exists but
follows the OS only. `versioned_dashboard()` (`server/app.py:374`) stamps `app.js` and
`style.css` with a content hash; any new asset file must be added to that list.

**Screens.**
- **Shell:** top bar with brand, sandbox tag and pack picker (a native `<select>`), plus a
  left nav that becomes a horizontal strip below 760 px.
- **Overview** (`app.js:142`): nine KPI tiles, an outcome-mix bar, and a daily-volume table
  with mini stacked bars.
- **Review queue** (`app.js:193`): cards for HUMAN_REVIEW and ABSTAIN decisions, each with a
  row of label buttons.
- **Audit trail** (`app.js:253`): a 100-row table. Clicking a row renders the hash-chain
  events, criteria, feedback and the raw JSON record below the table.
- **Evaluations** (`app.js:308`): dataset picker and "Run evaluation"; report tiles; local
  vs benchmark comparison; reliability diagram; threshold sweep; per-label table; subgroups
  table. The provider comparison (leaderboard, `app.js:342`) comes last.
- **Feedback** (`app.js:442`): three count tiles, a filter, JSONL export, and feedback cards.
- **Decision Card** (`app.js:528`): the manifest rendered as tiles, prose, question panels,
  policy, providers and JSON schemas.
- **Try it** (`app.js:580`): a message form for the inbox pack, or a JSON textarea seeded from
  behaviour tests for record packs. The result shows badges, reasons, a criteria table and
  probability bars.

**Data displayed.**
- **Counts and rates:** decisions, AUTO share, pending reviews, override rate, observed
  accuracy, latency p50/p95, cost per decision, feedback counts, audit-chain status.
- **Time series:** decisions per day by outcome (`stats.daily`, all days, grouped by UTC date).
- **Part-to-whole:** outcome mix, overall and per day.
- **Evaluation metrics:**
  - accuracy, macro-F1, ECE, Brier, automation, false automation, abstention, expected
    override rate;
  - critical-label detection;
  - reliability bins;
  - the threshold sweep;
  - per-label precision, recall and F1;
  - subgroup metrics;
  - a confusion matrix, which is computed but never shown.
- **Rankings:** the provider leaderboard (8–10 configurations per dataset, 2–4 datasets per
  pack, 28–36 rows), plus the other-questions accuracy/ECE matrix.
- **Per-decision data:** outcome, priority, recommendation, confidence, per-question label
  probabilities, criteria answers with evidence, reasons, fallback, and hash-chain events.

**How this review was done.**
- Ran the app locally (`openhealth serve`, free default providers, the local `var/dev.db`).
- Viewed every screen at 1440×900 and 390×844, in light and emulated dark mode. Screenshots
  were captured in the review session, not committed.
- Data used: the Patient Inbox Router (154 decisions, 13 pending, feedback), AI Guard
  (34-row leaderboard, a record-shaped Try it) and packs with no decisions (empty states).
- Contrast ratios were computed from the CSS token values. Labels, focusability and layout
  sizes were measured in the DOM.

**Verdict.** The dashboard is functional and careful in places:
- escaping is consistent;
- empty states exist for most lists;
- focus rings are visible;
- tables scroll inside their panels instead of the page;
- `tabular-nums` is applied to tables and tiles.

It reads as an engineer's instrument, not a product:
- Every piece of information has the same visual weight.
- Engineering identifiers are shown to clinicians: `HUMAN_REVIEW`, `safety_screen=rules`,
  `screen=rules_and_llm (anthropic/claude-sonnet-5) · dosing=dosing`.
- Emoji stand in for icons.
- The most important artefacts are buried. The provider comparison starts 2,195 px down the
  Evaluations page on desktop and 3,391 px down on mobile. Stat emergencies look like routine
  cards with a small red badge.
- Several accessibility failures are real, not cosmetic. Keyboard users cannot open an audit
  record. Primary buttons fail contrast in dark mode.

---

## 2. Findings by screen

Severity:
- **high:** blocks a task, misleads, or fails accessibility for a whole class of users;
- **medium:** clear friction or inconsistency;
- **low:** polish.

### Shell (all screens)

| Sev. | Finding | Where |
|---|---|---|
| high | Primary buttons ("Decide", "Run evaluation") fail contrast in dark mode: white on `#7ea2ff` is **2.47:1** (needs 4.5). | `style.css:32`, `style.css:86` |
| high | `<main aria-live="polite">` makes screen readers announce **the entire view** on every navigation and re-render. | `index.html:32` |
| medium | Changing view keeps the previous scroll position: Evaluations opened 3,775 px down after visiting the audit detail. Focus is not moved to the new view's heading. | `app.js:48` (`render`) |
| medium | Light theme badges fail AA at 11 px: AUTO green **4.40:1**; review amber **4.26:1**. The amber pair is also used by the sandbox tag and the review count. | `style.css:10–13`, `124–136`, `71`, `99` |
| medium | Input, select and button borders are **1.31:1** (light) and **1.36:1** (dark). WCAG 1.4.11 asks 3:1 for component boundaries. | `style.css:5`, `77–84` |
| medium | Dark mode follows the OS only: no toggle, no persisted choice. | `style.css:24–45` |
| medium | Loading is a single "Loading…" line that replaces the whole view; there are no skeletons. The error state is a bare sentence with no retry. Errors reach users as a toast that disappears after 2.6 s and is `role="status"`, not an alert. | `app.js:52`, `56`, `22–28`, `index.html:34` |
| medium | Emoji are used as UI (👍/👎 in tiles, headings, buttons, a download link). They render differently per OS and read as casual in a clinical tool. 15 uses. | `app.js:163`, `450–455`, `465`, `479`, `694–695`, `714–715` |
| medium | Raw enum and slot strings are shown as text (`HUMAN_REVIEW`, `safety_screen=rules`), in legends, table headers, filters and the pack subtitle. | `app.js:153`, `172`, `187`, `260` |
| medium | The pack picker, the context for every screen, is a small native select in the top-right corner. | `index.html:16–19` |
| medium | Mobile nav is a sideways-scrolling strip 792 px wide in a 390 px viewport. Half the sections are off-screen, with only a thin scrollbar as a cue. | `style.css:217` |
| medium | "sandbox · synthetic data only", a safety disclaimer, is hidden on mobile. | `style.css:222` |
| low | 31 inline `style=""` attributes in JS templates (margins, widths) bypass the stylesheet. | `app.js:70, 166, 170, …, 682` |
| low | Ad-hoc spacing values (2, 4, 6, 7, 8, 10, 12, 14, 16, 18, 20, 24, 28, 30, 60 px) and a 650 weight. Many system fonts render 650 as 600 or 700 unpredictably. | `style.css` passim |
| low | Smooth scrolling and the toast transition ignore `prefers-reduced-motion`. | `app.js:303`, `style.css:210` |
| low | `button:disabled` uses `cursor: progress`, even for buttons disabled for other reasons. | `style.css:87` |
| low | `main` is capped at 1,180 px and left-aligned; on wide screens the right third is empty. | `style.css:104` |

### Overview

| Sev. | Finding | Where |
|---|---|---|
| high | Nine equal tiles, no hierarchy. "Waiting for review: 13" is actionable and "Latency p95: 0.19 ms" is trivia, yet they look identical. On mobile the tiles fill about two screens before any chart. | `app.js:154–164` |
| medium | Daily volume is a time series shown as a table with tiny bars. Trend and spikes are not visible; the table grows by one row per day forever, since `stats.daily` returns every day. | `app.js:169–177`, `core/audit.py:528` |
| medium | The active providers are a run of `slot=variant` text in the subtitle, not a readable "what is deployed" summary. | `app.js:152–153` |
| low | "Audit chain verified" and feedback counts are status facts presented as KPI tiles. Cost shows `$0.00000` (five decimals, no unit scaling). | `app.js:161–163` |
| low | A pack with no decisions shows nine tiles of `0` / `n/a`. The only guidance is inside the daily-volume panel. | `app.js:177` |

### Review queue

| Sev. | Finding | Where |
|---|---|---|
| high | A stat emergency ("tight chest pressure… sweating") looks like a routine card. The only difference is an 11 px red `stat` badge. In a triage queue, priority should be the first thing you see. | `app.js:233–236` |
| medium | After resolving an item the card is removed and focus is lost (focus goes to `body`). Keyboard reviewers must tab from the top again. There are no shortcuts for "confirm recommendation". | `app.js:219` |
| low | The recommended label is marked only by a ✓ and an accent border among equal buttons. | `app.js:245–246`, `style.css:164` |
| low | No filter by priority or outcome, and a silent cap at 100 items. | `app.js:194` |

### Audit trail

| Sev. | Finding | Where |
|---|---|---|
| high | **Rows are mouse-only:** 100 `tr.clickable` rows, 0 focusable. Keyboard and screen-reader users cannot open a record. | `app.js:273`, `279` |
| high | The detail renders **below all 100 rows** and smooth-scrolls about 4,000 px. There is no close, no way back to the row, and no URL for the record. | `app.js:266`, `291–304` |
| high | **Mobile:** each row is 159 px tall (three rows per screen). The ID column is squeezed to 33 px and `.mono { overflow-wrap: anywhere }` wraps the 8-character ID one letter per line. The table is 789 px wide in a 356 px frame with no scroll cue. | `style.css:60`, `app.js:277` |
| medium | A 👍/👎 pair in every row adds 200 buttons of noise to a scanning table. | `app.js:277` |
| medium | The record is raw pretty-printed JSON, and event payloads are raw JSON in a table cell. | `app.js:298`, `302` |
| low | Full locale timestamps ("9/25/2026, 11:17:48 PM") in every row; there's no relative time or day grouping. | `app.js:274` |

### Evaluations (report)

| Sev. | Finding | Where |
|---|---|---|
| high | The dataset picker and the report shown are unrelated. The picker preselects `local-site-example` while the page shows the published `test` benchmark. A second, **unlabelled** select switches history. | `app.js:311–327`, `320` |
| high | Page length: 6,491 px (desktop) and 8,441 px (mobile). About 40 subgroup rows push the provider comparison, the table used to choose a deployment, to the very bottom. | `app.js:324`, `405–409` |
| medium | "Run evaluation" calls the deployed providers (Jev, Claude) with no warning of how many remote calls it makes. In production it spends money on a single click. | `app.js:328–339` |
| medium | Subgroups are one flat table in attribute order. Weak subgroups, small n and "n/a" rows aren't highlighted or sorted. | `app.js:405–409` |
| medium | The reliability diagram has 10 px labels, an "acc" abbreviation, no ticks and no bin-size legend. The threshold sweep is a table that hides the coverage/accuracy trade-off and doesn't mark the pack's own AUTO threshold. | `app.js:424–438`, `396–399` |
| low | Numbers are left-aligned in every table. `th { text-align: left }` applies to numeric columns too. | `style.css:148` |
| low | The primary question's confusion matrix is in the report but never displayed. | report `questions[primary].confusion` |

### Evaluations: provider comparison (leaderboard)

| Sev. | Finding | Where |
|---|---|---|
| high | The key column, "Critical detected", is a single semicolon-joined string (`verdict=block 20/20 (0 automated); question_emergency=yes 19/19 …`). It runs off-screen even at 1,440 px, and on mobile it's about 1,500 px of sideways scrolling away. | `app.js:345–346`, `354` |
| high | The configuration currently deployed isn't marked. Users can't see which row is live. | `app.js:353` |
| medium | Sorted by accuracy only. For a safety pack the ranking that matters is critical detection. There's no rank column and no sorting. | `app.js:353` |
| medium | Configuration labels are raw strings with constant noise (`· dosing=dosing` in every AI Guard row) and wrap to 3 lines. Constant columns (AUTO 0.0%, False automation n/a in every row at risk level 2) take space. | `app.js:353–356` |
| medium | Datasets are stacked vertically (2–4 tables of 8–10 rows each) instead of being switchable. Numbers are left-aligned; units are mixed between headers and cells. | `app.js:350`, `355` |

### Feedback

| Sev. | Finding | Where |
|---|---|---|
| medium | The page is emoji-led (tiles, filter, export link, card badges). The three tiles duplicate what the filter shows. | `app.js:450–467`, `479` |
| low | Cards don't link to their audit record; the decision id is plain text. | `app.js:488` |

### Decision Card

| Sev. | Finding | Where |
|---|---|---|
| medium | Prose runs to about 140 characters per line at 1,440 px (comfortable reading is 60–75). The card is a long document with no table of contents or section anchors. | `app.js:542–548`, `style.css:104` |
| medium | Policy rules are a dense bullet list mixing notation (`P(verdict ∈ [block]) ≥ 0.3`), badges and prose. | `app.js:551–556` |
| low | Risk level, thresholds and the CDS service are KPI tiles. They are facts, better as a definition list. The digest is a long hash with no copy action. JSON schemas are raw `<pre>` at the end. | `app.js:536–541`, `564–565` |

### Try it (and the decision result)

| Sev. | Finding | Where |
|---|---|---|
| high | The result doesn't lead with the outcome. BLOCK or stat is an 11 px badge; the fallback ("Do not send the draft…") is a plain paragraph. For a guard, "blocked, do this instead" should be the headline. | `app.js:676–683` |
| medium | "Decide" has no loading or disabled state. With remote providers (0.2–3 s) the button appears dead, and double clicks record duplicate decisions. The result renders below the fold with no scroll or focus to it. | `app.js:608–621`, `646–658` |
| medium | Semantic colour misuse: the chosen label's bar is always green (`--auto`), including when the verdict is `block`. Criteria rows that cause a block and rows that cause a review share the same amber highlight. | `style.css:169`, `188`; `app.js:120` |
| low | For record packs, the example picker says "Choose a behaviour test…" while the textarea already holds the first example. | `app.js:593–598` |
| low | Sample buttons for the inbox look identical to action buttons. | `app.js:629` |

---

## 3. Design system spec

Everything stays in the existing stack: CSS custom properties in `style.css` and plain
JavaScript. **No new runtime dependencies.** One vendored asset is proposed: about 12 SVG icons
copied from Lucide (ISC licence) into an inline sprite, to replace emoji. They are static files,
not a package.

### Colour tokens (semantic; components never use hex)

Every pair below was checked against WCAG AA with the formula used for the audit. Text pairs
are at least 4.5:1; UI boundaries and chart marks are at least 3:1.

| Token | Light | Dark | Use / checked contrast |
|---|---|---|---|
| `--color-bg` | `#f5f6f8` | `#0e131a` | page |
| `--color-surface` | `#ffffff` | `#151c26` | cards, tables |
| `--color-surface-sunken` | `#eef0f3` | `#1b2430` | table hover, code, tracks |
| `--color-border` | `#dde1e7` | `#2a3544` | decorative dividers only |
| `--color-border-strong` | `#7d8797` | `#6b7a8f` | inputs, selects, buttons (≥ 3.18:1 on every surface) |
| `--color-text` | `#17202c` | `#e6ebf2` | body (16.4 / 14.3:1 on surface) |
| `--color-text-muted` | `#566172` | `#9aa6b8` | secondary (≥ 5.49 / 6.35:1 on every surface) |
| `--color-accent` | `#1f4fc9` | `#7ea2ff` | links, primary actions, focus |
| `--color-on-accent` | `#ffffff` | `#0b1220` | text on accent (6.95 / 7.58:1; fixes the 2.47:1 failure) |
| `--color-accent-soft` | `#e7edfc` | `#1c2a4b` | selected nav, recommended label |
| `--color-success` / `-soft` | `#136c3e` / `#e2f3e9` | `#5fcf92` / `#12301f` | AUTO, "no degradation" (5.62 / 7.37:1) |
| `--color-warning` / `-soft` | `#8a4f00` / `#fcefd8` | `#f0b152` / `#35260c` | HUMAN_REVIEW, urgent, "review" effect (5.78 / 7.75:1) |
| `--color-neutral` / `-soft` | `#4a576c` / `#e8ecf2` | `#a7b3c6` / `#232d3b` | ABSTAIN, routine (6.17 / 6.56:1) |
| `--color-danger` / `-soft` | `#b3261e` / `#fbe5e3` | `#ff8a82` / `#3a1817` | BLOCK, stat, degraded (5.42 / 6.96:1) |
| `--color-danger-solid` | `#b3261e` | `#c93b3f` | solid stat/BLOCK banner with white text (6.54 / 5.02:1) |
| `--color-focus` | = accent | = accent | 2 px ring, 2 px offset |
| `--chart-1…6` | `#1f4fc9 #0f7a6c #8a4f00 #7a3fb0 #b3261e #4a576c` | `#7ea2ff #4fd1bd #f0b152 #c49bff #ff8a82 #a7b3c6` | categorical series (≥ 5.2:1 on surface in both themes) |

The outcome and priority aliases map onto the status palette, so meaning stays consistent:
- `--outcome-auto` → success;
- `--outcome-review` → warning;
- `--outcome-abstain` → neutral;
- `--outcome-block` → danger;
- `--priority-stat` → danger-solid;
- `--priority-urgent` and `--priority-asap` → warning;
- `--priority-routine` → neutral.

### Type scale

The base stays at 14 px: this is a dense data tool. The type is the system UI stack, with no
web-font dependency, and `font-variant-numeric: tabular-nums` applies to every numeric context.

| Token | Size / line height | Weight | Use |
|---|---|---|---|
| `--text-xs` | 12 / 16 | 500 | captions, badges (new minimum: nothing below 12 px) |
| `--text-sm` | 13 / 18 | 400 | table cells, secondary text |
| `--text-md` | 14 / 20 | 400 | body |
| `--text-lg` | 16 / 24 | 600 | panel titles (h2) |
| `--text-xl` | 20 / 28 | 600 | view titles (h1) |
| `--text-kpi` | 28 / 32 | 600 | primary KPI values |
| `--text-kpi-sm` | 18 / 24 | 600 | secondary KPI values |

Weights are 400, 500 and 600 only (650 is dropped). Prose blocks get `max-width: 72ch`.
Identifiers (ids, digests, models) use the mono stack and never wrap mid-token, except inside
explicit copyable blocks.

### Spacing, radii, elevation, motion

- **Spacing** (4 px base): `--space-1…10` = 4, 8, 12, 16, 20, 24, 32, 40, 48, 64. Panel padding
  is 16/20; the gap between panels is 16; table cell padding is 8/12. The 31 inline styles are
  replaced by utility classes (`.stack-sm`, `.mt-0`, `.cluster`).
- **Radii:** `--radius-sm` 6 (inputs, buttons, chips), `--radius-md` 10 (cards), `--radius-pill`.
- **Elevation:** `--shadow-1` (cards, light only) and `--shadow-2` (popovers, the detail
  drawer). Dark mode uses borders instead of shadows.
- **Motion:** `--duration-fast` 120 ms, `--duration-base` 200 ms, `--ease-out`. All motion,
  including `scrollIntoView({behavior:"smooth"})`, is disabled under
  `prefers-reduced-motion: reduce`.

### Components (one definition each, replacing one-offs)

- **Buttons:**
  - variants: primary, secondary, ghost and danger;
  - sizes: sm and md;
  - a loading state (spinner plus `aria-busy`);
  - `cursor: not-allowed` when disabled.
- **Badge** (outcome, priority, status). **Priority "stat"** is a solid danger chip, unlike the
  soft chips.
- **Card or panel** with an optional header row (title, actions).
- **Stat** (KPI) in primary and secondary sizes, with an optional delta and footnote. It
  replaces `tile()`.
- **Data table:**
  - `<thead>`, `<th scope>` and a `<caption>`;
  - numeric columns right-aligned and tabular;
  - sticky header;
  - an optional sort;
  - optional row selection with keyboard support.
- **Segmented control** (dataset tabs, theme toggle, evaluation sub-views).
- **Drawer** (audit detail): an `aria-modal` dialog with focus trap and Escape to close.
- **Callout** (fallback, degradation, sandbox notice).
- **Skeletons** (tile, table rows, card), an **error panel** with retry, and an **empty
  state** with a next action.
- **Icon sprite:** thumbs up/down, check, alert, shield, clock, copy, external link, sun, moon,
  monitor, chevron.

---

## 4. Theming plan

- **One token set, two themes.**
  - Light values are on `:root`.
  - Dark values are on `:root[data-theme="dark"]`.
  - The same dark values also apply under
    `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }`.
  - Components reference only semantic tokens, so a theme is a token swap.
- **Default:** follow `prefers-color-scheme`.
- **Toggle:** a three-state segmented control in the top bar (System · Light · Dark, with icons
  and labels). The choice is stored in `localStorage` under `oh.theme`, reusing the existing
  `storageGet`/`storageSet` helpers so private mode fails silently.
- **No flash:** a small inline `<script>` in the `<head>` of `index.html`, before the
  stylesheet, reads `oh.theme` and sets `data-theme` on `<html>` before first paint. A
  `<meta name="color-scheme" content="light dark">` makes native controls and scrollbars match.
  The script is about 6 lines. Note for v0.6: if a Content Security Policy is added, it needs
  a hash for this script.
- **Contrast verified in both themes:**
  - every text pair and non-text pair in section 3 (including chart marks, input borders,
    focus ring, badges and disabled text);
  - a small `tests/test_dashboard_contrast.py`, with no new dependency, parses the token blocks
    from `style.css` and asserts the pairs. A later colour edit can't regress AA unnoticed.
  - Disabled controls keep text at 4.5:1 or more and mark "disabled" with a dashed border.
    Opacity alone is not used, because it drops contrast unpredictably.
- **Charts** read colours from the same CSS variables (SVG `fill: var(--chart-1)`), so they
  switch with the theme without re-rendering.

---

## 5. Leaderboard spec

The data is the pack's `evals/leaderboard.json` rows, as returned in `detail.leaderboard`.
No new data or computation.

- **Placement.** Evaluations gets sub-views as a segmented control:
  - **Provider comparison** (default when a leaderboard exists);
  - **Benchmark report**;
  - **Run on a dataset**;
  - **History**.

  The comparison no longer sits under the subgroups table.
- **Dataset switcher.** A segmented control, one segment per dataset with its `n`, replaces
  stacked tables. The default is the pack's held-out set if it has one (its name ends in
  `_holdout`), otherwise its benchmark dataset. The choice is persisted per pack.
- **Semantic table.**
  - `<table>` with a `<caption>` naming the dataset and `n`;
  - `<thead>` with `position: sticky; top: 0` inside a scroll container;
  - `<th scope="col">` with `aria-sort`;
  - numeric columns right-aligned with `tabular-nums` and units in the header ("p50, ms",
    "Cost / decision, $").
- **Columns** (in priority order):
  1. **Rank**: position under the current sort.
  2. **Configuration**, split into readable parts:
     - the variant per slot as short labels;
     - the model as a chip (e.g. `Claude Sonnet 5`, `gpt-oss-120b`, `Jev 1.13`);
     - slots that are identical in every row (e.g. `dosing=dosing`) removed from the cells
       and named once in the caption ("All rows: dosing = dosing").
  3. **One column per critical label** (e.g. "Blocks caught", "Emergencies caught"): `20/20`
     with a thin fill bar. The cell turns danger-coloured when `detected < n`, and shows
     "`k` automated" when `auto_missed > 0`.
  4. **Accuracy.**
  5. **Macro-F1.**
  6. **ECE.**
  7. **AUTO rate.**
  8. **False automation.**
  9. **p50.**
  10. **Cost per decision.**
  11. **Errors.**

  Columns whose value is identical in every row (e.g. AUTO 0.0% and False automation n/a at
  risk level 2) collapse into the caption ("AUTO 0% in every configuration").
- **Sorting.**
  - The default is safety-first: critical detection (worst critical label first), then
    accuracy, then cost ascending.
  - Every column header is a `<button>` that toggles ascending and descending, with a
    keyboard-operable sort and an `aria-sort` update.
  - Ties keep the default order.
- **Highlighting.**
  - **There is no "current user" in this data.** The equivalent is the **configuration
    deployed right now**, known from `detail.active_providers`. It gets a subtle accent row
    background and a "Deployed" badge.
  - Row hover uses `--color-surface-sunken`.
- **Top 3.** A small numeric rank chip in accent-soft for ranks 1–3 and plain text after that.
  No medals, no gold, no confetti. The treatment follows the active sort.
- **Length.** Real leaderboards are 8–10 rows per dataset, so there's no pagination or
  virtualization. If a dataset ever has more than 25 rows, the table shows 25 with a
  "Show all" toggle.
- **Mobile layout.** The table stays a table, with a **sticky first column** (rank and
  configuration) and horizontal scroll inside its container. An edge shadow shows while more
  columns are hidden, and columns are ordered by priority so the critical-detection and
  accuracy columns come right after the configuration.
  - **Why:** the leaderboard's job is comparison across rows. Stacked-row cards break the
    vertical comparison of the very numbers people are choosing between. Priority ordering
    puts the decisive columns on screen at 390 px (configuration ≈ 150 px, critical ≈ 90 px,
    accuracy ≈ 70 px).
- **Other-questions matrix.** The same table component, with cells shaded on a sequential
  token scale by accuracy, collapsed behind "Per-check accuracy" by default.
- **Optional chart.** A small cost-versus-accuracy scatter (log cost on x), one dot per
  configuration with the deployed one ringed. It shows the actual trade-off behind a
  deployment choice (see section 6).

---

## 6. Data-visualization decisions

| Data | Chosen visual | Reason |
|---|---|---|
| Decisions (count) | Secondary stat | A single number with no comparison. |
| Automated share | **Primary stat** with the AUTO count as footnote | The headline efficiency number. |
| Waiting for review | **Primary stat**, linked to the queue, warning tone when > 0 | Actionable; it's the reason to open the dashboard. |
| Human override rate | Primary stat with the resolved count | A quality signal with its denominator. |
| Observed accuracy | Primary stat with the labelled count | A quality signal; needs its n to be honest. |
| Latency p95, cost per decision | Secondary stats | Operational context, not decisions. |
| Audit chain status | Status line with an icon ("Verified · 170 events") | A boolean fact, not a KPI. |
| Feedback up/down, 👎 on AUTO | Secondary stats with icons (not emoji), linked to Feedback | Small counts; the AUTO-wrong count is the one to watch. |
| Outcome mix (4 outcomes) | 100% stacked horizontal bar with direct labels | Part-to-whole with 4 parts; a pie is unreadable past 3 slices. |
| Decisions per day by outcome | **Stacked column chart** (last 30 days, windowed client-side), with the table behind "View data" | A trend over time with composition; the table remains for exact lookup. |
| Report: accuracy, macro-F1, ECE/Brier, automation, false automation, abstention, expected override | Stats: primary for accuracy, critical detection and false automation; secondary for the rest | Single values; safety metrics lead. |
| Critical-label detection | Primary stat `k/n`, danger when `k < n` | A safety-critical exact fraction. |
| Local vs benchmark comparison | Callout with status, delta and a findings list | A verdict plus reasons: text, not a chart. |
| Reliability bins | **Scatter against the diagonal** (dot area = bin n, labelled axes, ticks 0–1) | Calibration is a relationship between two quantities. |
| Threshold sweep | **Line chart**: coverage and accuracy against threshold, marking the pack's AUTO threshold; table behind "View data" | Shows the trade-off curve the table hides. |
| Per-label precision, recall, F1, support | Table, right-aligned, with the support column | Exact lookup across four metrics. |
| Confusion matrix (already in the report) | Table with sequential shading (optional, phase 3) | Exact counts, where errors cluster. |
| Subgroups (attribute = value) | Table grouped by attribute, sorted worst-first, with an inline accuracy bar and small-n (< 20) flagged; groups collapsible | Many categories and exact values; inline bars add comparison without a second chart. |
| Provider leaderboard | Table (section 5) | Ranking plus exact lookup across many metrics. |
| Cost against accuracy per configuration | Scatter (optional), log cost | The trade-off behind choosing a deployment. |
| Per-question label probabilities (Try it) | Horizontal bar chart, sorted, semantic colour for the chosen label (block = danger) | Comparison across a few categories. |
| Criteria answers and effect | Table with an effect badge coloured by the effect (block = danger, review = warning) | Exact per-criterion lookup. |
| Reasons, fallback, evidence | Text: the fallback as a callout, reasons as a list, evidence as chips | Explanations, not measurements. |
| Review items | Cards, grouped and ordered by priority, stat as a solid banner | Each item is a task, not a row. |
| Audit decisions | Table plus a detail drawer | Scanning, filtering and exact lookup. |
| Hash-chain events | Vertical timeline (event, time, short hash); payload collapsible | An ordered sequence. |
| Decision Card facts (risk level, thresholds, CDS service, digest) | Definition list; digest with copy | Facts, not metrics. |
| Questions, criteria, derive table, policy | Tables and ordered lists, with a table of contents | Reference documentation. |
| Input and output schemas, raw record | Collapsible code blocks | For developers, secondary. |

### Chart library

**Recommendation: no library. Add a small internal `charts.js` (about 250 lines)** with four
SVG primitives:
- stacked columns;
- line;
- scatter;
- horizontal bars.

Reasons:
- The existing reliability diagram shows the stack already does this.
- The data is tiny (≤ 100 points per chart).
- The repo has a no-build-step rule.
- Inline SVG inherits CSS variables, so both themes work with no JavaScript theme plumbing.
- SVG text and `<title>`/`<desc>` elements are in the accessibility tree. Canvas libraries
  (uPlot, Chart.js) need a parallel accessible layer.

Every chart will get:
- `role="img"`, an `aria-label` summary (e.g. "Decisions per day, last 30 days, peak 150 on
  26 September");
- a keyboard-focusable data table fallback ("View data");
- tooltips on hover **and focus**, showing exact values.

If a later milestone needs interactive exploration beyond this (brushing, many series), the
fallback is **Observable Plot**, vendored as an ES module. It renders SVG, so the theming and
accessibility approach carries over. The cost is a d3 dependency of about 200 KB, not justified
for four simple chart types today.

---

## 7. Prioritized implementation roadmap

Each phase is a separate PR, with screenshots at 1440 and 390 px in both themes in the PR.
Business logic, APIs and computations are not touched in any phase.

**Phase 0: accessibility and correctness fixes (≈ 0.5 day)**
- **Files:** `style.css`, `app.js`, `index.html`.
- **Changes:**
  - dark-mode primary button contrast and badge contrast (token values only);
  - input borders at 3:1 or more;
  - audit rows keyboard-operable (a `<button>` in the first cell);
  - `.mono` no longer breaks ids;
  - scroll reset and focus to `h1` on view change;
  - `aria-live` narrowed to a status region;
  - label for the history select and the feedback note;
  - Decide and label buttons get loading/disabled states;
  - the sandbox notice kept on mobile;
  - `prefers-reduced-motion`;
  - the example picker matches the textarea.

**Phase 1: design tokens and theming (≈ 1–1.5 days)**
- **Files:** `style.css` (rewritten around tokens and components), `index.html` (no-flash
  script, `color-scheme` meta, theme toggle), `app.js` (31 inline styles become classes; the
  theme toggle; the icon sprite replaces emoji; humanised enum labels such as "Human review"),
  new `tests/test_dashboard_contrast.py`, `server/app.py` (add new static assets to
  `versioned_dashboard()` if files are added).
- **Acceptance:** the contrast test passes in both themes; the toggle persists; no theme
  flash on reload.

**Phase 2: tables and leaderboard (≈ 1.5 days)**
- **Files:** new `dashboard/table.js` (sortable, sticky, caption, keyboard), `app.js`
  (`leaderboardView`, `otherQuestions`, audit, per-label and subgroup tables), `style.css`,
  `server/app.py` (asset list), `index.html` (`type="module"` scripts).
- **Acceptance:** everything in section 5, including the deployed-row highlight and the
  mobile sticky column; the audit detail becomes a drawer with its own URL
  (`#audit/<id>`).

**Phase 3: charts (≈ 1.5 days)**
- **Files:** new `dashboard/charts.js`, `app.js` (overview daily chart, threshold sweep line,
  reliability scatter, probability bars, optional confusion matrix and cost/accuracy
  scatter), `style.css`.
- **Acceptance:** every chart is driven by endpoint data, has an `aria-label` summary, a data
  table fallback and focusable tooltips, and switches with the theme.

**Phase 4: screen redesigns (≈ 2 days)**
- **Files:** `app.js`, `style.css`, `index.html`.
- **Changes:**
  - Overview hierarchy;
  - Review queue priority banners and keyboard flow;
  - Evaluations sub-views, and the dataset/report coherence;
  - Decision Card layout (72ch prose, table of contents, definition list, copy digest);
  - Try it result header;
  - Feedback page without emoji.

**Phase 5: QA pass (≈ 0.5 day)**
- Keyboard-only walkthrough of every screen.
- Screen-reader spot check (VoiceOver).
- 1440/390 screenshots in both themes.
- Contrast test in CI.

**Total: about 7–8 working days, in 6 PRs.**

### Before and after: the five highest-impact screens

1. **Overview.**
   - **Before:** nine identical tiles, a table of days, raw `slot=variant` text.
   - **After:**
     - a header strip naming the pack, risk level and what is deployed in plain words
       ("Router: Jev · Safety screen: rules + Jev");
     - four primary stats (Automated, Waiting for review with a link, Override rate, Observed
       accuracy);
     - a single row of secondary stats (volume, latency, cost, audit chain, feedback);
     - a 30-day stacked column chart of decisions by outcome, with the exact table one click
       away.
2. **Evaluations and leaderboard.**
   - **Before:** a 6,500 px page with the provider comparison last, a picker that doesn't
     match the report, and an off-screen critical column.
   - **After:**
     - sub-views with the comparison first;
     - dataset tabs;
     - a ranked, sortable table led by per-label critical-detection columns;
     - the deployed row marked;
     - constant columns folded into the caption;
     - the benchmark report as its own tab, with a threshold curve and a reliability scatter.
3. **Review queue.**
   - **Before:** every card identical; a stat chest-pain case distinguished by an 11 px badge.
   - **After:**
     - items grouped by priority;
     - stat cards carry a solid danger banner ("Stat: possible emergency. Nurse triage now");
     - the recommended label is a primary button, the alternatives secondary;
     - resolving moves focus to the next card;
     - a count per priority in the header.
4. **Try it result.**
   - **Before:** a row of small badges, a plain "Fallback" paragraph, a green bar for a block
     verdict.
   - **After:**
     - a result header that states the outcome in words and colour ("Blocked · urgent", or
       "Sent to review", or "Automated");
     - the fallback as a danger callout directly under it;
     - criteria effects coloured by consequence;
     - probability bars coloured by meaning;
     - the result scrolls into view and receives focus.
5. **Audit trail.**
   - **Before:** mouse-only rows, a detail 4,000 px below, 159 px rows on mobile.
   - **After:**
     - keyboard-operable rows with compact relative times;
     - the detail as a side drawer (full screen on mobile) with a URL, events as a timeline
       and the record as collapsible JSON;
     - feedback controls in the drawer, not in every row.

### Aesthetic direction

A calm clinical instrument, the feel of a well-made lab report or monitoring console. Neutral
surfaces, one accent colour, and colour reserved for meaning (AUTO, review, block, stat), so
that red always means "stop" and is rare. Numbers are first-class: tabular, right-aligned,
with units and denominators.

It should **not** be:
- a generic SaaS admin template (grids of equal pastel tiles);
- playful (emoji, gradients, sparkle icons);
- "AI" branding.

The distinctiveness comes from precision and hierarchy, not decoration.

---

## 8. Risks and open questions

**Risks**
- **No build step, no frontend tests.** The redesign touches most of `app.js`; regressions in
  rendering paths (record vs message packs, derived vs plain primaries) are the main risk.
  Mitigation: split into ES modules along the phases, keep every phase small, and verify each
  PR in the browser across all five packs. Consider a small smoke test (see question 5).
- **Theme script and a future CSP.** The no-flash inline script will need a CSP hash when v0.6
  adds a Content Security Policy.
- **Chart honesty on sparse data.** Local and preview databases have 1–3 days of decisions. A
  30-day chart must render short histories without implying zeros for missing days before the
  first decision.
- **Scope creep.** Generated forms for record packs, confusion-matrix views and the
  cost/accuracy scatter are marked optional so the core plan stays about 7–8 days.

**Open questions**
1. **Primary audience.** Is this dashboard for clinicians reviewing decisions, or for teams
   evaluating packs? It affects density, wording (humanised labels versus pack identifiers)
   and which screen opens by default.
2. **Leaderboard ranking.** Is safety-first ordering (critical detection, then accuracy, then
   cost) right for every pack, or should each pack declare its ranking metric?
3. **Feedback icons.** Your standing rule is "simple thumbs up / thumbs down". The plan keeps
   exactly that, as thumb icons with visible labels instead of emoji. OK?
4. **Days in Eastern time.** Daily volume is grouped by UTC date on the server, so days
   change at 8 PM Eastern. Changing that is a computation change and out of scope here.
   Should it be planned separately?
5. **Testing.** Is a small Playwright smoke suite (a dev-only dependency, not shipped)
   acceptable for the redesign, or should verification stay manual plus the contrast unit
   test?
6. **"Run evaluation" in production.** It spends Jev and LLM calls. The plan adds a
   confirmation showing `n` and the remote providers it will call. Should it instead be
   disabled on production and allowed only locally?
