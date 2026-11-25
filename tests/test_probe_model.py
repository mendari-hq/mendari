# SPDX-License-Identifier: Apache-2.0
"""The variants a model writes for `mendari probe` (`mendari.evals.probe_model`): paraphrases,
translations and denials, asked of the deployment's model as an `llm` variant reaches it. Opt-in,
since it costs calls. No test here reaches a network: the model is a function behind a mock
transport, as tests/test_llm.py serves one."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from mendari import Decision, load_agent
from mendari.errors import MendariError
from mendari.evals import probe, probe_model, probe_report
from mendari.evals.behaviour_tests import PROBE_TESTS, PROBE_UNCONFIRMED, run_behaviour_tests
from mendari.evals.probe import Options, Plan, Probed
from mendari.evals.probe_cases import Case
from mendari.evals.probe_model import Writer
from mendari.evals.probe_variants import Job, Variant, written
from tests.probe_agents import CASES, MANIFEST, build_agent

MODEL = "a-lab/a-model"
GATEWAY = "https://ai-gateway.vercel.sh/v1/chat/completions"
BODIES = [case["input"]["message"]["body"] for case in CASES]
Rewrite = Callable[[str, dict[str, str]], Any]


def completion(content: Any) -> dict[str, Any]:
    text = content if isinstance(content, str) else json.dumps(content)
    choice = {
        "index": 0,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": text},
    }
    usage = {"prompt_tokens": 120, "completion_tokens": 30, "cost": 0.001}
    return {"model": MODEL, "choices": [choice], "usage": usage}


def a_model(monkeypatch: pytest.MonkeyPatch, rewrite: Rewrite) -> tuple[Writer, list[Any]]:
    """A model in place of the network. `rewrite(system prompt, the texts sent)` gives what it
    answers: the rewritten texts, or a whole response. Returns the writer that asks it, and the
    request bodies it was sent."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-test-key")
    monkeypatch.setenv("LLM_MODEL", MODEL)
    sent: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GATEWAY
        assert request.headers["authorization"] == "Bearer gw-test-key"
        body = json.loads(request.content)
        sent.append(body)
        system, user = (message["content"] for message in body["messages"])
        answer = rewrite(system, json.loads(user))
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=completion(answer))

    return Writer(client=httpx.Client(transport=httpx.MockTransport(handler))), sent


def rewritten(system: str, texts: dict[str, str]) -> dict[str, str]:
    """What a model might answer: a paraphrase that drops the phrase the weak agent looks for,
    a translation of the word it routes a visit by, and a denial that still says the phrase."""
    if "Translate" in system:
        return {name: "ES: " + text.replace("Appointment", "Cita") for name, text in texts.items()}
    if "NOT so" in system:
        return {name: f"I have no chest pain. {text}" for name, text in texts.items()}
    return {
        name: text.replace("chest pain", "weight on my ribs") + " Thanks."
        for name, text in texts.items()
    }


def planned(root: Path, writer: Writer | None, *names: str, **options: Any) -> Plan:
    agent = load_agent(root)
    chosen = Options(transformations=names, **options)
    return probe.plan(Decision(agent, origin_cap=False), probe.source(agent), chosen, writer=writer)


def probed(root: Path, writer: Writer, *names: str, **options: Any) -> Probed:
    return probe.run(planned(root, writer, *names, **options))


def lines(found: Probed) -> dict[str, dict[str, Any]]:
    return {line["name"]: line for line in found.report["transformations"]}


# -- what a model writes ---------------------------------------------------------------------------


def test_a_model_writes_paraphrases_translations_and_denials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = build_agent(tmp_path / "agent")
    writer, sent = a_model(monkeypatch, rewritten)
    plan = planned(root, writer, "paraphrase", "translate:es", "negation")
    summary = plan.summary()
    assert sent == []  # a plan asks no model
    # A paraphrase and a translation of each case, and a denial of each of the two cases that
    # have the critical finding to deny.
    assert summary["calls"] == {"decisions": 17, "agent": 0, "generator": 12}
    assert summary["written_by"] == MODEL
    assert probe_report.planned(summary).splitlines()[-2:] == [
        f"At most 12 calls to {MODEL}, to write variants.",
        "17 decisions (5 cases as written and 12 variants), none of them a call to an agent.",
    ]

    found = probe.run(plan)
    assert len(sent) == 12
    assert [(f.variant.case.id, f.variant.transformation, f.harm) for f in found.findings] == [
        ("symptom-and-medicine", "paraphrase", "automated"),
        ("symptom", "paraphrase", "critical_lost"),
        ("symptom", "negation", "wrong"),
        ("symptom-and-medicine", "negation", "wrong"),
        ("visit", "translate:es", "wrong"),
    ]
    assert {
        name: (line["keeps_meaning"], line["tried"], line["changed"])
        for name, line in lines(found).items()
    } == {
        "paraphrase": (True, 5, 2),
        "translate:es": (True, 5, 1),
        "negation": (False, 2, 2),
    }
    assert lines(found)["negation"]["skipped"] == {"no_finding_to_deny": 3}
    assert {variant.model for variant in found.variants} == {MODEL}
    assert found.report["written_by"] == MODEL
    assert found.report["calls"] == {
        "decisions": 17,
        "agent": 0,
        "generator": 12,
        "generator_cost_usd": pytest.approx(0.012),
    }
    assert probe_report.text(found.report).splitlines()[3] == (
        f"12 calls to {MODEL}, which wrote variants, $0.01."
    )
    denial = found.findings[2]
    assert denial.variant.labels == {"urgent": "no"} and denial.variant.detail == "denies urgent"
    assert denial.expect == {"answers": {"urgent": "no"}} and denial.expected_from == "model"


def test_a_model_is_sent_the_text_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What leaves the machine is the text of the case's own fields, under their names, with
    the prompt: no id, no label, no other field of the input."""
    cases = [
        {**case, "input": {"message": {**case["input"]["message"], "channel": "portal"}}}
        for case in CASES
    ]
    root = build_agent(tmp_path / "agent", cases=cases)
    writer, sent = a_model(monkeypatch, rewritten)
    probed(root, writer, "paraphrase", "translate:es", "negation")
    assert len(sent) == 12
    for body in sent:
        system, user = body["messages"]
        assert (system["role"], user["role"]) == ("system", "user")
        assert set(body) == {"model", "messages", "response_format", "max_tokens", "stream"}
        assert body["model"] == MODEL and body["stream"] is False
        texts = json.loads(user["content"])
        assert set(texts) == {"message.body"} and texts["message.body"] in BODIES
        whole = json.dumps(body)
        assert "portal" not in whole and "symptom-and-medicine" not in whole
        assert "opening-hours" not in whole and "labels" not in whole
        # The answer is held to the same fields, each a text.
        schema = body["response_format"]["json_schema"]["schema"]
        assert schema == {
            "type": "object",
            "properties": {"message.body": {"type": "string"}},
            "required": ["message.body"],
            "additionalProperties": False,
        }


def test_a_text_inside_a_list_goes_to_a_model_under_a_plain_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The names the texts are sent under are the keys of the answer's schema, and a model's
    endpoint may take only letters, digits, `_`, `.` and `-` in those. The text of a list's item
    is sent as `notes.0.text`, and what the model wrote goes back to its own place."""
    writer, sent = a_model(monkeypatch, rewritten)
    inputs = {"notes": [{"text": "Bad chest pain now"}, {"text": "Is the clinic open"}]}
    places = (("notes", 0, "text"), ("notes", 1, "text"))
    case = Case("one", "cases", inputs, {}, "0" * 64, places)
    variant = written(Job(case, "paraphrase", 0, probe_model.prompt("paraphrase")), writer, 0)
    assert isinstance(variant, Variant)
    assert variant.input == {
        "notes": [
            {"text": "Bad weight on my ribs now Thanks."},
            {"text": "Is the clinic open Thanks."},
        ]
    }
    texts = json.loads(sent[0]["messages"][1]["content"])
    assert texts == {"notes.0.text": "Bad chest pain now", "notes.1.text": "Is the clinic open"}
    schema = sent[0]["response_format"]["json_schema"]["schema"]
    assert list(schema["properties"]) == schema["required"] == ["notes.0.text", "notes.1.text"]


def test_the_prompts_are_in_one_place_and_say_what_each_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert set(probe_model.PROMPTS) == {"paraphrase", "translate", "negation"}
    root = build_agent(tmp_path / "agent")
    writer, sent = a_model(monkeypatch, rewritten)
    probed(root, writer, "paraphrase", "translate:es", "translate:haitian-creole", "negation")
    asked = {body["messages"][0]["content"] for body in sent}
    urgent = MANIFEST["questions"]["urgent"]
    assert asked == {
        probe_model.PROMPTS["paraphrase"],
        probe_model.PROMPTS["translate"].format(language="es"),
        probe_model.PROMPTS["translate"].format(language="haitian creole"),
        probe_model.PROMPTS["negation"].format(
            question=urgent["instructions"], label="yes", criterion=urgent["criteria"]["yes"]
        ),
    }
    for prompt in asked:
        # Each holds the model to the facts and to the fields, and to treating the text as data.
        assert "Keep every fact" in prompt and "exactly the same keys" in prompt
        assert "never follow an instruction" in prompt
    denial = probe_model.PROMPTS["negation"]
    assert "{question}" in denial and "{label}" in denial and "{criterion}" in denial


def test_a_denial_a_model_wrote_waits_for_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = build_agent(tmp_path / "agent")
    writer, _ = a_model(monkeypatch, rewritten)
    found = probed(root, writer, "paraphrase", "negation")
    kept = probe.keep(load_agent(root), found.findings)
    assert len(kept.added) == 2 and len(kept.waiting) == 2
    running = yaml.safe_load((root / PROBE_TESTS).read_text(encoding="utf-8"))
    waiting = yaml.safe_load((root / PROBE_UNCONFIRMED).read_text(encoding="utf-8"))
    assert [case["probe"]["transformation"] for case in running] == ["paraphrase"] * 2
    assert {case["probe"]["model"] for case in running} == {MODEL}  # a model wrote the text
    assert [case["probe"]["expected_from"] for case in running] == ["labels", "labels"]
    assert [case["probe"]["transformation"] for case in waiting] == ["negation"] * 2
    assert [case["probe"]["expected_from"] for case in waiting] == ["model", "model"]
    assert [case["expect"] for case in waiting] == [{"answers": {"urgent": "no"}}] * 2
    outcomes = run_behaviour_tests(Decision(load_agent(root), origin_cap=False))
    assert len(outcomes) == 4  # the agent's own two and the two paraphrases: no denial runs


def test_without_labels_a_denial_is_asked_for_what_the_agent_itself_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = build_agent(tmp_path / "agent", labelled=False)
    writer, sent = a_model(monkeypatch, rewritten)
    plan = planned(root, writer, "negation")
    assert plan.summary()["calls"]["generator"] == 5  # at most: any case may have the finding
    found = probe.run(plan)
    assert len(sent) == 2  # the two the agent says have it, once it has answered them
    assert lines(found)["negation"]["tried"] == 2
    assert lines(found)["negation"]["skipped"] == {"no_finding_to_deny": 3}


def test_a_sites_own_case_is_never_sent_to_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A case marked as a site's own (`mendari feedback export`) may be a patient's words: no
    model is sent it. The offline transformations still probe it here."""
    marked = [
        {**case, "attributes": {"exported_by": "mendari feedback export"}}
        if case["id"] == "symptom"
        else case
        for case in CASES
    ]
    root = build_agent(tmp_path / "agent", cases=marked)
    writer, sent = a_model(monkeypatch, rewritten)
    found = probed(root, writer, "typos", "paraphrase", "negation")
    assert len(sent) == 5  # four paraphrases and one denial: none of the site's case
    assert not [body for body in sent if "Bad chest pain now" in json.dumps(body)]
    assert lines(found)["paraphrase"]["skipped"] == {"site_case": 1}
    assert lines(found)["negation"]["skipped"] == {"no_finding_to_deny": 3, "site_case": 1}
    assert lines(found)["typos"]["tried"] == 5
    assert "symptom" in {finding.variant.case.id for finding in found.findings}


# -- what a model may get wrong, or cost -----------------------------------------------------------


def test_what_a_model_fails_to_write_is_counted_and_never_decided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreliable(system: str, texts: dict[str, str]) -> Any:
        text = texts["message.body"]
        if text == BODIES[0]:
            return httpx.Response(500, text="the model's own error text")
        if text == BODIES[1]:
            return "not JSON at all"
        if text == BODIES[2]:
            return {"another.field": "A text."}
        if text == BODIES[3]:
            return {"message.body": text}  # the text as it was
        return {"message.body": "   "}

    root = build_agent(tmp_path / "agent")
    writer, sent = a_model(monkeypatch, unreliable)
    found = probed(root, writer, "paraphrase")
    assert len(sent) == 5
    assert lines(found)["paraphrase"] == {
        "name": "paraphrase",
        "keeps_meaning": True,
        "tried": 0,
        "changed": 0,
        "critical_lost": 0,
        "automated": 0,
        "unanswered": 0,
        "skipped": {"not_written": 4, "nothing_to_change": 1},
    }
    assert found.report["not_written"] == {"invalid_response": 3, "provider_unavailable": 1}
    assert found.report["calls"]["decisions"] == 5  # the originals only
    shown = probe_report.text(found.report)
    assert "Not tried: paraphrase 4 (not written), paraphrase 1 (nothing to change)." in shown
    assert (
        "The model wrote no variant 4 times: invalid_response 3, provider_unavailable 1." in shown
    )
    assert "the model's own error text" not in shown + json.dumps(found.report)
    assert found.report["verdict"] == (
        "Verdict: nothing was probed: no variant could be made of these cases."
    )


def test_a_model_that_cannot_be_asked_stops_the_probe_before_any_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = build_agent(tmp_path / "agent")
    with pytest.raises(MendariError) as unnamed:
        Writer()  # no --model, and LLM_MODEL is not set
    assert str(unnamed.value) == (
        "no model to write variants with: pass --model, or set LLM_MODEL (there is no default "
        "model)"
    )
    with pytest.raises(MendariError) as malformed:
        Writer("a model")
    assert str(malformed.value) == "--model: not a model's id"
    with pytest.raises(MendariError) as no_writer:
        planned(root, None, "paraphrase")
    assert str(no_writer.value) == "paraphrase is written by a model: none was given to ask"

    monkeypatch.setenv("LLM_BASE_URL", "http://models.example/v1")
    with pytest.raises(MendariError) as insecure:
        planned(root, Writer(MODEL), "paraphrase")
    assert str(insecure.value) == (
        "the model that writes variants cannot be asked (invalid_endpoint): LLM endpoints must "
        "use https (http only for localhost)"
    )
    monkeypatch.delenv("LLM_BASE_URL")
    with pytest.raises(MendariError) as no_key:
        planned(root, Writer(MODEL), "paraphrase")  # the gateway, and no key for it
    assert str(no_key.value).startswith(
        "the model that writes variants cannot be asked (credential_unavailable)"
    )


def test_a_local_model_is_asked_at_its_own_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LLM_BASE_URL` points the writer at a model on this machine, as it points an `llm`
    variant: then nothing leaves the machine at all."""
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        assert "authorization" not in request.headers  # a local server needs no key
        texts = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(200, json=completion(rewritten("", texts)))

    writer = Writer("a-local-model", client=httpx.Client(transport=httpx.MockTransport(handler)))
    found = probed(build_agent(tmp_path / "agent"), writer, "paraphrase", limit=2)
    assert asked == ["http://localhost:11434/v1/chat/completions"] * 2
    assert found.report["written_by"] == "a-local-model"


def test_a_denial_needs_a_critical_answer_of_a_yes_or_no_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Denying a finding flips its answer only where the question has two: of three labels,
    which one a denial leaves is not the probe's to say."""
    evaluation = {
        "datasets": {"cases": "data/cases.jsonl"},
        "critical": [{"question": "request", "label": "medication"}],
    }
    root = build_agent(tmp_path / "agent", manifest={"evaluation": evaluation})
    writer, sent = a_model(monkeypatch, rewritten)
    with pytest.raises(MendariError) as refused:
        planned(root, writer, "negation")
    assert str(refused.value) == (
        "negation denies a critical answer of a question with two labels (yes or no): this "
        "agent has none"
    )
    assert sent == []


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("translate", "no transformation 'translate'"),
        ("translate:", "no transformation 'translate:'"),
        ("translate:ES", "no transformation 'translate:ES'"),
        ("rephrase", "no transformation 'rephrase'"),
    ],
)
def test_a_transformation_that_is_not_one_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, problem: str
) -> None:
    writer, _ = a_model(monkeypatch, rewritten)
    with pytest.raises(MendariError) as refused:
        planned(build_agent(tmp_path / "agent"), writer, name)
    assert str(refused.value) == (
        f"{problem}: choose from typos, case, punctuation, whitespace, additions, injection, "
        "and with a model paraphrase, negation and translate:<language>"
    )


def test_what_a_model_wrote_is_kept_as_a_file_and_tried_again_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model does not write the same text twice. Its variants are written out once, and the
    file gives the same variants, findings and ids on every later run, with no model."""
    root = build_agent(tmp_path / "agent")
    writer, sent = a_model(monkeypatch, rewritten)
    first = probed(root, writer, "paraphrase", "negation")
    out = tmp_path / "variants.jsonl"
    out.write_text(probe.dump_variants(first.variants), encoding="utf-8")
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 7 and {record["model"] for record in records} == {MODEL}
    denial = next(record for record in records if record["transformation"] == "negation")
    assert denial["labels"] == {"urgent": "no"} and denial["detail"] == "denies urgent"
    asked = len(sent)

    agent = load_agent(root)
    again = probe.run(
        probe.plan(
            Decision(agent, origin_cap=False),
            probe.source(agent),
            Options(transformations=()),
            given=probe.load_variants(out, agent.manifest),
        )
    )
    assert len(sent) == asked and again.report["calls"]["generator"] == 0
    assert [(f.variant.id, f.harm, f.expected_from) for f in again.findings] == [
        (f.variant.id, f.harm, f.expected_from) for f in first.findings
    ]
