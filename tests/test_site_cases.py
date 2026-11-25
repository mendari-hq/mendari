# SPDX-License-Identifier: Apache-2.0
"""An agent that holds a site's cases is never made into an archive (`hub/archive.py` `build`):
`mendari archive build` and `mendari push` both stop there, before anything is signed or sent, and
a registry refuses one a client sent without looking. A site's cases say what they are: a case
`mendari feedback export` (or its route) writes says it was exported
(`attributes.exported_by`). That mark alone is what is looked for (`core/site_cases.py`), in
every file an archive would hold.

The cases here are synthetic: two messages the triage fixture decided, reviewed and rated in a
scratch database, as a site's reviewers would. Every agent written to is a copy under a
temporary directory."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_agent
from mendari.cli import main
from mendari.core import site_cases
from mendari.core.agent import agent_digest
from mendari.errors import HubError
from mendari.hub import archive
from mendari.hub.client import RegistryClient
from mendari.server.app import create_app
from tests.conftest import FIXTURE_AGENTS, ROOT, TRIAGE_DIR, Registry, message

CALIBRATION = "data/calibration.jsonl"
TEST = "data/test.jsonl"
SHIPPED = agent_digest(TRIAGE_DIR)
TRIAGE = load_agent(TRIAGE_DIR)
# Not the reference agent's version: at its own, a registry reads a changed copy as a shadow.
VERSION = "9.9.9"
# How a refusal starts; what follows names each file that holds the cases and how many times it
# is marked, as for the agent the exported cases were put in (`holding`).
REFUSED = "the agent holds cases"
HELD = f"(2 in {CALIBRATION}, 2 in {TEST})"
EXPORTED = {"exported_by": "mendari feedback export"}
CALL_BACK = "Can someone call me back?"
REFILL = "Hi, I need a refill on my metoprolol. I have about a week's worth of pills left."


@pytest.fixture(autouse=True)
def _the_repositorys_own_agent_is_never_written_to() -> Iterator[None]:
    """Every test here works on a copy (`tasks/lessons.md`, 2026-11-10)."""
    yield
    assert agent_digest(TRIAGE_DIR) == SHIPPED, "a test changed the triage fixture"


def copy_of_the_triage(root: Path) -> Path:
    """The triage fixture at `VERSION`, as a site's own agent."""
    shutil.copytree(TRIAGE_DIR, root)
    manifest = root / "agent.yaml"
    shipped = f"version: {TRIAGE.version}"
    assert manifest.read_text().count(shipped) == 1
    manifest.write_text(manifest.read_text().replace(shipped, f"version: {VERSION}"))
    return root


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def as_archived(root: Path) -> dict[str, bytes]:
    """The agent's files as an archive would hold them."""
    return {f.relative_to(root).as_posix(): f.read_bytes() for f in archive.agent_files(root)}


def trusted(key: archive.Ed25519PrivateKey) -> dict[str, archive.Ed25519PublicKey]:
    return {archive.key_id(key.public_key()): key.public_key()}


@pytest.fixture(scope="module")
def key() -> archive.Ed25519PrivateKey:
    return archive.load_private_key(archive.generate_key()[0])


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "publisher.key"
    path.write_bytes(archive.generate_key()[0])
    return path


def names_no_case(said: str, cases: Iterable[Mapping[str, Any]]) -> bool:
    """Whether `said` is free of every one of these cases' id, decision and text."""
    return not any(
        part in said
        for case in cases
        for part in (
            case["id"],
            case["attributes"]["decision_id"],
            case["input"]["message"]["body"],
        )
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every request an HTTP client sends from here on, none of which arrives anywhere."""
    requests: list[str] = []

    def send(self: httpx.Client, request: httpx.Request, **_: Any) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        raise httpx.ConnectError("no registry in this test", request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    return requests


def test_a_record_is_read_for_the_marks_it_carries() -> None:
    plain = {"id": "a", "input": {}, "attributes": {"source": "review"}}
    exported = {"id": "c", "input": {}, "attributes": {"source": "review", **EXPORTED}}
    assert site_cases.carried([]) == {} and site_cases.carried([plain]) == {}
    assert site_cases.carried([plain, exported]) == EXPORTED
    assert site_cases.carried([exported, plain]) == EXPORTED
    # A record is read as a file of it is, the way an archive's builder reads it: a mark
    # anywhere in it counts, not only in its attributes.
    elsewhere = {"id": "d", "input": {"message": {"body": "x"}, **EXPORTED}}
    assert site_cases.carried([elsewhere]) == EXPORTED
    spelt = (
        b'{"exported_by":"mendari feedback export"}\n{"exported_by" : "mendari feedback export"}\n'
    )
    assert site_cases.times_marked(spelt) == 2
    # JSON may put a tab or a line break beside the colon: the mark is still there.
    spaced = (
        b'{"exported_by":\t"mendari feedback export",\n "x": 1}\n'
        b'{"exported_by"\r\n:\n  "mendari feedback export"}'
    )
    assert site_cases.times_marked(spaced) == 2


def test_no_agent_of_the_repository_holds_one() -> None:
    agents = sorted(
        path.parent
        for pattern in ("agents/*/agent.yaml", f"{FIXTURE_AGENTS.relative_to(ROOT)}/*/agent.yaml")
        for path in ROOT.glob(pattern)
    )
    assert TRIAGE_DIR in agents and ROOT / "agents" / "portal-message-agent" in agents
    for root in agents:
        assert site_cases.marked_files(as_archived(root)) == {}, root.name


# -- an export is a site's cases -----------------------------------------------------------------


def a_sites_database(path: Path) -> Path:
    """A site's database: two messages the triage fixture sent to a person, each resolved by a
    nurse and rated wrong with the right answer."""
    with AuditStore(path) as store:
        decision = Decision.from_path(TRIAGE_DIR, store=store)
        for body in (CALL_BACK, REFILL):
            made = decision(message(body))
            store.resolve_review(made.id, final_label="nurse", reviewer="rn-1")
            store.record_feedback(
                made.id, rating="down", expected={"question": "destination", "label": "nurse"}
            )
    return path


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """What `mendari feedback export --source reviews` wrote of a site's two reviewed
    decisions, shared by the tests that put it somewhere."""
    scratch = tmp_path_factory.mktemp("exported")
    db = a_sites_database(scratch / "audit.db")
    out = scratch / "reviewed.jsonl"
    argv = ["feedback", "export", str(TRIAGE_DIR), "--db", str(db), "--out", str(out)]
    assert main([*argv, "--source", "reviews"]) == 0
    return out.read_bytes()


def cases_of(exported: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in exported.splitlines()]


@pytest.fixture
def holding(exported: bytes, tmp_path: Path) -> Path:
    """A site's own copy of the triage fixture whose test and calibration sets each had the
    exported cases put in them."""
    root = copy_of_the_triage(tmp_path / "holding" / "triage")
    for dataset in (TEST, CALIBRATION):
        with (root / dataset).open("ab") as handle:
            handle.write(exported)
    return root


@pytest.mark.parametrize("source", ["ratings", "reviews"])
def test_every_record_an_export_writes_says_it_was_exported(
    source: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command's file and the route's answer, of ratings and of reviews: each record is a
    site's case and says so (`attributes.exported_by`), and is read so in the bytes written.
    The command says so too, and where the file belongs."""
    db = a_sites_database(tmp_path / "audit.db")
    out = tmp_path / "export.jsonl"
    argv = ["feedback", "export", str(TRIAGE_DIR), "--db", str(db), "--out", str(out)]
    assert main([*argv, "--source", source]) == 0
    said = capsys.readouterr().out
    assert "a site's cases" in said and "outside the agent's folder" in said
    served = TestClient(create_app(db_path=db)).get(
        f"/api/agents/{TRIAGE.id}/feedback/export", params={"source": source}
    )
    assert served.status_code == 200
    for written in (out.read_bytes(), served.content):
        records = [json.loads(line) for line in written.splitlines()]
        assert len(records) == 2
        assert all(record["attributes"].items() >= EXPORTED.items() for record in records)
        assert site_cases.times_marked(written) == 2


# -- no archive is made of an agent that holds them ------------------------------------------------


def test_an_agent_that_holds_learned_cases_is_not_built(
    holding: Path, exported: bytes, key: archive.Ed25519PrivateKey
) -> None:
    """Exported cases put in the datasets an agent evaluates and calibrates with."""
    with pytest.raises(HubError) as refused:
        archive.build(load_agent(holding), key)
    said = str(refused.value)
    # The files and how many each holds, in one line, and never a case.
    assert said.startswith(REFUSED) and HELD in said
    assert "\n" not in said
    assert names_no_case(said, cases_of(exported))
    assert "reviewed-" not in said


def test_a_refusal_names_a_few_files_and_counts_the_rest() -> None:
    case = b'{"id": "c", "input": {}, "attributes": {"exported_by": "mendari feedback export"}}\n'
    files = {f"data/part-{number}.jsonl": case * (number + 1) for number in range(7)}
    files["agent.yaml"] = b"id: site/agent\n"
    with pytest.raises(HubError) as refused:
        archive.refuse_site_cases(files)
    assert (
        "(1 in data/part-0.jsonl, 2 in data/part-1.jsonl, 3 in data/part-2.jsonl, "
        "4 in data/part-3.jsonl, 5 in data/part-4.jsonl, and 13 more in 2 other files)"
    ) in str(refused.value)
    del files["data/part-6.jsonl"]
    with pytest.raises(HubError, match=r"5 in data/part-4\.jsonl, and 6 more in 1 other file\)"):
        archive.refuse_site_cases(files)
    archive.refuse_site_cases({"agent.yaml": files["agent.yaml"]})  # none: nothing is raised


def test_a_refusal_is_one_line_whatever_a_file_is_called() -> None:
    marked = b'{"attributes": {"exported_by": "mendari feedback export"}}\n'
    with pytest.raises(HubError) as refused:
        archive.refuse_site_cases({"data/one\ntwo.jsonl": marked})
    said = str(refused.value)
    assert "\n" not in said and r"1 in 'data/one\ntwo.jsonl'" in said


class NeverUsed:
    """A publisher's key that fails the test if anything is signed with it, or asked of it."""

    def sign(self, data: bytes) -> bytes:
        raise AssertionError("something was signed")

    def public_key(self) -> Any:
        raise AssertionError("the key was read")


def test_it_is_refused_before_anything_is_signed(holding: Path) -> None:
    with pytest.raises(HubError, match=REFUSED):
        archive.build(load_agent(holding), cast(archive.Ed25519PrivateKey, NeverUsed()))


def test_agent_build_refuses_it_and_writes_no_archive(
    holding: Path,
    exported: bytes,
    key_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "dist"
    argv = ["archive", "build", str(holding), "--key", str(key_file), "--out", str(out)]
    assert main(argv) == 1
    printed = capsys.readouterr()
    assert printed.out == ""
    (line,) = printed.err.splitlines()  # one line
    assert line.startswith(f"error: {REFUSED}") and HELD in line
    assert "take them out before it is built or pushed" in line  # and what to do about it
    assert names_no_case(line, cases_of(exported)) and "Traceback" not in line
    assert not out.exists()


def test_push_refuses_it_before_anything_is_sent(
    holding: Path, key: archive.Ed25519PrivateKey
) -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    registry = RegistryClient(
        "https://registry.example", "a-token", transport=httpx.MockTransport(answer)
    )
    with registry, pytest.raises(HubError, match=REFUSED):
        registry.push(load_agent(holding), key)
    assert requests == []


def test_the_push_command_refuses_it_and_sends_nothing(
    holding: Path,
    exported: bytes,
    key_file: Path,
    tmp_path: Path,
    sent: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["push", "--key", str(key_file), "--registry", "http://127.0.0.1:9"]
    assert main([*argv, str(holding)]) == 1
    printed = capsys.readouterr()
    (line,) = printed.err.splitlines()
    assert line.startswith(f"error: {REFUSED}") and HELD in line
    assert names_no_case(line, cases_of(exported)) and printed.out == ""
    assert sent == []

    # The same agent without the cases is signed and sent (to a registry that is not there,
    # here): the refusal above is the cases', not the command's.
    ordinary = copy_of_the_triage(tmp_path / "ordinary" / "triage")
    assert main([*argv, str(ordinary)]) == 1
    assert "could not reach http://127.0.0.1:9" in capsys.readouterr().err
    assert sent == [f"PUT /v1/agents/{TRIAGE.id}/{VERSION}"]


# -- wherever in the agent they are, and nothing else ---------------------------------------------


def test_a_learned_case_is_found_in_whatever_file_of_the_agent_holds_it(
    exported: bytes, tmp_path: Path, key: archive.Ed25519PrivateKey
) -> None:
    """An archive holds every file of a pack, so every file is read, whatever it is called and
    however it is laid out: a copy kept beside the datasets that the manifest does not name,
    one cut short, the same cases as one indented JSON document or written without spaces, in
    a folder of their own. A hidden file is no part of an agent."""
    root = copy_of_the_triage(tmp_path / "triage")
    archive.build(load_agent(root), key)  # with none in it: built

    def refused_for(where: str, count: int) -> None:
        with pytest.raises(HubError) as refused:
            archive.build(load_agent(root), key)
        said = str(refused.value)
        assert said.startswith(REFUSED) and f"({count} in {where})" in said

    aside = root / "data" / "reviewed.copy.jsonl"
    aside.write_bytes(exported)
    refused_for("data/reviewed.copy.jsonl", 2)
    aside.write_bytes(exported[: len(exported) - 2])  # its last record is not whole
    refused_for("data/reviewed.copy.jsonl", 2)
    aside.unlink()

    notes = root / "notes"
    notes.mkdir()
    indented = notes / "cases.json"
    indented.write_text(json.dumps(cases_of(exported), indent=2, ensure_ascii=False))
    refused_for("notes/cases.json", 2)
    indented.unlink()
    compact = notes / "cases.txt"
    compact.write_text(json.dumps(cases_of(exported), separators=(",", ":")))
    refused_for("notes/cases.txt", 2)

    # A hidden file is no part of an agent: no archive holds it, so it stops nothing.
    compact.rename(notes / ".cases.txt")
    built = archive.build(load_agent(root), key)
    assert "notes/.cases.txt" not in archive.verify(built, trusted(key)).files


def test_an_agent_that_holds_none_is_built_whatever_its_cases_look_like(
    tmp_path: Path, key: archive.Ed25519PrivateKey
) -> None:
    """An ordinary agent is built as it always was. So is one whose own cases only look like a
    site's: by their id, by where they came from, by an attribute of the same name that says
    something else, or by text that quotes the mark."""
    root = copy_of_the_triage(tmp_path / "triage")
    agent = load_agent(root)
    verified = archive.verify(archive.build(agent, key), trusted(key))
    assert verified.manifest["digest"] == agent.digest
    assert verified.files == as_archived(root)

    test = root / TEST
    case = lines(test)[0]
    quoted = {"message": {"body": 'one file said "exported_by": "mendari feedback export"'}}
    alike = [
        {**case, "id": "reviewed-0123456789abcdef"},
        {**case, "id": "ours-1", "attributes": {"source": "review", "decision_id": "decision-1"}},
        {**case, "id": "ours-4", "input": quoted},
        {**case, "id": "ours-5", "attributes": {"exported_by": "the practice's own tool"}},
        {
            **case,
            "id": "ours-6",
            "attributes": {"exported_by": True, "by": "mendari feedback export"},
        },
    ]
    with test.open("a") as handle:
        handle.writelines(json.dumps(record) + "\n" for record in alike)
    assert site_cases.marked_files(as_archived(root)) == {}
    agent = load_agent(root)
    verified = archive.verify(archive.build(agent, key), trusted(key))
    assert verified.manifest["digest"] == agent.digest
    assert verified.files[TEST] == test.read_bytes()


def test_an_agent_with_an_export_saved_in_its_folder_is_not_built_or_pushed(
    tmp_path: Path, key_file: Path, sent: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--out` in the agent's own folder: an archive holds every file there, and this one is a
    site's cases. The command says so when it writes the file, and neither `mendari archive build`
    nor `mendari push` makes an archive until the file is out of the folder."""
    root = copy_of_the_triage(tmp_path / "triage")
    db = a_sites_database(tmp_path / "audit.db")
    saved = root / "reviewed.jsonl"
    export = ["feedback", "export", str(root), "--db", str(db), "--source", "reviews"]
    assert main([*export, "--out", str(saved)]) == 0
    said = capsys.readouterr().out
    assert "a site's cases" in said and "outside the agent's folder" in said
    cases = lines(saved)
    assert len(cases) == 2

    dist = tmp_path / "dist"
    build = ["archive", "build", str(root), "--key", str(key_file), "--out", str(dist)]
    push = ["push", "--key", str(key_file), "--registry", "http://127.0.0.1:9", str(root)]
    for argv in (build, push):
        assert main(argv) == 1
        printed = capsys.readouterr()
        (line,) = printed.err.splitlines()  # one line
        assert line.startswith(f"error: {REFUSED}") and "(2 in reviewed.jsonl)" in line
        assert names_no_case(line, cases) and printed.out == ""
    assert sent == [] and not dist.exists()

    saved.rename(tmp_path / "reviewed.jsonl")  # kept beside the agent, not in it
    assert main(build) == 0
    assert [path.suffix for path in dist.iterdir()] == [".mendari"]


def test_exported_cases_are_found_in_whatever_file_of_the_agent_holds_them(
    exported: bytes, tmp_path: Path, key: archive.Ed25519PrivateKey
) -> None:
    """Copied into a dataset the agent tests with, or kept as one JSON document, indented or
    written without spaces: the records still say what they are."""
    root = copy_of_the_triage(tmp_path / "triage")

    def refused_for(where: str) -> None:
        with pytest.raises(HubError) as refused:
            archive.build(load_agent(root), key)
        said = str(refused.value)
        assert said.startswith(REFUSED) and f"(2 in {where})" in said

    test = root / TEST
    as_shipped = test.read_bytes()
    test.write_bytes(as_shipped + exported)
    refused_for(TEST)
    test.write_bytes(as_shipped)

    records = cases_of(exported)
    kept = root / "tests" / "from-review.json"
    kept.write_text(json.dumps(records, indent=2, ensure_ascii=False))
    refused_for("tests/from-review.json")
    kept.write_text(json.dumps(records, separators=(",", ":")))
    refused_for("tests/from-review.json")
    kept.unlink()
    agent = load_agent(root)  # with none left, it is built as it always was
    assert (
        archive.verify(archive.build(agent, key), trusted(key)).manifest["digest"] == agent.digest
    )


# -- and a registry does not take one ------------------------------------------------------------


def test_a_registry_refuses_one_a_client_sent_without_looking(
    holding: Path,
    exported: bytes,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check before an archive is built is the publisher's own. A client from before it, or
    anything else that can sign, builds the archive all the same: the registry reads what it is
    sent, once it knows who signed it, and stores nothing of it."""
    agent = load_agent(holding)
    token, key = registry.fixture_publisher()  # the triage fixture's namespace's
    stranger = archive.load_private_key(archive.generate_key()[0])
    with monkeypatch.context() as without_the_check:
        without_the_check.setattr(archive, "refuse_site_cases", lambda files: None)
        data = archive.build(agent, key)
        by_a_stranger = archive.build(agent, stranger)
    path = f"/v1/agents/{TRIAGE.id}/{VERSION}"
    publisher = {"authorization": f"Bearer {token}"}

    answer = registry.client.put(path, content=data, headers=publisher)
    assert answer.status_code == 422, answer.text
    detail = answer.json()["detail"]
    assert detail.startswith(REFUSED) and HELD in detail
    assert names_no_case(answer.text, cases_of(exported))
    assert registry.client.get(path).status_code == 404
    assert registry.client.get("/v1/health").json()["agents"] == 0

    # The signature comes first: an archive nobody the namespace trusts signed is not read.
    answer = registry.client.put(path, content=by_a_stranger, headers=publisher)
    assert answer.status_code == 422 and "not trusted" in answer.text
    assert REFUSED not in answer.text

    # The same agent without the cases, with an export saved in its folder, is refused too;
    # with the file out of the folder, it is published.
    root = copy_of_the_triage(tmp_path / "ordinary" / "triage")
    (root / "reviewed.jsonl").write_bytes(exported)
    with monkeypatch.context() as without_the_check:
        without_the_check.setattr(archive, "refuse_site_cases", lambda files: None)
        data = archive.build(load_agent(root), key)
    answer = registry.client.put(path, content=data, headers=publisher)
    assert answer.status_code == 422, answer.text
    detail = answer.json()["detail"]
    assert detail.startswith(REFUSED) and "(2 in reviewed.jsonl)" in detail
    assert registry.client.get("/v1/health").json()["agents"] == 0
    (root / "reviewed.jsonl").unlink()
    assert registry.publish(load_agent(root)).status_code == 201
