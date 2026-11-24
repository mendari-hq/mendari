# SPDX-License-Identifier: Apache-2.0
"""An agent's public evaluation and its badge (`hub/evaluation_page.py`, `hub/badge.py`): what
the Agent Hub's evaluation page shows and what a README's badge says, worked out on every read
from the publisher's signed evaluations that still verify, and from nothing else.

A publish round trip (publish, read back, verify the signature with the namespace's key, the
page's answer and the badge); a record changed after it was stored shown as not verified, with
none of its numbers; the comparison with one, two and several configurations; an agent with no
evaluation; every state of the badge, that nothing a caller sends reaches its markup, and its
headers and limits. The demo agent's synthetic test set only; every key and token is made
here, and the registry is this process's."""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari.core.decision import Decision
from mendari.core.pack import Pack, load_pack
from mendari.errors import MendariError
from mendari.evals import figures, ranking
from mendari.evals.harness import evaluation, resolve_dataset
from mendari.hub import archive, badge, evaluation_page, evaluations, yank
from mendari.hub.registry import BADGE_HEADERS, REGISTRY_CSP, create_registry_app
from tests.conftest import Registry
from tests.test_publisher_evaluations import DEMO_DIR, demo, send, signed
from tests.test_registry_mount import CROSS_SITE, World, served, sign_in_on

AGENT = "/v1/agents/mendari/portal-message-agent"
SVG = "{http://www.w3.org/2000/svg}"
# Everything a badge is made of: these elements and these attributes, and nothing else.
BADGE_ELEMENTS = {"svg", "title", "rect", "path", "g", "text"}
BADGE_ATTRIBUTES = {
    *("width", "height", "viewBox", "role", "aria-label", "rx", "fill", "d"),
    *("text-anchor", "font-family", "font-size", "text-rendering", "x", "y", "transform"),
    "textLength",
}


@pytest.fixture
def published(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> Registry:
    """The demo agent published in the `mendari` namespace; the registry re-runs evaluations."""
    assert registry.publish(demo()).status_code == 201
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "on")
    return registry


def page(registry: Registry, **params: str) -> dict[str, Any]:
    answer = registry.client.get(f"{AGENT}/evaluation", params=params)
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


def parsed(image: bytes | str) -> tuple[str, str]:
    """A badge's two texts (its label and its words), once it is well-formed XML made of the
    elements and attributes a badge has and nothing else."""
    root = ElementTree.fromstring(image)
    for element in root.iter():
        assert element.tag.removeprefix(SVG) in BADGE_ELEMENTS, element.tag
        assert set(element.attrib) <= BADGE_ATTRIBUTES, element.attrib
    label, words = (text.text or "" for text in root.iter(f"{SVG}text"))
    assert root.findtext(f"{SVG}title") == root.attrib["aria-label"] == f"{label}: {words}"
    return label, words


def said(registry: Registry, agent: str = AGENT) -> str:
    answer = registry.client.get(f"{agent}/badge.svg")
    assert answer.status_code == 200, answer.text
    label, words = parsed(answer.content)
    assert label == "Mendari"
    return words


def as_v1(record: dict[str, Any], key: Any) -> dict[str, Any]:
    """The same run as a `mendari.publisher-evaluation/1` record, as a client from before `/2`
    signs it."""
    fields = copy.deepcopy({k: v for k, v in record.items() if k not in ("key_id", "signature")})
    fields["format"] = evaluations.FORMAT_V1
    del fields["engine"], fields["claimed"]["failed_cases"]
    fields["dataset"] = {k: fields["dataset"][k] for k in ("name", "sha256", "n")}
    return evaluations.sign(fields, key)


def rewrite(registry: Registry, evaluation_id: str, **columns: str) -> None:
    """Change a stored evaluation's row behind the registry's back, as someone with the
    database could."""
    assignments = ", ".join(f"{column} = ?" for column in columns)
    with registry.operator._db.session(exclusive=True) as db:
        db.all(
            f"UPDATE hub_evaluations SET {assignments} WHERE id = ?",
            (*columns.values(), evaluation_id),
        )


def stored_body(registry: Registry, evaluation_id: str) -> dict[str, Any]:
    with registry.operator._db.session(exclusive=False) as db:
        row = db.one("SELECT body FROM hub_evaluations WHERE id = ?", (evaluation_id,))
    assert row is not None
    body: dict[str, Any] = json.loads(row["body"])
    return body


def body_text(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


# -- a round trip ---------------------------------------------------------------------------------


def test_a_published_evaluation_is_read_back_verified_and_shown(published: Registry) -> None:
    labelled = {**signed(published.key)["dataset"], "labelled_by": "The script that wrote them"}
    rules = signed(published.key, "rules", dataset=labelled)
    agent = signed(published.key, "agent", dataset=labelled)
    for record in (rules, agent):
        assert send(published, record).status_code == 201
    assert rules["format"] == "mendari.publisher-evaluation/2"
    assert rules["engine"] and rules["claimed"]["failed_cases"] == 0
    # Read back: the signed record itself, which verifies with the key the namespace lists.
    listed = published.client.get(f"{AGENT}/{demo().version}/evaluations").json()["evaluations"]
    assert [e["signature"]["verified"] for e in listed] == [True, True]
    rules_id = evaluations.evaluation_id(rules)
    held = published.client.get(f"{AGENT}/{demo().version}/evaluations/{rules_id}").json()
    assert held["record"] == rules and held["signature"]["verified"] and not held["revoked"]
    keys = {
        key["key_id"]: archive.load_public_key(key["public_pem"].encode())
        for key in published.client.get("/v1/namespaces/mendari").json()["keys"]
    }
    assert evaluations.signature_holds(held["record"], keys[held["record"]["key_id"]])
    assert evaluations.evaluation_id(held["record"]) == held["id"]
    # Shown: the page's answer, from that record and nothing else.
    shown = page(published)
    assert shown["format"] == "mendari.agent-evaluation/1" and shown["state"] == "evaluated"
    assert shown["id"] == "mendari/portal-message-agent" and shown["not_shown"] == []
    assert shown["agent"]["version"] == demo().version and shown["agent"]["synthetic"] is True
    assert shown["agent"]["digest"] == demo().digest
    assert shown["publisher"] == {
        "namespace": "mendari",
        "display_name": "Mendari",
        "publisher_type": "community",
    }
    assert shown["versions"] == {
        "shown": demo().version,
        "latest": demo().version,
        "evaluated": [demo().version],
    }
    [dataset] = shown["datasets"]
    assert (dataset["name"], dataset["n"], dataset["sha256"]) == (
        "test",
        600,
        rules["dataset"]["sha256"],
    )
    assert dataset["provenance"] == "synthetic"
    assert dataset["labelled_by"] == ["The script that wrote them"]
    assert dataset["critical"] == [{"question": "urgent_symptoms", "label": "yes", "n": 114}]
    by_label = {c["label"]: c for c in dataset["configurations"]}
    assert set(by_label) == {"reader=agent", "reader=rules"}
    counted = rules["report"]
    [critical] = counted["decisions"]["critical"]
    one = by_label["reader=rules"]
    assert one["id"] == rules_id and one["rerun"] == "matched"
    assert one["signed"] == {
        "key_id": rules["key_id"],
        "at": rules["created_at"],
        "verified": True,
    }
    assert one["engine"] == rules["engine"] and one["models"] == rules["models"]
    assert one["record_format"] == "mendari.publisher-evaluation/2"
    assert one["providers"] == counted["providers"] and one["tokens"] == counted["tokens"]
    assert one["claimed"] == rules["claimed"]
    # Every figure is k of n with its interval, from the counts: the outcome mix, the automation
    # rate, the critical cases detected and automated.
    assert one["figures"]["critical"][0]["detected"] == figures.of(
        critical["detected"], critical["n"]
    )
    assert one["figures"]["critical"][0]["automated"] == figures.of(
        critical["auto_missed"], critical["n"]
    )
    assert one["critical_cases"] == critical["n"]
    assert one["critical_missed"] == critical["n"] - critical["detected"]
    assert one["critical_automated"] == critical["auto_missed"]
    outcomes = counted["decisions"]["outcomes"]
    assert {k: v["count"] for k, v in one["figures"]["outcomes"].items()} == outcomes
    assert one["figures"]["outcomes"]["AUTO"]["share"] == figures.of(outcomes["AUTO"], 600)
    assert sum(outcomes.values()) == 600
    assert by_label["reader=agent"]["rerun"] == "calls_a_model"
    assert "dataset" not in one and "row" not in one  # the order's working, never served
    # The badge says the same, in words and whole numbers.
    assert shown["badge"]["state"] == "evaluated"
    assert shown["badge"]["message"] == said(published)
    assert re.fullmatch(
        r"evaluated, 2 configurations, 600 synthetic cases, (\d+ to )?\d+ critical automated",
        shown["badge"]["message"],
    )
    assert published.operator.verify()["ok"]


def test_a_record_of_the_first_format_is_still_taken_and_shown(published: Registry) -> None:
    """What a client from before `mendari.publisher-evaluation/2` signs: taken, verified on
    every read, and shown with what it does not state as not stated."""
    old = as_v1(signed(published.key, "agent"), published.key)
    assert send(published, old).status_code == 201
    assert stored_body(published, evaluations.evaluation_id(old)) == old  # stored as it was sent
    [listed] = published.client.get(f"{AGENT}/{demo().version}/evaluations").json()["evaluations"]
    assert listed["signature"]["verified"] and listed["record_format"] == evaluations.FORMAT_V1
    assert listed["dataset"]["provenance"] is None and listed["engine"] is None
    [dataset] = page(published)["datasets"]
    [shown] = dataset["configurations"]
    assert dataset["provenance"] is None and dataset["labelled_by"] == []
    assert shown["engine"] is None and "failed_cases" not in shown["claimed"]
    assert shown["signed"]["verified"] is True
    # That the data is synthetic is still said: the signed archive's manifest says it.
    assert page(published)["agent"]["synthetic"] is True
    assert said(published).startswith("evaluated, 600 synthetic cases")
    # A record of one format never passes as the other: the format is in the signed bytes.
    relabelled = {**old, "format": evaluations.FORMAT_V2}
    answer = send(published, relabelled)
    assert answer.status_code == 422 and "mendari.publisher-evaluation/2 record" in answer.text


def test_what_the_second_format_adds_is_checked(published: Registry) -> None:
    """The dataset's provenance is the manifest's own word, who labelled it is one printable
    line, and the failed cases are cases of the dataset. A refusal names the field, never a
    value."""
    record = signed(published.key)
    for claimed_of in ("de_identified", "site_records", None):
        claimed = {**record["dataset"], "provenance": claimed_of}
        answer = send(published, signed(published.key, dataset=claimed))
        assert answer.status_code == 422 and "dataset.provenance: it is not what" in answer.text
    made_up = {**record["dataset"], "provenance": "clinically validated"}
    answer = send(published, signed(published.key, dataset=made_up))
    assert answer.status_code == 422 and "record: dataset.provenance" in answer.text
    assert "clinically validated" not in answer.text
    for odd in ("x" * 201, "two\nlines", "‮right to left", " ", "", 7):
        labelled = {**record["dataset"], "labelled_by": odd}
        answer = send(published, signed(published.key, dataset=labelled))
        assert answer.status_code == 422 and "record: dataset.labelled_by" in answer.text, odd
    failed = {**record["claimed"], "failed_cases": 601}
    answer = send(published, signed(published.key, claimed=failed))
    assert answer.status_code == 422 and "claimed.failed_cases: it is more than" in answer.text
    for missing in ("engine", "dataset", "claimed"):
        fields = {k: v for k, v in record.items() if k not in ("key_id", "signature", missing)}
        without = evaluations.sign(fields, published.key)
        assert send(published, without).status_code == 422, missing
    assert page(published)["state"] == "none"  # nothing above was stored


# -- a record changed after it was stored ---------------------------------------------------------


def test_a_tampered_record_is_shown_as_not_verified_with_none_of_its_numbers(
    published: Registry,
) -> None:
    rules, agent = signed(published.key, "rules"), signed(published.key, "agent")
    for record in (rules, agent):
        assert send(published, record).status_code == 201
    before = page(published)
    assert len(before["datasets"][0]["configurations"]) == 2
    # Someone with the database flatters the rules: every critical case detected, none automated.
    forged = copy.deepcopy(rules)
    forged["report"]["decisions"]["critical"][0].update(detected=114, auto_missed=0)
    rules_id = evaluations.evaluation_id(rules)
    rewrite(published, rules_id, body=body_text(forged))
    shown = page(published)
    [not_shown] = shown["not_shown"]
    assert not_shown == {
        "id": rules_id,
        "configuration": "reader=rules",
        "dataset": "test",
        "key_id": rules["key_id"],
        "reason": "record",
    }
    [dataset] = shown["datasets"]
    assert [c["label"] for c in dataset["configurations"]] == ["reader=agent"]
    assert dataset["comparison"]["status"] == "one_configuration"
    # Nothing the forged record says is served: not its counts, and no figure worked out of them.
    forged_figure = figures.of(114, 114)
    assert json.dumps(forged_figure)[1:-1] not in json.dumps(shown)
    assert shown["state"] == "evaluated" and "2 configurations" not in said(published)
    # The listing of the version says the same, and the record's own read too.
    listing = published.client.get(f"{AGENT}/{demo().version}/evaluations").json()
    changed = next(e for e in listing["evaluations"] if e["id"] == rules_id)
    assert changed["signature"] == {
        "key_id": rules["key_id"],
        "verified": False,
        "problem": "record",
    }
    assert changed["excluded"] == "not verified" and changed["figures"] is None
    assert changed["models"] == [] and changed["claimed"] == {}
    held = published.client.get(f"{AGENT}/{demo().version}/evaluations/{rules_id}").json()
    assert held["signature"]["verified"] is False and held["record"] == forged
    # With the id recomputed to match, the signature is what fails.
    rewrite(published, rules_id, id=evaluations.evaluation_id(forged))
    [not_shown] = page(published)["not_shown"]
    assert not_shown["reason"] == "signature"


@pytest.mark.parametrize(
    ("columns", "reason"),
    [
        ({"body": "not json"}, "record"),
        ({"body": "[]"}, "record"),
        ({"body": '{"format": "mendari.publisher-evaluation/2"}'}, "record"),
        ({"configuration": '{"reader":"agent"}'}, "record"),
        ({"configuration": "not json"}, "record"),
        ({"configuration": "[]"}, "record"),
        ({"dataset": "examples"}, "record"),
        ({"key_id": "ed25519:0000000000000000"}, "record"),
        ({"created_at": "2020-01-01T00:00:00Z"}, "record"),
        ({"rerun": "calls_a_model"}, "record"),
        ({"rerun": "checked by a clinician"}, "record"),
    ],
)
def test_a_row_that_is_no_longer_its_record_is_not_shown(
    published: Registry, columns: dict[str, str], reason: str
) -> None:
    record = signed(published.key)
    assert send(published, record).status_code == 201
    rewrite(published, evaluations.evaluation_id(record), **columns)
    shown = page(published)
    assert shown["state"] == "not_verified" and shown["datasets"] == []
    assert [n["reason"] for n in shown["not_shown"]] == [reason]
    assert shown["badge"] == {
        "state": "not_verified",
        "label": "Mendari",
        "message": "evaluation not verified",
        "tone": "bad",
    }
    assert said(published) == "evaluation not verified"
    # The version's listing answers too, and says the same of it.
    listing = published.client.get(f"{AGENT}/{demo().version}/evaluations")
    assert listing.status_code == 200, listing.text
    [listed] = listing.json()["evaluations"]
    assert listed["signature"]["verified"] is False and listed["signature"]["problem"] == reason
    assert listed["excluded"] == "not verified" and listed["figures"] is None


def test_a_model_s_evaluation_is_never_said_to_have_been_run_again(published: Registry) -> None:
    """Whether the registry ran a configuration again follows from the signed record (one that
    calls no model is run again before it is stored, one that calls a model never is), so a
    row that says otherwise is not its record's: the page never says "re-run" on a row's word."""
    record = signed(published.key, "agent")
    assert record["report"]["remote_consent"] is True
    assert send(published, record).status_code == 201
    [shown] = page(published)["datasets"][0]["configurations"]
    assert shown["rerun"] == "calls_a_model"
    rewrite(published, evaluations.evaluation_id(record), rerun="matched")
    changed = page(published)
    assert changed["state"] == "not_verified" and changed["datasets"] == []
    assert [n["reason"] for n in changed["not_shown"]] == ["record"]
    assert "matched" not in json.dumps(changed)
    held = published.client.get(
        f"{AGENT}/{demo().version}/evaluations/{evaluations.evaluation_id(record)}"
    ).json()
    assert held["signature"] == {
        "key_id": record["key_id"],
        "verified": False,
        "problem": "record",
    }


def test_a_record_signed_by_a_key_that_is_not_the_namespace_s_is_not_shown(
    published: Registry,
) -> None:
    """A whole record swapped for one a stranger signed, with every column of its row made to
    match it (its id, its key's id, its time)."""
    record = signed(published.key)
    assert send(published, record).status_code == 201
    stranger = archive.load_private_key(archive.generate_key()[0])
    swapped = signed(stranger)
    rewrite(
        published,
        evaluations.evaluation_id(record),
        id=evaluations.evaluation_id(swapped),
        body=body_text(swapped),
        key_id=swapped["key_id"],
        created_at=swapped["created_at"],
    )
    shown = page(published)
    assert shown["state"] == "not_verified"
    assert [n["reason"] for n in shown["not_shown"]] == ["key"]


def test_a_revoked_key_s_evaluations_leave_the_page_and_the_badge(published: Registry) -> None:
    record = signed(published.key, "agent")
    assert send(published, record).status_code == 201
    assert said(published).startswith("evaluated")
    published.operator.revoke_key("mendari", record["key_id"], reason="lost", by="operator")
    shown = page(published)
    assert shown["state"] == "not_verified" and shown["datasets"] == []
    assert [n["reason"] for n in shown["not_shown"]] == ["key_revoked"]
    assert said(published) == "evaluation not verified"


# -- the comparison -------------------------------------------------------------------------------


def member(
    name: str,
    *,
    critical: tuple[int, int, int] | None = (120, 120, 0),
    false_automation: int = 0,
    right: int = 590,
    n: int = 600,
) -> dict[str, Any]:
    """An evaluation as `compare` reads it: its counts on a dataset of `n` cases, `critical`
    the one critical label's (positives, detected, automated), or none."""
    labels = []
    if critical is not None:
        positives, detected, automated = critical
        labels = [
            {
                "question": "urgent",
                "label": "yes",
                "n": positives,
                "detected": detected,
                "auto_missed": automated,
            }
        ]
    return {
        "id": name,
        "position": None,
        "row": {
            "config": name,
            "dataset": "test",
            "dataset_sha256": "0" * 64,
            "n": n,
            "pack_digest": "sha256:d",
            "critical": labels,
            "false_automation": {"k": false_automation, "n": n},
            "accuracy_proportion": {"k": right, "n": n},
            "cost_per_decision_usd": None,
            "evaluation": name,
        },
    }


def compared(*members: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    ordered, found = evaluation_page.compare(list(members), "sha256:d")
    assert found["method"] == "mendari.rank/1" and found["measures"] == list(ranking.MEASURES)
    return [m["id"] for m in ordered], found


def test_one_configuration_is_compared_with_nothing() -> None:
    order, found = compared(member("only"))
    assert order == ["only"] and found["status"] == "one_configuration"
    assert found["first"] is None and found["decided_by"] is None and found["separated"] is None


def test_two_configurations_the_one_that_automates_fewer_critical_cases_is_first() -> None:
    safe = member("safe", critical=(120, 118, 0), right=540)
    risky = member("risky", critical=(120, 120, 14), right=599)  # more accurate, and not first
    order, found = compared(risky, safe)
    assert order == ["safe", "risky"]
    assert (found["status"], found["first"], found["second"]) == ("ordered", "safe", "risky")
    assert found["decided_by"] == "critical_automated"
    assert found["figures"] == {"first": figures.of(0, 120), "second": figures.of(14, 120)}
    assert found["separated"] is True  # [0.0-3.0%] and [6.5-18.7%] keep apart
    assert [m["position"] for m in (safe, risky)] == [1, 2]


def test_a_difference_inside_the_intervals_is_said_to_be_one() -> None:
    order, found = compared(
        member("a", critical=(120, 120, 1)), member("b", critical=(120, 120, 0))
    )
    assert order == ["b", "a"] and found["decided_by"] == "critical_automated"
    assert found["separated"] is False  # 0 of 120 and 1 of 120: the intervals overlap


@pytest.mark.parametrize(
    ("first", "second", "measure"),
    [
        (
            {"critical": (120, 119, 0)},
            {"critical": (120, 100, 0)},
            "critical_detection",
        ),
        ({"false_automation": 1}, {"false_automation": 30}, "false_automation"),
        ({"right": 590}, {"right": 500}, "accuracy"),
    ],
)
def test_each_measure_decides_when_those_before_it_are_equal(
    first: dict[str, Any], second: dict[str, Any], measure: str
) -> None:
    order, found = compared(member("worse", **second), member("better", **first))
    assert order == ["better", "worse"] and found["decided_by"] == measure
    assert found["first"] == "better" and found["separated"] is True
    if measure == "critical_detection":  # the figure names the label it is of
        assert found["figures"]["first"] == {
            **figures.of(119, 120),
            "question": "urgent",
            "label": "yes",
        }


def test_several_configurations_are_ordered_and_the_first_two_decide_what_is_said() -> None:
    members = [
        member("d", critical=(120, 120, 9)),
        member("b", critical=(120, 110, 0)),
        member("c", critical=(120, 120, 2)),
        member("a", critical=(120, 119, 0)),
    ]
    order, found = compared(*members)
    assert order == ["a", "b", "c", "d"]
    assert (found["first"], found["second"]) == ("a", "b")
    assert found["decided_by"] == "critical_detection" and found["separated"] is False
    assert {m["id"]: m["position"] for m in members} == {"a": 1, "b": 2, "c": 3, "d": 4}


def test_configurations_that_did_the_same_are_tied_and_none_is_first() -> None:
    order, found = compared(member("b"), member("a"), member("c", right=400))
    assert order == ["a", "b", "c"]  # by name, where no count tells them apart
    assert found["status"] == "tied" and found["tied"] == ["a", "b"]
    assert found["first"] is None and found["decided_by"] is None


def test_too_few_critical_cases_name_no_first_configuration() -> None:
    """As `mendari.rank/1`: nothing ranks while no critical label has 100 positives."""
    order, found = compared(
        member("b", critical=(99, 99, 0)), member("a", critical=(99, 50, 20), right=100)
    )
    assert order == ["a", "b"] and found["status"] == "insufficient_evidence"
    assert found["first"] is None
    order, found = compared(member("b", critical=None), member("a", critical=None, right=1))
    assert order == ["a", "b"] and found["status"] == "no_critical_label"


def test_a_publisher_s_cost_never_orders_the_configurations(published: Registry) -> None:
    """`mendari.rank/1` breaks a tie by cost; here cost is the publisher's own figure, so it
    is left out and the tie stays one."""
    record = signed(published.key, "agent")
    cheap = {**record["claimed"], "cost_usd_per_decision": 0.0}
    assert send(published, signed(published.key, "agent", claimed=cheap)).status_code == 201
    [dataset] = page(published)["datasets"]
    [shown] = dataset["configurations"]
    assert shown["claimed"]["cost_usd_per_decision"] == 0.0
    members = [evaluation_page._shown(row, json.loads(row["body"])) for row in _rows(published)]
    assert all(m is not None and m["row"]["cost_per_decision_usd"] is None for m in members)


def _rows(registry: Registry) -> list[dict[str, Any]]:
    rows, _ = registry.operator.live_evaluations("mendari", "portal-message-agent", demo().version)
    return rows


def three_readers(tmp_path: Path) -> Pack:
    """The demo agent under another name with a third variant of its one slot: rules that read
    any mention of breath, chest, face or numbness as urgent."""
    root = tmp_path / "three-readers"
    shutil.copytree(DEMO_DIR, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text(encoding="utf-8"))
    manifest["id"] = "mendari/three-readers"
    manifest["providers"]["reader"]["variants"]["cautious"] = {
        "type": "rules",
        "config": "logic/cautious.yaml",
    }
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    rules = yaml.safe_load((root / "logic" / "rules.yaml").read_text(encoding="utf-8"))
    rules["questions"]["urgent_symptoms"]["labels"]["yes"].append(
        {"name": "any-sign", "pattern": "breath|chest|face|numb|weak|dizzy|faint", "weight": 6}
    )
    (root / "logic" / "cautious.yaml").write_text(yaml.safe_dump(rules), encoding="utf-8")
    return load_pack(root)


def test_three_configurations_of_one_agent_are_published_and_compared(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "on")
    pack = three_readers(tmp_path)
    assert registry.publish(pack).status_code == 201
    base = "/v1/agents/mendari/three-readers"
    for variant in ("rules", "cautious", "agent"):
        decision = Decision(pack, providers={"reader": variant}, origin_cap=False)
        made = evaluation(decision, resolve_dataset(pack, "test"))
        record = evaluations.build(pack, made, key=registry.key)
        sent = registry.client.post(
            f"{base}/{pack.version}/evaluations",
            json=record,
            headers={"authorization": f"Bearer {registry.token}"},
        )
        assert sent.status_code == 201, sent.text
    shown = registry.client.get(f"{base}/evaluation").json()
    [dataset] = shown["datasets"]
    listed = dataset["configurations"]
    assert {c["label"] for c in listed} == {"reader=agent", "reader=cautious", "reader=rules"}
    assert [c["position"] for c in listed] == [1, 2, 3]
    # In the order's own terms: no configuration is listed above one that automated fewer
    # critical cases.
    automated = [c["critical_automated"] for c in listed]
    assert automated == sorted(automated) and automated[0] < automated[-1]
    found = dataset["comparison"]
    assert found["status"] == "ordered" and found["first"] == listed[0]["id"]
    assert found["second"] == listed[1]["id"] and found["decided_by"] in ranking.MEASURES
    assert said(registry, base).startswith("evaluated, 3 configurations, 600 synthetic cases, ")
    # The badge gives the fewest and the most: never the best configuration alone.
    assert said(registry, base).endswith(
        f", {min(automated)} to {max(automated)} critical automated"
    )


# -- no evaluation, a recall, an agent that is not there -------------------------------------------


def test_an_agent_with_no_evaluation_says_so(published: Registry) -> None:
    shown = page(published)
    assert shown["state"] == "none" and shown["datasets"] == [] and shown["not_shown"] == []
    assert shown["versions"]["evaluated"] == [] and shown["agent"]["version"] == demo().version
    assert shown["badge"]["message"] == "no evaluation published"
    assert shown["badge"]["tone"] == "plain"
    assert said(published) == "no evaluation published"


def test_evaluations_belong_to_a_version_and_a_new_version_starts_with_none(
    published: Registry, tmp_path: Path
) -> None:
    assert send(published, signed(published.key, "agent")).status_code == 201
    newer = tmp_path / "newer"
    shutil.copytree(DEMO_DIR, newer)
    manifest = (newer / "agent.yaml").read_text(encoding="utf-8")
    (newer / "agent.yaml").write_text(
        manifest.replace(f"version: {demo().version}", "version: 9.0.0", 1), encoding="utf-8"
    )
    assert published.publish(load_pack(newer)).status_code == 201
    shown = page(published)
    assert shown["state"] == "none" and shown["agent"]["version"] == "9.0.0"
    assert shown["versions"] == {
        "shown": "9.0.0",
        "latest": "9.0.0",
        "evaluated": [demo().version],
    }
    assert said(published) == "no evaluation published"  # the badge is the newest version's
    earlier = page(published, version=demo().version)
    assert earlier["state"] == "evaluated" and earlier["versions"]["shown"] == demo().version
    assert (
        published.client.get(f"{AGENT}/evaluation", params={"version": "8.0.0"}).status_code == 404
    )
    odd = published.client.get(f"{AGENT}/evaluation", params={"version": "../x"})
    assert odd.status_code == 404 and "../x" not in odd.text


def test_a_recalled_agent_reads_recalled(published: Registry) -> None:
    record = signed(published.key, "agent")
    assert send(published, record).status_code == 201
    found = published.operator.version("mendari", "portal-message-agent", demo().version)
    recall = yank.sign(
        pack="mendari/portal-message-agent",
        version=demo().version,
        digest=found["digest"],
        archive_sha256=found["archive_sha256"],
        reason="a test",
        severity="high",
        signer="publisher",
        key=published.key,
        key_id=record["key_id"],
    )
    recalled = published.client.post(
        f"{AGENT}/{demo().version}/yank",
        json={"record": recall},
        headers={"authorization": f"Bearer {published.token}"},
    )
    assert recalled.status_code == 201, recalled.text
    shown = page(published)
    assert shown["state"] == "recalled" and shown["datasets"] == [] and shown["agent"] is None
    assert shown["versions"]["latest"] is None and shown["versions"]["evaluated"] == []
    assert shown["badge"]["tone"] == "bad" and said(published) == "recalled"
    assert page(published, version=demo().version)["state"] == "recalled"


def test_an_agent_that_is_not_there_is_a_404_and_a_badge_that_says_so(
    registry: Registry,
) -> None:
    assert registry.client.get("/v1/agents/mendari/nothing/evaluation").status_code == 404
    answer = registry.client.get("/v1/agents/mendari/nothing/badge.svg")
    assert answer.status_code == 404
    assert answer.headers["content-type"] == "image/svg+xml"
    assert parsed(answer.content) == ("Mendari", "agent not found")


def test_an_unlisted_namespace_s_badge_is_what_the_public_reads(
    registry: Registry, tmp_path: Path
) -> None:
    """A namespace whose claim is not approved answers the public as one that does not exist;
    its own token reads its page, and the badge is the public's whoever asks."""
    registry.operator.claim("waiting", org="waiting", by="operator", display_name="Waiting")
    token = registry.mint("waiting", org="waiting")
    private, public = archive.generate_key()
    own = {"authorization": f"Bearer {token}"}
    added = registry.client.post(
        "/v1/namespaces/waiting/keys", json={"public_pem": public.decode()}, headers=own
    )
    assert added.status_code == 201
    root = tmp_path / "waiting"
    shutil.copytree(DEMO_DIR, root)
    manifest = (root / "agent.yaml").read_text(encoding="utf-8")
    (root / "agent.yaml").write_text(
        manifest.replace("id: mendari/portal-message-agent", "id: waiting/agent", 1),
        encoding="utf-8",
    )
    sent = registry.publish(load_pack(root), key=archive.load_private_key(private), token=token)
    assert sent.status_code == 201, sent.text
    base = "/v1/agents/waiting/agent"
    assert registry.client.get(f"{base}/evaluation").status_code == 404
    assert registry.client.get(f"{base}/evaluation", headers=own).json()["state"] == "none"
    for sent_with in ({}, own):  # the badge reads no token
        answer = registry.client.get(f"{base}/badge.svg", headers=sent_with)
        assert answer.status_code == 404
        assert parsed(answer.content) == ("Mendari", "agent not found")
    registry.operator.decide_claim("waiting", approve=True, by="operator", note="read")
    assert said(registry, base) == "no evaluation published"


# -- the badge ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "totals", "words", "tone"),
    [
        ("none", None, "no evaluation published", "plain"),
        ("not_verified", None, "evaluation not verified", "bad"),
        ("recalled", None, "recalled", "bad"),
        ("not_found", None, "agent not found", "plain"),
        ("something else", None, "no evaluation published", "plain"),
        ("evaluated", None, "no evaluation published", "plain"),
        (
            "evaluated",
            {"configurations": 1, "datasets": 1, "cases": 600, "synthetic": False,
             "least_automated": 0, "most_automated": 0},
            "evaluated, 600 cases, 0 critical automated",
            "info",
        ),
        (
            "evaluated",
            {"configurations": 1, "datasets": 1, "cases": 1, "synthetic": True,
             "least_automated": 1, "most_automated": 1},
            "evaluated, 1 synthetic case, 1 critical automated",
            "warn",
        ),
        (
            "evaluated",
            {"configurations": 2, "datasets": 1, "cases": 600, "synthetic": True,
             "least_automated": 0, "most_automated": 9},
            "evaluated, 2 configurations, 600 synthetic cases, 0 to 9 critical automated",
            "warn",
        ),
        (
            "evaluated",  # every configuration automated the same number: said once
            {"configurations": 2, "datasets": 1, "cases": 600, "synthetic": True,
             "least_automated": 9, "most_automated": 9},
            "evaluated, 2 configurations, 600 synthetic cases, 9 critical automated",
            "warn",
        ),
        (
            "evaluated",
            {"configurations": 3, "datasets": 2, "cases": None, "synthetic": True,
             "least_automated": 0, "most_automated": 0},
            "evaluated, 3 configurations, 2 datasets, 0 critical automated",
            "info",
        ),
        (
            "evaluated",  # an agent that names no critical label: nothing is said of one
            {"configurations": 1, "datasets": 1, "cases": 250, "synthetic": False,
             "least_automated": None, "most_automated": None},
            "evaluated, 250 cases",
            "info",
        ),
    ],
)  # fmt: skip
def test_the_badge_s_words_for_each_state(
    state: str, totals: dict[str, Any] | None, words: str, tone: str
) -> None:
    assert badge.message(state, totals) == (words, tone)
    assert parsed(badge.svg(words, tone)) == ("Mendari", words)
    assert f'fill="{badge.TONES[tone]}"' in badge.svg(words, tone)


def _luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def test_the_badge_reads_in_every_tone_and_is_as_wide_as_its_text() -> None:
    """White text on each fill meets WCAG AA (4.5:1), no tone is green (nothing here is a pass),
    and a badge is as wide as its two texts with their padding: a text never runs off it."""
    white = _luminance(badge.TEXT_FILL)
    for fill in (*badge.TONES.values(), badge.LABEL_FILL):
        assert (white + 0.05) / (_luminance(fill) + 0.05) >= 4.5, fill
        red, green, blue = (int(fill[i : i + 2], 16) for i in (1, 3, 5))
        assert not (green > red and green > blue), fill
    assert badge.width("Mendari") == 441 and badge.width("") == 0
    assert badge.width("é中") == 2 * 110  # a character with no width here is a wide one
    for words in (
        "recalled",
        "evaluated, 3 configurations, 2 datasets, 0 to 12 critical automated",
    ):
        root = ElementTree.fromstring(badge.svg(words))
        label, text = root.iter(f"{SVG}text")
        widths = [int(label.attrib["textLength"]), int(text.attrib["textLength"])]
        assert widths == [badge.width("Mendari"), badge.width(words)]
        assert int(root.attrib["width"]) * 10 >= sum(widths) + 4 * badge.PAD
        assert int(root.attrib["width"]) * 10 < sum(widths) + 4 * badge.PAD + 10
        assert root.attrib["height"] == "20" and root.attrib["viewBox"].endswith(" 20")


HOSTILE = (
    '"><script>alert(1)</script>',
    "</text><a href='javascript:alert(1)'>x</a>",
    "' onload='alert(1)",
    "<![CDATA[<script>]]>",
    "&lt;script&gt; &amp; &#x3c;",
    "]]><svg onload=alert(1)>",
    "\x00\x08\x1b[2J\x7f",
    "‮⁦ right to left",
    "x" * 5000,
    "",
)


@pytest.mark.parametrize("text", HOSTILE)
def test_no_text_can_put_markup_in_a_badge(text: str) -> None:
    """Whatever the two texts hold, the badge is the same six kinds of element with the same
    attributes, and the texts are text."""
    plain = badge.svg("fine")
    for image in (badge.svg(text), badge.svg("fine", label=text), badge.svg(text, tone=text)):
        label, words = parsed(image)  # well-formed, and made of a badge's parts only
        assert len(label) <= badge.MAX_TEXT and len(words) <= badge.MAX_TEXT
        # As many tags as a plain badge has: the texts added none.
        assert image.count("<") == plain.count("<") and image.count(">") == plain.count(">")
    shown = parsed(badge.svg(text))[1]
    if text and text.isprintable():
        assert shown == text[: badge.MAX_TEXT]  # as written, as text
    assert all(c.isprintable() for c in shown) and shown


@pytest.mark.parametrize(
    "totals",
    [
        {"configurations": "<b>2</b>", "datasets": [], "cases": "600<script>", "synthetic": "yes",
         "least_automated": "0", "most_automated": "<svg>"},
        {"configurations": -3, "datasets": 2.5, "cases": True, "most_automated": float("nan")},
        {"cases": {"n": 1}, "most_automated": None, "least_automated": 4},
        {},
    ],
)  # fmt: skip
def test_a_total_that_is_not_a_whole_count_is_left_unsaid(totals: dict[str, Any]) -> None:
    words, tone = badge.message("evaluated", totals)
    assert words == "evaluated" and tone == "info"


def test_a_hostile_name_in_the_address_never_reaches_the_badge(registry: Registry) -> None:
    """A name that is not an agent's is never looked up and never written: the badge says the
    agent is not there, in its own words."""
    for namespace, name in (
        ("%3Cscript%3E", "x"),
        ("mendari", "%22%3E%3Csvg%20onload%3Dalert(1)%3E"),
        ("MENDARI", "Portal"),
        ("a" * 64, "b"),
        ("mendari", "x%00y"),
        ("mendari", "%E2%80%AEevil"),
    ):
        answer = registry.client.get(f"/v1/agents/{namespace}/{name}/badge.svg")
        assert answer.status_code == 404, (namespace, name)
        assert parsed(answer.content) == ("Mendari", "agent not found")
        assert b"script" not in answer.content and b"onload" not in answer.content
    # A name with a slash in it is another address, which no route answers: no image at all.
    elsewhere = registry.client.get("/v1/agents/mendari/..%2F..%2Fx/badge.svg")
    assert elsewhere.status_code == 404 and elsewhere.json() == {"detail": "Not Found"}


def test_the_badge_s_headers(published: Registry) -> None:
    assert send(published, signed(published.key, "agent")).status_code == 201
    answer = published.client.get(f"{AGENT}/badge.svg")
    headers = answer.headers
    assert headers["content-type"] == "image/svg+xml"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == f"{REGISTRY_CSP}; sandbox"
    assert headers["cache-control"] == "public, max-age=300"
    assert headers["cross-origin-resource-policy"] == "cross-origin"  # a README anywhere shows it
    assert headers["x-frame-options"] == "DENY" and headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in headers and "etag" not in headers
    assert answer.content.isascii() and len(answer.content) < 2000
    assert BADGE_HEADERS.keys() == {
        "Content-Security-Policy",
        "Cross-Origin-Resource-Policy",
        "Cache-Control",
    }
    # The same image whoever asks, and whatever they send: nothing of the request is read.
    for sent in (
        {"authorization": f"Bearer {published.token}"},
        {"authorization": "Bearer ohr_made-up"},
        {"cookie": "mendari_session=abc", "referer": "https://example.org/readme"},
        {"accept": "text/html", "user-agent": "<script>"},
    ):
        again = published.client.get(f"{AGENT}/badge.svg", headers=sent)
        assert (
            again.content == answer.content
            and again.headers["cache-control"] == "public, max-age=300"
        )
    assert published.client.get(f"{AGENT}/badge.svg?x=%3Cscript%3E").content == answer.content
    # A badge that says the agent is not there is an image another site may show, too.
    missing = published.client.get("/v1/agents/mendari/nothing/badge.svg")
    assert missing.headers["cross-origin-resource-policy"] == "cross-origin"
    assert missing.headers["content-security-policy"].endswith("; sandbox")
    # Every other answer keeps the registry's own headers: never cached, never embedded.
    for path in (f"{AGENT}/evaluation", AGENT, f"{AGENT}/{demo().version}/evaluations"):
        other = published.client.get(path)
        assert other.headers["cache-control"] == "no-store", path
        assert other.headers["cross-origin-resource-policy"] == "same-origin", path
        assert other.headers["content-security-policy"] == REGISTRY_CSP, path
    assert published.client.post(f"{AGENT}/badge.svg").status_code == 405


def test_the_badge_counts_against_the_read_limit_as_every_read_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "on,registry_read=3/min")
    with TestClient(create_registry_app(tmp_path / "limited.db")) as client:
        path = "/v1/agents/mendari/portal-message-agent/badge.svg"
        assert [client.get(path).status_code for _ in range(3)] == [404, 404, 404]
        refused = client.get(path)
        assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
        assert refused.json()["error"] == "RateLimited"
        # A refusal is the registry's own answer: not an image, not kept, not embedded.
        assert refused.headers["content-type"] == "application/json"
        assert refused.headers["cache-control"] == "no-store"
        assert refused.headers["cross-origin-resource-policy"] == "same-origin"
        # The one budget: the page's answer is refused with it, and so is any read.
        assert client.get(path.replace("badge.svg", "evaluation")).status_code == 429
        assert client.get("/v1/health").status_code == 429


def test_the_badge_under_the_engine_keeps_its_headers(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mounted at `/registry`, behind the engine's own guard and headers: still an image any
    site's page may show, read without a credential, and no engine route answers for it."""
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    with served(db_target, tmp_path) as world:
        _mounted_badge(world)


def _mounted_badge(world: World) -> None:
    token, pack = world.published()
    world.registry.decide_claim("acme", approve=True, by="operator", note="read")
    path = f"/registry/v1/agents/{pack.id}/badge.svg"
    for sent in ({}, CROSS_SITE, {"sec-fetch-site": "cross-site", "sec-fetch-dest": "image"}):
        answer = world.http.get(path, headers=sent)
        assert answer.status_code == 200, (sent, answer.text)
        assert parsed(answer.content) == ("Mendari", "no evaluation published")
        assert answer.headers["content-type"] == "image/svg+xml"
        assert answer.headers["cache-control"] == "public, max-age=300"
        assert answer.headers["cross-origin-resource-policy"] == "cross-origin"
        assert answer.headers["content-security-policy"] == f"{REGISTRY_CSP}; sandbox"
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert "set-cookie" not in answer.headers
    shown = world.http.get(f"/registry/v1/agents/{pack.id}/evaluation")
    assert shown.status_code == 200 and shown.json()["state"] == "none"
    assert shown.headers["cache-control"] == "no-store"
    del token


# -- where the Hub shows it -----------------------------------------------------------------------


def test_the_registry_names_its_hub_s_page_when_it_is_told_where_the_hub_is(
    published: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert page(published)["links"] == {"page": None}
    monkeypatch.setenv("MENDARI_REGISTRY_HUB_URL", "https://hub.example/")
    assert page(published)["links"] == {
        "page": "https://hub.example/agents/mendari/portal-message-agent/evaluation"
    }
    monkeypatch.setenv("MENDARI_REGISTRY_HUB_URL", "http://127.0.0.1:3100/at")
    assert evaluation_page.page_url("a", "b") == "http://127.0.0.1:3100/at/agents/a/b/evaluation"


@pytest.mark.parametrize(
    "value",
    [
        "hub.example",
        "http://hub.example",
        "https://user:secret-value@hub.example",
        "https://hub.example/?next=1",
        "https://hub.example/#top",
        "javascript:alert(1)",
        "https://hub.example/a b",
        "https://",
    ],
)
def test_a_hub_address_that_is_not_one_stops_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_HUB_URL", value)
    with pytest.raises(MendariError, match="MENDARI_REGISTRY_HUB_URL") as refused:
        create_registry_app(tmp_path / "registry.db")
    assert "secret-value" not in str(refused.value)  # the setting is named, never its value
