# SPDX-License-Identifier: Apache-2.0
"""The publisher's one step on the command line: `mendari push AGENT --key K --evaluate DATASET`
publishes the version, then evaluates each configuration on the agent's own dataset and
publishes every one, signed with the same key (`hub/evaluations.py`); `mendari eval --publish`
does it for one. Then it says where the Agent Hub shows the evaluation and prints the Markdown
that puts its badge in a README (`hub/evaluation_page.py`, `hub/badge.py`).

Against a registry in this process, over HTTP; the demo agent's synthetic test set only; every
key and token is made here."""

from __future__ import annotations

import copy
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from mendari.cli import _linkable, _publish_run, main
from mendari.core.pack import load_pack
from mendari.errors import HubError, MendariError
from mendari.evals.harness import Evaluation
from mendari.evals.ledger import Ledger
from mendari.evals.result import ERROR
from mendari.hub import evaluations
from tests.conftest import Registry
from tests.test_evaluation_page import page, three_readers
from tests.test_hub_client import Hub, hub  # noqa: F401 (the in-process registry over HTTP)
from tests.test_publisher_evaluations import DEMO_DIR, demo, run, send


@pytest.fixture
def published(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> Registry:
    """The demo agent published in the `mendari` namespace; the registry re-runs evaluations."""
    assert registry.publish(demo()).status_code == 201
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "on")
    return registry


@pytest.mark.parametrize(
    "url",
    [
        "http://hub.example/agents/a/b/evaluation",
        "https://hub.example/agents/a/b/evaluation)](https://evil.example",
        "https://hub.example/agents/a/b/evaluation\x1b[2J",
        "https://hub.example/agents/a/c/evaluation",
        "https://u:p@hub.example/agents/a/b/evaluation",
        "https://hub.example/x y/agents/a/b/evaluation",
        "javascript:alert(1)//agents/a/b/evaluation",
        "https://hub.example/" + "x" * 500 + "/agents/a/b/evaluation",
        None,
        17,
    ],
)
def test_an_address_a_registry_gives_is_printed_only_when_it_is_one(url: Any) -> None:
    assert _linkable(url, "/agents/a/b/evaluation") is None
    fine = "https://hub.example/agents/a/b/evaluation"
    assert _linkable(fine, "/agents/a/b/evaluation") == fine
    local = "http://127.0.0.1:3100/agents/a/b/evaluation"
    assert _linkable(local, "/agents/a/b/evaluation") == local


# -- the command line -----------------------------------------------------------------------------


def key_file(tmp_path: Path, key: Any) -> str:
    path = tmp_path / "publisher.key"
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    return str(path)


def test_push_evaluate_publishes_the_version_and_every_configuration_in_one_step(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "on")
    token, key = hub.namespace("mendari")
    assert main(["login", "--registry", hub.url, "--token", token]) == 0
    capsys.readouterr()
    command = ["push", str(DEMO_DIR), "--key", key_file(tmp_path, key), "--evaluate", "test"]
    assert main([*command, "--labelled-by", "The script that wrote the cases"]) == 0
    out = capsys.readouterr().out
    version = demo().version
    assert f"published mendari/portal-message-agent@{version} {demo().digest}" in out
    assert "  reader=agent: signed by the publisher; not re-run: calls a model" in out
    assert "  reader=rules: re-run by the registry: matched" in out
    assert (
        f"published 2 of 2 evaluations of mendari/portal-message-agent@{version} on test "
        "(600 cases), counts only, signed"
    ) in out
    # The loop closed: the README's line, the badge at the registry and what it reads.
    badge_url = f"{hub.url}/v1/agents/mendari/portal-message-agent/badge.svg"
    answer = f"{hub.url}/v1/agents/mendari/portal-message-agent/evaluation"
    assert f"badge: [![Mendari evaluation]({badge_url})]({answer})" in out
    assert "this registry names no Agent Hub" in out and "MENDARI_REGISTRY_HUB_URL" in out
    assert re.search(
        r"it reads: Mendari \| evaluated, 2 configurations, 600 synthetic cases, ", out
    )
    with hub.client() as reader:
        shown = reader.evaluation("mendari", "portal-message-agent")
        assert reader.badge_url("mendari", "portal-message-agent") == badge_url
        assert reader.evaluation_url("mendari", "portal-message-agent") == answer
    [dataset] = shown["datasets"]
    assert {c["label"] for c in dataset["configurations"]} == {"reader=agent", "reader=rules"}
    assert dataset["labelled_by"] == ["The script that wrote the cases"]
    # Again: the version is not pushed twice, and each evaluation supersedes its own.
    assert main([*command, "--configuration", "reader=rules"]) == 0
    out = capsys.readouterr().out
    assert f"already published: mendari/portal-message-agent@{version}" in out
    assert "published 1 of 1 evaluations" in out and "reader=agent" not in out
    with hub.client() as reader:
        again = reader.evaluations("mendari", "portal-message-agent", version)
    assert len(again["evaluations"]) == 2 and again["superseded"] == 1


class Sent:
    """What `_publish_run` asks of a registry client, answered by the test's registry."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def publish_evaluation(self, record: dict[str, Any]) -> dict[str, Any]:
        answer = send(self.registry, record)
        assert answer.status_code == 201, answer.text
        taken: dict[str, Any] = answer.json()
        return taken


def test_a_run_in_which_a_provider_failed_says_on_how_many_cases(
    published: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    """Where a provider failed the engine sent the case to a person, so those cases say nothing
    of the configuration: the record counts them, the command says so, and the page shows it."""
    whole = run("agent")
    cases = copy.deepcopy(whole.ledger.cases)
    for case in cases[:3]:
        next(iter(case["q"].values()))[ERROR] = "provider_timeout"
    failed = Evaluation(whole.report, Ledger(whole.ledger.header, cases), whole.usage)
    assert evaluations.build(demo(), whole, key=published.key)["claimed"]["failed_cases"] == 0
    taken = _publish_run(Sent(published), demo(), failed, published.key, None)
    assert taken["status"] == "live"
    assert "note: a provider failed on 3 of 600 cases of this run" in capsys.readouterr().err
    [dataset] = page(published)["datasets"]
    assert dataset["configurations"][0]["claimed"]["failed_cases"] == 3
    _publish_run(Sent(published), demo(), run("rules"), published.key, None)
    assert "note:" not in capsys.readouterr().err  # a run with no failure says nothing


def test_a_registry_from_before_the_second_format_is_said_to_be_one(published: Registry) -> None:
    said_before = "not a mendari.publisher-evaluation/1 record: format"

    class Earlier:
        def publish_evaluation(self, record: dict[str, Any]) -> dict[str, Any]:
            raise HubError(f"the registry answered HTTP 422: {said_before}")

    with pytest.raises(HubError) as earlier:
        _publish_run(Earlier(), demo(), run("rules"), published.key, None)
    assert said_before in str(earlier.value)
    assert "a release before mendari.publisher-evaluation/2" in str(earlier.value)

    class Refusing:
        def publish_evaluation(self, record: dict[str, Any]) -> dict[str, Any]:
            raise HubError("the registry answered HTTP 409: version: it is recalled", 409)

    with pytest.raises(HubError) as refused:  # any other refusal, as it is
        _publish_run(Refusing(), demo(), run("rules"), published.key, None)
    assert str(refused.value).endswith("it is recalled") and refused.value.status == 409


def test_the_command_line_prints_the_hub_s_page_when_the_registry_names_one(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_HUB_URL", "https://hub.example")
    token, key = hub.namespace("mendari")
    assert main(["login", "--registry", hub.url, "--token", token]) == 0
    assert main(["push", str(DEMO_DIR), "--key", key_file(tmp_path, key)]) == 0
    capsys.readouterr()
    published_one = [
        *("eval", str(DEMO_DIR), "--dataset", "test", "--provider", "reader=agent"),
        *("--publish", "--key", key_file(tmp_path, key), "--labelled-by", "Two nurses"),
    ]
    assert main(published_one) == 0
    err = capsys.readouterr().err
    shown = "https://hub.example/agents/mendari/portal-message-agent/evaluation"
    badge_url = f"{hub.url}/v1/agents/mendari/portal-message-agent/badge.svg"
    assert f"page:  {shown}\n" in err
    assert f"badge: [![Mendari evaluation]({badge_url})]({shown})\n" in err
    assert "it reads: Mendari | evaluated, 600 synthetic cases, " in err
    with hub.client() as reader:
        [dataset] = reader.evaluation("mendari", "portal-message-agent")["datasets"]
    assert dataset["labelled_by"] == ["Two nurses"]


def test_push_evaluate_refuses_what_it_cannot_publish_before_it_sends_anything(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token, key = hub.namespace("mendari")
    assert main(["login", "--registry", hub.url, "--token", token]) == 0
    push = ["push", str(DEMO_DIR), "--key", key_file(tmp_path, key)]
    data = str(DEMO_DIR / "data" / "test.jsonl")
    for arguments, words in (
        (["--evaluate", data], "never a file"),
        (["--evaluate", "nothing"], "never a file"),
        (["--evaluate", "examples"], "is of at least 100 cases, and examples has 12"),
        (["--evaluate", "test", "--configuration", "reader=nothing"], "reader=nothing"),
        (["--evaluate", "test", "--configuration", "writer=rules"], "no slot 'writer'"),
        (["--evaluate", "test", "--labelled-by", "two\nlines"], "--labelled-by is one line"),
        (["--configuration", "reader=rules"], "--configuration is for --evaluate DATASET"),
        (["--labelled-by", "x"], "--labelled-by is for --evaluate DATASET"),
        (["--workers", "2"], "--workers is for --evaluate DATASET"),
    ):
        assert main([*push, *arguments]) == 1, arguments
        assert words in capsys.readouterr().err, arguments
    with hub.client() as reader:  # nothing was pushed
        assert reader.search(q="portal") == []
    assert main(["eval", str(DEMO_DIR), "--dataset", "test", "--labelled-by", "x"]) == 1
    assert "--labelled-by is said of a published evaluation" in capsys.readouterr().err
    small = ["eval", str(DEMO_DIR), "--dataset", "examples", "--publish"]
    assert main([*small, "--key", key_file(tmp_path, key)]) == 1
    assert "--publish: a published evaluation is of at least 100 cases" in capsys.readouterr().err


def test_push_evaluate_says_when_the_version_is_another_build(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token, key = hub.namespace("mendari")
    assert main(["login", "--registry", hub.url, "--token", token]) == 0
    assert main(["push", str(DEMO_DIR), "--key", key_file(tmp_path, key)]) == 0
    changed = tmp_path / "changed"
    shutil.copytree(DEMO_DIR, changed)
    manifest = (changed / "agent.yaml").read_text(encoding="utf-8")
    (changed / "agent.yaml").write_text(manifest.replace("auto: 0.90", "auto: 0.95", 1))
    capsys.readouterr()
    push = ["push", str(changed), "--key", key_file(tmp_path, key), "--evaluate", "test"]
    assert main(push) == 1
    err = capsys.readouterr().err
    assert "is published as another build" in err and "give this one a new version" in err
    # Without --evaluate a second push is refused as it always was.
    assert main(["push", str(DEMO_DIR), "--key", key_file(tmp_path, key)]) == 1
    assert "versions are immutable" in capsys.readouterr().err


def test_an_agent_with_many_configurations_is_asked_which_to_evaluate(tmp_path: Path) -> None:
    from mendari.cli import MAX_EVALUATED, _evaluated_configurations

    pack = three_readers(tmp_path)
    assert _evaluated_configurations(pack, []) == [
        {"reader": "agent"},
        {"reader": "rules"},
        {"reader": "cautious"},
    ]
    named = _evaluated_configurations(pack, ["reader=rules", "reader=cautious", "reader=rules"])
    assert named == [{"reader": "rules"}, {"reader": "cautious"}]  # once each
    root = tmp_path / "many"
    shutil.copytree(pack.root, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text(encoding="utf-8"))
    variants = manifest["providers"]["reader"]["variants"]
    for index in range(MAX_EVALUATED):
        variants[f"more{index}"] = dict(variants["rules"])
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    with pytest.raises(MendariError, match=r"has 11 configurations: name the ones to evaluate"):
        _evaluated_configurations(load_pack(root), [])
