# Overnight log: design roadmap to completion

_Started 2026-09-29, late evening ET. Scope: every item in `docs/design-grade.md` (pre-approved).
Rules: one focused PR per item, merged only on green CI after a self-review, squash merge._

## Starting point

Phases A, B (B1–B3) and C and the re-grade were already merged by an earlier session
(#57–#61, CI green on `main`, re-graded C+ → B+). What remained in `docs/design-grade.md`:
the production-deploy check and the four "small remaining gaps" under "What an A still needs".

## Pull requests

| # | Title | Closes | CI |
|---|---|---|---|
| — | Production deploy check (no PR) | todo: "Check the production deploy" | — |
| [#62](https://github.com/Junebugg1214/openhealth-hub/pull/62) | Plain words in criteria tables and Try-it answer headings | Small gap: "plain words in the Try-it criteria table"; category 4 | Red once (a test line over 100 columns, edited after lint ran), then green; preview checked; merged |
| [#63](https://github.com/Junebugg1214/openhealth-hub/pull/63) | Links carry the pack (?pack= in the address) | Small gap: "links that carry the pack"; category 5 | Green first time; preview checked (including `/?pack=`); merged |
| pending | A view fades in where its skeleton was | Small gap: "a view transition from skeleton to content"; category 9 | pending |

## Decisions made on your behalf

- **Your later messages widened the run** (2026-09-29, before you went to bed): the whole project
  roadmap, v0.4 to v1.0, is approved, and so is new infrastructure where I recommend it. Every
  milestone still starts with its plan committed to `docs/plans/`, as the roadmap requires.

- **Production check without a PR.** `vercel curl` (authenticated CLI) returned 200 for `/`,
  `/api/health` and `/static/main.js` on the production alias; health reports Postgres, 5 packs,
  `America/New_York`. Unauthenticated requests still get 302 (lesson 2026-09-28). The rendered
  dashboard in your Chrome showed the Phase C UI.
- **Plain words come from each question's `instructions`**, first sentence: the pack already writes
  them for people, so no new pack field or digest change is needed. The code stays as secondary
  monospace text and the full instructions stay in the tooltip.
- **Screenshots are described in the PRs, not attached.** `gh` cannot upload images to a PR, and
  committing screenshots would add unrelated binaries to the repo. Every change was checked in
  the running app at 1440 and 390 px, light and dark.
- **The pack goes in the query, not the hash** (`?pack=openhealth/ai-guard#review`). Every
  in-app link is a bare `#view` link and keeps the query, so no link had to change. A pack change
  replaces the history entry rather than adding one, as before. Back across an earlier pack
  change shows that pack. An unknown pack falls back to the last one chosen, and the address is
  corrected.
- **Skeleton to content is a 200 ms fade-in of the view, not a crossfade of two layers.** It
  matches the Evaluations tab crossfade from Phase C, needs no second copy of the view in the
  page, and uses opacity only: a transform on `main` would move sticky and fixed children while
  it runs.
- **The engine's reason lines stay as they are** ("not_eligible: hba1c_in_range is no"). They
  come from the runtime, and changing them is business logic, out of scope.

## Skipped or blocked

- **Items in "What an A still needs" that code can't deliver:** a visual identity (type pairing,
  a designed mark), usability sessions with clinicians and evaluators, production-scale data,
  and a VoiceOver/NVDA, Safari/Firefox and real-device pass. These need people, devices or a
  designer.

## Final state

_Filled in at the end of the run._
