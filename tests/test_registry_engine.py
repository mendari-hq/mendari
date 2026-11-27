# SPDX-License-Identifier: Apache-2.0
"""Agents the deployment does not own run on the bounded engine (LP-16a, ADR-0009).

An agent pulled from a registry runs its patterns on RE2 (#133), with the load-time caps, the
schema keyword list, the input limits and a CPU budget per decision, then a person decides.
Unless its digest, the one `load_agent` hashed, is a reference agent's of the installed release: a
copy of a reference agent keeps the maintained engine. The engine is all that sets it apart: its
label, its automation and its policy are a registry agent's. Maintained and local agents stay on
`re`, unchanged.

In the suite the test fixtures' agents stand where a release's bundled agents do (`conftest.py`),
so they are its reference agents: the triage fixture, whose patterns all run on RE2, and the
others beside it.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import Decision, load_agent
from mendari.core import input_schema, patterns
from mendari.core.agent import Agent, PatternsRefused, agent_digest
from mendari.core.reference import bundled_dir, reference_agents, reference_digests
from mendari.core.registry import SearchPath, discover, resolve_agent, version_key
from mendari.core.spec import Outcome
from mendari.errors import AgentValidationError
from mendari.evals.behaviour_tests import _run, load_cases
from mendari.evals.validation import input_problems
from mendari.hub.client import agents_cache
from mendari.providers import build_provider
from mendari.server.app import create_app
from tests.conftest import (
    CHART_DIR,
    CLINICAL_DIR,
    DEMO_AGENT_DIR,
    FIXTURE_AGENTS,
    TRIAGE_DIR,
    cpu_time,
)
from tests.support.registry import (
    BILL,
    BLOCK_RULE,
    BOUND_S,
    EMOJI,
    ROUTER_RULES,
    SCHEMA,
    SCREEN_RULES,
    TRIAGE,
    fan_out,
    long_message,
    outside_agent,
    slow_rules,
    with_lookahead,
)

AGENTS = FIXTURE_AGENTS  # where the reference agents of the suite's release are


def bundled() -> list[Path]:
    root = bundled_dir()
    assert root is not None
    return [d for d in sorted(root.iterdir()) if (d / "agent.yaml").is_file()]


def copied(
    base: Path,
    name: str,
    *,
    version: str | None = None,
    edit: Callable[[Path], None] | None = None,
) -> Path:
    """A copy of the reference agent in the folder `name` where a puller caches it, at `version`
    (its own unless given), with `edit` applied to the copy's files."""
    source = AGENTS / name
    shipped = load_agent(source)
    root = base / "cache" / shipped.id / (version or shipped.version)
    shutil.copytree(source, root)
    if version is not None:
        manifest = root / "agent.yaml"
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
    """What a decision says, without what differs from one run or one agent version to the next."""
    found = result.to_dict()
    for key in ("id", "created_at", "latency_ms", "agent"):
        found.pop(key)
    return dict(found)


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line)["input"] for line in path.read_text().splitlines() if line.strip()]


def outside_sorter(base: Path, **changes: Any) -> Agent:
    """An outside publisher's agent (`tests/support/registry.py` `outside_agent`), as pulled from
    a registry: an `Agent` made afresh of its fields, nothing cached in it by its loading
    (`tests/test_registry_recalls.py` copies one through its `__dict__`)."""
    return replace(load_agent(outside_agent(base, **changes), origin="hub"))


def lookahead(root: Path) -> None:
    """A changed rule: one more router pattern, a lookahead (`with_lookahead`)."""
    with_lookahead(root)


# -- the engine ------------------------------------------------------------------------------------


def test_an_agent_from_a_registry_runs_on_the_bounded_engine(tmp_path: Path) -> None:
    agent = load_agent(copied(tmp_path, TRIAGE_DIR.name, version="9.9.9"), origin="hub")
    assert agent.bounded
    assert agent.origin_label == "From a registry"
    decision = Decision(agent)
    assert isinstance(decision._validator, input_schema._Re2Validator)
    rules: Any = decision.providers["screen"]
    compiled = [
        regex
        for labels in rules._compiled.values()
        for found in labels.values()
        for _, regex in found
    ]
    assert compiled and all(isinstance(regex, patterns._Re2Pattern) for regex in compiled)
    maintained = Decision(load_agent(TRIAGE_DIR, origin="maintained"))
    assert not isinstance(maintained._validator, input_schema._Re2Validator)


@pytest.mark.parametrize("name", [path.name for path in (TRIAGE_DIR, CLINICAL_DIR, CHART_DIR)])
def test_a_registry_copy_decides_the_shipped_inputs_as_the_reference_does(
    tmp_path: Path, name: str
) -> None:
    """The bounded engine changes no decision of these agents: on every behaviour test and every
    case of every dataset they ship, the hub copy (a new version, so not the reference) decides
    with the same outcome, recommendation, priority, answers, evidence and reasons."""
    reference = Decision(load_agent(AGENTS / name, origin="maintained"))
    # The engine compared, not the origin cap (LP-18d): the agent's own policy on both.
    hub = Decision(
        load_agent(copied(tmp_path, name, version="9.9.9"), origin="hub"), origin_cap=False
    )
    assert hub.agent.bounded and not reference.agent.bounded
    inputs = [case["input"] for case in load_cases(reference.agent)]
    for path in sorted((AGENTS / name / "data").glob("*.jsonl")):
        inputs += jsonl(path)
    differ = [
        number
        for number, given in enumerate(inputs)
        if decided(hub.decide(given)) != decided(reference.decide(given))
    ]
    assert len(inputs) > 100 and differ == []


def test_a_pulled_copy_of_a_reference_agent_keeps_the_maintained_engine(tmp_path: Path) -> None:
    """Every reference agent, copied as a pull caches it (with what the digest leaves out: an
    attestation and a hidden file), is the reference agent of this release: it loads from the
    registry cache on the maintained engine."""

    def extras(root: Path) -> None:
        (root / "evals").mkdir(exist_ok=True)
        (root / "evals" / "x.json").write_text("{}")
        (root / ".mendari-verified").write_text("sha256 of an archive\n")

    for directory in bundled():
        root = copied(tmp_path, directory.name, edit=extras)
        agent = load_agent(root, origin="hub")
        assert (agent.origin, agent.bounded) == ("hub", False), agent.id
        assert agent.digest == load_agent(directory).digest


def test_one_changed_byte_puts_a_reference_copy_on_the_bounded_engine(tmp_path: Path) -> None:
    """One changed byte makes a copy of a reference agent not the reference agent: at another
    version it runs on the bounded engine (where the lookahead the change added cannot run),
    and at the reference agent's own version it does not load at all (F: it would take its
    place)."""
    root = copied(tmp_path, TRIAGE_DIR.name, version="9.9.9", edit=lookahead)
    with pytest.raises(PatternsRefused) as refused:
        load_agent(root, origin="hub")
    problems = refused.value.problems
    assert any("a-refill-not-a-question" in p and "lookahead" in p for p in problems)
    local = load_agent(root, origin="local")  # an operator's own copy: owned, on `re`
    assert not local.bounded
    same = copied(tmp_path / "same", TRIAGE_DIR.name, edit=lookahead)
    with pytest.raises(AgentValidationError) as shadow:
        load_agent(same, origin="hub")
    assert not isinstance(shadow.value, PatternsRefused)
    assert "never takes a bundled agent's place" in shadow.value.problems[0]
    assert not load_agent(same, origin="local").bounded


def test_a_changed_copy_of_an_agent_mendari_maintains_is_told_to_use_the_release_copy(
    tmp_path: Path, release_agents: None
) -> None:
    """An agent of Mendari's own namespace (the release's demo agent), changed and at another
    version, is refused for a pattern RE2 cannot run with what to do: use the copy this release
    bundles, or a version published for it. The patterns themselves are listed apart."""
    reference = load_agent(DEMO_AGENT_DIR)
    root = tmp_path / "cache" / reference.id / "9.9.9"
    shutil.copytree(DEMO_AGENT_DIR, root)
    manifest = root / "agent.yaml"
    manifest.write_text(
        manifest.read_text().replace(f"version: {reference.version}", "version: 9.9.9", 1)
    )

    def lookaround(rules: dict[str, Any]) -> None:
        labels = rules["questions"]["request"]["labels"]["refill"]
        labels.append({"name": "not-a-question", "pattern": "refill(?! question)", "weight": 1})

    yaml_edit("logic/rules.yaml", lookaround)(root)
    with pytest.raises(PatternsRefused) as refused:
        load_agent(root, origin="hub")
    problems = refused.value.problems
    assert any("not-a-question" in problem and "lookahead" in problem for problem in problems)
    told = f"{reference.id}@9.9.9 is not the copy of it in this release"
    assert any(told in problem for problem in problems)
    assert not any("not the copy" in problem for problem in refused.value.patterns)


def test_the_engine_follows_the_bytes_not_the_manifest(tmp_path: Path) -> None:
    """A changed copy that keeps the reference agent's id and `publisher.type` is not the
    reference agent: what chooses the engine is the digest `load_agent` hashed from its files. One
    that keeps its version too does not load at all (F), whatever a marker beside it says."""
    reference = TRIAGE
    edit = one_more_line(ROUTER_RULES)
    root = copied(tmp_path, TRIAGE_DIR.name, version="9.9.9", edit=edit)
    agent = load_agent(root, origin="hub")
    assert agent.id == reference.id
    assert agent.manifest.publisher.type == reference.manifest.publisher.type
    assert agent.digest == agent_digest(root) != reference.digest
    assert agent.bounded
    # Nothing written beside the files (a marker naming the reference digest) changes it.
    (root / ".mendari-verified").write_text(reference.digest + "\n")
    assert load_agent(root, origin="hub").bounded
    same = copied(tmp_path / "same", TRIAGE_DIR.name, edit=edit)
    (same / ".mendari-verified").write_text(reference.digest + "\n")
    shadow = load_agent(same, origin="local")
    assert (shadow.id, shadow.version) == (reference.id, reference.version)
    assert shadow.manifest.publisher.type == reference.manifest.publisher.type
    with pytest.raises(AgentValidationError, match="never takes a bundled agent's place"):
        load_agent(same, origin="hub")


# -- a pulled agent never takes a bundled agent's place (F) --------------------------------------


def shadow(base: Path, version: str | None = None) -> Path:
    """A copy of the triage fixture with one more line in its router's rules, at `version` (the
    reference agent's own unless given), where `mendari pull` caches it."""
    root = agents_cache() / TRIAGE.id / (version or TRIAGE.version)
    root.parent.mkdir(parents=True, exist_ok=True)
    edit = one_more_line(ROUTER_RULES)
    shutil.copytree(copied(base, TRIAGE_DIR.name, version=version, edit=edit), root)
    return root


def test_a_pulled_agent_never_takes_a_reference_agents_name_and_version(tmp_path: Path) -> None:
    """An agent from a registry with the id and version of a reference agent of this release but
    other files would be served in its place (the cache wins a version tie): it is refused at
    load, and the reason names it, the reference agent and the cached directory to remove."""
    reference = TRIAGE
    root = shadow(tmp_path)
    with pytest.raises(AgentValidationError) as refused:
        load_agent(root, origin="hub")
    (problem,) = refused.value.problems
    assert problem.startswith(
        f"{reference.id}@{reference.version} has the id and version of an agent this "
        f"release bundles ({reference.id}@{reference.version}) but other files (digest "
        f"{agent_digest(root)[:23]}): an agent from a registry never takes a bundled agent's place"
    )
    assert problem.endswith(f"; remove {root.resolve()} from the cache")
    # Checked where it is not cached (an archive being checked), it names no directory.
    edit = one_more_line(ROUTER_RULES)
    elsewhere = copied(tmp_path / "unpacked", TRIAGE_DIR.name, edit=edit)
    with pytest.raises(AgentValidationError) as unpacked:
        load_agent(elsewhere, origin="hub")
    assert "never takes a bundled agent's place" in unpacked.value.problems[0]
    assert "remove" not in unpacked.value.problems[0]


@pytest.mark.parametrize("tie", ["pre-release", "leading zero"])
def test_a_version_that_ties_a_reference_agents_is_refused_too(tmp_path: Path, tie: str) -> None:
    """Versions are compared as the search path orders them (`version_key`), not as text:
    `1.0.0-x` and `1.00.0` tie `1.0.0`, so either would take the reference agent's place."""
    reference = TRIAGE
    major, minor, patch = reference.version.split(".")
    version = f"{reference.version}-x" if tie == "pre-release" else f"{major}.0{minor}.{patch}"
    assert version != reference.version and version_key(version) == version_key(reference.version)
    with pytest.raises(AgentValidationError) as refused:
        load_agent(shadow(tmp_path, version), origin="hub")
    (problem,) = refused.value.problems
    assert problem.startswith(f"{reference.id}@{version} has the id and version of an agent")


def test_a_pulled_copy_of_a_reference_agent_and_a_newer_version_still_load(tmp_path: Path) -> None:
    """What F leaves as it was: an identical copy is the reference agent (the maintained engine),
    a changed copy at another version runs on the bounded engine, and an operator's local
    copy at the reference agent's version is its own choice."""
    reference = TRIAGE
    identical = load_agent(copied(tmp_path / "same", TRIAGE_DIR.name), origin="hub")
    assert (identical.bounded, identical.digest) == (False, reference.digest)
    edit = one_more_line(ROUTER_RULES)
    newer = load_agent(
        copied(tmp_path / "new", TRIAGE_DIR.name, version="9.9.9", edit=edit), origin="hub"
    )
    assert newer.bounded and newer.version == "9.9.9"
    local = load_agent(copied(tmp_path / "local", TRIAGE_DIR.name, edit=edit))
    assert (local.origin, local.bounded, local.version) == ("local", False, reference.version)
    assert local.digest != reference.digest


def test_a_shadow_in_the_cache_is_listed_and_the_reference_agent_is_served(tmp_path: Path) -> None:
    """One cached before this release refused it is listed (`invalid`), and the reference agent
    is served under its id, by version too, wherever agents are listed."""
    reference = TRIAGE
    root = shadow(tmp_path)
    paths = [SearchPath(agents_cache(), "hub"), SearchPath(AGENTS, "maintained")]
    found = discover(paths)
    assert "never takes a bundled agent's place" in found.problems[str(root.resolve())]
    for ref in (reference.id, f"{reference.id}@{reference.version}"):
        served = resolve_agent(ref, paths)
        assert (served.origin, served.digest) == ("maintained", reference.digest), ref
    client = TestClient(create_app(db_path=tmp_path / "s.db", agents_paths=paths))
    assert str(root.resolve()) in client.get("/api/health").json()["invalid"]
    detail = client.get(f"/api/agents/{reference.id}").json()
    assert (detail["digest"], detail["origin"]) == (reference.digest, "maintained")


def test_decision_from_path_refuses_a_cached_shadow(tmp_path: Path) -> None:
    root = shadow(tmp_path)
    with pytest.raises(AgentValidationError) as refused:
        Decision.from_path(root)
    assert f"remove {root.resolve()} from the cache" in refused.value.problems[0]


# -- what the engine holds a registry agent to
# ------------------------------------------------------


def test_a_registry_agent_reads_at_most_10000_characters(tmp_path: Path) -> None:
    """A copy whose schema takes a message twice that long: from a registry, it reads at most
    10,000 characters of it; owned, all of them."""

    def longer(schema: dict[str, Any]) -> None:
        schema["properties"]["message"]["properties"]["body"]["maxLength"] = 20_000

    edit = json_edit(SCHEMA, longer)
    root = copied(tmp_path, TRIAGE_DIR.name, version="9.9.9", edit=edit)
    given = {"message": {"body": "Low back pain. " * 1_000}}  # 15,000 characters
    result = Decision(load_agent(root, origin="hub")).decide(given)
    assert result.outcome is Outcome.BLOCK
    assert result.reasons == (
        "input rejected: message.body: longer than 10,000 characters, the most an agent from a "
        "registry reads",
    )
    owned = Decision(load_agent(root, origin="local")).decide(given)
    assert not any(reason.startswith("input rejected") for reason in owned.reasons)


def test_a_registry_agents_rules_stop_at_the_budget_and_a_person_decides(tmp_path: Path) -> None:
    """Its patterns share one CPU budget per decision: once it is spent, the rules fail as a
    provider that cannot answer does, and the decision never automates."""
    agent = outside_sorter(tmp_path, rules=slow_rules(), schema=long_message)
    assert agent.bounded
    with cpu_time() as spent:
        result = Decision(agent).decide({"message": {"body": EMOJI}})
    assert spent() < BOUND_S
    assert set(result.errors.values()) == {"time_budget"}
    assert result.outcome in (Outcome.ABSTAIN, Outcome.HUMAN_REVIEW)
    assert result.outcome is not Outcome.AUTO


def nested_input(depth: int) -> dict[str, Any]:
    """A triage input whose deepest value is `depth` levels down (the input itself is 1)."""
    deep: Any = 0
    for _ in range(depth - 2):
        deep = {"n": deep}
    return {"message": {"body": "hi"}, "patient": deep}


def test_a_registry_agent_refuses_an_input_too_large_before_its_schema_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At decide, as every agent on that engine: more than 10,000 values, nesting past 32 or texts
    past 10,000 characters are refused, in the registry agent's words, before its schema check
    (`Decision.validate`) runs. Called alone, its check holds to them too."""
    root = copied(tmp_path, TRIAGE_DIR.name, version="9.9.9")
    hub = Decision(load_agent(root, origin="hub"))
    values = {"message": {"body": "hi"}, "patient": {"tags": [0] * 11_000}}
    long = {"message": {"body": "x" * 10_000, "subject": "y"}}
    expected = {
        "values": "(root): more than 10,000 values, the most an agent from a registry reads",
        "depth": "(root): nested more than 32 deep, the most an agent from a registry reads",
        "long": "the input's texts together: longer than 10,000 characters, the most an agent from "
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
    local = Decision(load_agent(copied(tmp_path / "local", TRIAGE_DIR.name), origin="local"))
    assert not any("the most" in reason for reason in local.decide(values).reasons)


def test_a_registry_agents_typo_corrections_never_lengthen_its_text_past_the_length(
    tmp_path: Path,
) -> None:
    """On that engine, a correction that would make a text longer than 10,000 characters
    is not made, and correcting stops at the budget. A local copy corrects all, with no
    budget."""

    def tolerant(rules: dict[str, Any]) -> None:
        rules["typo_tolerance"] = {"min_length": 4}

    hub = outside_sorter(tmp_path / "hub", rules=tolerant)
    local = load_agent(outside_agent(tmp_path / "local", rules=tolerant))
    assert hub.bounded and not local.bounded
    text = "refil " * 1_666  # 9,996 characters, each word one letter short of "refill"
    reader: Any = build_provider(hub, "router")
    capped, fixes = reader._normalize(text)
    assert len(capped) <= 10_000 and fixes and "refil " in capped
    owned: Any = build_provider(local, "router")
    full, all_fixes = owned._normalize(text)
    assert len(full) == 9_996 + 1_666 and len(all_fixes) == 1_666
    with patterns.time_budget(0):
        sum(range(100_000))  # a little CPU: the budget is spent
        with pytest.raises(patterns.OutOfTime):
            reader._normalize(text)
        assert owned._normalize(text)[0] == full


def test_a_registry_agents_schema_errors_stop_at_100_with_short_reasons(tmp_path: Path) -> None:
    """On that engine, its schema check stops collecting at 100 errors, and its reasons
    are short (a `required` names five fields and how many more). A local copy lists what it
    always listed."""

    def schema(found: dict[str, Any]) -> None:
        found["properties"]["xs"] = {"type": "array", "items": {"type": "integer"}}
        found["properties"]["meta"] = {"type": "object", "required": [f"n{i}" for i in range(9)]}

    hub = Decision(outside_sorter(tmp_path / "hub", schema=schema))
    local = Decision(load_agent(outside_agent(tmp_path / "local", schema=schema)))
    assert hub.agent.bounded and not local.agent.bounded

    class Counting:
        def __init__(self, inner: Any) -> None:
            self.inner, self.pulled = inner, 0

        def iter_errors(self, value: Any) -> Any:
            for error in self.inner.iter_errors(value):
                self.pulled += 1
                yield error

    bill = {"body": "my bill"}
    for decision, pulled in ((hub, 100), (local, 150)):
        counting = Counting(decision._validator)
        decision._validator = counting  # type: ignore[assignment]
        found = decision.validate({"message": bill, "xs": ["a"] * 150})
        assert counting.pulled == pulled and len(found) == 10
    short = hub.decide({"message": bill, "meta": {}}).reasons
    assert short == ("input rejected: meta: n0, n1, n2, n3, n4 and 4 more is required",)
    listed = local.decide({"message": bill, "meta": {}}).reasons
    # Every missing name, once (not once per missing name: `tests/test_decision.py`).
    assert listed == (f"input rejected: meta: {', '.join(f'n{i}' for i in range(9))} is required",)


def test_mendari_test_stops_a_registry_agent_at_the_budget(tmp_path: Path) -> None:
    """`mendari test` holds a registry agent to the budget as a decision does: checking its test
    inputs against a schema whose keywords fan out stops, and a case its rules run out of time
    on says why, in the registry agent's words. Checked alone, outside a decision, the schema
    check stops too."""

    def noted(schema: dict[str, Any]) -> None:
        schema["properties"]["note"] = {"type": "string", "$ref": "#/$defs/t0"}
        schema["$defs"] = fan_out(22)

    root = outside_agent(tmp_path / "schema", schema=noted)
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text() + '\n- name: noted\n  input: {message: {body: "my bill"}, note: x}\n'
        "  expect: {recommendation: billing}\n"
    )
    hub = load_agent(root, origin="hub")
    with cpu_time() as spent:
        found = [str(problem) for problem in input_problems(hub)]
    assert spent() < BOUND_S
    assert any("checking its input against the schema stopped" in problem for problem in found)
    # Checked alone, outside a decision, its schema check has a budget too.
    with cpu_time() as spent:
        alone = Decision(hub).validate({"message": {"body": "my bill"}, "note": "x"})
    assert spent() < BOUND_S
    assert len(alone) == 1 and alone[0].startswith("(root): checking it against the schema stopped")
    slow = outside_sorter(tmp_path / "rules", rules=slow_rules(), schema=long_message)
    case = {
        "name": "slow",
        "input": {"message": {"body": EMOJI}},
        "expect": {"recommendation": "billing"},
    }
    failures = _run(Decision(slow), case).failures
    assert any("time_budget" in failure for failure in failures)
    assert any(
        "ran out of the 1 s time budget an agent from a registry has in each decision" in failure
        for failure in failures
    )


def test_a_registry_agents_schema_uses_only_the_keywords_mendari_bounds(tmp_path: Path) -> None:
    def unique(schema: dict[str, Any]) -> None:
        schema["properties"]["tags"] = {"type": "array", "uniqueItems": True}

    edit = json_edit(SCHEMA, unique)
    root = copied(tmp_path, TRIAGE_DIR.name, version="9.9.9", edit=edit)
    with pytest.raises(AgentValidationError) as refused:
        load_agent(root, origin="hub")
    assert any(
        "uniqueItems" in problem and "a registry or store agent's schema may not use" in problem
        for problem in refused.value.problems
    )
    assert not load_agent(root, origin="local").bounded


def test_a_registry_agents_caps_are_checked_at_load(tmp_path: Path) -> None:
    edit = yaml_edit(SCREEN_RULES, lambda rules: rules.update(negation_window=300))
    root = copied(tmp_path, TRIAGE_DIR.name, version="9.9.9", edit=edit)
    with pytest.raises(AgentValidationError) as refused:
        load_agent(root, origin="hub")
    assert refused.value.problems == [
        f"{SCREEN_RULES}: negation_window is 300; a registry or store agent's is at most "
        "200 characters (30 to 40 is usual)"
    ]
    assert not load_agent(root, origin="local").bounded


def test_a_registry_agent_keeps_its_own_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs on the bounded engine and nothing more: it automates under its own policy and
    its own BLOCK rule blocks; it is labelled as from a registry. Like every agent the deployment
    does not own (LP-16e), its pinned model gives way to the deployment's. It automates only once
    its organization approves its digest (LP-18d): approved here."""
    monkeypatch.setenv("LLM_MODEL", "example/deployment-model")
    agent = outside_sorter(tmp_path / "plain")
    assert agent.bounded and agent.origin == "hub"
    assert Decision(agent, origin_approval=lambda _: True).decide(BILL).outcome is Outcome.AUTO
    assert build_provider(agent, "router", "llm").info.model == "example/deployment-model"

    def blocks(manifest: dict[str, Any]) -> None:
        manifest["policy"]["rules"].append(BLOCK_RULE)

    blocking = outside_sorter(tmp_path / "blocking", manifest=blocks)
    assert Decision(blocking, origin_approval=lambda _: True).decide(BILL).outcome is Outcome.BLOCK


def test_discover_lists_a_refused_registry_agent_and_serves_the_others(tmp_path: Path) -> None:
    """In the cache, a version on the bounded engine that cannot run (a lookahead) is listed
    as refused, and the reference agent of its id is served; another agent's new version is."""
    refused = copied(tmp_path, TRIAGE_DIR.name, version="9.9.9", edit=lookahead)
    other = load_agent(copied(tmp_path, CLINICAL_DIR.name, version="9.9.9"))
    paths = [SearchPath(tmp_path / "cache", "hub"), SearchPath(AGENTS, "maintained")]
    found = discover(paths)
    assert set(found.problems) == {str(refused.resolve())}
    assert "lookahead" in found.problems[str(refused.resolve())]
    served = {(agent.id, agent.version): agent for agent in found.agents}
    assert served[(other.id, "9.9.9")].bounded
    assert not served[(TRIAGE.id, TRIAGE.version)].bounded
    assert (TRIAGE.id, "9.9.9") not in served
    client = TestClient(create_app(db_path=tmp_path / "e.db", agents_paths=paths))
    assert str(refused.resolve()) in client.get("/api/health").json()["invalid"]
    detail = client.get(f"/api/agents/{TRIAGE.id}").json()
    assert detail["manifest"]["version"] == TRIAGE.version
    detail = client.get(f"/api/agents/{other.id}").json()
    assert detail["manifest"]["version"] == "9.9.9"


@pytest.mark.parametrize(
    ("first", "second", "newest"),
    [
        # One folder, walked in path order: rc.1 is found before rc.2.
        (("one", "9.9.9-rc.1"), ("one", "9.9.9-rc.2"), "9.9.9-rc.2"),
        # A release on a later search path than its pre-release.
        (("early", "9.9.9-rc.1"), ("late", "9.9.9"), "9.9.9"),
    ],
)
def test_the_newest_of_an_agents_versions_is_by_semantic_version_precedence(
    tmp_path: Path, first: tuple[str, str], second: tuple[str, str], newest: str
) -> None:
    """`version_key` ties a release and its pre-releases (so a pre-release may not take a
    reference agent's place), and the newest of an agent's versions was whichever was found
    first among those that tie: rc.1 served over rc.2, a pre-release over its release. The
    engine orders them as the registry does (`precedence`)."""
    for base, version in (first, second):
        other = load_agent(copied(tmp_path / base, CLINICAL_DIR.name, version=version))
    bases = dict.fromkeys((first[0], second[0]))  # each search path once, in order
    paths = [SearchPath(tmp_path / base / "cache", "hub") for base in bases]
    assert resolve_agent(other.id, paths).version == newest
    client = TestClient(create_app(db_path=tmp_path / "e.db", agents_paths=paths))
    assert client.get(f"/api/agents/{other.id}").json()["manifest"]["version"] == newest


def test_decision_from_path_takes_the_origin_of_the_path(tmp_path: Path) -> None:
    """H3: a pulled agent loaded by its path (`Decision.from_path`) is still an agent from a
    registry, on its engine; a reference agent by its path is maintained."""
    cached = agents_cache() / TRIAGE.id / "9.9.9"
    shutil.copytree(copied(tmp_path, TRIAGE_DIR.name, version="9.9.9"), cached)
    pulled = Decision.from_path(cached).agent
    assert (pulled.origin, pulled.bounded) == ("hub", True)
    reference = Decision.from_path(TRIAGE_DIR).agent
    assert (reference.origin, reference.bounded) == ("maintained", False)


def test_the_reference_digests_are_the_installed_releases() -> None:
    """Whatever agents the release bundles (in the suite, the fixtures' folder), each by the id
    its manifest names."""
    expected = {agent_digest(directory) for directory in bundled()}
    assert expected and reference_digests() == expected
    assert {resolve_agent(load_agent(d).id).digest for d in bundled()} == expected


def test_the_reference_index_is_each_bundled_agents_id_version_and_digest() -> None:
    """`reference_agents`, read from each manifest and the files' digest without loading the
    agent, says what loading it says."""
    loaded = [load_agent(directory) for directory in bundled()]
    assert loaded
    assert {
        agent_id: [(each.version, each.digest) for each in agents]
        for agent_id, agents in reference_agents().items()
    } == {agent.id: [(agent.version, agent.digest)] for agent in loaded}


def test_owned_agents_stay_on_re(tmp_path: Path) -> None:
    """Reference agents, and an operator's own agents (a changed copy included), run on `re`: no
    caps, no budget, and a pattern that backtracks loads."""
    for directory in bundled():
        agent = load_agent(directory, origin="maintained")
        assert not agent.bounded
        assert not resolve_agent(agent.id).bounded
    changed = copied(tmp_path, TRIAGE_DIR.name, edit=lookahead)
    assert not load_agent(changed, origin="local").bounded

    def backtracks(rules: Any) -> None:
        labels = rules["questions"]["destination"]["labels"]["front_desk"]
        labels.append({"name": "slow", "pattern": "(a+)+$", "weight": 1.0})

    edit = yaml_edit(ROUTER_RULES, backtracks)
    local = load_agent(copied(tmp_path / "slow", TRIAGE_DIR.name, edit=edit), origin="local")
    assert not local.bounded
    provider: Any = build_provider(local, "router", "rules")
    regexes = [
        regex
        for labels in provider._compiled.values()
        for found in labels.values()
        for _, regex in found
    ]
    assert all(isinstance(regex, re.Pattern) for regex in regexes)


def test_mendari_test_runs_a_registry_agent_on_its_engine(tmp_path: Path) -> None:
    """`mendari test` checks a registry agent's behaviour-test inputs as a decision would: within
    the input limits of the engine it runs on."""

    def big_case(cases: Any) -> None:
        cases.append(
            {
                "name": "a very large input",
                "input": {"message": {"body": "hi"}, "patient": {"tags": [0] * 11_000}},
                "expect": {"outcome": "HUMAN_REVIEW"},
            }
        )

    edit = yaml_edit("tests/cases.yaml", big_case)
    root = copied(tmp_path, TRIAGE_DIR.name, version="9.9.9", edit=edit)
    hub = load_agent(root, origin="hub")
    assert any(
        "its input is too large: more than 10,000 values, the most an agent from a registry reads"
        in problem
        for problem in map(str, input_problems(hub))
    )
    local = load_agent(root, origin="local")
    assert not any("too large" in str(problem) for problem in input_problems(local))
