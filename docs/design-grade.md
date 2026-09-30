# OpenHealth Hub dashboard: design grade

_Reviewed 2026-09-29 against `main` at `acc2e56` (after the redesign, Phases 0–5, and the Try-it
forms). Reviewer's note: I built most of this frontend, so this grade is written as an outside
reviewer would write it, against Apple, Airbnb and best-in-class data-visualization work, not
against typical internal tools._

## How it was reviewed

- **Rendered UI, 110 captures:** 11 screens (Overview, Review queue, Audit trail, the audit
  drawer, Provider comparison, Benchmark report, Run on a dataset, Feedback, Decision Card, Try it,
  a Try-it result) × 2 packs (Patient Inbox Router, a message pack; AI Guard, a record pack) at
  1440 px and 390 px in light and dark, plus 768 px and 1920 px in light for the inbox. Chromium,
  seeded synthetic data.
- **Measurements on every capture:** cumulative layout shift during load, distinct font sizes,
  interactive targets under 24 px (WCAG 2.5.8) and under 44 px (Apple HIG), content width against
  the viewport, height of the fixed chrome.
- **Code:** `src/openhealth/dashboard/` (`app.js` 1,835 lines, `charts.js` 380, `form.js` 242,
  `table.js` 100, `style.css` 686, `index.html` 88): tokens against raw values, component reuse,
  state handling, motion inventory.
- **Earlier evidence reused:** the Phase 5 keyboard walkthrough and accessibility-tree audit, and
  the CI contrast test.

**Not assessed:** a real screen reader (VoiceOver, NVDA); Safari and Firefox rendering; real touch
hardware (viewport emulation only); production data volumes (local history is one day, and the
30-day chart was only seen with injected history in tests); low-end devices; RTL or other
languages.

## Overall grade: C+

A careful, accessible, well-tested internal tool that is still visibly an internal tool. Its
foundations are close to the bar: a real token system with light and dark themes checked for AA
contrast in CI, keyboard access and focus management beyond most production dashboards, correct
chart choices, and a leaderboard table with Stripe-level engineering. What it does not have is
the craft that defines Apple and Airbnb. Titles are 20 px on a 14 px body, and every section is
the same bordered white card. There is one transition in the entire UI. Loading blanks the view
to a word. The layout breaks between phone and desktop: a 0.50 layout shift at 768 px, and 572 px
of empty screen at 1920 px. Controls are sized for a mouse, not a finger. The charts are honest
in type but not in finish: no direct labels, an axis that starts at 90%, one configuration hidden
exactly behind another, KPIs with no trend. It is about two structural phases and one polish phase from a B+. An A would also
need a visual identity and research with clinicians, which no amount of CSS supplies.

## Scorecard

| Category | Grade | Reason |
|---|---|---|
| 1. Visual design | C+ | Clean and calm, but generic: 20 px titles, 7 bordered cards on the Decision Card, 46-word intros, a Unicode ◆ as the logo. |
| 2. Consistency and design system | B− | Colour and type are tokens (checked in CI), but about 120 raw pixel values and 8 ad-hoc z-indexes sit outside them, and there are three toggle-group styles. |
| 3. Data visualization | B− | Right chart for each dataset, accessible and themed; loses on finish: no direct labels, a 90%-based axis, overlapping points, single-day charts, KPIs without trend. |
| 4. Tables and leaderboards | B | Sticky header and first column, `aria-sort`, tabular right-aligned numbers, safety-first ranking; 20 px sort targets, redundant configuration text, a clipped header at 768 px. |
| 5. Interaction and UX logic | B− | Clear navigation, error and empty states, a cost confirmation; but an override is one irreversible tap, loading blanks the view, and links don't carry the pack. |
| 6. Responsiveness | C+ | 390 px holds (no sideways scroll); 768 px shifts 0.50 on load and keeps a 200 px sidebar; 1920 px wastes 30% of the screen; a 104 px sticky bar on phones. |
| 7. Light/dark theming | A− | Complete token themes, AA pairs tested in CI, no flash, follows the system live; near the bar. |
| 8. Accessibility | B+ | Skip link, focus trap, live regions, heading order and reduced motion all in place; targets below 24 px on dense screens and no real screen-reader pass. |
| 9. Motion and micro-interactions | D | One transition (the toast). Drawer, dialog, tabs, row removal and hover states all cut instantly; no pressed states. |
| 10. Frontend code quality | C+ | No build step by design, and 71 browser tests; but a 1,835-line `app.js`, 32 whole-subtree `innerHTML` renders, globals shared across scripts by load order. |

## Top 10 gaps against the benchmark, by impact

### 1. The layout only works at phone and laptop widths

- **What's wrong:**
  - **768 px:** the pack picker fills with the long pack names after first paint, the top bar
    wraps to a second row, and the whole shell drops 28 px. Cumulative layout shift is **0.50** on
    every screen; above 0.25 counts as poor.
  - **Tablet:** the fixed 200 px sidebar stays until 760 px, so at 768 px the content gets 568 px
    and the leaderboard shows three columns.
  - **1920 px:** `main` is capped at 1,180 px and left-aligned, so content ends at 1,348 px and
    **572 px** on the right stay empty.
- **Benchmark:** Stripe and Linear keep layout stable while data arrives, with CLS under 0.1.
  Their content width grows up to about 1,440–1,600 px and centres. Below about 1,024 px the
  sidebar collapses.
- **Change:**
  - In `style.css`, `.pack-picker select` gets `width: 15rem` from first paint (0.50 → about
    0.002, as at other widths).
  - `main`: `max-width: 1180px` → `max-width: 1440px; margin-inline: auto`.
  - The phone layout's breakpoint (the section list across the top): `760px` → `1024px`.

### 2. Hierarchy and identity read as a generic admin template

- **What's wrong:**
  - **Weak hierarchy:** every view title is 20 px / 600 on a 14 px body, and the sizes step
    20 / 16 / 14.
  - **Too many cards:** every section is the same bordered, shadowed white card: 7 on the
    Decision Card, 6 on the Review queue.
  - **Long intros:** the paragraph under each title runs 46 words (Feedback), 27 (Decision Card)
    and 23 (Evaluations).
  - **No mark:** the logo is the Unicode character ◆.
- **Benchmark:** Apple and Linear use one confident title size (28–34 px), group by whitespace
  rather than boxes, keep boxes for content that is an object (a table, a chart), and use one
  sentence of supporting copy.
- **Change:**
  - **Title:** a `--text-title` token (28 px / 34 px, weight 600) for the view `h1` in
    `style.css`, currently `--text-xl` 20 px.
  - **Panels:** keep the border and shadow only on tables, charts and cards that are objects (a
    review item). Sections of a view, such as the card's "Intended use" and "Policy", become
    unboxed with 32 px (`--space-7`) between them.
  - **Intros** (`app.js`, each view): at most one line at 1440 px, about 16 words. The
    Feedback intro drops from 46 words to "Rate decisions right or wrong. A wrong rating with the
    right answer becomes a test case."
  - **Logo:** the ◆ in `index.html` becomes the favicon's SVG mark, inline, at 20 px.

### 3. A human override is one irreversible tap

- **What's wrong:** in the Review queue, any label other than the recommendation records a human
  override the moment it's tapped. `resolveCard` in `app.js` POSTs at once, and the audit chain is
  append-only, so a mis-tap on a phone becomes a permanent override in a clinical record.
- **Benchmark:** Apple and Airbnb either confirm irreversible actions or give a short undo window
  (Gmail's "Undo send").
- **Change:** confirming the recommendation stays one tap. Choosing another label shows a 5-second
  toast, "Override to billing · Undo", and sends only when it ends. This is client side only, and
  the API is unchanged. File: `app.js` (`resolveCard`, `toast`).

### 4. Loading blanks the page

- **What's wrong:** every navigation replaces the view with a single "Loading…" line
  (`render()` in `app.js`), so the page collapses and then jumps back. A dataset evaluation, which
  may run for minutes with remote models, shows only "Running…" on its button.
- **Benchmark:** Stripe and Linear keep the old layout or show skeletons shaped like the content,
  and long jobs show that they are alive.
- **Change:**
  - **Skeletons:** `render()` draws a per-view skeleton: four stat blocks for Overview, eight
    table rows for Audit, three cards for Review. Uses `--color-surface-sunken`, with a shimmer
    that is off under reduced motion.
  - **Long runs:** the Run tab shows an indeterminate bar with elapsed seconds and "Running 400
    records". No fake percentage, since the API is synchronous.
  - **Files:** `app.js` (`render`, `showRunTab`), `style.css` (`.skeleton`).

### 5. Controls are sized for a mouse, and phones lose 104 px to chrome

- **What's wrong:**
  - **Under 44 pt:** a median of 25 of about 27 interactive targets per screen, at 390 px:
    - buttons: 36 px;
    - choice pills: 30 px;
    - theme buttons: 32 × 26 px;
    - icon buttons: 30 × 30 px.
  - **Under WCAG's 24 px:** sort header buttons and disclosure toggles are 20 px tall. The
    benchmark report has 34 targets under 24 px, the audit trail 16.
  - **The sticky top bar on phones is 104 px** (brand, theme toggle and pack picker), 12% of an
    844 px screen, with a further 52 px section list under it.
- **Benchmark:** Apple HIG targets are at least 44 × 44 pt, and a compact navigation bar is about
  44–56 px.
- **Change:**
  - **Touch sizes:** `@media (pointer: coarse)` gives `button, a.button, select, input,
    summary` `min-height: 44px`, and `th button.sort` `padding-block: 12px`.
  - **Mouse sizes:** on fine pointers, sort buttons go 20 → 28 px and summaries 20 → 28 px.
  - **Phone top bar:** 56 px with the brand and one "Settings" icon button that opens a sheet
    holding the theme and the pack. The pack picker also moves into the section list row, which
    is not sticky.
  - **Files:** `style.css`, `index.html`, `app.js` (the sheet).

### 6. Charts are the right type but not Datawrapper-grade

- **What's wrong:**
  - **Truncated axis:** the threshold sweep's y-axis starts at 90% (`makeAxis("rate")` in
    `charts.js` starts at the lowest tenth), so a 3.3-point drop in coverage reads as a cliff.
    Nothing says the axis is truncated.
  - **Overlapping, unlabelled points:** on AI Guard's cost-against-accuracy scatter (held-out
    set), "Jev" and "Rules + Jev" plot at exactly the same point, so one hides the other
    entirely. "Claude Haiku 4.5" and "Rules + Claude Haiku 4.5" are 11 px apart. None is labelled
    without hovering.
  - **Legends instead of labels:** the outcome mix and the daily chart use a separate legend
    rather than direct labels.
  - **Sparse history:** one day of history renders as a single 36 px column in an 1,100 px panel.
- **Benchmark:** Datawrapper labels series and points directly, marks a truncated axis with a
  break and a note, and avoids overlapping marks by labelling or dodging them. Observable Plot's
  `dodge` is the standard way.
- **Change** (`charts.js`, and `app.js` for `sweepChart`, `costChart`, `dailyChart`, `stackBar`):
  - **Rate axes:** start at 0. When the data spans under 15 points, keep the zoom but draw an
    axis break (two 6 px diagonal strokes at the bottom of the y-axis) and add the note "Axis
    starts at 90%".
  - **Scatter:** points within 12 px of each other are offset vertically in 6 px steps with a
    1 px leader; identical points get one mark labelled with both names. The deployed configuration and the ones no other beats on both cost and accuracy
    get their name as a direct label.
  - **Outcome mix:** segments at least 12% wide carry their label and share inside the bar; the
    legend only lists the rest.
  - **Daily chart:** under 7 days of history, replace the chart with a one-line summary per day
    ("Sep 29: 11 decisions · 9 review · 1 automated · 1 abstained").

### 7. KPIs have no trend or period

- **What's wrong:** Overview stats are single numbers with no time window or comparison. "Automated
  9.1%" doesn't say since when, or whether that is better or worse than last week.
- **Benchmark:** Stripe's KPI cards pair each value with its period, the change against the
  previous period and a sparkline.
- **Change:**
  - **Period selector** "7 days · 30 days · All" above the stats.
  - **Decisions and Automated** get a change against the previous period and a 30-day sparkline
    (a new small primitive in `charts.js`), computed client-side from `stats.daily`, which already
    has outcome counts per day.
  - **Human override rate and Observed accuracy** need per-day review counts, which the API
    doesn't return. That would be a small read-only addition to `/api/stats` and needs your
    approval; until then these two show their period only.
  - **Files:** `app.js` (`primaryStats`), `charts.js`.

### 8. Motion is missing

- **What's wrong:** `style.css` has one transition, the toast. Everything else cuts instantly:
  - the audit drawer and the evaluation dialog appearing;
  - switching Evaluations tabs;
  - a review card disappearing after it's resolved;
  - Right/Wrong toggling;
  - button hover (border colour only).

  There are no pressed states.
- **Benchmark:** Apple uses motion to show where things come from and go: sheets slide, removed
  rows collapse, and controls respond on press.
- **Change** (all under the existing reduced-motion override; files `style.css`, and `app.js`
  (`showDrawer`/`closeDrawer`, `resolveCard`) so removal waits for `transitionend`):
  - **Drawer:** slides in with `translateX(100%)` → `0` over 220 ms `--ease-out`, backdrop fading
    over 160 ms. Closing reverses it.
  - **Dialog:** opacity 0 → 1 and scale 0.98 → 1 over 160 ms.
  - **Review card resolved:** height and opacity collapse over 200 ms, then focus moves on.
  - **Buttons, links and chart marks:** 120 ms transitions on background, border and colour.
  - **Pressed state:** `button:active { transform: translateY(1px) }`.

### 9. Raw identifiers in the primary reading path

- **What's wrong:** decision 1 put plain words first and codes one step away, but several primary
  labels are still codes:
  - benchmark stat labels "destination accuracy" and "urgency = urgent caught";
  - leaderboard configurations "safety_screen: Rules + Jev" followed by a "Jev 1.13.0" chip, which
    names Jev twice;
  - the drawer title "Decision d6a4f5ea";
  - column headers such as `question_emergency = yes caught`.
- **Benchmark:** Airbnb's content design puts people's words first and moves identifiers into
  secondary text.
- **Change** (`app.js`: `reportView`, `configCell`/`configName`, `openRecord`, the leaderboard's
  critical columns):
  - **Question names** are humanised ("Destination accuracy", "Urgent: caught").
  - **Slot names** use `slotText` ("Safety screen").
  - **The variant word is dropped** when its model chip names the same provider ("Jev 1.13.0"
    alone).
  - **The drawer title** reads "Human review · nurse", with the id as secondary monospace text.
  - Codes stay in tooltips and in the JSON.

### 10. The design system is not enforced, and `app.js` is a monolith

- **What's wrong:**
  - **Tokens not enforced:** about 120 raw pixel lengths sit outside the token block (for
    example `6px` 18 times, `2px` 13 times, `10px` 8 times, `7px` 3 times), and there are 8
    z-index values from 1 to 40 with no scale.
  - **Three toggle-group styles:** `.segmented` buttons, `.segmented.tabs` links and `.choices`
    pills.
  - **One monolith:** `app.js` holds every view: 112 functions, and 32 `innerHTML` assignments
    that re-render whole subtrees.
  - **Hidden coupling:** `table.js`, `charts.js` and `form.js` depend on `esc` and `icon` from
    `app.js` through global scope, in script order.
- **Benchmark:** Airbnb's DLS defines each pattern once and puts spacing, elevation and layering
  on scales.
- **Change:**
  - **Spacing:** a `--space-1-5` (6 px) token, and every raw spacing value mapped to the scale.
  - **Layering:** `--z-sticky`, `--z-drawer`, `--z-dialog`, `--z-toast`.
  - **Toggle groups:** one pattern, pressed-button or current-link, with two sizes.
  - **Modules:** `app.js` becomes ES modules per view (`overview.js`, `review.js`, `audit.js`,
    `evals.js`, `feedback.js`, `card.js`, `try.js`) plus `ui.js` (`esc`, `icon`, `stat`,
    badges), loaded through an import map in `index.html`. That keeps the no-build rule.
    `versioned_dashboard()` in `server/app.py` would stamp the import map's entries, so cached
    modules can't go stale.
  - **Files:** `style.css`, `dashboard/*.js`, `index.html`, `server/app.py`.

## What I would change first

**No new dependencies are needed.** Every change below fits the current no-build stack
(hand-written SVG charts, plain CSS and JavaScript). Business logic, data and computations are
untouched; the one exception, per-day review counts for gap 7, is flagged for approval.

### Phase A: quick wins (under a day)
1. **Tablet layout shift:** reserve the pack picker's width, `.pack-picker select { width: 15rem }`,
   taking CLS at 768 px from 0.50 to about 0.002 (gap 1).
2. **Wide screens:** `main { max-width: 1440px; margin-inline: auto }` (gap 1).
3. **Titles:** view titles at 28/34 px through a `--text-title` token (gap 2).
4. **Intros:** trimmed to one line: Feedback 46 → about 16 words, Decision Card 27, Evaluations
   23 (gap 2).
5. **Sweep axis:** starts at 0, or zooms with a visible axis break and "Axis starts at 90%"
   (gap 6).
6. **Plain labels:** for report stats, leaderboard configurations and the drawer title (gap 9).
7. **Touch targets:** `@media (pointer: coarse)` minimums of 44 px; sort buttons and summaries at
   28 px for mouse (gap 5).
8. **Hover and press:** 120 ms hover transitions and a 1 px pressed state on buttons (gap 8).
9. **Small code text:** `code` inside 12 px text rendered at 11 px; set it to 12 px (`code {
   font-size: max(0.92em, 12px) }`).

### Phase B: structural (about 4–5 days)
1. **Breakpoint and phone chrome:** the 1024 px breakpoint, and the 56 px phone top bar with a
   settings sheet (gaps 1, 5).
2. **Loading:** skeletons instead of blanking, and progress for long evaluation runs (gap 4).
3. **Override undo:** a 5-second undo window before an override is sent (gap 3).
4. **Chart rework:** direct labels, dodged points, axis rules, the sparse-history summary, and
   labels inside the outcome mix (gap 6).
5. **KPI context:** a period selector, changes and sparklines for Decisions and Automated from
   `stats.daily`. The other two need the read-only API addition (gap 7).
6. **Design-system enforcement:** spacing and z-index scales, one toggle-group component, and
   `app.js` split into view modules with version-stamped imports (gap 10).

### Phase C: polish (about 2–3 days)
1. **Motion:** the drawer slide, dialog fade and scale, collapse of resolved review cards,
   crossfade between Evaluations tabs, transitions on chart marks (gap 8).
2. **Fewer boxes:** sections grouped by whitespace, with cards kept only for objects (gap 2).
3. **Brand mark:** the SVG in the top bar (gap 2).
4. **Stat labels:** 12 px, weight 500, uppercase with 0.04 em tracking.
5. **Empty states:** designed, with a short line and one action (Overview, Review, Feedback,
   History).

## What is already good

Only what is at or near the bar:
- **Theming:** semantic colour tokens for both themes, every text and UI pair checked for WCAG AA
  in CI (`tests/test_dashboard_contrast.py`), set before first paint with no flash, and following
  the system setting live. This is on par with the best dashboards.
- **Keyboard and focus:**
  - a skip link;
  - the audit drawer is a real dialog: page behind inert, focus held inside, Escape returns to
    the row;
  - after a review is resolved, focus moves to the next card;
  - roving focus through chart points, with tooltips on focus;
  - errors announced as alerts;
  - a heading order that is audited.

  This goes further than Stripe's and most dashboards'.
- **The leaderboard:** safety-first ranking, a sticky header and sticky first columns with edge
  shadows, right-aligned tabular numbers with units in headers, the deployed row marked, and
  columns that are the same in every row folded into one note. The engineering matches Stripe and
  Linear.
- **Chart foundations:**
  - the right chart for each dataset: a reliability diagram against the diagonal with area as
    sample size, a threshold sweep with the operating point marked, and a log-cost scatter with a
    "$0" slot;
  - drawn at real size, so text stays 12 px on a phone;
  - a data table behind every chart;
  - summaries in `aria-label`.
- **Honest statements:** every sentence that claims behaviour is derived from the pack's caps and
  the deployed providers ("Not here: at risk level 2…"), and checked against all five packs.
- **Guard rails:** 71 browser tests plus the contrast test run in required CI jobs, so regressions
  in the rendered UI are caught.

## Projected grade after each phase

| Category | Now | After A | After B | After C |
|---|---|---|---|---|
| Visual design | C+ | B− | B | B+ |
| Consistency and design system | B− | B− | B+ | B+ |
| Data visualization | B− | B | A− | A− |
| Tables and leaderboards | B | B+ | B+ | A− |
| Interaction and UX logic | B− | B− | B+ | B+ |
| Responsiveness | C+ | B | A− | A− |
| Light/dark theming | A− | A− | A− | A |
| Accessibility | B+ | A− | A− | A− |
| Motion and micro-interactions | D | C | C | B+ |
| Frontend code quality | C+ | C+ | B+ | B+ |
| **Overall** | **C+** | **B−** | **B** | **B+** |

**What an A would still need:** a visual identity beyond a token palette (a considered type pairing,
a brand mark, and illustration for empty states), usability testing with clinicians and evaluators
on real task flows, density tuned on production-scale data, and a real screen-reader and
cross-browser pass. None of that is reachable by these three phases alone.
