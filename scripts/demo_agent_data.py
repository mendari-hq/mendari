# SPDX-License-Identifier: Apache-2.0
"""The bundled demo agent's evaluation set: synthetic patient portal messages, with labels.

    uv run python scripts/demo_agent_data.py           # write the demo agent's data/test.jsonl
    uv run python scripts/demo_agent_data.py --check   # fail if the committed file is not this

Every message is made here from the phrase lists below with a fixed seed, so the file is the same
on every machine and holds no patient's words. One case in five describes a symptom that needs a
nurse today; some of those are worded as a patient might, not as a keyword list would have it
("an elephant sitting on my chest"), so an agent that only matches keywords misses them and the
evaluation shows it. A few cases deny a symptom ("no chest pain") and are not urgent.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "agents" / "portal-message-agent" / "data" / "test.jsonl"
SEED = 20261121
CASES = 600

MEDICINES = (
    "metformin", "lisinopril", "atorvastatin", "levothyroxine", "amlodipine", "omeprazole",
    "sertraline", "albuterol inhaler", "losartan", "blood pressure pills", "thyroid medicine",
    "cholesterol tablets", "insulin pens", "allergy tablets",
)  # fmt: skip
OPENERS = ("", "", "Hello, ", "Hi, ", "Good morning. ", "Quick question: ", "Hi there, ")
CLOSERS = ("", "", " Thank you.", " Thanks!", " Please let me know.", " I appreciate it.")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "next week", "tomorrow morning")

REFILL = (
    "Can you send a refill of my {med} to the pharmacy?",
    "I am running low on my {med} and need a refill.",
    "Please renew my prescription for {med}.",
    "I ran out of my {med} yesterday.",
    "Could I get my {med} refilled before the weekend?",
    "My pharmacy says my {med} prescription has no refills left. Can you renew it?",
    "I need more {med}, I only have two days left.",
)
APPOINTMENT = (
    "I need to book an appointment for {day}.",
    "Can I reschedule my appointment to {day}?",
    "Please cancel my visit on {day}.",
    "I would like to schedule my annual physical.",
    "Is there an appointment available on {day}?",
    "I have to move my appointment, something came up at work.",
    "Could you book me in for a follow-up visit?",
)
OTHER = (
    "Can you send me a copy of my vaccination record?",
    "I have a question about my last bill.",
    "Did my lab results come back yet?",
    "What time does the clinic open on Saturday?",
    "I need a note for my employer about my visit.",
    "Has my referral to the specialist been sent?",
    "Can you update my phone number on file?",
    "I need a refill of my {med} and also want to book an appointment for {day}.",
    "My insurance changed this month. Who should I tell?",
)
# Symptoms that need a nurse today, as a keyword list words them.
URGENT_PLAIN = (
    "I have had chest pain since last night.",
    "I am having trouble breathing when I lie down.",
    "My husband says my speech is slurred this morning.",
    "The left side of my face is drooping.",
    "I feel chest pressure when I climb the stairs.",
)
# The same kinds of symptom, as a patient might put them.
URGENT_PARAPHRASED = (
    "It feels like an elephant is sitting on my chest.",
    "I cannot catch my breath even when I am resting.",
    "My words are coming out all wrong since this morning.",
    "One side of my mouth will not move when I smile.",
    "There is a tightness in my chest that will not go away.",
    "I get winded just walking to the kitchen and my lips look blue.",
)
DENIED = (
    "I have no chest pain, I just wanted to ask about my results.",
    "No chest pain or anything like that, only a question.",
)


def _fill(rng: random.Random, template: str) -> str:
    return template.format(med=rng.choice(MEDICINES), day=rng.choice(DAYS))


def _routine(rng: random.Random) -> tuple[str, str]:
    """A message with no urgent symptom, and what it asks for."""
    request = rng.choices(("refill", "appointment", "other"), weights=(35, 33, 32))[0]
    templates = {"refill": REFILL, "appointment": APPOINTMENT, "other": OTHER}[request]
    return _fill(rng, rng.choice(templates)), request


def build(cases: int = CASES, seed: int = SEED) -> list[dict[str, object]]:
    rng = random.Random(seed)
    records: list[dict[str, object]] = []
    for number in range(1, cases + 1):
        roll = rng.random()
        if roll < 0.20:  # a symptom that needs a nurse today, alone or with a request
            symptom = rng.choice(URGENT_PLAIN if rng.random() < 0.7 else URGENT_PARAPHRASED)
            if rng.random() < 0.5:
                asked, request = _routine(rng)
                body = f"{asked} {symptom}" if rng.random() < 0.5 else f"{symptom} {asked}"
            else:
                body, request = symptom, "other"
            urgent = "yes"
        elif roll < 0.24:  # a symptom said not to be there
            asked, request = _routine(rng)
            body, urgent = f"{rng.choice(DENIED)} {asked}", "no"
            request = "other"  # more than one thing: the front desk reads it
        else:
            body, request = _routine(rng)
            urgent = "no"
        body = f"{rng.choice(OPENERS)}{body}{rng.choice(CLOSERS)}"
        records.append(
            {
                "id": f"demo-{number:04d}",
                "input": {"message": {"body": body}},
                "labels": {"request": request, "urgent_symptoms": urgent},
            }
        )
    return records


def render(records: list[dict[str, object]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    args = parser.parse_args(argv)
    text = render(build())
    if args.check:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print(
                f"error: {OUT.relative_to(ROOT)} is not what this script writes: run "
                "uv run python scripts/demo_agent_data.py and commit it",
                file=sys.stderr,
            )
            return 1
        print(f"ok  {OUT.relative_to(ROOT)}")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({CASES} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
