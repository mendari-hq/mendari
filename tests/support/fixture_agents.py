# SPDX-License-Identifier: Apache-2.0
"""The generated files of the test fixtures' agents: each one's synthetic datasets, and its
Decision Card rendered from them.

    uv run python -m tests.support.fixture_agents           # write them
    uv run python -m tests.support.fixture_agents --check   # exit 1 if a committed file is not this

Everything here is synthetic, made for tests of the engine: no case holds a person's words, no
fixture is clinically reviewed, and none is for patient care. Every case is made from the phrase
lists below, by a random generator seeded with a text (`random.Random("triage/test")`), so each
file is the same on every machine and every Python; nothing reads a clock, the environment or
the network. Each case's labels are the facts it was made from, never what a provider answered:
an agent that misses one is wrong, and its evaluation says so. Each case says what it is in its
attributes: `source` is `synthetic`, and `kind` names the facts it was made from. No two cases of
a fixture hold the same text.

`mendari-test/triage` (`triage/`), synthetic messages to a clinic:

- `examples` (12), `test` (300) and `calibration` (200): requests for each team (the front desk,
  the pharmacy, billing, a nurse), same-day symptoms worded as a keyword list words them
  (`urgent_plain`) or as a patient might (`urgent_paraphrased`, which the fixture's keyword rules
  miss on purpose), alone or beside a request; symptoms said not to be there (`denied`); and
  mild synthetic insults (`abusive`, `abusive_urgent`). Each message has a channel (portal, SMS
  or email), and some a subject.
- `critical` (40): same-day symptoms only, most worded as a patient might, about half of them
  beside a request: the rules miss those worded so, and without the policy's
  `automation_requires` they would automate some (the safety invariant's test of its own check).

`mendari-test/clinical-review` (`clinical-review/`), synthetic triage notes: `examples` (12) and
`test` (120), from a symptom, how long it has lasted and how severe it is; a severe symptom told
in other words (`severe_paraphrased`) is missed by the fixture's rules on purpose.

Each fixture's `DECISION_CARD.md` is the card the engine renders for it as a bundled agent, from
the files written here (its digest covers the data): rendered in a copy of the folder, so the
committed folder is only read. Manifests, rules, schemas, canned answers and behaviour tests
are written by hand. Each fixture's folder is the one `tests/conftest.py` names, so a test and
this module never disagree on where it is. Another fixture's datasets are one more `Fixture` in
`FIXTURES`; files of another kind (a fixture's FHIR records) one more mapping in `files`.
`--root DIR` writes, or compares, under DIR instead of this repository.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.conftest import CLINICAL_DIR, ROOT, TRIAGE_DIR

SOURCE = "synthetic"  # every case's `source` attribute
CARD = "DECISION_CARD.md"
# A case is redrawn while another case of its fixture holds the same text; this many tries at
# most, so a phrase list too short for its counts is an error, never a loop.
MAX_TRIES = 200

Case = dict[str, Any]

# -- mendari-test/triage --------------------------------------------------------------------------

TEAMS = ("front_desk", "pharmacy", "billing", "nurse")
MEDICINES = (
    "metformin", "lisinopril", "atorvastatin", "levothyroxine", "amlodipine", "omeprazole",
    "sertraline", "losartan", "albuterol inhaler", "insulin pens", "allergy tablets",
    "blood pressure pills", "thyroid medicine", "cholesterol tablets",
)  # fmt: skip
WHEN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "next week", "tomorrow")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
# What each team is asked, as a sentence.
REQUESTS: Mapping[str, tuple[str, ...]] = {
    "front_desk": (
        "I need to book an appointment for {when}.",
        "Can I move my appointment to {when}?",
        "Please cancel my appointment on {weekday}.",
        "I would like to schedule my yearly checkup.",
        "Is there an appointment open on {weekday}?",
        "Could you send me a copy of my vaccination records?",
        "I need a work form filled out before {weekday}.",
        "What time does the clinic open on {weekday}?",
        "I am not sure who to ask about my visit last week.",
    ),
    "pharmacy": (
        "Can you send a refill of my {med} to the pharmacy?",
        "I am running low on my {med} and need a refill.",
        "Please renew my prescription for {med}.",
        "I ran out of my {med} yesterday.",
        "My {med} prescription has no refills left. Can you renew it?",
        "Could you refill my {med} before the weekend?",
    ),
    "billing": (
        "I have a question about my last bill.",
        "Why was I charged twice this month?",
        "Can I set up a payment plan for my balance?",
        "My insurance changed this month. Who should I tell?",
        "I paid my copay but the statement says I owe more.",
        "Can you explain the charges on my statement?",
    ),
    "nurse": (  # questions about health, none of them urgent
        "I have had a mild rash on my arm for a few days.",
        "Is it normal to feel tired after starting my {med}?",
        "My son has had a low fever since yesterday but he is eating well.",
        "I think my {med} is giving me an upset stomach.",
        "My cough is better but it still keeps me up at night.",
        "Should I keep taking my {med} while I have a cold?",
        "I felt a little dizzy after my new {med}.",
        "I just feel off today and wanted to ask about it.",
    ),
}
# Same-day symptoms as a keyword list words them: the fixture's screen rules find each one.
URGENT_PLAIN = (
    "I have had chest pain since last night.",
    "I feel chest pressure when I climb the stairs.",
    "I am short of breath even when I sit down.",
    "I have trouble breathing when I lie down.",
    "My husband says my speech is slurred this morning.",
    "The left side of my face is drooping.",
    "My right arm went suddenly numb an hour ago.",
    "I have been coughing up blood since this morning.",
    "I fainted at work this morning.",
)
# The same kinds of symptom as a patient might put them: the screen rules find none.
URGENT_PARAPHRASED = (
    "It feels like an elephant is sitting on my chest.",
    "I cannot catch my breath even when I am resting.",
    "My words are coming out all wrong since this morning.",
    "One side of my mouth will not move when I smile.",
    "There is a heavy squeezing feeling in the middle of my chest.",
    "I get winded walking to the kitchen and my lips look blue.",
    "The room keeps going dark and I almost hit the floor.",
    "My left arm feels heavy and strange and I cannot lift it well.",
)
# A symptom said not to be there: it changes nothing about what the message asks.
DENIED = (
    "I have no chest pain or trouble breathing.",
    "I do not have chest pain.",
    "I am not short of breath.",
    "No chest pain, just a question.",
)
# Mild insults of the people who read the message: synthetic, and the screen rules find each.
INSULTS = (
    "You people are useless.",
    "Nobody here does their job, you idiots.",
    "This clinic is run by incompetent fools.",
    "Answer me this time, you morons.",
    "You are all useless and lazy.",
)
OPENERS = ("Hello.", "Hi there.", "Good morning.", "Quick question.")
CLOSERS = ("Thank you.", "Thanks.", "Please let me know.", "I appreciate it.")
SUBJECTS: Mapping[str, tuple[str, ...]] = {
    "front_desk": ("Appointment", "Scheduling"),
    "pharmacy": ("Refill", "Prescription"),
    "billing": ("Bill", "Billing question"),
    "nurse": ("Question", "Medical question"),
    "urgent": ("Question", "Help"),
}
# How a message came: portal, then SMS, then email, by these shares.
CHANNELS = (("portal", 0.60), ("sms", 0.25), ("email", 0.15))
URGENT_KINDS = ("urgent_plain", "urgent_paraphrased", "abusive_urgent")
# How many cases of each kind each dataset holds; each kind of the four teams is a request.
TRIAGE_COUNTS: Mapping[str, Mapping[str, int]] = {
    "examples": {
        "front_desk": 2, "pharmacy": 2, "billing": 1, "nurse": 1, "urgent_plain": 2,
        "urgent_paraphrased": 1, "denied": 1, "abusive": 1, "abusive_urgent": 1,
    },
    "test": {
        "front_desk": 54, "pharmacy": 54, "billing": 38, "nurse": 46, "urgent_plain": 30,
        "urgent_paraphrased": 30, "denied": 18, "abusive": 24, "abusive_urgent": 6,
    },
    "calibration": {
        "front_desk": 36, "pharmacy": 36, "billing": 26, "nurse": 30, "urgent_plain": 20,
        "urgent_paraphrased": 20, "denied": 12, "abusive": 16, "abusive_urgent": 4,
    },
    "critical": {"urgent_plain": 16, "urgent_paraphrased": 24},
}  # fmt: skip
TRIAGE_IDS = {
    "examples": "triage-ex",
    "test": "triage",
    "calibration": "triage-cal",
    "critical": "triage-crit",
}


def _request(rng: random.Random, team: str) -> str:
    template = rng.choice(REQUESTS[team])
    return template.format(
        med=rng.choice(MEDICINES), when=rng.choice(WHEN), weekday=rng.choice(WEEKDAYS)
    )


def _channel(rng: random.Random) -> str:
    roll, total = rng.random(), 0.0
    for channel, share in CHANNELS:
        total += share
        if roll < total:
            return channel
    return CHANNELS[-1][0]


def _either_order(rng: random.Random, first: str, second: str) -> list[str]:
    return [first, second] if rng.random() < 0.5 else [second, first]


def triage_message(rng: random.Random, kind: str) -> tuple[dict[str, Any], dict[str, str]]:
    """One synthetic message of `kind`, as the fixture's input, and its labels: the facts it was
    made from (which team, whether a symptom needs attention today, whether it insults)."""
    channel = _channel(rng)
    asked = kind if kind in TEAMS else None
    if kind in TEAMS:
        parts = [_request(rng, kind)]
        labels = {"destination": kind, "urgency": "no", "abusive": "no"}
    elif kind in URGENT_KINDS:
        symptom = rng.choice(URGENT_PARAPHRASED if kind == "urgent_paraphrased" else URGENT_PLAIN)
        parts = [symptom]
        if rng.random() < 0.5:  # beside a request, which the nurse reads too
            asked = rng.choice(TEAMS)
            parts = _either_order(rng, _request(rng, asked), symptom)
        abusive = "no"
        if kind == "abusive_urgent":
            parts = _either_order(rng, rng.choice(INSULTS), " ".join(parts))
            abusive = "yes"
        labels = {"destination": "nurse", "urgency": "yes", "abusive": abusive}
    elif kind == "denied":
        asked = rng.choice(TEAMS)
        parts = [rng.choice(DENIED), _request(rng, asked)]
        labels = {"destination": asked, "urgency": "no", "abusive": "no"}
    elif kind == "abusive":
        asked = rng.choice(TEAMS)
        parts = _either_order(rng, _request(rng, asked), rng.choice(INSULTS))
        labels = {"destination": asked, "urgency": "no", "abusive": "yes"}
    else:
        raise ValueError(f"no kind of message {kind!r}")
    opener = rng.choice(OPENERS) if rng.random() < 0.4 else ""
    closer = rng.choice(CLOSERS) if rng.random() < 0.4 else ""
    body = " ".join(part for part in (opener, *parts, closer) if part)
    message: dict[str, str] = {}
    if channel != "sms" and rng.random() < 0.5:
        message["subject"] = rng.choice(SUBJECTS[asked or "urgent"])
    message.update(body=body, channel=channel)
    return {"message": message}, labels


def _triage_case(rng: random.Random, kind: str) -> Case:
    given, labels = triage_message(rng, kind)
    channel = given["message"]["channel"]
    return {
        "input": given,
        "labels": labels,
        "attributes": {"source": SOURCE, "channel": channel, "kind": kind},
    }


# -- mendari-test/clinical-review -----------------------------------------------------------------

SYMPTOMS = (
    "head pain", "throat pain", "back pain", "coughing", "stomach pain", "ear pain", "itching",
    "knee pain",
)  # fmt: skip
PAINS = ("head pain", "stomach pain", "back pain", "ear pain", "knee pain")
SHORT = ("since this morning", "since yesterday", "for two days")
LONG = ("for three weeks", "for a month", "for several weeks")
NOTE_OPENERS = ("", "Triage note: ", "Nurse line note: ")
NOTES: Mapping[str, tuple[str, ...]] = {
    "none": (
        "Patient asks for travel advice and has no symptoms today.",
        "Patient asks about a vaccine and has no symptoms today.",
        "Patient asks about a test result and has no symptoms today.",
        "Patient checks in after {symptom} last month and feels well.",
        "Follow-up after {symptom} last month: the patient feels well now.",
    ),
    "mild": (
        "Patient reports mild {symptom} {duration}.",
        "Patient reports slight {symptom} {duration}.",
        "Patient reports a little {symptom} {duration} and is managing at home.",
    ),
    "moderate": (
        "Patient reports {symptom} {duration} that keeps them up at night.",
        "Patient reports {symptom} {duration}, and it has been getting worse.",
        "Patient reports moderate {symptom} {duration} that makes it hard to work.",
    ),
    "severe": (
        "Patient reports severe {symptom} {duration}.",
        "Patient reports unbearable {symptom} {duration}.",
        "Patient reports {symptom} {duration} so severe they cannot stand up.",
    ),
    # The same severity told in other words: the fixture's rules find none of them.
    "severe_paraphrased": (
        "Patient reports {symptom} {duration} that makes them double over, and they cannot "
        "keep water down.",
        "Patient reports {symptom} {duration} that is the worst they have ever felt.",
    ),
}
# Each kind of note: its templates, its symptoms, its durations, and its labels (the severity,
# and how soon the patient should be seen).
NOTE_KINDS: Mapping[str, tuple[str, tuple[str, ...], tuple[str, ...], str, str]] = {
    "none": ("none", SYMPTOMS, ("",), "none", "self_care"),
    "mild_short": ("mild", SYMPTOMS, SHORT, "mild", "self_care"),
    "mild_long": ("mild", SYMPTOMS, LONG, "mild", "book_visit"),
    "moderate": ("moderate", SYMPTOMS, SHORT + LONG, "moderate", "book_visit"),
    "severe": ("severe", PAINS, SHORT + LONG, "severe", "see_today"),
    "severe_paraphrased": ("severe_paraphrased", PAINS, SHORT, "severe", "see_today"),
}
CLINICAL_COUNTS: Mapping[str, Mapping[str, int]] = {
    "examples": dict.fromkeys(NOTE_KINDS, 2),
    "test": {
        "none": 12, "mild_short": 24, "mild_long": 18, "moderate": 30, "severe": 24,
        "severe_paraphrased": 12,
    },
}  # fmt: skip
CLINICAL_IDS = {"examples": "review-ex", "test": "review"}


def clinical_note(rng: random.Random, kind: str) -> tuple[dict[str, Any], dict[str, str]]:
    """One synthetic triage note of `kind`, as the fixture's input, and its labels."""
    templates, symptoms, durations, severity, disposition = NOTE_KINDS[kind]
    template = rng.choice(NOTES[templates])
    text = template.format(symptom=rng.choice(symptoms), duration=rng.choice(durations))
    text = rng.choice(NOTE_OPENERS) + text
    return {"note": {"text": text}}, {"disposition": disposition, "severity": severity}


def _clinical_case(rng: random.Random, kind: str) -> Case:
    given, labels = clinical_note(rng, kind)
    return {"input": given, "labels": labels, "attributes": {"source": SOURCE, "kind": kind}}


# -- datasets, cards and files --------------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    """A fixture agent whose datasets are generated here: its folder, and for each dataset, in
    the order they are made, how many cases of each kind, the prefix of its case ids, and how a
    case of a kind is made."""

    folder: Path
    counts: Mapping[str, Mapping[str, int]]
    ids: Mapping[str, str]
    make: Callable[[random.Random, str], Case]

    @property
    def name(self) -> str:
        return self.folder.name

    def datasets(self) -> dict[str, list[Case]]:
        """Every dataset, by name: each its own seed (`<folder>/<dataset>`), its kinds in an
        order the seed shuffles, and no text that an earlier case of the fixture holds."""
        seen: set[str] = set()
        made: dict[str, list[Case]] = {}
        for dataset, counts in self.counts.items():
            rng = random.Random(f"{self.name}/{dataset}")
            kinds = [kind for kind, count in counts.items() for _ in range(count)]
            rng.shuffle(kinds)
            cases = []
            for number, kind in enumerate(kinds, start=1):
                case = self._new(rng, kind, seen)
                width = 2 if len(kinds) < 100 else 4
                cases.append({"id": f"{self.ids[dataset]}-{number:0{width}d}", **case})
            made[dataset] = cases
        return made

    def _new(self, rng: random.Random, kind: str, seen: set[str]) -> Case:
        for _ in range(MAX_TRIES):
            case = self.make(rng, kind)
            text = json.dumps(case["input"], sort_keys=True)
            if text not in seen:
                seen.add(text)
                return case
        raise ValueError(f"{self.name}: no new case of kind {kind!r} after {MAX_TRIES} tries")


FIXTURES = (
    Fixture(TRIAGE_DIR, TRIAGE_COUNTS, TRIAGE_IDS, _triage_case),
    Fixture(CLINICAL_DIR, CLINICAL_COUNTS, CLINICAL_IDS, _clinical_case),
)


def render(cases: list[Case]) -> str:
    """A dataset as JSON Lines, one case a line."""
    return "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)


def card(folder: Path, data: Mapping[str, str]) -> str:
    """The Decision Card the engine renders for the fixture in `folder` as a bundled agent, with
    `data` (its files under `data/`, by name) in place of its own: rendered in a copy, so the
    folder is only read."""
    from mendari.core.agent import load_agent
    from mendari.core.card import render_card

    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / folder.name
        shutil.copytree(folder, copy, ignore=shutil.ignore_patterns(CARD, "__pycache__"))
        (copy / "data").mkdir(exist_ok=True)
        for name, text in data.items():
            (copy / "data" / name).write_bytes(text.encode("utf-8"))
        return render_card(load_agent(copy, origin="maintained"))


def files(fixtures: tuple[Fixture, ...] = FIXTURES) -> dict[Path, str]:
    """Every file generated here, by its path in the repository: each fixture's datasets, then
    its Decision Card."""
    written: dict[Path, str] = {}
    for fixture in fixtures:
        data = {f"{name}.jsonl": render(cases) for name, cases in fixture.datasets().items()}
        written.update({fixture.folder / "data" / name: text for name, text in data.items()})
        written[fixture.folder / CARD] = card(fixture.folder, data)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="the repository the files are written to or compared with (default: this one)",
    )
    args = parser.parse_args(argv)
    generated = {args.root / path.relative_to(ROOT): text for path, text in files().items()}
    if args.check:
        failed = 0
        for path, text in generated.items():
            shown = path.relative_to(args.root)
            if not path.is_file() or path.read_bytes() != text.encode("utf-8"):
                print(
                    f"error: {shown} is not what this module writes: run "
                    "uv run python -m tests.support.fixture_agents and commit it",
                    file=sys.stderr,
                )
                failed = 1
            else:
                print(f"ok  {shown}")
        return failed
    for path, text in generated.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        print(f"wrote {path.relative_to(args.root)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
