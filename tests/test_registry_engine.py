# SPDX-License-Identifier: Apache-2.0
"""Packs the deployment does not own run on the community engine (LP-16a, ADR-0009).

A pack pulled from a registry runs as a community pack's patterns do (#133): RE2, the load-time
caps, the schema keyword list, the input limits and a CPU budget per decision, then a person
decides. Unless its digest, the one `load_pack` hashed, is a reference pack's of the installed
release: a copy of a reference pack keeps the maintained engine. The engine is all it shares
with a community pack: its label, its automation and its policy are a registry pack's.
Maintained and local packs stay on `re`, unchanged.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import Decision, load_pack
from mendari.core import input_schema, patterns
from mendari.core.pack import Pack, PatternsRefused, pack_digest
from mendari.core.reference import bundled_dir, reference_digests
from mendari.core.registry import SearchPath, discover, resolve_pack
from mendari.core.spec import Outcome
from mendari.errors import PackValidationError
from mendari.evals.pack_tests import _run, input_problems, load_cases
from mendari.hub.client import packs_cache
from mendari.providers import build_provider
from mendari.server.app import create_app
from tests.conftest import INBOX_DIR, ROOT, cpu_time
from tests.test_community_budget import BOUND_S, EMOJI, fan_out, long_message, slow_rules
from tests.test_community_caps import sorter

PACKS = INBOX_DIR.parent
DATASETS = ROOT / "datasets"
BILL = {"message": "I have a question about my last bill."}
BLOCK_RULE = {
    "name": "blk",
    "when": {"question": "topic", "labels": ["billing"]},
    "outcome": "BLOCK",
    "reason": "Billing is blocked.",
}


def bundled() -> list[Path]:
    root = bundled_dir()
    assert root is not None
    return [d for d in sorted(root.iterdir()) if (d / "pack.yaml").is_file()]


def copied(
    base: Path,
    name: str,
    *,
    version: str | None = None,
    edit: Callable[[Path], None] | None = None,
) -> Path:
    """A copy of the reference pack `name` where a puller caches it, at `version` (its own
    unless given), with `edit` applied to the copy's files."""
    source = PACKS / name
    shipped = load_pack(source)
    root = base / "cache" / "mendari" / name / (version or shipped.version)
    shutil.copytree(source, root)
    if version is not None:
        manifest = root / "pack.yaml"
        text = manifest.read_text()
        manifest.write_text(text.replace(f"version: {shipped.version}", f"version: {version}", 1))
    if edit is not None:
        edit(root)
    return root


def one_more_line(relative: str) -> Callable[[Path], None]:
    """One changed byte, as far as behaviour goes: a comment line at the end of a file."""

    def edit(root: Path) -> None:
        path = root / relative
        path.write_text(path.read_text() + "# one more line\n")

    return edit


def yaml_edit(relative: str, change: Callable[[Any], None]) -> Callable[[Path], None]:
    def edit(root: Path) -> None:
        path = root / relative
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    return edit


def json_edit(relative: str, change: Callable[[Any], None]) -> Callable[[Path], None]:
    def edit(root: Path) -> None:
        path = root / relative
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))

    return edit


def decided(result: Any) -> dict[str, Any]:
    """What a decision says, without what differs from one run or one pack version to the next."""
    found = result.to_dict()
    for key in ("id", "created_at", "latency_ms", "pack"):
        found.pop(key)
    return dict(found)


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line)["input"] for line in path.read_text().splitlines() if line.strip()]


def outside_sorter(base: Path, **kwargs: Any) -> Pack:
    """The community fixture under an id outside the community namespace, pulled from a
    registry."""
    return load_pack(sorter(base, pack_id="someone/visit-sorter", **kwargs), origin="hub")


# -- the engine ------------------------------------------------------------------------------------


def test_a_pack_from_a_registry_runs_on_the_community_engine(tmp_path: Path) -> None:
    pack = load_pack(copied(tmp_path, "patient-inbox-router", version="9.9.9"), origin="hub")
    assert pack.bounded and not pack.community
    assert pack.origin_label == "From a registry"
    decision = Decision(pack)
    assert isinstance(decision._validator, input_schema._Re2Validator)
    rules: Any = decision.providers["safety_screen"]
    compiled = [
        regex
        for labels in rules._compiled.values()
        for found in labels.values()
        for _, regex in found
    ]
    assert compiled and all(isinstance(regex, patterns._Re2Pattern) for regex in compiled)
    maintained = Decision(load_pack(INBOX_DIR, origin="maintained"))
    assert not isinstance(maintained._validator, input_schema._Re2Validator)


@pytest.mark.parametrize(
    ("name", "local_site"),
    [
        ("patient-inbox-router", "local-site-example.jsonl"),
        ("prior-auth-preflight", "prior-auth-local-site-example.jsonl"),
    ],
)
def test_a_registry_copy_decides_the_shipped_inputs_as_the_reference_does(
    tmp_path: Path, name: str, local_site: str
) -> None:
    """The community engine changes no decision of these packs: on every behaviour test, the
    test split and the local-site example, the hub copy (a new version, so not the reference)
    decides with the same outcome, recommendation, priority, answers, evidence and reasons."""
    reference = Decision(load_pack(PACKS / name, origin="maintained"))
    hub = Decision(load_pack(copied(tmp_path, name, version="9.9.9"), origin="hub"))
    assert hub.pack.bounded and not reference.pack.bounded
    inputs = [case["input"] for case in load_cases(reference.pack)]
    inputs += jsonl(PACKS / name / "data" / "test.jsonl") + jsonl(DATASETS / local_site)
    differ = [
        number
        for number, given in enumerate(inputs)
        if decided(hub.decide(given)) != decided(reference.decide(given))
    ]
    assert len(inputs) > 100 and differ == []


def test_a_pulled_copy_of_a_reference_pack_keeps_the_maintained_engine(tmp_path: Path) -> None:
    """Every reference pack, the three with lookarounds included, copied as a pull caches it
    (with what the digest leaves out: an attestation and a hidden file), is the reference pack
    of this release: it loads from the registry cache on the maintained engine."""

    def extras(root: Path) -> None:
        (root / "evals").mkdir(exist_ok=True)
        (root / "evals" / "x.json").write_text("{}")
        (root / ".mendari-verified").write_text("sha256 of an archive\n")

    for directory in bundled():
        root = copied(tmp_path, directory.name, edit=extras)
        pack = load_pack(root, origin="hub")
        assert (pack.origin, pack.bounded) == ("hub", False), pack.id
        assert pack.digest == load_pack(directory).digest


def test_one_changed_byte_puts_a_reference_copy_on_the_community_engine(tmp_path: Path) -> None:
    root = copied(tmp_path, "referral-router", edit=one_more_line("logic/triage_rules.yaml"))
    with pytest.raises(PatternsRefused) as refused:
        load_pack(root, origin="hub")
    problems = refused.value.problems
    assert any("new-lung-mass" in problem and "lookahead" in problem for problem in problems)
    assert any("is not the copy of it in this release" in problem for problem in problems)
    assert not any("not the copy" in problem for problem in refused.value.patterns)
    local = load_pack(root, origin="local")  # an operator's own copy: owned, on `re`
    assert not local.bounded


def test_the_engine_follows_the_bytes_not_the_manifest(tmp_path: Path) -> None:
    """A changed copy that keeps the reference pack's id, version and `publisher.type` is not the
    reference pack: what chooses the engine is the digest `load_pack` hashed from its files."""
    reference = load_pack(INBOX_DIR)
    root = copied(tmp_path, "patient-inbox-router", edit=one_more_line("logic/routing_rules.yaml"))
    pack = load_pack(root, origin="hub")
    assert (pack.id, pack.version) == (reference.id, reference.version)
    assert pack.manifest.publisher.type == reference.manifest.publisher.type
    assert pack.digest == pack_digest(root) != reference.digest
    assert pack.bounded
    # Nothing written beside the files (a marker naming the reference digest) changes it.
    (root / ".mendari-verified").write_text(reference.digest + "\n")
    assert load_pack(root, origin="hub").bounded


# -- what the engine holds a registry pack to ------------------------------------------------------


def test_a_registry_pack_reads_at_most_10000_characters(tmp_path: Path) -> None:
    given = dict(jsonl(PACKS / "prior-auth-preflight" / "data" / "test.jsonl")[0])
    given["notes"] = "Low back pain. " * 1_000  # 15,000 characters: the schema allows 20,000
    hub = load_pack(copied(tmp_path, "prior-auth-preflight", version="9.9.9"), origin="hub")
    result = Decision(hub).decide(given)
    assert result.outcome is Outcome.BLOCK
    assert result.reasons == (
        "input rejected: notes: longer than 10,000 characters, the most a pack from a registry "
        "reads",
    )
    maintained = Decision(load_pack(PACKS / "prior-auth-preflight", origin="maintained"))
    reference = maintained.decide(given)
    assert not any(reason.startswith("input rejected") for reason in reference.reasons)


def test_a_registry_packs_rules_stop_at_the_budget_and_a_person_decides(tmp_path: Path) -> None:
    """As a community pack's (`tests/test_community_budget.py`): the rules fail as a provider
    that cannot answer does, and the decision never automates."""
    pack = outside_sorter(tmp_path, rules=slow_rules(), schema=long_message)
    assert pack.bounded and not pack.community
    with cpu_time() as spent:
        result = Decision(pack).decide({"message": EMOJI})
    assert spent() < BOUND_S
    assert set(result.errors.values()) == {"time_budget"}
    assert result.outcome in (Outcome.ABSTAIN, Outcome.HUMAN_REVIEW)
    assert result.outcome is not Outcome.AUTO


def nested_input(depth: int) -> dict[str, Any]:
    """An inbox input whose deepest value is `depth` levels down (the input itself is 1)."""
    deep: Any = 0
    for _ in range(depth - 2):
        deep = {"n": deep}
    return {"message": {"body": "hi"}, "patient": deep}


def test_a_registry_pack_refuses_an_input_too_large_before_its_schema_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At decide, as a community pack does: more than 10,000 values, nesting past 32 or texts
    past 10,000 characters are refused, in the registry pack's words, before its schema check
    (`Decision.validate`) runs. Called alone (`mendari omop`), its check holds to them too."""
    root = copied(tmp_path, "patient-inbox-router", version="9.9.9")
    hub = Decision(load_pack(root, origin="hub"))
    values = {"message": {"body": "hi"}, "patient": {"tags": [0] * 11_000}}
    long = {"message": {"body": "x" * 10_000, "subject": "y"}}
    expected = {
        "values": "(root): more than 10,000 values, the most a pack from a registry reads",
        "depth": "(root): nested more than 32 deep, the most a pack from a registry reads",
        "long": "the input's texts together: longer than 10,000 characters, the most a pack from "
        "a registry reads",
    }
    given = {"values": values, "depth": nested_input(33), "long": long}
    for name, inputs in given.items():
        assert hub.validate(inputs) == [expected[name]], name
    with monkeypatch.context() as patched:
        patched.setattr(Decision, "validate", lambda *_: pytest.fail("the schema check ran"))
        for name, inputs in given.items():
            result = hub.decide(inputs)
            assert result.outcome is Outcome.BLOCK, name
            assert result.reasons == (f"input rejected: {expected[name]}",), name
    within = hub.decide(nested_input(32))  # one level less: the schema decides
    assert not any("deep" in reason for reason in within.reasons)
    local = Decision(load_pack(copied(tmp_path / "local", "patient-inbox-router"), origin="local"))
    assert not any("the most" in reason for reason in local.decide(values).reasons)


def test_a_registry_packs_typo_corrections_never_lengthen_its_text_past_the_length(
    tmp_path: Path,
) -> None:
    """As a community pack's: a correction that would make a text longer than 10,000 characters
    is not made, and correcting stops at the budget. A local copy corrects all, with no
    budget."""

    def tolerant(rules: dict[str, Any]) -> None:
        rules["typo_tolerance"] = {"min_length": 4}

    hub = outside_sorter(tmp_path / "hub", rules=tolerant)
    local = load_pack(sorter(tmp_path / "local", rules=tolerant, pack_id="someone/visit-sorter"))
    assert hub.bounded and not local.bounded
    text = "invoce " * 1_428  # 9,996 characters
    reader: Any = build_provider(hub, "reader")
    capped, fixes = reader._normalize(text)
    assert len(capped) <= 10_000 and fixes and "invoce" in capped
    owned: Any = build_provider(local, "reader")
    full, all_fixes = owned._normalize(text)
    assert len(full) == 9_996 + 1_428 and len(all_fixes) == 1_428
    with patterns.time_budget(0):
        sum(range(100_000))  # a little CPU: the budget is spent
        with pytest.raises(patterns.OutOfTime):
            reader._normalize(text)
        assert owned._normalize(text)[0] == full


def test_a_registry_packs_schema_errors_stop_at_100_with_short_reasons(tmp_path: Path) -> None:
    """As a community pack's: its schema check stops collecting at 100 errors, and its reasons
    are short (a `required` names five fields and how many more). A local copy lists what it
    always listed."""

    def schema(found: dict[str, Any]) -> None:
        found["properties"]["xs"] = {"type": "array", "items": {"type": "integer"}}
        found["properties"]["meta"] = {"type": "object", "required": [f"n{i}" for i in range(9)]}

    hub = Decision(outside_sorter(tmp_path / "hub", schema=schema))
    root = sorter(tmp_path / "local", schema=schema, pack_id="someone/visit-sorter")
    local = Decision(load_pack(root))
    assert hub.pack.bounded and not local.pack.bounded

    class Counting:
        def __init__(self, inner: Any) -> None:
            self.inner, self.pulled = inner, 0

        def iter_errors(self, value: Any) -> Any:
            for error in self.inner.iter_errors(value):
                self.pulled += 1
                yield error

    for decision, pulled in ((hub, 100), (local, 150)):
        counting = Counting(decision._validator)
        decision._validator = counting  # type: ignore[assignment]
        found = decision.validate({"message": "my bill", "xs": ["a"] * 150})
        assert counting.pulled == pulled and len(found) == 10
    short = hub.decide({"message": "my bill", "meta": {}}).reasons
    assert short == ("input rejected: meta: n0, n1, n2, n3, n4 and 4 more is required",)
    listed = local.decide({"message": "my bill", "meta": {}}).reasons
    assert listed[0] == f"input rejected: meta: {', '.join(f'n{i}' for i in range(9))} is required"
    assert len(listed) == 9  # one per missing name, as before


def test_mendari_test_stops_a_registry_pack_at_the_budget(tmp_path: Path) -> None:
    """`mendari test` holds a registry pack to the budget as a decision does: checking its test
    inputs against a schema whose keywords fan out stops, and a case its rules run out of time
    on says why, in the registry pack's words. Checked alone (`mendari omop`), the schema check
    stops too."""

    def noted(schema: dict[str, Any]) -> None:
        schema["properties"]["note"] = {"type": "string", "$ref": "#/$defs/t0"}
        schema["$defs"] = fan_out(22)

    root = sorter(tmp_path / "schema", schema=noted, pack_id="someone/visit-sorter")
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text() + '- name: noted\n  input: {message: "my bill", note: x}\n'
        "  expect: {recommendation: billing}\n"
    )
    hub = load_pack(root, origin="hub")
    with cpu_time() as spent:
        found = input_problems(hub)
    assert spent() < BOUND_S
    assert any("checking its input against the schema stopped" in problem for problem in found)
    # Checked alone, outside a decision (`mendari omop`), its schema check has a budget too.
    with cpu_time() as spent:
        alone = Decision(hub).validate({"message": "my bill", "note": "x"})
    assert spent() < BOUND_S
    assert len(alone) == 1 and alone[0].startswith("(root): checking it against the schema stopped")
    slow = outside_sorter(tmp_path / "rules", rules=slow_rules(), schema=long_message)
    case = {"name": "slow", "input": {"message": EMOJI}, "expect": {"recommendation": "billing"}}
    failures = _run(Decision(slow), case).failures
    assert any("time_budget" in failure for failure in failures)
    assert any(
        "ran out of the 1 s time budget a pack from a registry has in each decision" in failure
        for failure in failures
    )


def test_a_registry_packs_schema_uses_only_the_keywords_mendari_bounds(tmp_path: Path) -> None:
    def unique(schema: dict[str, Any]) -> None:
        schema["properties"]["patient"]["properties"]["tags"] = {
            "type": "array",
            "uniqueItems": True,
        }

    edit = json_edit("schemas/input.schema.json", unique)
    root = copied(tmp_path, "patient-inbox-router", edit=edit)
    with pytest.raises(PackValidationError) as refused:
        load_pack(root, origin="hub")
    assert any(
        "uniqueItems" in problem and "a community or registry pack's schema may not use" in problem
        for problem in refused.value.problems
    )
    assert not load_pack(root, origin="local").bounded


def test_a_registry_packs_caps_are_checked_at_load(tmp_path: Path) -> None:
    edit = yaml_edit("logic/red_flags.yaml", lambda rules: rules.update(negation_window=300))
    root = copied(tmp_path, "patient-inbox-router", edit=edit)
    with pytest.raises(PackValidationError) as refused:
        load_pack(root, origin="hub")
    assert refused.value.problems == [
        "logic/red_flags.yaml: negation_window is 300; a community or registry pack's is at most "
        "200 characters (the shipped packs use 30 to 40)"
    ]
    assert not load_pack(root, origin="local").bounded


def test_a_registry_pack_is_never_a_community_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs on the community engine and nothing more: it automates under its own policy,
    its pinned model stands and its own BLOCK rule blocks; it is labelled as from a registry."""
    monkeypatch.setenv("LLM_MODEL", "example/deployment-model")
    pack = outside_sorter(tmp_path / "plain")
    assert pack.bounded and not pack.community and pack.shown_origin == "hub"
    assert Decision(pack).decide(BILL).outcome is Outcome.AUTO
    assert build_provider(pack, "reader", "llm").info.model == "example/pinned-model"

    def blocks(manifest: dict[str, Any]) -> None:
        manifest["policy"]["rules"] = [BLOCK_RULE]

    blocking = outside_sorter(tmp_path / "blocking", manifest=blocks)
    assert Decision(blocking).decide(BILL).outcome is Outcome.BLOCK


def test_discover_lists_a_refused_registry_pack_and_serves_the_others(tmp_path: Path) -> None:
    referral = copied(tmp_path, "referral-router", version="9.9.9")
    copied(tmp_path, "patient-inbox-router", version="9.9.9")
    paths = [SearchPath(tmp_path / "cache", "hub"), SearchPath(PACKS, "maintained")]
    found = discover(paths)
    assert set(found.problems) == {str(referral.resolve())}
    assert "lookahead" in found.problems[str(referral.resolve())]
    served = {(pack.id, pack.version): pack for pack in found.packs}
    assert served[("mendari/patient-inbox-router", "9.9.9")].bounded
    shipped = load_pack(PACKS / "referral-router").version
    assert not served[("mendari/referral-router", shipped)].bounded
    assert ("mendari/referral-router", "9.9.9") not in served
    client = TestClient(create_app(db_path=tmp_path / "e.db", packs_paths=paths))
    assert str(referral.resolve()) in client.get("/api/health").json()["invalid"]
    detail = client.get("/api/packs/mendari/referral-router").json()
    assert detail["manifest"]["version"] == shipped
    detail = client.get("/api/packs/mendari/patient-inbox-router").json()
    assert detail["manifest"]["version"] == "9.9.9"


def test_decision_from_path_takes_the_origin_of_the_path(tmp_path: Path) -> None:
    """H3: a pulled pack loaded by its path (`Decision.from_path`) is still a pack from a
    registry, on its engine; a reference pack by its path is maintained."""
    cached = packs_cache() / "mendari" / "patient-inbox-router" / "9.9.9"
    shutil.copytree(copied(tmp_path, "patient-inbox-router", version="9.9.9"), cached)
    pulled = Decision.from_path(cached).pack
    assert (pulled.origin, pulled.bounded) == ("hub", True)
    reference = Decision.from_path(INBOX_DIR).pack
    assert (reference.origin, reference.bounded) == ("maintained", False)


def test_the_reference_digests_are_the_installed_releases() -> None:
    expected = {pack_digest(directory) for directory in bundled()}
    assert reference_digests() == expected and len(expected) == 5
    assert {resolve_pack(f"mendari/{d.name}").digest for d in bundled()} == expected


def test_owned_packs_stay_on_re(tmp_path: Path) -> None:
    """Reference packs, and an operator's local packs (a changed copy included), run on `re` as
    before: no caps, no budget, and a pattern that backtracks still loads."""
    for directory in bundled():
        assert not load_pack(directory, origin="maintained").bounded
        assert not resolve_pack(f"mendari/{directory.name}").bounded
    changed = copied(tmp_path, "referral-router", edit=one_more_line("logic/triage_rules.yaml"))
    assert not load_pack(changed, origin="local").bounded

    def backtracks(rules: Any) -> None:
        labels = rules["questions"]["destination"]["labels"]["front_desk"]
        labels.append({"name": "slow", "pattern": "(a+)+$", "weight": 1.0})

    edit = yaml_edit("logic/routing_rules.yaml", backtracks)
    local = load_pack(copied(tmp_path / "slow", "patient-inbox-router", edit=edit), origin="local")
    assert not local.bounded
    provider: Any = build_provider(local, "router", "rules")
    regexes = [
        regex
        for labels in provider._compiled.values()
        for found in labels.values()
        for _, regex in found
    ]
    assert all(isinstance(regex, re.Pattern) for regex in regexes)


def test_mendari_test_runs_a_registry_pack_on_its_engine(tmp_path: Path) -> None:
    """`mendari test` checks a registry pack's behaviour-test inputs as a decision would: within
    the input limits of the engine it runs on."""

    def big_case(cases: Any) -> None:
        cases.append(
            {
                "name": "a very large input",
                "input": {"message": {"body": "hi"}, "patient": {"tags": [0] * 11_000}},
                "expect": {"outcome": "HUMAN_REVIEW"},
            }
        )

    root = copied(tmp_path, "patient-inbox-router", edit=yaml_edit("tests/cases.yaml", big_case))
    hub = load_pack(root, origin="hub")
    assert any(
        "its input is too large: more than 10,000 values, the most a pack from a registry reads"
        in problem
        for problem in input_problems(hub)
    )
    local = load_pack(root, origin="local")
    assert not any("too large" in problem for problem in input_problems(local))
