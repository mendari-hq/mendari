# SPDX-License-Identifier: Apache-2.0
"""Publisher evaluations, signed and checked (ADR-0020, `hub/evaluations.py`): a publisher signs
an evaluation of a listed version per configuration and public dataset; the registry binds it
to the signed archive, re-runs a local configuration and refuses counts that are not its own,
and serves figures it works out itself. The demo agent's synthetic test set only; every key and
token is made here."""

from __future__ import annotations

import base64
import copy
import json
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from mendari.cli import main
from mendari.core.decision import Decision
from mendari.core.pack import Pack, load_pack
from mendari.evals import figures
from mendari.evals.harness import Evaluation, evaluation, resolve_dataset
from mendari.hub import archive, evaluations, site_signing, yank
from mendari.hub.client import RegistryClient
from tests.conftest import ROOT, Registry
from tests.test_hub_client import Hub, hub  # noqa: F401 (the in-process registry over HTTP)

DEMO_DIR = ROOT / "agents" / "portal-message-agent"
SECRET = "do-not-echo-this-value"


@cache
def demo() -> Pack:
    return load_pack(DEMO_DIR)


@cache
def run(variant: str) -> Evaluation:
    """The demo agent's run on its synthetic test set with `reader=variant` (cached: the
    runs are the same in every test, each test signs them with its own key)."""
    pack = demo()
    decision = Decision(pack, providers={"reader": variant}, origin_cap=False)
    return evaluation(decision, resolve_dataset(pack, "test"))


def signed(registry_key: Any, variant: str = "rules", **changes: Any) -> dict[str, Any]:
    """A signed evaluation of the demo agent, its fields changed first (then signed again)."""
    record = evaluations.build(demo(), run(variant), key=registry_key)
    if not changes:
        return record
    fields = {k: v for k, v in record.items() if k not in ("key_id", "signature")}
    fields.update(changes)
    return evaluations.sign(fields, registry_key)


def path(version: str | None = None) -> str:
    return f"/v1/packs/mendari/portal-message-agent/{version or demo().version}/evaluations"


def send(registry: Registry, record: Any, token: str | None = None) -> Any:
    return registry.client.post(
        path(), json=record, headers={"authorization": f"Bearer {token or registry.token}"}
    )


def stored(registry: Registry) -> list[dict[str, Any]]:
    with registry.operator._db.session(exclusive=False) as db:
        return db.all("SELECT * FROM hub_evaluations ORDER BY created_at, id")


def events(registry: Registry, name: str) -> list[dict[str, Any]]:
    with registry.operator._db.session(exclusive=False) as db:
        rows = db.all("SELECT * FROM hub_events WHERE event = ? ORDER BY seq", (name,))
    return [json.loads(row["payload"]) for row in rows]


@pytest.fixture
def published(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> Registry:
    """The demo agent published in the `mendari` namespace; the registry re-runs evaluations."""
    assert registry.publish(demo()).status_code == 201
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "on")
    return registry


def refused(answer: Any, status: int, words: str) -> None:
    assert answer.status_code == status, answer.text
    assert words in answer.json()["detail"], answer.text
    assert SECRET not in answer.text


def test_a_local_configuration_is_re_run_and_accepted(published: Registry) -> None:
    record = signed(published.key)
    answer = send(published, record)
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["id"] == evaluations.evaluation_id(record)
    assert (body["rerun"], body["status"]) == ("matched", "live")
    assert body["rerun_words"] == "re-run by the registry: matched"
    [row] = stored(published)
    assert (row["rerun"], row["configuration"], row["dataset"]) == (
        "matched",
        '{"reader":"rules"}',
        "test",
    )
    [event] = events(published, "evaluation.published")
    assert event["evaluation_id"] == body["id"] and event["rerun"] == "matched"
    assert published.operator.verify()["ok"]


def test_the_figures_are_the_registrys_from_the_counts(published: Registry) -> None:
    record = signed(published.key)
    assert send(published, record).status_code == 201
    listing = published.client.get(path()).json()
    assert listing["format"] == "mendari.publisher-evaluations/1"
    assert "can be tuned" in listing["note"] and listing["superseded"] == 0
    [shown] = listing["evaluations"]
    report = record["report"]
    primary = report["questions"][report["primary"]]
    assert shown["figures"]["accuracy"] == figures.of(primary["right"], primary["n"])
    assert shown["figures"]["accuracy"]["low"] < primary["right"] / primary["n"]
    [critical] = report["decisions"]["critical"]
    [served] = shown["figures"]["critical"]
    assert served["detected"] == figures.of(critical["detected"], critical["n"])
    assert served["automated_misses"] == critical["auto_missed"]
    assert shown["figures"]["false_automation"] == figures.of(
        report["decisions"]["false_automation"], primary["n"]
    )
    assert shown["figures"]["outcomes"]["AUTO"]["count"] == report["decisions"]["outcomes"]["AUTO"]
    assert shown["dataset"] == {"name": "test", "n": 600, "sha256": record["dataset"]["sha256"]}
    assert shown["claimed"] == record["claimed"] and shown["models"] == record["models"]
    assert shown["excluded"] is None and shown["key_id"] == record["key_id"]


def test_a_local_evaluation_with_one_count_changed_is_refused(published: Registry) -> None:
    record = signed(published.key)
    report = copy.deepcopy(record["report"])
    report["decisions"]["false_automation"] += 1  # still a valid count, not the run's
    answer = send(published, signed(published.key, report=report))
    refused(answer, 409, "re-run counted otherwise")
    assert stored(published) == []


def test_a_configuration_that_calls_a_model_is_accepted_and_not_re_run(
    published: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo agent's `agent` variant answers offline from its mock file here, but its
    provider type is `agent`, a model type: the registry goes by the type and never runs it."""
    from mendari.hub import regression

    monkeypatch.setattr(regression, "rerun", lambda *a: pytest.fail("a model was re-run"))
    record = signed(published.key, "agent")
    assert record["report"]["remote_consent"] is True
    answer = send(published, record)
    assert answer.status_code == 201, answer.text
    assert answer.json()["rerun_words"] == "signed by the publisher; not re-run: calls a model"
    [shown] = published.client.get(path()).json()["evaluations"]
    assert shown["rerun"] == "calls_a_model" and shown["models"] == ["mock-portal-agent"]


def test_both_configurations_are_listed_side_by_side_in_a_fixed_order(published: Registry) -> None:
    assert send(published, signed(published.key, "rules")).status_code == 201
    assert send(published, signed(published.key, "agent")).status_code == 201
    listed = published.client.get(path()).json()["evaluations"]
    assert [e["configuration"] for e in listed] == [{"reader": "agent"}, {"reader": "rules"}]
    assert all("best" not in json.dumps(e) and "rank" not in json.dumps(e) for e in listed)


def test_with_re_runs_off_a_local_configuration_is_refused(
    published: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "off")
    refused(send(published, signed(published.key)), 409, "does not re-run evaluations")
    assert send(published, signed(published.key, "agent")).status_code == 201


def test_a_wrong_signature_is_refused(published: Registry) -> None:
    record = signed(published.key)
    forged = {**record, "signature": base64.b64encode(b"\0" * 64).decode()}
    refused(send(published, forged), 403, "signature")
    changed = copy.deepcopy(record)
    changed["report"]["decisions"]["false_automation"] += 1  # the old signature, a new body
    refused(send(published, changed), 403, "signature")
    assert stored(published) == []


def test_another_namespaces_key_is_refused(published: Registry) -> None:
    other_token = published.namespace("other-publisher")
    private, public = archive.generate_key()
    added = published.client.post(
        "/v1/namespaces/other-publisher/keys",
        json={"public_pem": public.decode()},
        headers={"authorization": f"Bearer {other_token}"},
    )
    assert added.status_code == 201
    stranger = archive.load_private_key(private)
    refused(send(published, signed(stranger)), 403, "key_id: it is not a key of this namespace")
    # Its own token cannot publish to this namespace either.
    refused(send(published, signed(published.key), other_token), 403, "a token for namespace")


def test_a_non_publisher_token_is_refused_before_the_body_is_read(published: Registry) -> None:
    answer = published.client.post(path(), content=b"{" * 10)
    assert answer.status_code == 403
    answer = send(published, signed(published.key), "not-a-token")
    assert answer.status_code == 403
    assert stored(published) == []


def test_a_revoked_keys_evaluations_are_listed_and_left_out(published: Registry) -> None:
    record = signed(published.key, "agent")
    assert send(published, record).status_code == 201
    published.operator.revoke_key("mendari", record["key_id"], reason="lost laptop", by="operator")
    [shown] = published.client.get(path()).json()["evaluations"]
    assert shown["excluded"] == "key revoked"
    refused(send(published, signed(published.key)), 403, "key_id: that key is revoked")
    # A revoked key signs no archive either: the namespace's only key was this one.
    answer = published.publish(demo())
    assert answer.status_code == 422 and "has no publisher key" in answer.text
    [event] = events(published, "key.revoked")
    assert event["key_id"] == record["key_id"]
    assert published.operator.verify()["ok"]
    # Registering it again says it is revoked, rather than a 201 for a key that signs nothing.
    public = archive.public_pem(published.key.public_key()).decode()
    again = published.client.post(
        "/v1/namespaces/mendari/keys",
        json={"public_pem": public},
        headers={"authorization": f"Bearer {published.token}"},
    )
    assert again.status_code == 409 and "revoked" in again.json()["detail"]
    assert "mendari keys generate" in again.json()["detail"]


def test_the_same_body_again_is_idempotent_and_a_superseded_one_is_refused(
    published: Registry,
) -> None:
    first = signed(published.key, "agent")
    one = send(published, first)
    assert one.status_code == 201
    again = send(published, first)  # the same signed bytes
    assert again.status_code == 201 and again.json() == one.json()
    assert len(stored(published)) == 1 and len(events(published, "evaluation.published")) == 1
    later = (datetime.now(UTC) + timedelta(seconds=2)).isoformat(timespec="seconds")
    second = signed(published.key, "agent", created_at=later.replace("+00:00", "Z"))
    assert send(published, second).status_code == 201
    refused(send(published, first), 409, "superseded")


def test_superseding_keeps_both_in_the_chain(published: Registry) -> None:
    first = signed(published.key)
    assert send(published, first).status_code == 201
    later = (datetime.now(UTC) + timedelta(seconds=2)).isoformat(timespec="seconds")
    second = signed(published.key, created_at=later.replace("+00:00", "Z"))
    assert send(published, second).status_code == 201
    rows = stored(published)
    assert [r["status"] for r in rows] == ["superseded", "live"]
    published_events = events(published, "evaluation.published")
    assert [e["evaluation_id"] for e in published_events] == [r["id"] for r in rows]
    assert published_events[1]["supersedes"] == rows[0]["id"]
    [superseded] = events(published, "evaluation.superseded")
    assert superseded == {
        "evaluation_id": rows[0]["id"],
        "superseded_by": rows[1]["id"],
        "by": superseded["by"],
        "row": superseded["row"],
    }
    listing = published.client.get(path()).json()
    assert listing["superseded"] == 1 and len(listing["evaluations"]) == 1
    # One signed before the live one does not take its place (its time set, not the clock's:
    # a slow run would otherwise sign it after the live one).
    earlier = (datetime.fromisoformat(later) - timedelta(seconds=1)).isoformat(timespec="seconds")
    older = signed(published.key, created_at=earlier.replace("+00:00", "Z"))
    refused(send(published, older), 409, "signed later is live")
    assert published.operator.verify()["ok"]


def test_a_digest_that_is_not_the_versions_is_refused(published: Registry) -> None:
    record = signed(published.key)
    pack = {**record["pack"], "digest": "sha256:" + "1" * 64}
    report = {**record["report"], "pack": pack}
    refused(send(published, signed(published.key, pack=pack, report=report)), 409, "pack.digest")


@pytest.mark.parametrize(
    ("dataset", "words"),
    [
        ({"name": SECRET}, "dataset.name: it is not a dataset the agent's manifest declares"),
        ({"name": "data/test.jsonl"}, "not a mendari.publisher-evaluation/1 record: dataset.name"),
        ({"sha256": "0" * 64}, "dataset.sha256: it is not the archived dataset's"),
    ],
)
def test_a_dataset_not_in_the_archive_is_refused(
    published: Registry, dataset: dict[str, Any], words: str
) -> None:
    record = signed(published.key)
    changed = {**record["dataset"], **dataset}
    refused(send(published, signed(published.key, dataset=changed)), 422, words)
    assert stored(published) == []


@pytest.mark.parametrize(
    ("configuration", "words"),
    [
        ({"reader": SECRET}, "configuration: it names a variant the agent does not declare"),
        ({"reader": "rules", SECRET: "rules"}, "configuration: it must name a variant for each"),
    ],
)
def test_an_unknown_slot_or_variant_is_refused(
    published: Registry, configuration: dict[str, str], words: str
) -> None:
    refused(send(published, signed(published.key, configuration=configuration)), 422, words)


def test_a_recalled_or_deleted_version_is_refused(published: Registry) -> None:
    record = signed(published.key)
    found = published.operator.version("mendari", "portal-message-agent", demo().version)
    recall = yank.sign(
        pack="mendari/portal-message-agent",
        version=demo().version,
        digest=found["digest"],
        archive_sha256=found["archive_sha256"],
        reason="a test",
        severity="low",
        signer="publisher",
        key=published.key,
        key_id=record["key_id"],
    )
    answer = published.client.post(
        path().removesuffix("/evaluations") + "/yank",
        json={"record": recall},
        headers={"authorization": f"Bearer {published.token}"},
    )
    assert answer.status_code == 201, answer.text
    refused(send(published, record), 409, "version: it is recalled, deleted or not listed")
    published.operator.delete(
        "mendari", "portal-message-agent", demo().version, reason="a test", by="operator"
    )
    refused(send(published, record), 409, "version: it is recalled, deleted or not listed")


def test_a_body_over_the_cap_is_refused(published: Registry) -> None:
    big = b'{"format": "' + b"x" * (evaluations.MAX_BYTES + 1) + b'"}'
    answer = published.client.post(
        path(), content=big, headers={"authorization": f"Bearer {published.token}"}
    )
    assert answer.status_code == 413


def test_a_record_of_another_kind_or_time_is_refused(published: Registry) -> None:
    record = signed(published.key)
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    stale = signed(published.key, created_at=old.replace("+00:00", "Z"))
    refused(send(published, stale), 422, "created_at: it is not now")
    # A site's signature over the same fields is not a publisher's, nor the reverse.
    fields = {k: v for k, v in record.items() if k not in ("signature",)}
    as_site = base64.b64encode(published.key.sign(site_signing.signed_bytes(fields))).decode()
    refused(send(published, {**record, "signature": as_site}), 403, "signature")
    extra = {**record, "note": SECRET}
    refused(send(published, extra), 422, "not a mendari.publisher-evaluation/1 record")
    with_null = copy.deepcopy(record["report"])
    with_null["decisions"]["outcomes"]["BLOCK"] = None
    refused(send(published, signed(published.key, report=with_null)), 422, "no null cell")


def test_a_version_not_published_has_no_evaluations(registry: Registry) -> None:
    assert registry.client.get(path()).status_code == 404


def test_the_cli_publishes_an_evaluation_end_to_end(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "on")
    token, key = hub.namespace("mendari")
    key_file = tmp_path / "publisher.key"
    key_file.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    assert main(["login", "--registry", hub.url, "--token", token]) == 0
    assert main(["push", str(DEMO_DIR), "--key", str(key_file)]) == 0
    capsys.readouterr()
    common = [str(DEMO_DIR), "--dataset", "test", "--publish", "--key", str(key_file)]
    assert main(["eval", *common, "--provider", "reader=rules"]) == 0
    assert "re-run by the registry: matched" in capsys.readouterr().err
    assert main(["eval", *common, "--provider", "reader=agent"]) == 0
    assert "not re-run: calls a model" in capsys.readouterr().err
    with hub.client() as reader:
        listed = reader.evaluations("mendari", "portal-message-agent", demo().version)
    assert [e["rerun"] for e in listed["evaluations"]] == ["calls_a_model", "matched"]
    # A file, --limit and --share are refused before anything runs.
    data = DEMO_DIR / "data" / "test.jsonl"
    for refused_args in (
        [str(DEMO_DIR), "--dataset", str(data), "--publish", "--key", str(key_file)],
        [*common, "--limit", "10"],
        [*common, "--share"],
        [str(DEMO_DIR), "--dataset", "test", "--publish"],
    ):
        assert main(["eval", *refused_args]) == 1
    assert "never a file" in capsys.readouterr().err


def test_the_client_sends_only_an_evaluation_it_can_read() -> None:
    with (
        RegistryClient("https://registry.example") as client,
        pytest.raises(Exception, match="not a"),
    ):
        client.publish_evaluation({"format": "mendari.site-submission/1"})
