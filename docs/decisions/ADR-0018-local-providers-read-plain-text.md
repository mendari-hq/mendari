# ADR-0018: Local providers read text in its plain form

## Status
Proposed (2026-11-10). Built on the branch that proposes it; it changes what an agent decides on
some inputs, so merging it is the owner's decision.

Amended by [ADR-0026](ADR-0026-one-release-baseline-agents-only.md) (2026-11-25): `dosing`,
`naive_bayes` and `criteria` are removed. `rules` is the one provider it names that stays, and it
reads its text fields in their plain form (`providers/base.py`, `text_from`).

Terminology updated 2026-11-23 and 2026-11-25: what Mendari governs is called an agent here,
as everywhere since [ADR-0026](ADR-0026-one-release-baseline-agents-only.md).

## Context
An agent's patterns are written as a keyboard types them: `can'?t breathe`, `chest (pain|pressure)`.
The text they are matched against is written by phones, web forms, word processors and language
models, which write the same words in other characters. Measured on the agents shipped at the
time, on their own datasets with their default offline providers: every true critical case an
agent detected as shipped was retyped by a fixed rule (every apostrophe curly, a phone's default;
every third space no-break and every third doubled; every third space zero-width and every third
a line separator; a soft hyphen inside every fourth word), and matched as received. A curly
apostrophe cost a few detections; the spaces and invisible characters cost every agent many, up
to about two thirds of its critical cases.

Single cases say the same:

- **A curly apostrophe.** "It won't stop bleeding", typed on a phone: a safety screen's
  P(urgent) falls from 0.98 to 0.12 and the message loses its stat priority.
- **A soft hyphen**, as a word processor leaves in a pasted word. One inside "insulin" hides the
  medication from a criterion that excludes insulin.
- **An en dash in a range**, as a language model writes one. "Acetaminophen 500 mg, 2 tablets
  every 4 to 6 hours", written with the dash: a dose check finds no frequency,
  judges the single dose alone and answers at 0.98 that the limit is not exceeded. The
  instruction allows 6,000 mg a day against a limit of 4,000.
- **An ellipsis character**, a phone's three full stops. A negation runs across it: "No fever",
  the ellipsis, "I have chest pain now" is read as no chest pain.

In every case the provider answers with the confidence of a clean match. No case was automated
by this in the default configurations (those need a model beside the rules), but a possible
emergency lost its priority and an unsafe draft its block.

## Decision
The providers that decide on this machine read free text in its **plain form**
(`core/text.py`, spec §5): the same words in a keyboard's characters. `rules`, `dosing` and
`naive_bayes` read their text fields through it, and `criteria` a medication's name.

1. A zero-width space becomes a space, since it separates words. The other invisible format
   characters (the soft hyphen, zero-width joiners, the byte order mark, direction marks) are
   left out, since they sit inside words.
2. A letter and its accent written as two characters are composed (NFC), unless that would make
   the text longer.
3. Apostrophes and quotation marks become `'` and `"`, dashes and the minus sign `-`. The
   ellipsis character becomes a full stop, or a hyphen between two digits, where a full stop
   would read as a decimal point.
4. A carriage return before a newline is left out, and every run of white space that holds no
   newline becomes one space.

- **Typography only.** No case folding, no compatibility folding (a ligature, a full-width
  letter and the micro sign stay), no spelling. A newline stays a newline: agents treat one as the
  end of a sentence. Every other character that ends a line somewhere (a lone carriage return, a
  vertical tab, a form feed, the line and paragraph separators) is white space, as a pattern has
  always read it.
- **Never longer than the text**, so the 10,000 characters an agent on the community engine's
  patterns run on holds for the plain form too.
- **Only what a local provider matches.** The decision's record, its input hash, the input
  schema's checks and what a remote model is sent keep the text as received.
- **In the engine, for every agent and every spec version.** No agent file changes.

## Consequences
- Text that differs only in typography decides the same. Every true critical case of every
  bundled agent's datasets is retyped six ways in the tests and must decide as shipped.
- Nothing that ships changes: no text in any bundled agent's datasets differs from its plain
  form, so the data, the trained models, the benchmarks and the digests are byte-identical, and
  every decision on them is the same.
- A decision on an input that holds such characters can change. Nearly always a pattern matches
  where typography hid it. Three things a pattern matched in the text as received and does not
  match in its plain form, each as the same words typed on a keyboard already decide:
  - words joined by a format character other than the zero-width space, with nothing else
    between them ("asthma", a word joiner, "attack"): they join, as they are shown, and a
    pattern that read the invisible character as a gap no longer matches;
  - a negation cue that a long run of spaces had kept outside a rule's `negation_window`
    ("Smoker: no", 45 spaces, "chest pain: yes"): the run is one space, so the cue is as near
    as it reads and the match is negated;
  - a pattern or a negation that reached across an ellipsis character: it ends there, as at
    three full stops.
- An agent's patterns, names and tokens must be written in plain form: a character the plain form
  never holds matches nothing. The bundled agents' are (a test), and the spec lists the
  characters.
- Accents are composed for a whole field or not at all: a field that also holds one of the few
  characters that compose to more keeps its accents as they were written.

## Alternatives Considered
- **Each agent writes its patterns for typography** (a class of apostrophes in every
  contraction, `\s+` for every space). Every author must know every variant, and the agents
  shipped at the time did not: 12 of their 102 rule patterns used `\s` at all.
- **Unicode NFKC.** It also folds ligatures and full-width letters, but it rewrites the micro
  sign as the Greek letter mu and a fraction or a superscript as digits, which changes what a
  dose or a unit says, and it can make a text longer (one character can become eighteen), past
  the length the community engine's patterns are bounded for. Left out until a need is measured.
- **Every line-ending character a newline.** A vertical tab, a form feed and the line and
  paragraph separators end a line somewhere, but a pattern has always matched across them, and a
  newline ends a pattern's reach: "coughing up", a vertical tab, "blood" would stop being found.
- **Every format character left out**, the zero-width space too. It joins the words it
  separates: "asthma", a zero-width space, "attack" would stop being found.
- **Normalising the input itself**, before the schema and the record. The record would no longer
  hold what was received, and the input hash would change.
- **An agent opts in.** The bundled agents would stay as they are until each is changed and
  rebuilt, and a community agent would be exposed by default.
- **Joining wrapped lines.** Text wrapped mid-phrase also hides it, but a newline is an agent's
  sentence boundary and nothing tells a wrap from a paragraph. Not done; left to measure.
