# SPDX-License-Identifier: Apache-2.0
"""A pack that holds cases `mendari improve` added is never made into an archive
(`hub/archive.py` `build`): `mendari pack build` and `mendari push` both stop there, before
anything is signed or sent, and a registry refuses one a client sent without looking. A case a
run adds says so (`attributes.added_by`), a model built from such cases says so too, and that
mark alone is what is looked for (`core/learned.py`), in every file an archive would hold.

The cases here are the harder inbox draft's train split (synthetic; a draft, not clinically
reviewed), standing in for what a site's reviewers would label. Every pack written to is a copy
under a temporary directory."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from mendari import load_pack
from mendari.cli import main
from mendari.core import learned
from mendari.core.pack import pack_digest
from mendari.errors import HubError, MendariError
from mendari.evals.harness import load_dataset
from mendari.evals.improve import Improvement, apply, improve
from mendari.hub import archive
from mendari.hub.client import RegistryClient
from tests.conftest import INBOX_DIR, ROOT, Registry

HARDER = ROOT / "datasets" / "drafts" / "patient-inbox-harder" / "train.jsonl"
MODEL = "models/destination-nb.json"
TRAIN = "data/train.jsonl"
CALIBRATION = "data/calibration.jsonl"
TEST = "data/test.jsonl"
SHIPPED = pack_digest(INBOX_DIR)
# Not the reference pack's version: at its own, a registry reads a changed copy as a shadow.
VERSION = "9.9.9"
REFUSED = "the pack holds cases `mendari improve` added, or a model built from them"
# What a refusal says of the pack a run was kept in: each file, and how many times it is marked.
HELD = f"{REFUSED} (70 in {CALIBRATION}, 216 in {TRAIN}, 1 in {MODEL})"
MARK = {"added_by": "mendari improve"}


@pytest.fixture(autouse=True)
def _the_repositorys_own_pack_is_never_written_to() -> Iterator[None]:
    """Every test here works on a copy (`tasks/lessons.md`, 2026-11-10)."""
    yield
    assert pack_digest(INBOX_DIR) == SHIPPED, "a test changed packs/patient-inbox-router"


def copy_of_the_inbox(root: Path) -> Path:
    """The inbox pack at `VERSION`, as a site's own pack."""
    shutil.copytree(INBOX_DIR, root)
    manifest = root / "pack.yaml"
    shipped = f"version: {load_pack(INBOX_DIR).version}"
    assert manifest.read_text().count(shipped) == 1
    manifest.write_text(manifest.read_text().replace(shipped, f"version: {VERSION}"))
    return root


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def as_archived(root: Path) -> dict[str, bytes]:
    """The pack's files as an archive would hold them."""
    return {f.relative_to(root).as_posix(): f.read_bytes() for f in archive.pack_files(root)}


def trusted(key: archive.Ed25519PrivateKey) -> dict[str, archive.Ed25519PublicKey]:
    return {archive.key_id(key.public_key()): key.public_key()}


@dataclass(frozen=True)
class Improved:
    root: Path  # a pack `mendari improve --write` wrote to
    found: Improvement  # the run it kept
    added: dict[str, list[dict[str, Any]]]  # the records it added, by dataset file

    @property
    def counts(self) -> dict[str, int]:
        return {name: len(records) for name, records in self.added.items()}

    def without_its_cases(self, root: Path) -> dict[str, bytes]:
        """A copy at `root` with the added cases taken out of its two datasets, and its model
        as the run left it. Returns the lines taken out, by dataset file."""
        shutil.copytree(self.root, root)
        taken: dict[str, bytes] = {}
        for name, count in self.counts.items():
            held = (root / name).read_bytes().splitlines(keepends=True)
            (root / name).write_bytes(b"".join(held[:-count]))
            taken[name] = b"".join(held[-count:])
        return taken


@pytest.fixture(scope="module")
def improved(tmp_path_factory: pytest.TempPathFactory) -> Improved:
    """One pack a run was kept in, shared by the tests that only read it."""
    scratch = tmp_path_factory.mktemp("improved")
    root = copy_of_the_inbox(scratch / "patient-inbox-router")
    held = {name: len(lines(root / name)) for name in (CALIBRATION, TRAIN)}
    reviewed = scratch / "reviewed.jsonl"
    reviewed.write_text(
        "".join(
            json.dumps(
                {**record, "attributes": {"source": "review", "decision_id": f"decision-{number}"}}
            )
            + "\n"
            for number, record in enumerate(load_dataset(HARDER).records)
        )
    )
    found = improve(load_pack(root), [load_dataset(reviewed)])
    apply(load_pack(root), found)
    added = {name: lines(root / name)[count:] for name, count in held.items()}
    assert added[TRAIN] and added[CALIBRATION]
    return Improved(root, found, added)


@pytest.fixture(scope="module")
def key() -> archive.Ed25519PrivateKey:
    return archive.load_private_key(archive.generate_key()[0])


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "publisher.key"
    path.write_bytes(archive.generate_key()[0])
    return path


def names_no_case(message: str, improved: Improved) -> bool:
    """Whether `message` is free of every added case's id, decision and text."""
    for records in improved.added.values():
        for record in records:
            said = (
                record["id"],
                record["attributes"]["decision_id"],
                record["input"]["message"]["body"],
            )
            if any(part in message for part in said):
                return False
    return True


# -- a case a run adds says so, and so does a model built from it --------------------------------


def test_every_case_a_run_adds_says_it_was_added(improved: Improved) -> None:
    for name, records in improved.added.items():
        own = lines(improved.root / name)[: -len(records)]
        assert all(record["attributes"].items() >= MARK.items() for record in records)
        assert not any("added_by" in record.get("attributes", {}) for record in own)
    assert improved.counts == {CALIBRATION: 70, TRAIN: 216}
    # The model learned from them and holds their words, so it says so too.
    assert json.loads((improved.root / MODEL).read_text())["training"].items() >= MARK.items()
    assert learned.learned_cases(as_archived(improved.root)) == {**improved.counts, MODEL: 1}


def test_a_case_that_does_not_say_so_is_never_written(improved: Improved, tmp_path: Path) -> None:
    """The mark is what stops a pack being published, so the one writer checks it: a run whose
    cases do not carry it is not kept, and nothing is written."""
    root = copy_of_the_inbox(tmp_path / "patient-inbox-router")
    before = pack_digest(root)
    found = improved.found

    def unmarked(cases: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        return (*cases[:-1], {**cases[-1], "attributes": {"source": "review"}})  # one of them

    for run in (
        replace(found, learned=unmarked(found.learned)),
        replace(found, calibration=unmarked(found.calibration)),
    ):
        with pytest.raises(MendariError, match="a case this run would add does not say"):
            apply(load_pack(root), run)
        assert pack_digest(root) == before
    apply(load_pack(root), found)  # the run as it was made is kept
    assert learned.learned_cases(as_archived(root)) == {**improved.counts, MODEL: 1}


def test_a_runs_report_does_not_carry_the_mark(improved: Improved) -> None:
    """A report holds counts, never a case: kept beside a pack's files, it stops nothing."""
    assert learned.cases_added(json.dumps(improved.found.report).encode()) == 0


def test_no_pack_of_the_repository_holds_one() -> None:
    packs = sorted(path.parent for path in ROOT.glob("*packs/*/pack.yaml"))
    assert len(packs) >= 5 and INBOX_DIR in packs
    for root in packs:
        assert learned.learned_cases(as_archived(root)) == {}, root.name


# -- no archive is made of a pack that holds them ------------------------------------------------


def test_a_pack_that_holds_learned_cases_is_not_built(
    improved: Improved, key: archive.Ed25519PrivateKey
) -> None:
    with pytest.raises(HubError) as refused:
        archive.build(load_pack(improved.root), key)
    message = str(refused.value)
    # The files and how many each holds, in one line, and never a case.
    assert message.startswith(HELD)
    assert "\n" not in message
    assert names_no_case(message, improved)
    assert "learned-" not in message and "decision-" not in message


def test_a_refusal_names_a_few_files_and_counts_the_rest() -> None:
    case = b'{"id": "c", "input": {}, "attributes": {"added_by": "mendari improve"}}\n'
    files = {f"data/part-{number}.jsonl": case * (number + 1) for number in range(7)}
    files["pack.yaml"] = b"id: site/pack\n"
    with pytest.raises(HubError) as refused:
        archive.refuse_learned_cases(files)
    assert (
        "(1 in data/part-0.jsonl, 2 in data/part-1.jsonl, 3 in data/part-2.jsonl, "
        "4 in data/part-3.jsonl, 5 in data/part-4.jsonl, and 13 more in 2 other files)"
    ) in str(refused.value)
    del files["data/part-6.jsonl"]
    with pytest.raises(HubError, match=r"5 in data/part-4\.jsonl, and 6 more in 1 other file\)"):
        archive.refuse_learned_cases(files)
    archive.refuse_learned_cases({"pack.yaml": files["pack.yaml"]})  # none: nothing is raised


def test_a_refusal_is_one_line_whatever_a_file_is_called() -> None:
    marked = b'{"attributes": {"added_by": "mendari improve"}}\n'
    with pytest.raises(HubError) as refused:
        archive.refuse_learned_cases({"data/one\ntwo.jsonl": marked})
    message = str(refused.value)
    assert "\n" not in message and r"1 in 'data/one\ntwo.jsonl'" in message


class NeverUsed:
    """A publisher's key that fails the test if anything is signed with it, or asked of it."""

    def sign(self, data: bytes) -> bytes:
        raise AssertionError("something was signed")

    def public_key(self) -> Any:
        raise AssertionError("the key was read")


def test_it_is_refused_before_anything_is_signed(improved: Improved) -> None:
    with pytest.raises(HubError, match=REFUSED):
        archive.build(load_pack(improved.root), cast(archive.Ed25519PrivateKey, NeverUsed()))


def test_pack_build_refuses_it_and_writes_no_archive(
    improved: Improved, key_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "dist"
    argv = ["pack", "build", str(improved.root), "--key", str(key_file), "--out", str(out)]
    assert main(argv) == 1
    printed = capsys.readouterr()
    assert printed.out == ""
    (line,) = printed.err.splitlines()  # one line
    assert line.startswith(f"error: {HELD}")
    assert "mendari train" in line  # and what to do about it
    assert names_no_case(line, improved) and "Traceback" not in line
    assert not out.exists()


def test_push_refuses_it_before_anything_is_sent(
    improved: Improved, key: archive.Ed25519PrivateKey
) -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    registry = RegistryClient(
        "https://registry.example", "a-token", transport=httpx.MockTransport(answer)
    )
    with registry, pytest.raises(HubError, match=REFUSED):
        registry.push(load_pack(improved.root), key)
    assert requests == []


def test_the_push_command_refuses_it_and_sends_nothing(
    improved: Improved,
    key_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[str] = []

    def send(self: httpx.Client, request: httpx.Request, **_: Any) -> httpx.Response:
        sent.append(f"{request.method} {request.url.path}")
        raise httpx.ConnectError("no registry in this test", request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    argv = ["push", "--key", str(key_file), "--registry", "http://127.0.0.1:9"]
    assert main([*argv, str(improved.root)]) == 1
    printed = capsys.readouterr()
    (line,) = printed.err.splitlines()
    assert line.startswith(f"error: {HELD}")
    assert names_no_case(line, improved) and printed.out == ""
    assert sent == []

    # The same pack before it learned anything is signed and sent (to a registry that is not
    # there, here): the refusal above is the cases', not the command's.
    ordinary = copy_of_the_inbox(tmp_path / "patient-inbox-router")
    assert main([*argv, str(ordinary)]) == 1
    assert "could not reach http://127.0.0.1:9" in capsys.readouterr().err
    assert sent == [f"PUT /v1/packs/mendari/patient-inbox-router/{VERSION}"]


# -- wherever in the pack they are, and nothing else ---------------------------------------------


def test_a_model_built_from_them_is_refused_until_it_is_trained_again(
    improved: Improved,
    tmp_path: Path,
    key: archive.Ed25519PrivateKey,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Taking the cases out of the datasets is half of it: the model still holds their words.
    It says what it was built from, so the pack is refused until `mendari train` builds the
    model again from what is left, which here is the model the pack came with."""
    root = tmp_path / "patient-inbox-router"
    improved.without_its_cases(root)
    assert (root / TRAIN).read_bytes() == (INBOX_DIR / TRAIN).read_bytes()
    assert (root / MODEL).read_bytes() != (INBOX_DIR / MODEL).read_bytes()
    with pytest.raises(HubError, match=rf"{REFUSED} \(1 in {MODEL}\)"):
        archive.build(load_pack(root), key)

    assert main(["train", str(root)]) == 0
    capsys.readouterr()
    assert (root / MODEL).read_bytes() == (INBOX_DIR / MODEL).read_bytes()
    pack = load_pack(root)
    assert archive.verify(archive.build(pack, key), trusted(key)).manifest["digest"] == pack.digest


def test_a_learned_case_is_found_in_whatever_file_of_the_pack_holds_it(
    improved: Improved,
    tmp_path: Path,
    key: archive.Ed25519PrivateKey,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An archive holds every file of a pack, so every file is read, whatever it is called and
    however it is laid out: a dataset the pack declares and does not train on, a copy kept
    beside the datasets that the manifest does not name, the same cases as one indented JSON
    document or written without spaces, in a folder of their own, and a file cut short."""
    root = tmp_path / "patient-inbox-router"
    taken = improved.without_its_cases(root)
    assert main(["train", str(root)]) == 0
    capsys.readouterr()
    archive.build(load_pack(root), key)  # with none left, and its model built again: built

    def refused_for(where: str, count: int) -> None:
        with pytest.raises(HubError) as refused:
            archive.build(load_pack(root), key)
        assert str(refused.value).startswith(f"{REFUSED} ({count} in {where})")

    test = root / TEST
    as_shipped = test.read_bytes()
    test.write_bytes(as_shipped + taken[TRAIN].splitlines(keepends=True)[0])
    refused_for(TEST, 1)
    test.write_bytes(as_shipped)

    aside = root / "data" / "train.improved.jsonl"
    aside.write_bytes(taken[TRAIN])
    refused_for("data/train.improved.jsonl", 216)
    aside.write_bytes(taken[TRAIN][: len(taken[TRAIN]) - 2])  # its last record is not whole
    refused_for("data/train.improved.jsonl", 216)
    aside.unlink()

    notes = root / "notes"
    notes.mkdir()
    indented = notes / "cases.json"
    indented.write_text(json.dumps(improved.added[CALIBRATION], indent=2, ensure_ascii=False))
    refused_for("notes/cases.json", 70)
    indented.unlink()
    compact = notes / "cases.txt"
    compact.write_text(json.dumps(improved.added[CALIBRATION], separators=(",", ":")))
    refused_for("notes/cases.txt", 70)

    # A hidden file is no part of a pack: no archive holds it, so it stops nothing.
    compact.rename(notes / ".cases.txt")
    built = archive.build(load_pack(root), key)
    assert "notes/.cases.txt" not in archive.verify(built, trusted(key)).files


def test_a_pack_that_holds_none_is_built_whatever_its_cases_look_like(
    tmp_path: Path, key: archive.Ed25519PrivateKey
) -> None:
    """An ordinary pack is built as it always was. So is one whose own cases only look like a
    run's: by their id, by where they came from, by an attribute of the same name that says
    something else, or by text that quotes the mark."""
    root = copy_of_the_inbox(tmp_path / "patient-inbox-router")
    pack = load_pack(root)
    verified = archive.verify(archive.build(pack, key), trusted(key))
    assert verified.manifest["digest"] == pack.digest
    assert verified.files == as_archived(root)

    train = root / TRAIN
    case = lines(train)[0]
    quoted = {"message": {"body": 'the file said "added_by": "mendari improve" on each line'}}
    alike = [
        {**case, "id": "learned-0123456789abcdef"},
        {**case, "id": "ours-1", "attributes": {"source": "review", "decision_id": "decision-1"}},
        {**case, "id": "ours-2", "attributes": {"added_by": "a nurse educator"}},
        {**case, "id": "ours-3", "attributes": {"added_by": True, "by": "mendari improve"}},
        {**case, "id": "ours-4", "input": quoted},
    ]
    with train.open("a") as handle:
        handle.writelines(json.dumps(record) + "\n" for record in alike)
    assert learned.learned_cases(as_archived(root)) == {}
    pack = load_pack(root)
    verified = archive.verify(archive.build(pack, key), trusted(key))
    assert verified.manifest["digest"] == pack.digest
    assert verified.files[TRAIN] == train.read_bytes()


# -- the cases a run is given stay outside the pack ----------------------------------------------


def test_a_run_is_not_given_cases_kept_in_the_packs_own_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cases a run is given carry no mark (a third of them are only held out), and every
    file in a pack's folder goes into its archive. So a run reads none from there, with
    `--write` or without, however the path is spelt."""
    root = copy_of_the_inbox(tmp_path / "patient-inbox-router")
    given = root / "data" / "reviewed.jsonl"
    shutil.copyfile(HARDER, given)
    before = pack_digest(root)
    spellings = [given, root / "data" / ".." / "data" / "reviewed.jsonl"]
    respelt = Path(str(root).swapcase()) / "data" / "reviewed.jsonl"
    if respelt.is_file():  # a volume that ignores case: PACK/ is pack/
        spellings.append(respelt)
    for spelt in spellings:
        for extra in ([], ["--write"]):
            assert main(["improve", str(root), "--with", str(spelt), *extra]) == 1
            printed = capsys.readouterr()
            assert "--with reviewed.jsonl: this file is in the pack's own folder" in printed.err
            assert "keep the cases a run is given outside it" in printed.err
            assert printed.out == ""  # no run was made
    assert pack_digest(root) == before


# -- and a registry does not take one ------------------------------------------------------------


def test_a_registry_refuses_one_a_client_sent_without_looking(
    improved: Improved, registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check before an archive is built is the publisher's own. A client from before it, or
    anything else that can sign, builds the archive all the same: the registry reads what it is
    sent, once it knows who signed it, and stores nothing of it."""
    pack = load_pack(improved.root)
    stranger = archive.load_private_key(archive.generate_key()[0])
    with monkeypatch.context() as without_the_check:
        without_the_check.setattr(archive, "refuse_learned_cases", lambda files: None)
        data = archive.build(pack, registry.key)
        by_a_stranger = archive.build(pack, stranger)
    path = f"/v1/packs/mendari/patient-inbox-router/{VERSION}"
    publisher = {"authorization": f"Bearer {registry.token}"}

    answer = registry.client.put(path, content=data, headers=publisher)
    assert answer.status_code == 422, answer.text
    assert answer.json()["detail"].startswith(HELD)
    assert names_no_case(answer.text, improved)
    assert registry.client.get(path).status_code == 404
    assert registry.client.get("/v1/health").json()["packs"] == 0

    # The signature comes first: an archive nobody the namespace trusts signed is not read.
    answer = registry.client.put(path, content=by_a_stranger, headers=publisher)
    assert answer.status_code == 422 and "not trusted" in answer.text
    assert REFUSED not in answer.text

    # The same pack before it learned anything is published.
    ordinary = load_pack(copy_of_the_inbox(tmp_path / "patient-inbox-router"))
    assert registry.publish(ordinary).status_code == 201
