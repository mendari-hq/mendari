# SPDX-License-Identifier: Apache-2.0
"""The patterns of an agent the deployment does not own run on RE2, which never backtracks (plan
B11; core/patterns.py, core/re2_syntax.py): its rules and its input schema's `pattern`,
`patternProperties` and `additionalProperties` (and so `propertyNames`). Such an agent is here one
pulled from a registry whose digest is no bundled agent's (`tests/support/engine.py`). Every
agent the deployment owns runs on `re`, exactly as before. A pattern RE2 cannot run makes such
an agent invalid at load, saying what to write instead."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from mendari import Decision, load_agent
from mendari.core import input_schema, patterns
from mendari.core.agent import authored_patterns
from mendari.core.re2_syntax import translate
from mendari.core.registry import SearchPath, discover
from mendari.errors import AgentValidationError
from mendari.providers.rules import RulesProvider
from tests.conftest import DEMO_AGENT_DIR, FIXTURE_AGENTS, FIXTURE_NAMESPACE, ROOT
from tests.support.engine import RULES, SCHEMA, SORTER_ID, pulled, sorter

# Patterns that backtrack on `re` (seconds to hours on these texts), with a text that makes
# them: the classic shapes, the probe's own slow pattern, and the last check's findings.
SLOW = [
    (r"(a+)+$", "a" * 9_999 + "!"),
    (r"(\w+\s?)*$", "word " * 1_999 + "!"),
    (r"(?:01){0,450}(?:01){0,450}(?:01){0,450}x", "01" * 5_000),  # size 2,701: under the cap
    (r"\bno\b.{0,300}\bno\b.{0,300}\bno\b.{0,300}\bfever\b", "no " * 3_333),
    (r"x.{0,1000}x.{0,1000}y", "x" * 10_000),
    (r"\d.{0,300}[a-z].{0,300}\d.{0,300}!", "0a" * 5_000),
    (r"\Bitis\s(?:\w{1,20}\s?){1,20}:", "arthritis " + "a" * 9_990),
    (r"(?:[a-z]\d)*x(?:\d[a-z])*\b", "x0" * 5_000),
    (r"(?:[ab][cd])*b?(?:[cd][ab])*!", "bc" * 5_000),
    ("[^\\W\\x00-ɏ]+\\s*[^\\W\\x00-ɏ]+!", "я" * 10_000),
    (r"[^.!?]+\?", "a" * 10_000),
]
IDS = [
    "nested",
    "words",
    "counts",
    "gaps",
    "x-gaps",
    "mixed-gaps",
    "suffix",
    "realigned",
    "optional-between",
    "cyrillic",
    "question",
]
# Each takes milliseconds, but `counts`: RE2's time is the text's length times the pattern's
# size (`re2_syntax.size`, capped by `core/caps.py`), and 2,700 counted copies take a tenth of a
# second on 10,000 characters.
LIMIT_S = 1.0


def bounded_agent(
    base: Path,
    *,
    rules: Sequence[str] = (),
    schema: dict[str, Any] | None = None,
    agent_id: str = SORTER_ID,
) -> Path:
    """The synthetic sorter under `base`, with `rules` as its billing patterns, its message up to
    10,000 characters, and `schema` merged into the message's schema. Loaded with `pulled`, it
    runs on the bounded engine; with `load_agent`, it is the deployment's own."""

    def billing(config: dict[str, Any]) -> None:
        if rules:
            config["questions"]["topic"]["labels"]["billing"] = [
                {"name": f"p{i}", "pattern": text, "weight": 8} for i, text in enumerate(rules)
            ]

    def message(document: dict[str, Any]) -> None:
        document["properties"]["message"]["maxLength"] = 10_000
        document["properties"]["message"].update(schema or {})

    return sorter(base, rules=billing, schema=message, agent_id=agent_id)


def billing_patterns(decision: Decision) -> list[Any]:
    """The compiled billing patterns of the fixture's rules provider."""
    provider = cast(RulesProvider, decision.providers["reader"])
    return [regex for _, regex in provider._compiled["topic"]["billing"]]


def timed(decision: Decision, inputs: dict[str, Any]) -> tuple[float, Any]:
    """The processor time one decision takes, and the decision."""
    started = time.process_time()
    result = decision.decide(inputs)
    return time.process_time() - started, result


# -- the engine ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("pattern", "text"), SLOW, ids=IDS)
def test_a_bounded_agents_rules_decide_slow_patterns_fast(
    tmp_path: Path, pattern: str, text: str
) -> None:
    agent = pulled(bounded_agent(tmp_path, rules=[pattern]))
    decision = Decision(agent)
    timed(decision, {"message": "warm"})  # translation tables, compiled once a process
    seconds, result = timed(decision, {"message": text[:9_990]})  # with the key: 9,997
    assert seconds < LIMIT_S, f"{seconds:.2f} s"
    assert not result.rejected


@pytest.mark.parametrize(("pattern", "text"), SLOW, ids=IDS)
def test_a_bounded_agents_schema_patterns_decide_slow_patterns_fast(
    tmp_path: Path, pattern: str, text: str
) -> None:
    schema = {"not": {"pattern": pattern}}  # runs the pattern on every message
    root = bounded_agent(tmp_path, schema=schema)
    document = json.loads((root / SCHEMA).read_text())
    document["properties"]["extra"] = {"type": "object", "patternProperties": {pattern: {}}}
    document["properties"]["names"] = {"type": "object", "propertyNames": {"pattern": pattern}}
    (root / SCHEMA).write_text(json.dumps(document, ensure_ascii=False))
    decision = Decision(pulled(root))
    timed(decision, {"message": "warm"})
    part = text[:3_300]  # the message and two keys: 9,900 characters, with the keys' names
    inputs = {"message": part, "extra": {part + "1": 1}, "names": {part + "2": 1}}
    seconds, result = timed(decision, inputs)
    assert seconds < LIMIT_S, f"{seconds:.2f} s"
    assert not any("characters" in problem for problem in result.schema_problems)


def test_the_same_patterns_stay_on_re_in_any_other_agent(tmp_path: Path) -> None:
    """The deployment's own agent. Never run on the slow texts: `re` would take minutes."""
    root = bounded_agent(tmp_path, rules=[p for p, _ in SLOW], agent_id="local/visit-sorter")
    agent = load_agent(root)
    assert not agent.bounded
    for text, _ in SLOW:
        assert type(patterns.compile(text, patterns.RULES_FLAGS)) is re.Pattern
    decision = Decision(agent)
    compiled = billing_patterns(decision)
    assert all(type(regex) is re.Pattern for regex in compiled)
    assert [regex.pattern for regex in compiled] == [p for p, _ in SLOW]
    assert decision.decide({"message": "a question?"}).answers["topic"].label == "billing"
    validator = input_schema.validator(agent.input_schema)
    assert type(validator) is Draft202012Validator


def test_a_bounded_agents_rules_run_on_re2(tmp_path: Path) -> None:
    agent = pulled(bounded_agent(tmp_path, rules=[r"\bbill(?:ing)?\b"]))
    decision = Decision(agent)
    assert all(type(regex) is not re.Pattern for regex in billing_patterns(decision))
    assert decision.decide({"message": "A BILLING question"}).answers["topic"].label == "billing"
    assert decision.decide({"message": "billings"}).answers["topic"].label == "other"


@pytest.mark.parametrize(
    ("pattern", "says"),
    [
        (r"pain(?= now)", "a lookahead or lookbehind"),
        (r"(?<=chest )pain", "a lookahead or lookbehind"),
        (r"(?<!no )pain", "a negative lookahead or lookbehind"),
        (r"pain(?! free)", "a negative lookahead or lookbehind"),
        (r"(\w)\1", "a back reference"),
        (r"(?P<a>x)(?P=a)", "a back reference"),
        (r"(a)?(?(1)b|c)", "a conditional group"),
        (r"(?>pain)", "an atomic group"),
        (r"pain++", "a possessive quantifier"),
    ],
)
def test_a_pattern_re2_cannot_run_makes_a_bounded_agent_invalid(
    tmp_path: Path, pattern: str, says: str
) -> None:
    root = bounded_agent(tmp_path, rules=[pattern])
    with pytest.raises(AgentValidationError) as raised:
        pulled(root)
    (problem,) = raised.value.problems
    assert problem.startswith(f"{RULES}: questions.topic.labels.billing[0] (p0): uses {says}")
    assert "RE2 (the engine agents from a registry or a store run on) does not have: " in problem
    root = bounded_agent(tmp_path / "schema", schema={"pattern": pattern})
    with pytest.raises(AgentValidationError) as raised:
        pulled(root)
    (problem,) = raised.value.problems
    assert problem.startswith(f"input schema: properties/message/pattern: uses {says}")
    # The same pattern, in an agent the deployment owns, loads as before.
    load_agent(bounded_agent(tmp_path / "local", rules=[pattern], agent_id="local/sorter"))


@pytest.mark.parametrize(
    ("pattern", "says"),
    [
        (r"(?:a{1,50}){1,50}", "repeats nested inside each other multiply past 1,000"),
        (r"[\s\S]{0,1000}" * 40, "too large for RE2"),
    ],
)
def test_a_pattern_re2_cannot_compile_is_named(tmp_path: Path, pattern: str, says: str) -> None:
    with pytest.raises(AgentValidationError) as raised:
        pulled(bounded_agent(tmp_path, rules=[pattern]))
    (problem,) = raised.value.problems
    assert says in problem


def test_a_bounded_schema_may_not_hold_unevaluated_and_pattern_properties(
    tmp_path: Path,
) -> None:
    """jsonschema's `unevaluatedProperties` searches `patternProperties` with `re`."""
    schema = {"type": "object", "patternProperties": {"^x": {}}, "unevaluatedProperties": False}
    root = bounded_agent(tmp_path, schema={})
    document = json.loads((root / SCHEMA).read_text())
    document["properties"]["extra"] = schema
    (root / SCHEMA).write_text(json.dumps(document))
    with pytest.raises(AgentValidationError) as raised:
        pulled(root)
    (problem,) = raised.value.problems
    assert problem.startswith("input schema: properties/extra/unevaluatedProperties: ")
    load_agent(root)  # the same agent as the deployment's own: as before
    # Written down in the spec.
    pages = sorted((ROOT / "docs" / "spec").glob("agent-manifest*.md"))
    text = " ".join(" ".join(page.read_text().split()) for page in pages)
    assert "`unevaluatedProperties`" in text and "`unevaluatedItems`" in text


@pytest.mark.parametrize(
    ("pattern", "says", "never"),
    [
        (r"([a-z])\1", "e.g. (?:aa|bb) for ([ab])\\1", "write the group's pattern again"),
        (
            r"(?<!no )refill",
            "mark the rule negatable: true and list the word in negation_cues",
            "a class such as [^x]",
        ),
        (r"refill(?! now)", "say what must come there instead", "a class such as [^x]"),
    ],
)
def test_a_refused_pattern_says_what_to_write_instead(pattern: str, says: str, never: str) -> None:
    """The last check: "write the group's pattern again" turned `([a-z])\\1` into `[a-z][a-z]`,
    which matches "ab", and a negative lookbehind's advice never named negation_cues."""
    problem = patterns.problem(pattern, patterns.RULES_FLAGS, bounded=True)
    assert problem is not None and says in problem and never not in problem
    assert problem.endswith('(see "Patterns run on RE2" in docs/guides/authoring-an-agent.md)')


def test_a_deployment_without_bounded_agents_never_imports_re2(tmp_path: Path) -> None:
    """RE2, the rewriting and the caps are imported only for an agent the deployment does not
    own: the Vercel function (and every deployment until it runs one) loads none of them."""
    code = (
        "import sys, mendari.server.vercel, mendari.cli; "
        "print(sorted(m for m in ('re2', 'mendari.core.re2_syntax', 'mendari.core.caps') "
        "if m in sys.modules))"
    )
    ran = subprocess.run(  # in a scratch folder: the entrypoint's app opens var/mendari.db there
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert ran.stdout.strip() == "[]"


def test_a_bounded_agents_validator_checks_keys_and_texts_on_re2(tmp_path: Path) -> None:
    root = bounded_agent(tmp_path, schema={"pattern": r"^\S"})
    document = json.loads((root / SCHEMA).read_text())
    document["properties"]["codes"] = {
        "type": "object",
        "patternProperties": {r"^[a-z]+$": {"type": "integer"}},
        "propertyNames": {"pattern": r"^\w+$"},
        "additionalProperties": False,
    }
    (root / SCHEMA).write_text(json.dumps(document))
    agent = pulled(root)
    validator = input_schema.validator(agent.input_schema, bounded=True)
    assert type(validator) is not Draft202012Validator
    decision = Decision(agent)
    assert decision.validate({"message": "ok", "codes": {"ab": 1, "été": 2}}) == [
        "codes: has a field the schema does not allow"
    ]
    assert decision.validate({"message": " no"}) == [
        "message: does not match the pattern the schema requires"
    ]
    assert decision.validate({"message": "ok", "codes": {"ab": "x"}}) == [
        "codes/ab: must be of type integer"
    ]
    assert decision.validate({"message": "ok", "codes": {"a-b": 1}}) == [
        "codes: does not match the pattern the schema requires",  # propertyNames
        "codes: has fields the schema does not allow: a-b",
    ]
    messages = [e.message for e in validator.iter_errors({"message": "ok", "codes": {"A": 1}})]
    assert messages == ["'A' does not match any of the regexes: '^[a-z]+$'"]


def test_a_lone_surrogate_in_a_text_is_read_as_a_replacement_character(tmp_path: Path) -> None:
    agent = pulled(bounded_agent(tmp_path, rules=[r"bill.?\?"]))
    decision = Decision(agent)
    result = decision.decide({"message": "bill\ud800?"})
    assert result.answers["topic"].label == "billing"


# -- what the patterns mean ----------------------------------------------------------------------

# (pattern, flags, text): RE2 finds a match exactly where `re` does.
SAME = [
    (r"\bfever\b", re.I, "No FEVER today"),
    (r"\bmarché\b", 0, "au marché"),
    (r"\bmarché\b", 0, "au marchés"),
    (r"\b(?:fébrile|été)\b", 0, "un été chaud"),
    (r"\bé+\b", 0, "x ééé y"),
    (r"\b\w+\b", 0, "ñandú"),
    (r"\Bitis\b", re.I, "arthritis"),
    (r"\w+", 0, "naïve"),
    (r"\d+", 0, "pulse ٣٢"),
    (r"\s", 0, "a\u00a0b"),
    (r"\S+", 0, "\u3000x"),
    (r"(?a)\w+", 0, "été"),
    (r"(?a)\bete\b", 0, "été ete"),
    (r"[^\W\d_]+", 0, "42 да"),
    ("[^\\W\\x00-ɏ]+", 0, "abc я"),
    (r"foo$", 0, "foo\n"),
    (r"foo$", 0, "foo\nbar"),
    (r"(?m)^bar$", 0, "foo\nbar\nbaz"),
    (r"x\Z", 0, "x\n"),
    (r"\Ax", 0, "yx"),
    (r"a{,3}b", 0, "aaab"),
    (r"(?:01){0,3000}x", 0, "0101x"),
    (r"a{1500}", 0, "a" * 1_499 + "b" + "a" * 1_500),
    (r"a{1200,}b", 0, "a" * 1_300 + "b"),
    (r"a{2,1500}?b", 0, "c" + "a" * 1_501 + "b"),
    (r"(?i)ΣΑΣ", 0, "σας"),
    (r"(?i)straße", 0, "STRAẞE"),
    (r"kelvin", re.I, "\u212aelvin"),
    (r"(?i:A)a", 0, "aA Aa"),
    (r"(?-i:A)b", re.I, "aB AB"),
    (r"(?s:a.b)", 0, "a\nb"),
    (r"a.b", 0, "a\nb"),
    (r"a[^b]c", 0, "a\nc"),
    (r"(?x) a \  b # comment", 0, "a b"),
    (r"[\]\-^]+", 0, "x-]^"),
    (r"(?:a|ab)(?:c|bcd)", 0, "abcd"),
    (r"x*?y", 0, "xxy"),
    # Case, as `re` ignores it (the last check's findings): RE2 folds no case of its own.
    (r"i", re.I, "\u0131"),  # the dotless i
    (r"(?a)k", re.I, "\u212a"),  # the Kelvin sign, with (?a): ASCII only, as in `re`
    (r"ilaç", re.I, "İLAÇ KULLANIYORUM"),
    (r"[iı]laç", re.I, "İLAÇ"),  # noqa: RUF001
    (r"ağrı", re.I, "BAŞ AĞRISI"),  # noqa: RUF001
    (r"ishal", re.I, "İSHAL VAR"),
    (r"\bkriz\b", re.I, "KRİZ"),
    (r"(?:^|\W)πόνο(?:\W|$)", re.I, "οι πόνοι στο στήθος"),  # noqa: RUF001
    (r"πόνο\W", re.I, "πόνοι"),
    (r"[^\W\d_]+", re.I, "1ι"),  # noqa: RUF001
    (r"\w", re.I, "\u0345"),
    (r"[^a]", re.I, "A"),
    (r"(?i)[^\Wk]", 0, "\u212a1"),
]


@pytest.mark.parametrize(("pattern", "flags", "text"), SAME)
def test_re2_matches_where_re_does(pattern: str, flags: int, text: str) -> None:
    expected = re.search(pattern, text, flags)
    found = patterns.compile(pattern, flags, bounded=True).search(text)
    assert (found and found.start()) == (expected and expected.start()), translate(pattern, flags)


# (pattern, flags, text, RE2's answer): what still differs, written down in core/re2_syntax.py
# and the guide: a letter or digit outside ASCII just outside the match, whatever the pattern
# names next to the boundary.
DIFFERENT = [
    (r"\bpie", 0, "épie", True),
    (r"\bma\b", 0, "mañana", True),
    (r"\bрак\b", re.I, "практика", True),  # cancer, inside "practice"  # noqa: RUF001
    (r"\bболь\b", re.I, "больница", True),  # noqa: RUF001
    (r"\bπόνο\b", re.I, "πόνος", True),  # noqa: RUF001
    (r"\bmarché\b", re.I, "marchéñ", True),
]


@pytest.mark.parametrize(("pattern", "flags", "text", "on_re2"), DIFFERENT)
def test_the_differences_left_are_the_ones_written_down(
    pattern: str, flags: int, text: str, on_re2: bool
) -> None:
    assert bool(re.search(pattern, text, flags)) is not on_re2
    assert bool(patterns.compile(pattern, flags, bounded=True).search(text)) is on_re2
    written = " ".join((ROOT / "src" / "mendari" / "core" / "re2_syntax.py").read_text().split())
    assert '`\\bpie` also matches in "épie", `\\bma\\b` inside "mañana"' in written
    assert '`\\bрак\\b` inside "практика"' in written  # noqa: RUF001


def test_ignoring_case_matches_what_re_matches_for_every_class() -> None:
    """Every class `re` has, and letters case relates, with case ignored, on every character
    case relates and a sample of the rest: RE2 matches exactly the characters `re` matches."""
    every = "".join(chr(c) for c in range(0x3000) if not 0xD800 <= c <= 0xDFFF)
    every += "\u212a\u017f\u1fbe\u1e9e\U00010400\U00010428\U0001e900"
    for pattern in [
        r"\w",
        r"\W",
        r"\d",
        r"\D",
        r"\s",
        r"\S",
        r"[^\W\d_]",
        r"[\w-]",
        r"[^\s\d]",
        r"[a-z]",
        r"[^a-z]",
        r"[K]",
        r"[^ı]",  # noqa: RUF001
        r"i",
        r"I",
        r"ı",  # noqa: RUF001
        r"İ",
        r"ß",
        r"σ",  # noqa: RUF001
        r"ς",
        r"\u0345",
        r"µ",
        r".",
    ]:
        for flags in (re.I, re.I | re.A):
            expected = {m.start() for m in re.finditer(pattern, every, flags)}
            found = patterns.compile(pattern, flags, bounded=True)
            assert {m.start() for m in found.finditer(every)} == expected, (pattern, flags)


def maintained_agents() -> list[Any]:
    """The agents the deployment owns that the engine's tests read: the bundled demo agent and
    the test fixtures' agents (by their namespace, whatever else their folder holds)."""
    found = discover([SearchPath(FIXTURE_AGENTS, "maintained")]).agents
    fixtures = [agent for agent in found if agent.id.startswith(f"{FIXTURE_NAMESPACE}/")]
    return [*fixtures, load_agent(DEMO_AGENT_DIR)]


def maintained_cases() -> list[tuple[str, str, int, list[str]]]:
    """Every maintained pattern, with the texts of its agent's datasets and examples."""
    agents = maintained_agents()
    cases = []
    for agent in agents:
        texts: list[str] = []
        for path in sorted((agent.root / "data").glob("*.jsonl")):
            for line in path.read_text().splitlines():
                if line.strip():
                    texts += strings(json.loads(line))
        texts = sorted(set(texts))
        cases += [
            (agent.id, where, flags, [pattern, *texts])
            for where, pattern, flags in authored_patterns(agent)
        ]
    return cases


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in strings(item)]
    return []


def test_every_maintained_pattern_means_the_same_on_re2() -> None:
    """The agents the deployment owns stay on `re`; their patterns, rewritten for RE2, find the
    same first match in every text of their datasets (and match themselves where they did).
    Every one runs on RE2: a lookaround (or anything else RE2 cannot run) in the demo agent or
    a test fixture's agent fails here."""
    cases = maintained_cases()
    differ, refused, compared = [], [], 0
    for agent_id, where, flags, (pattern, *texts) in cases:
        problem = patterns.problem(pattern, flags, bounded=True)
        if problem:
            refused.append((agent_id, where, problem))
            continue
        compared += 1
        expected = re.compile(pattern, flags)
        found = patterns.compile(pattern, flags, bounded=True)
        for text in texts:
            a, b = expected.search(text), found.search(text)
            if (a and a.start()) != (b and b.start()):
                differ.append((agent_id, where, text[:60]))
    assert refused == [] and differ == []
    assert compared == len(cases) > 40
    # Each agent's patterns are among them.
    assert {agent_id for agent_id, *_ in cases} == {agent.id for agent in maintained_agents()}
