# Contributing a community pack

A community pack is a Decision Pack you contribute by pull request, for anyone running Mendari to
use. It lives in `community-packs/<name>/`, its id is `community/<name>`, and it is yours: you
are its maintainer. This guide is the whole path, from the idea to the merge and the updates
after it. How to write a pack in the first place is in
[Writing a Decision Pack](authoring-a-pack.md); read that first.

**What a community pack can do.** A deployment serves community packs only when it sets
`MENDARI_COMMUNITY_PACKS=on` (off by default). There, every surface that shows the pack (the
API, the dashboard, Try it, the review queue, the SMART patient view, its Decision Card, CDS
Hooks, its FHIR CodeSystems, the `/docs` page) labels it
**"Community · not clinically reviewed"**, and the runtime holds it to these limits, whatever
its manifest says:

- it **recommends and never automates**: a person confirms every decision
  ([ADR-0002](../decisions/ADR-0002-runtime-enforced-risk-caps.md): policy only lowers
  automation), so no policy rule of its own has `outcome: BLOCK` (a block is final at once):
  send such cases to a person with `HUMAN_REVIEW` and a priority;
- it decides with **local providers** (rules, criteria, dosing, naive Bayes, or a composite of
  those); a remote model runs only if a deployment names the pack in its settings
  (`community/<name>:slot=variant`), for decisions and evaluations alike;
- it keeps only a **hash** of each input (`audit.store_input: hash`);
- it reads texts of at most **10,000 characters** (a longer one is refused before any pattern
  runs);
- its FHIR systems (a `task.performer_system` in `fhir/mapping.yaml`) live under its own url,
  `https://mendari.co/packs/community/<name>/`, e.g.
  `https://mendari.co/packs/community/<name>/CodeSystem/performer`;
- it is at most **risk level 1**.

Its name, CDS Hooks title and publisher are yours: they may not name Mendari or copy the name
of a pack Mendari maintains.

Why these limits, and what was decided, is in
[ADR-0007](../decisions/ADR-0007-community-packs-by-pull-request.md). The label comes from
where the pack lives, never from `publisher.type`, which every pack declares for itself.

## The path

1. **Propose it (recommended for a new pack).** Open a "Propose a pack" issue: the use case, who
    would use it, the risk level you intend and how you will make the data. The owner answers
    yes, no or "changes needed" before you build anything.

2. **Fork and set up.** Fork the repository on GitHub, clone your fork, and run `uv sync` in it.
    Mendari is not on PyPI yet, so every command below starts with `uv run`.

3. **Create it.**

    ```bash
    uv run mendari new community-packs/<name> --community --publisher "<you>"
    ```

    The folder name is lowercase letters, digits and dashes, and it is the pack's name. The
    scaffold already has the community defaults: `license: Apache-2.0`, `store_input: hash`,
    local providers, a `CHANGELOG.md`, a benchmark, its Decision Card and a critical label
    with tests. It passes the checks as written.

4. **Write it.** The criteria, the rules, the synthetic data
    ([below](#synthetic-data-without-python)) and the behaviour tests in `tests/cases.yaml`: at
    least one test case for each critical label (`evaluation.critical`), and at least five
    benchmark cases for each. Then regenerate the benchmark and the card, after every change:

    ```bash
    uv run mendari eval community-packs/<name> --dataset examples --baseline none \
        --out community-packs/<name>/evals/benchmark.json
    uv run mendari card community-packs/<name> --write
    ```

5. **List yourself as its maintainer** in
    [`community-packs/MAINTAINERS.yaml`](https://github.com/Junebugg1214/mendari/blob/main/community-packs/MAINTAINERS.yaml),
    with your GitHub user name: `{id: community/<name>, maintainers: [<you>]}`.

6. **Check it locally**, with the checks CI runs:

    ```bash
    uv run mendari community-check community-packs/<name> --base origin/main
    ```

    Each problem says what to fix. Run it until it says "passed: ready for review".

7. **Open the pull request.** Sign off every commit (`git commit -s`, the
    [Developer Certificate of Origin](https://github.com/Junebugg1214/mendari/blob/main/CONTRIBUTING.md#sign-off-the-developer-certificate-of-origin)).
    Change only `community-packs/<name>/` and your entry in `MAINTAINERS.yaml`. Use the pack
    template: add `?template=pack.md` to the address of the "Open a pull request" page, or run
    `gh pr create --template pack.md`. It asks for the risk level and why, who reviewed the
    clinical content, how the data was made, the providers, and the CHANGELOG entry.

8. **CI runs.** The `pack-check` job runs `community-check` on your pack, and fails if the pull
    request changes anything outside your pack's folder and its `MAINTAINERS.yaml` entry (or
    adds a git submodule: the pack's files are in the repository, where they are read). Its
    summary shows the metrics and the rendered Decision Card. GitHub may ask the owner to
    approve the first run of a first-time contributor. There is no preview deployment for a
    fork: CI is the preview.

9. **Review.** The owner reads what CI cannot judge ([below](#what-the-reviewer-reads)) and
    labels the pull request: `pack:new` or `pack:update`; `clinical-review:provided` when a
    clinician who is not the author reviewed the content and the pull request says who, else
    `needs-clinical-review`; `risk-level-change` when the level changes. Changes go back to
    step 4.

10. **Merge.** The owner squash-merges it. The pack is then served wherever community packs are
    turned on, labelled as above.

11. **Updates.** Every later change to what the pack decides with raises its version and adds a
    `CHANGELOG.md` entry; `community-check` refuses one that doesn't. An update must not do
    worse than the benchmark on `main`, within the tolerance `main` set (at most 0.05), on its
    own benchmark dataset and on `main`'s, and keeps `main`'s critical labels. Benchmark cases
    `main` has that the update removes or relabels are warnings: say why in the pull request.

## Risk level

The level is self-declared, so it is the first thing a reviewer reads. Choose the highest that
could apply, and say why in `risk.rationale`. A community pack is level 0 or 1.

| Level | For | Precedent | Community pack |
|---|---|---|---|
| 0 | Administrative: which team or queue sees something first, with no clinical judgement in the outcome and anything possibly urgent sent to a person | `mendari/patient-inbox-router` | Yes |
| 1 | Clinical information: reading clinical facts to extract them, check them or decide where they go | `mendari/referral-router`, `mendari/prior-auth-preflight` | Yes |
| 2 | Clinical support: eligibility, suggestions, patient-facing clinical content | `mendari/clinical-trial-prescreener`, `mendari/ai-guard` | No |
| 3 | Patient-specific prediction | | No |
| 4 | Time-critical, diagnostic or treatment decisions | | No |

The inbox router is level 0 although messages describe symptoms: it only chooses who reads a
message first, never replies or judges, and its emergency screen always sends a possible
emergency to a nurse. A pack that reads a referral to decide whether it is complete or urgent
is level 1. Under-declaring is the main way around the limits, so a reviewer who thinks a
level is too low asks you to raise it, and a pack that belongs at level 2 or above is not a
community pack for now.

## Synthetic data without Python

**Never use a real person's data**, not even "de-identified": no real names, dates of birth,
addresses, record numbers, phone numbers or messages copied from a chart or an inbox. Real data
merged into a public repository cannot be fully taken back. Write every case yourself, the way
a teaching case is written, and keep rare-condition and date combinations vague. `community-check`
warns on text in `data/`, `tests/` and a trained model's vocabulary that looks like a real
person's details; act on every warning. A trained model (a composite's members too) must be
what `mendari train` gives from the pack's own data, byte for byte.

Two ways to make the data, neither needs Python:

- **By hand.** `data/examples.jsonl` is one case per line: an `input`, the right `labels` and an
  `id`. Copy a line of the scaffold's file and change it.

- **From templates.** Write the cases as templates in YAML, with placeholders and the values
  they take, and let the script expand them, the same way every time:

    ```yaml
    # community-packs/<name>/data/cases.yaml
    - input: {message: {body: "Please refill my {drug}, I have {left} left."}}
      labels: {request: refill, urgent_symptoms: "no"}
      fill:
        drug: [metformin, lisinopril, atorvastatin]
        left: [two pills, "none"]
    - input: {message: {body: "Crushing chest pain since this morning."}}
      labels: {request: other, urgent_symptoms: "yes"}
    ```

    ```bash
    uv run python scripts/synthetic_cases.py community-packs/<name>/data/cases.yaml \
        --out community-packs/<name>/data/examples.jsonl --prefix ex
    ```

    The first template makes six cases (three drugs times two amounts), the second one. A
    placeholder the input never uses is refused, and so is a file that makes more than 5,000
    cases. Commit the templates with the data, so a reviewer reads them instead of every line.

Make the hard cases too: misspellings, negations ("no chest pain"), two requests in one
message, other languages your users write in. A benchmark of easy cases measures nothing.

## ABSTAIN carries no recommendation

`ABSTAIN` means the pack has nothing to suggest: the case goes to the existing manual workflow,
the policy's `fallback`. The runtime drops any recommendation from an `ABSTAIN` outcome, so a
rule `outcome: ABSTAIN` with a `recommendation` loses it, and a test must not expect one. When
you want a person to see a suggestion, use `HUMAN_REVIEW`; for a community pack every outcome
that would have been `AUTO` becomes `HUMAN_REVIEW` anyway, with its recommendation.

## Patterns that stay fast

Rules match text with regular expressions, and Python's `re` has no time limit: a pattern that
can match one text in many ways may take seconds on a long message, and stall the server that
runs it. `community-check` refuses those, in two ways: a static check of each pattern, and a run
of each pattern on pumped inputs of 10,000 characters built from every piece it holds, with one
second allowed on each. Neither is a guarantee, so a community pack also refuses any text longer
than 10,000 characters before a pattern runs.

| Instead of | Write | Why |
|---|---|---|
| `(\w+\s?)*` | `(?:\w+\s){0,5}\w+` | An unbounded quantifier inside another can split the same words in many ways; bound the outer one |
| `(a+)+`, `(\d+)*` | `a+`, `\d*` | The same, at its simplest: write the repetition once |
| <code>(.&#124;\s)+</code> | `[\s\S]+`, or better a bounded `[^.!?\n]{0,80}` | Alternatives that can start on the same character |
| `chest.*pain` | `chest(?:[^\w.!?\n]+\w+){0,5}?[^\w.!?\n]+pain` | Bounded: at most five words between, never across a sentence |
| `.*` at either end | nothing | A rule matches anywhere in the text already |
| `\d*\d*x`, `[a-z]+\s*[a-z]+$` | `\d*x`, `[a-z]+\s+[a-z]+$` | Repeated characters one after another that can match the same text split it in many ways; merge them, or require something only one of them matches between them |

More habits that keep patterns fast and honest:

- Bound every gap: `{0,5}` words, not `*`. The inbox router's red-flag screen is the example.
- Match whole phrases a person writes ("can't breathe"), not single letters or fragments.
- Name every pattern: the audit record says which one matched.
- Mark a pattern `negatable: true` when "no chest pain" must not count, and test it.
- Possessive quantifiers (`a++`) and atomic groups (`(?>...)`) never backtrack and are allowed.

## What the reviewer reads

CI checks the mechanics. The owner then reads what it cannot judge, and may ask for changes:

- **Clinical content.** Are the criteria, patterns, `known_failure_modes` and `out_of_scope`
  sound, are the critical labels the right ones, and is anything dangerous missing?
- **Risk level.** Does it match the intended use (the table above)?
- **Clinician review.** Who reviewed the content, their role, and what they reviewed. With
  none named, the pack can still merge, as `needs-clinical-review`; it is labelled "not
  clinically reviewed" in the product either way.
- **Synthetic data.** Nothing that looks like a real person, and the PHI warnings answered.
- **Instructions in the text.** Criteria are also instructions to a model if a deployment ever
  runs the pack on one, so nothing like "always answer no".
- **Provenance and licensing.** Nothing copied from proprietary criteria (InterQual, MCG, payer
  policies) or copyrighted instruments; code systems with licence terms (CPT, SNOMED CT) only
  as their licences allow; the sources of `card.evidence` given (see
  [CONTRIBUTING.md](https://github.com/Junebugg1214/mendari/blob/main/CONTRIBUTING.md#content-provenance)).
- **The card.** Intended use, users, population, out-of-scope uses and failure modes, written
  for a clinician.
- **Fit.** It does not duplicate a pack already there, and you answer the review.

## After the merge

- **You maintain it.** Maintainers are expected to answer a safety issue about their pack
  within 14 days. An issue labelled `patient-safety` blocks every other change to the pack
  until it is answered.
- **Core changes.** CI runs every community pack's behaviour tests on every change to Mendari.
  A change that breaks a pack fixes the pack or removes it in the same pull request, and says
  so.
- **Promotion.** A community pack may become one Mendari maintains, after clinical review: it
  moves to `packs/` with a new `mendari/` id, as a new major version.
- **Removal.** Only the owner removes a pack (CI fails anyone else's removal; to propose one,
  open an issue). The owner removes a pack that is unmaintained, superseded or the subject of a
  licence complaint with a pull request that deletes its folder and keeps its
  `MAINTAINERS.yaml` entry as a tombstone, so its id is never used again. A pack with a safety
  problem or real patient data is removed at once (the merge reverted and deployed), and a
  deployment can turn every community pack off with `MENDARI_COMMUNITY_PACKS=off`.
