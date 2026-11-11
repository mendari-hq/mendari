# ADR-0018: Local providers read text in its plain form

## Status
Proposed (2026-11-10). Built on the branch that proposes it; it changes what a pack decides on
some inputs, so merging it is the owner's decision.

## Context
A pack's patterns are written as a keyboard types them: `can'?t breathe`, `chest (pain|pressure)`.
The text they are matched against is written by phones, web forms, word processors and language
models, which write the same words in other characters. Measured on the five reference packs'
own datasets, with their default offline providers:

- **A curly apostrophe**, a phone's default. "It won't stop bleeding", typed on a phone: the
  inbox screen's P(urgent) falls from 0.98 to 0.12 and the message loses its stat priority. So
  do 3 of the 55 true emergencies the keyword rules flag.
- **A no-break space**, as a web form writes one. With 15% of spaces no-break, the referral
  screen stops detecting 22 of its 136 urgent referrals, the prior-authorization red-flag screen
  4 of 35, and the AI Guard 39 of the 110 unsafe drafts it finds.
- **A zero-width space**, carried in pasted text. One inside "insulin" hides the medication from
  the trial prescreener's insulin exclusion.
- **An en dash in a range**, as a language model writes one. "Acetaminophen 500 mg, 2 tablets
  every 4 to 6 hours", written with the dash: the AI Guard's dose check finds no frequency,
  judges the single dose alone and answers at 0.98 that the limit is not exceeded. The
  instruction allows 6,000 mg a day against a limit of 4,000.
- **An ellipsis character**, a phone's three full stops. A negation runs across it: "No fever",
  the ellipsis, "I have chest pain now" is read as no chest pain.

In every case the provider answers with the confidence of a clean match. No case was automated
by this in the default configurations (those need a model beside the rules), but a possible
emergency lost its priority and an unsafe draft its block.

## Decision
The providers that decide on this machine read free text in its **plain form**
(`core/text.py`, spec §5): invisible format characters left out, a letter and its accent
composed, apostrophes, quotation marks and dashes as a keyboard types them, the ellipsis
character a full stop, every line end a newline and every other run of white space one space.
`rules`, `dosing` and `naive_bayes` read their text fields through it, and `criteria` a
medication's name.

- **Typography only.** No case folding, no compatibility folding (a ligature, a full-width
  letter and the micro sign stay), no spelling. A newline stays a newline: packs treat one as the
  end of a sentence.
- **Never longer than the text**, so the 10,000 characters a pack on the community engine's
  patterns run on holds for the plain form too.
- **Only what a local provider matches.** The decision's record, its input hash, the input
  schema's checks and what a remote model is sent keep the text as received.
- **In the engine, for every pack and every spec version.** No pack file changes.

## Consequences
- Text that differs only in typography decides the same. Every true critical case of every
  reference pack's datasets is retyped four ways in the tests and must decide as shipped.
- Nothing that ships changes: no text in any reference pack's datasets differs from its plain
  form (7,390 cases), so the data, the trained models, the benchmarks and the digests are
  byte-identical, and every decision on them is the same.
- A decision on an input that holds such characters can change, in both directions. A pattern
  matches where typography hid it (more possible emergencies flagged). A negation cue matches
  where typography hid it too, and the ellipsis now ends a negation and a pattern's reach as
  three full stops do.
- A pack's patterns, names and tokens must be written in plain form: a character the plain form
  never holds matches nothing. The reference packs' are (a test), and the spec lists the
  characters.

## Alternatives Considered
- **Each pack writes its patterns for typography** (a class of apostrophes in every
  contraction, `\s+` for every space). Every author must know every variant, and the five
  reference packs did not: 12 of their 102 rule patterns use `\s` at all.
- **Unicode NFKC.** It also folds ligatures and full-width letters, but it rewrites the micro
  sign as the Greek letter mu and a fraction or a superscript as digits, which changes what a
  dose or a unit says, and it can make a text longer (one character can become eighteen), past
  the length the community engine's patterns are bounded for. Left out until a need is measured.
- **Normalising the input itself**, before the schema and the record. The record would no longer
  hold what was received, and the input hash would change.
- **A pack opts in.** The reference packs would stay as they are until each is changed and
  rebuilt, and a community pack would be exposed by default.
- **Joining wrapped lines.** Text wrapped at 40 columns also hides phrases (16 of the referral
  screen's 136), but a newline is a pack's sentence boundary and nothing tells a wrap from a
  paragraph. Not done; left to measure.
