# SPDX-License-Identifier: Apache-2.0
"""The reference packs' lookarounds, rewritten so they run on RE2 (LP-16c, ADR-0009), decide
exactly as the lookarounds did.

RE2, the engine for packs the deployment does not own, has no lookahead or lookbehind, and four
patterns of the reference packs had one (`ORIGINALS`): a copy of such a pack that is not this
release's (a new version pulled from a registry) is refused. A rewrite changes the pack's digest
and must change nothing it decides: every shipped input decides the same, reasons and evidence
included, the evaluation ledger's cases are the same, and a seeded fuzz finds the pattern
matching where the lookaround did, on `re` and on RE2.

16c-1 rewrote ai-guard's `claim-about-the-record`, exact on every input. The other three wait
for the owner (16c-2); `tests/test_community_re2.py` skips exactly those.
"""

from __future__ import annotations

import json
import random
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import Decision, load_pack
from mendari.core import patterns
from mendari.core.pack import Pack
from mendari.core.patterns import RULES_FLAGS
from mendari.evals.harness import evaluation, resolve_dataset
from mendari.evals.pack_tests import load_cases
from mendari.providers import build_provider
from mendari.providers.base import ProviderOutput
from tests.conftest import ROOT
from tests.test_registry_engine import copied, jsonl, one_more_line

PACKS = ROOT / "tests" / "fixtures" / "reference-packs"

# The four reference-pack patterns with a lookaround, as they were, by (pack, rules file, rule).
ORIGINALS: dict[tuple[str, str, str], str] = {
    ("mendari/ai-guard", "logic/screen_rules.yaml", "claim-about-the-record"): (
        r"your (latest|last|recent)[^.;!?\n]{0,30}(test|tests|results?|level|panel|a1c)|your "
        r"(a1c|potassium|kidney function|cholesterol)[^.;!?\n]{0,30}(was|were|showed)|your chart "
        r"lists|keep taking your (?!(medicines?|medications?|meds|pills?)\b)\w+"
    ),
    ("mendari/clinical-trial-prescreener", "logic/note_rules.yaml", "pregnant"): (
        r"pregnan(?:t|cy)\b(?![^.;!?\n]*\b(?:test (?:is |was )?negative|negative|ruled out)\b)"
    ),
    ("mendari/clinical-trial-prescreener", "logic/note_rules.yaml", "pancreatitis"): (
        r"pancreatitis\b(?![^.;!?\n]*\bruled out\b)"
    ),
    ("mendari/referral-router", "logic/triage_rules.yaml", "new-lung-mass"): (
        r"\bnew\b(?:[^.;!?\n]|\.(?=\d)){0,30}\b(nodule|mass)\b|shows a (new )?mass|spiculated"
    ),
}
GUARD = ("mendari/ai-guard", "logic/screen_rules.yaml", "claim-about-the-record")
GUARD_DIR = PACKS / "ai-guard"
# The alternative the rewrite replaced: "keep taking your" and a word that is not one of these.
WORDS = ("medicine", "medicines", "medication", "medications", "meds", "pill", "pills")
LOOKAHEAD = re.compile(r"keep taking your (?!(medicines?|medications?|meds|pills?)\b)\w+", re.I)
# A rules provider's model is its rules file's hash, which any edit changes (`rules.py`).
RULES_MODEL = re.compile(r"^rules@[0-9a-f]{12}$")


def unhashed(model: str | None) -> str | None:
    return "rules@" if model is not None and RULES_MODEL.match(model) else model


def rule(pack_dir: Path, relative: str, name: str) -> dict[str, Any]:
    """The rule called `name` in a pack's rules file, as written."""
    rules = yaml.safe_load((pack_dir / relative).read_text())
    found = [
        item
        for spec in rules["questions"].values()
        for items in spec["labels"].values()
        for item in items
        if item["name"] == name
    ]
    assert len(found) == 1, name
    return dict(found[0])


SHIPPED = rule(GUARD_DIR, GUARD[1], GUARD[2])["pattern"]


@pytest.fixture(scope="module")
def lookahead_guard(tmp_path_factory: pytest.TempPathFactory) -> Pack:
    """The shipped ai-guard with `claim-about-the-record` as it was, with its lookahead."""
    root = tmp_path_factory.mktemp("lookahead") / "ai-guard"
    shutil.copytree(GUARD_DIR, root)
    path = root / GUARD[1]
    text = path.read_text()
    written = json.dumps(SHIPPED)  # the file's double-quoted YAML string
    assert ORIGINALS[GUARD] != SHIPPED and text.count(written) == 1
    path.write_text(text.replace(written, json.dumps(ORIGINALS[GUARD])))
    assert rule(root, GUARD[1], GUARD[2])["pattern"] == ORIGINALS[GUARD]
    return load_pack(root, origin="maintained")


@pytest.fixture(scope="module")
def rewritten_guard() -> Pack:
    return load_pack(GUARD_DIR, origin="maintained")


def shipped_inputs(pack: Pack) -> list[dict[str, Any]]:
    """Every input ai-guard ships: its datasets, the local-site example and its behaviour tests."""
    inputs = [case["input"] for case in load_cases(pack)]
    for path in sorted((GUARD_DIR / "data").glob("*.jsonl")):
        inputs += jsonl(path)
    return inputs + jsonl(ROOT / "datasets" / "guard-local-site-example.jsonl")


def decided(result: Any) -> dict[str, Any]:
    """A decision as recorded, without what differs from one run or one digest to the next: its
    id, time, latency, the pack's digest and the rules file's hash."""
    found = result.to_dict()
    for key in ("id", "created_at", "latency_ms"):
        found.pop(key)
    found["pack"].pop("digest")
    for answer in found["answers"].values():
        answer["provider"]["model"] = unhashed(answer["provider"]["model"])
    return dict(found)


def answered(output: ProviderOutput) -> dict[str, Any]:
    """What a provider answered, without the rules file's hash."""
    answers = {question: answer.to_dict() for question, answer in output.answers.items()}
    for answer in answers.values():
        answer["provider"]["model"] = unhashed(answer["provider"]["model"])
    return {"answers": answers, "failures": output.failures, "cost": output.cost_usd}


def test_the_rewritten_ai_guard_decides_every_shipped_input_as_the_lookahead_did(
    lookahead_guard: Pack, rewritten_guard: Pack
) -> None:
    """Outcome, recommendation, priority, every answer's probabilities and evidence, and the
    reasons, on every input ai-guard ships; and the rules provider alone, as the composite
    variants (rules_and_jev, rules_and_llm) run it beside a model."""
    before, after = Decision(lookahead_guard), Decision(rewritten_guard)
    assert before.pack.digest != after.pack.digest
    inputs = shipped_inputs(rewritten_guard)
    differ = [
        n
        for n, given in enumerate(inputs)
        if decided(before.decide(given)) != decided(after.decide(given))
    ]
    assert len(inputs) > 1_200 and differ == []

    rules_before = build_provider(lookahead_guard, "screen", "rules")
    rules_after = build_provider(rewritten_guard, "screen", "rules")
    assert rules_before.info.model != rules_after.info.model
    differ = [
        n
        for n, given in enumerate(inputs)
        if answered(rules_before.answer(given)) != answered(rules_after.answer(given))
    ]
    assert differ == []

    # Not a vacuous check: on these inputs the replaced alternative matches ("keep taking your
    # furosemide"), and its lookahead refuses a match many more times ("... your medicines").
    drafts = [str(given.get("draft", "")) for given in inputs]
    keeping = [draft for draft in drafts if re.search("keep taking your", draft, re.I)]
    assert sum(1 for draft in keeping if LOOKAHEAD.search(draft)) >= 1
    assert sum(1 for draft in keeping if not LOOKAHEAD.search(draft)) >= 30
    evidence = [after.decide(given).answers["unsupported_claim"].evidence for given in inputs]
    assert sum("unknown:claim-about-the-record" in found for found in evidence) > 50


def test_the_rewritten_ai_guard_ledger_lines_are_the_lookaheads(
    lookahead_guard: Pack, rewritten_guard: Pack
) -> None:
    """The benchmark's ledger (`mendari eval --dataset test`) holds the same case lines; its
    header differs only in the digest and the rules file's hash."""
    before, after = (
        evaluation(Decision(pack), resolve_dataset(pack, "test")).ledger.dumps().splitlines()
        for pack in (lookahead_guard, rewritten_guard)
    )
    assert len(after) == 601 and before[1:] == after[1:]

    def header(line: bytes) -> dict[str, Any]:
        found = json.loads(line)
        found["pack"].pop("digest")
        found["models"] = [unhashed(model) for model in found["models"]]
        for provider in found["providers"].values():
            provider["model"] = unhashed(provider["model"])
        return dict(found)

    assert before[0] != after[0] and header(before[0]) == header(after[0])


# What the fuzz writes: "keep taking your" and the words, their prefixes, other spellings of
# their letters (`re` ignores case: a dotless or dotted capital i is an i, a long s an s, the
# Kelvin sign a k), an accented e, digits, `_`, punctuation and a lone surrogate (RE2 reads it as
# U+FFFD).
PIECES = sorted({word[:end] for word in WORDS for end in range(1, len(word) + 1)})
OTHER_I = "\N{LATIN SMALL LETTER DOTLESS I}\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"
OTHER_S = "\N{LATIN SMALL LETTER LONG S}"
KELVIN = "\N{KELVIN SIGN}"
SPELLINGS = {"i": "iI" + OTHER_I, "s": "sS" + OTHER_S, "k": "kK" + KELVIN}
CHARACTERS = [
    *OTHER_I,
    *OTHER_S,
    KELVIN,
    "\N{LATIN SMALL LETTER E WITH ACUTE}",
    "\ud800",
    *"x_0123456789 .,;:!?'-\n",
]


def spelled(rng: random.Random, text: str) -> str:
    return "".join(rng.choice(SPELLINGS.get(c, c + c.upper())) for c in text)


def fuzzed(rng: random.Random) -> str:
    text = ""
    for _ in range(rng.randint(1, 3)):
        if rng.random() < 0.9:
            lead = "keep taking your "
            text += spelled(rng, lead) if rng.random() < 0.2 else lead
        for _ in range(rng.randint(0, 3)):
            piece = rng.choice(PIECES)
            text += spelled(rng, piece) if rng.random() < 0.6 else rng.choice(CHARACTERS)
    return text


def test_the_rewritten_ai_guard_pattern_matches_where_the_lookahead_did_on_both_engines() -> None:
    """The rule is not negatable, so only whether (and where first) its pattern matches decides
    it (`RulesProvider._matches`): the shipped pattern on `re` and on RE2 matches first where the
    lookahead did on `re`, in 20,000 seeded strings."""
    assert not rule(GUARD_DIR, GUARD[1], GUARD[2]).get("negatable", False)
    assert "lookahead" in str(patterns.problem(ORIGINALS[GUARD], RULES_FLAGS, community=True))
    assert patterns.problem(SHIPPED, RULES_FLAGS, community=True) is None
    original = re.compile(ORIGINALS[GUARD], RULES_FLAGS)
    on_re = patterns.compile(SHIPPED, RULES_FLAGS)
    on_re2 = patterns.compile(SHIPPED, RULES_FLAGS, community=True)
    assert isinstance(on_re2, patterns._Re2Pattern)

    def start(found: Any) -> int | None:
        return None if found is None else int(found.start())

    rng = random.Random(1_604)
    texts = [fuzzed(rng) for _ in range(20_000)]
    differ, matched = [], 0
    for text in texts:
        expected = start(original.search(text))
        matched += expected is not None
        if start(on_re.search(text)) != expected or start(on_re2.search(text)) != expected:
            differ.append(text)
    assert differ == []
    # Both answers are common, and the words spelled with other letters are in the mix.
    assert 2_000 < matched < 18_000
    other = re.compile(f"keep taking your [^ ]*[{OTHER_I}{OTHER_S}]")
    assert any(LOOKAHEAD.search(text) is None and other.search(text) for text in texts)


def test_ai_guard_runs_on_the_community_engine(tmp_path: Path, rewritten_guard: Pack) -> None:
    """A copy of ai-guard that is not this release's (one more line in its rules, at a new
    version) loads from a registry on RE2, which the lookahead kept it from, and decides every
    shipped input as the reference pack does on `re`."""
    root = copied(tmp_path, "ai-guard", version="9.9.9", edit=one_more_line(GUARD[1]))
    hub = Decision(load_pack(root, origin="hub"), origin_cap=False)  # the engine, not the cap
    assert hub.pack.bounded and hub.pack.digest != rewritten_guard.digest
    rules: Any = hub.providers["screen"]
    compiled = [
        regex
        for labels in rules._compiled.values()
        for found in labels.values()
        for _, regex in found
    ]
    assert compiled and all(isinstance(regex, patterns._Re2Pattern) for regex in compiled)
    reference = Decision(rewritten_guard)
    inputs = shipped_inputs(rewritten_guard)

    def unversioned(result: Any) -> dict[str, Any]:  # the copy's version is its own
        found = decided(result)
        found["pack"].pop("version")
        return found

    differ = [
        n
        for n, given in enumerate(inputs)
        if unversioned(hub.decide(given)) != unversioned(reference.decide(given))
    ]
    assert differ == []


def test_ai_guard_corrects_no_typos_so_the_rewrite_changes_no_keyword() -> None:
    """The rewrite spells "medicines", "medications" and "pills" out letter by letter, so they
    are no longer whole words in the rules' text, and typo correction (which corrects towards
    the rules' words) would no longer know them: "keep taking your medicnes" would be corrected
    to "medicines" by the lookahead's rules (no match) and left alone by the rewrite's (a
    match). ai-guard corrects no typos, so its decisions are the same; turning typo tolerance on
    for these rules needs those words added back as keywords first."""
    rules = yaml.safe_load((GUARD_DIR / GUARD[1]).read_text(encoding="utf-8"))
    assert "typo_tolerance" not in rules, (
        "typo tolerance on ai-guard's screen rules: add medicines, medications and pills back "
        "as keywords before enabling it (tests/test_lookaround_rewrites.py)"
    )
