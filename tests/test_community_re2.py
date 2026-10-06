"""A community pack's patterns run on RE2, which never backtracks (plan B11; core/patterns.py,
core/re2_syntax.py): its rules and its input schema's `pattern`, `patternProperties` and
`additionalProperties` (and so `propertyNames`). Every other pack's run on `re`, exactly as
before. A pattern RE2 cannot run makes a community pack invalid at load, saying what to write
instead."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from jsonschema import Draft202012Validator

from mendari import Decision, load_pack
from mendari.core import input_schema, patterns
from mendari.core.pack import authored_patterns
from mendari.core.re2_syntax import boundary_problem, translate
from mendari.core.registry import SearchPath, discover
from mendari.errors import PackValidationError
from mendari.providers.rules import RulesProvider
from tests.conftest import ROOT

SORTER = ROOT / "tests" / "fixtures" / "community-packs" / "visit-sorter"
RULES = "logic/rules.yaml"
SCHEMA = "schemas/input.schema.json"

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


def community_pack(
    base: Path,
    *,
    rules: Sequence[str] = (),
    schema: dict[str, Any] | None = None,
    pack_id: str = "community/visit-sorter",
) -> Path:
    """A copy of the community fixture under `base/community-packs/`, with `rules` as its
    billing patterns, its message up to 10,000 characters, and `schema` merged into the
    message's schema."""
    root = base / "community-packs" / pack_id.partition("/")[2]
    shutil.copytree(SORTER, root)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["id"] = pack_id
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    if rules:
        config = yaml.safe_load((root / RULES).read_text())
        config["questions"]["topic"]["labels"]["billing"] = [
            {"name": f"p{i}", "pattern": text, "weight": 8} for i, text in enumerate(rules)
        ]
        (root / RULES).write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    document = json.loads((root / SCHEMA).read_text())
    document["properties"]["message"]["maxLength"] = 10_000
    document["properties"]["message"].update(schema or {})
    (root / SCHEMA).write_text(json.dumps(document, ensure_ascii=False))
    return root


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
def test_a_community_packs_rules_decide_slow_patterns_fast(
    tmp_path: Path, pattern: str, text: str
) -> None:
    pack = load_pack(community_pack(tmp_path, rules=[pattern]), origin="community")
    decision = Decision(pack)
    timed(decision, {"message": "warm"})  # translation tables, compiled once a process
    seconds, result = timed(decision, {"message": text[:9_990]})  # with the key: 9,997
    assert seconds < LIMIT_S, f"{seconds:.2f} s"
    assert not result.rejected


@pytest.mark.parametrize(("pattern", "text"), SLOW, ids=IDS)
def test_a_community_packs_schema_patterns_decide_slow_patterns_fast(
    tmp_path: Path, pattern: str, text: str
) -> None:
    schema = {"not": {"pattern": pattern}}  # runs the pattern on every message
    root = community_pack(tmp_path, schema=schema)
    document = json.loads((root / SCHEMA).read_text())
    document["properties"]["extra"] = {"type": "object", "patternProperties": {pattern: {}}}
    document["properties"]["names"] = {"type": "object", "propertyNames": {"pattern": pattern}}
    (root / SCHEMA).write_text(json.dumps(document, ensure_ascii=False))
    decision = Decision(load_pack(root, origin="community"))
    timed(decision, {"message": "warm"})
    part = text[:3_300]  # the message and two keys: 9,900 characters, with the keys' names
    inputs = {"message": part, "extra": {part + "1": 1}, "names": {part + "2": 1}}
    seconds, result = timed(decision, inputs)
    assert seconds < LIMIT_S, f"{seconds:.2f} s"
    assert not any("characters" in problem for problem in result.schema_problems)


def test_the_same_patterns_stay_on_re_in_any_other_pack(tmp_path: Path) -> None:
    """Never run on the slow texts: `re` would take minutes."""
    root = community_pack(tmp_path, rules=[p for p, _ in SLOW], pack_id="local/visit-sorter")
    pack = load_pack(root)
    assert not pack.community
    for text, _ in SLOW:
        assert type(patterns.compile(text, patterns.RULES_FLAGS)) is re.Pattern
    decision = Decision(pack)
    compiled = billing_patterns(decision)
    assert all(type(regex) is re.Pattern for regex in compiled)
    assert [regex.pattern for regex in compiled] == [p for p, _ in SLOW]
    assert decision.decide({"message": "a question?"}).answers["topic"].label == "billing"
    validator = input_schema.validator(pack.input_schema)
    assert type(validator) is Draft202012Validator


def test_a_community_packs_rules_run_on_re2(tmp_path: Path) -> None:
    pack = load_pack(community_pack(tmp_path, rules=[r"\bbill(?:ing)?\b"]), origin="community")
    decision = Decision(pack)
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
def test_a_pattern_re2_cannot_run_makes_a_community_pack_invalid(
    tmp_path: Path, pattern: str, says: str
) -> None:
    root = community_pack(tmp_path, rules=[pattern])
    with pytest.raises(PackValidationError) as raised:
        load_pack(root, origin="community")
    (problem,) = raised.value.problems
    assert problem.startswith(f"{RULES}: questions.topic.labels.billing[0] (p0): uses {says}")
    assert "RE2 (the engine a community pack's patterns run on) does not have: " in problem
    root = community_pack(tmp_path / "schema", schema={"pattern": pattern})
    with pytest.raises(PackValidationError) as raised:
        load_pack(root, origin="community")
    (problem,) = raised.value.problems
    assert problem.startswith(f"input schema: properties/message/pattern: uses {says}")
    # The same pattern, outside a community pack, loads as before.
    load_pack(community_pack(tmp_path / "local", rules=[pattern], pack_id="local/sorter"))


@pytest.mark.parametrize(
    ("pattern", "says"),
    [
        (r"(?:a{1,50}){1,50}", "repeats nested inside each other multiply past 1,000"),
        (r"[\s\S]{0,1000}" * 40, "too large for RE2"),
    ],
)
def test_a_pattern_re2_cannot_compile_is_named(tmp_path: Path, pattern: str, says: str) -> None:
    with pytest.raises(PackValidationError) as raised:
        load_pack(community_pack(tmp_path, rules=[pattern]), origin="community")
    (problem,) = raised.value.problems
    assert says in problem


def test_a_community_schema_may_not_hold_unevaluated_and_pattern_properties(
    tmp_path: Path,
) -> None:
    """jsonschema's `unevaluatedProperties` searches `patternProperties` with `re`."""
    schema = {"type": "object", "patternProperties": {"^x": {}}, "unevaluatedProperties": False}
    root = community_pack(tmp_path, schema={})
    document = json.loads((root / SCHEMA).read_text())
    document["properties"]["extra"] = schema
    (root / SCHEMA).write_text(json.dumps(document))
    with pytest.raises(PackValidationError) as raised:
        load_pack(root, origin="community")
    (problem,) = raised.value.problems
    assert problem.startswith("input schema: properties/extra/unevaluatedProperties: ")
    with pytest.raises(PackValidationError):  # its id is in community/, wherever it is
        load_pack(root, origin="local")
    root = community_pack(tmp_path / "local", schema={}, pack_id="local/sorter")
    (root / SCHEMA).write_text(json.dumps(document))
    load_pack(root)  # any other pack: as before
    # Written down where a contributor reads the limits.
    for doc in ("docs/guides/contributing-a-pack.md", "docs/spec/decision-pack-0.2.md"):
        text = " ".join((ROOT / doc).read_text().split())
        assert "`unevaluatedProperties`" in text and "`unevaluatedItems`" in text, doc


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
    problem = patterns.problem(pattern, patterns.RULES_FLAGS, community=True)
    assert problem is not None and says in problem and never not in problem
    assert problem.endswith('(see "Patterns run on RE2" in docs/guides/contributing-a-pack.md)')


def test_a_deployment_without_community_packs_never_imports_re2() -> None:
    """RE2, the rewriting and the caps are imported only for a community pack: the Vercel
    function (and every deployment, community packs being off by default) loads none of them."""
    code = (
        "import sys, mendari.server.vercel, mendari.cli; "
        "print(sorted(m for m in ('re2', 'mendari.core.re2_syntax', 'mendari.core.caps') "
        "if m in sys.modules))"
    )
    ran = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert ran.stdout.strip() == "[]"


def test_a_community_packs_validator_checks_keys_and_texts_on_re2(tmp_path: Path) -> None:
    root = community_pack(tmp_path, schema={"pattern": r"^\S"})
    document = json.loads((root / SCHEMA).read_text())
    document["properties"]["codes"] = {
        "type": "object",
        "patternProperties": {r"^[a-z]+$": {"type": "integer"}},
        "propertyNames": {"pattern": r"^\w+$"},
        "additionalProperties": False,
    }
    (root / SCHEMA).write_text(json.dumps(document))
    pack = load_pack(root, origin="community")
    validator = input_schema.validator(pack.input_schema, community=True)
    assert type(validator) is not Draft202012Validator
    decision = Decision(pack)
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
    pack = load_pack(community_pack(tmp_path, rules=[r"bill.?\?"]), origin="community")
    decision = Decision(pack)
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
    found = patterns.compile(pattern, flags, community=True).search(text)
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
    assert bool(patterns.compile(pattern, flags, community=True).search(text)) is on_re2
    written = " ".join((ROOT / "src" / "mendari" / "core" / "re2_syntax.py").read_text().split())
    assert '`\\bpie` also matches in "épie", `\\bma\\b` inside "mañana"' in written
    assert '`\\bрак\\b` inside "практика"' in written  # noqa: RUF001
    guide = " ".join((ROOT / "docs" / "guides" / "contributing-a-pack.md").read_text().split())
    assert '`\\bpie` also matches in "épie"' in guide
    assert '`\\bрак\\b` (cancer) also matches inside "практика"' in guide  # noqa: RUF001
    assert "work as in `re`" not in guide  # the claim the last check disproved
    # community-check warns on each next to a letter the pattern names outside ASCII
    assert boundary_problem(pattern, flags) is not pattern.isascii()


def test_community_check_warns_on_a_boundary_next_to_a_letter_outside_ascii(
    tmp_path: Path,
) -> None:
    from mendari.community.check import check

    root = community_pack(tmp_path, rules=[r"\bрак\b", r"\bpain\b", r"(?:^|\W)боль(?:\W|$)"])  # noqa: RUF001
    warned = [f.message for f in check(root).warnings if f.rule == "patterns"]
    assert len(warned) == 1 and warned[0].startswith(
        f"{RULES}: questions.topic.labels.billing[0] (p0): a word boundary"
    )
    assert '\\bрак\\b also matches inside "практика"' in warned[0]  # noqa: RUF001


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
            found = patterns.compile(pattern, flags, community=True)
            assert {m.start() for m in found.finditer(every)} == expected, (pattern, flags)


def maintained_cases() -> list[tuple[str, str, int, list[str]]]:
    """Every maintained pattern, with the texts of its pack's datasets and examples."""
    packs = discover([SearchPath(ROOT / "packs", "maintained")]).packs
    cases = []
    for pack in packs:
        texts: list[str] = []
        for path in sorted((pack.root / "data").glob("*.jsonl")):
            for line in path.read_text().splitlines():
                if line.strip():
                    texts += strings(json.loads(line))
        texts = sorted(set(texts))
        cases += [
            (pack.id, where, flags, [pattern, *texts])
            for where, pattern, flags in authored_patterns(pack)
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
    """The maintained packs stay on `re`; their patterns, rewritten for RE2, find the same
    first match in every text of their datasets (and match themselves where they did)."""
    cases = maintained_cases()
    differ, compared = [], 0
    for pack_id, where, flags, (pattern, *texts) in cases:
        expected = re.compile(pattern, flags)
        if patterns.problem(pattern, flags, community=True):
            continue  # a lookaround: maintained packs may use them
        compared += 1
        found = patterns.compile(pattern, flags, community=True)
        for text in texts:
            a, b = expected.search(text), found.search(text)
            if (a and a.start()) != (b and b.start()):
                differ.append((pack_id, where, text[:60]))
    assert compared > 50 and differ == []
