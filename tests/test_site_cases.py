# SPDX-License-Identifier: Apache-2.0
"""A pack that holds a site's cases is never made into an archive (`hub/archive.py` `build`):
`mendari pack build` and `mendari push` both stop there, before anything is signed or sent, and
a registry refuses one a client sent without looking. A site's cases say what they are: a case
`mendari feedback export` (or its route) writes says it was exported
(`attributes.exported_by`), a case `mendari improve` adds to a pack says a run added it
(`attributes.added_by`), and a model built from either says what they said. Those marks alone
are what is looked for (`core/site_cases.py`), in every file an archive would hold.

The cases here are synthetic: the harder inbox draft's train split (a draft, not clinically
reviewed), standing in for what a site's reviewers would label, and two messages decided and
reviewed in a scratch database. Every pack written to is a copy under a temporary directory."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_pack
from mendari.cli import main
from mendari.core import site_cases
from mendari.core.pack import pack_digest
from mendari.errors import HubError, MendariError
from mendari.evals.harness import load_dataset
from mendari.evals.improve import Improvement, apply, improve
from mendari.hub import archive
from mendari.hub.client import RegistryClient
from mendari.server.app import create_app
from tests.conftest import INBOX_DIR, ROOT, Registry, message

HARDER = ROOT / "datasets" / "drafts" / "patient-inbox-harder" / "train.jsonl"
MODEL = "models/destination-nb.json"
TRAIN = "data/train.jsonl"
CALIBRATION = "data/calibration.jsonl"
TEST = "data/test.jsonl"
SHIPPED = pack_digest(INBOX_DIR)
# Not the reference pack's version: at its own, a registry reads a changed copy as a shadow.
VERSION = "9.9.9"
REFUSED = (
    "the pack holds cases `mendari improve` added or `mendari feedback export` wrote, or a "
    "model built from them"
)
# What a refusal says of the pack a run was kept in: each file, and how many times it is marked.
HELD = f"{REFUSED} (70 in {CALIBRATION}, 216 in {TRAIN}, 1 in {MODEL})"
MARK = {"added_by": "mendari improve"}
EXPORTED = {"exported_by": "mendari feedback export"}
CALL_BACK = "Can someone call me back?"
REFILL = "Hi, I need a refill on my metoprolol. I have about a week's worth of pills left."


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

    @property
    def cases(self) -> list[dict[str, Any]]:
        return [record for records in self.added.values() for record in records]

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
    """One pack a run was kept in, shared by the tests that only read it. The run was given the
    cases as an export writes them, each marked as exported."""
    scratch = tmp_path_factory.mktemp("improved")
    root = copy_of_the_inbox(scratch / "patient-inbox-router")
    held = {name: len(lines(root / name)) for name in (CALIBRATION, TRAIN)}
    reviewed = scratch / "reviewed.jsonl"
    reviewed.write_text(
        "".join(
            json.dumps(
                {
                    **record,
                    "attributes": {
                        "source": "review",
                        "decision_id": f"decision-{number}",
                        **EXPORTED,
                    },
                }
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


# -- a case a run adds says so, and so does a model built from it --------------------------------


def test_every_case_a_run_adds_says_it_was_added(improved: Improved) -> None:
    for name, records in improved.added.items():
        own = lines(improved.root / name)[: -len(records)]
        assert all(record["attributes"].items() >= MARK.items() for record in records)
        assert not any("added_by" in record.get("attributes", {}) for record in own)
        # A run's own mark, once: the export's stays on the file the run was given.
        assert not any("exported_by" in record["attributes"] for record in records)
    assert improved.counts == {CALIBRATION: 70, TRAIN: 216}
    # The model learned from them and holds their words, so it says so too.
    assert json.loads((improved.root / MODEL).read_text())["training"].items() >= MARK.items()
    assert site_cases.marked_files(as_archived(improved.root)) == {**improved.counts, MODEL: 1}


def test_a_case_that_does_not_say_so_is_never_written(improved: Improved, tmp_path: Path) -> None:
    """The mark is what stops a pack being published, so the one writer checks it: a run whose
    cases do not carry it is not kept, and nothing is written."""
    root = copy_of_the_inbox(tmp_path / "patient-inbox-router")
    before = pack_digest(root)
    found = improved.found

    def with_one(
        cases: tuple[dict[str, Any], ...], attributes: dict[str, Any]
    ) -> tuple[dict[str, Any], ...]:
        return (*cases[:-1], {**cases[-1], "attributes": attributes})

    # One case with no mark, or with an export's, which is not a run's.
    for attributes in ({"source": "review"}, {"source": "review", **EXPORTED}):
        for run in (
            replace(found, learned=with_one(found.learned, attributes)),
            replace(found, calibration=with_one(found.calibration, attributes)),
        ):
            with pytest.raises(MendariError, match="a case this run would add does not say"):
                apply(load_pack(root), run)
            assert pack_digest(root) == before
    apply(load_pack(root), found)  # the run as it was made is kept
    assert site_cases.marked_files(as_archived(root)) == {**improved.counts, MODEL: 1}


def test_a_runs_report_does_not_carry_the_mark(improved: Improved) -> None:
    """A report holds counts, never a case: kept beside a pack's files, it stops nothing."""
    assert site_cases.times_marked(json.dumps(improved.found.report).encode()) == 0


def test_a_record_is_read_for_the_marks_it_carries() -> None:
    plain = {"id": "a", "input": {}, "attributes": {"source": "review"}}
    added = {"id": "b", "input": {}, "attributes": {"source": "review", **MARK}}
    exported = {"id": "c", "input": {}, "attributes": {"source": "review", **EXPORTED}}
    assert site_cases.carried([]) == {} and site_cases.carried([plain]) == {}
    assert site_cases.carried([plain, exported]) == EXPORTED
    # Both, in one order whichever record comes first: a model's file is its bytes.
    both = [*MARK.items(), *EXPORTED.items()]
    assert list(site_cases.carried([exported, plain, added]).items()) == both
    assert list(site_cases.carried([added, exported]).items()) == both
    assert site_cases.added(added)
    assert not site_cases.added(exported) and not site_cases.added(plain)
    # A record is read as a file of it is, the way an archive's builder reads it: a mark
    # anywhere in it counts, not only in its attributes.
    elsewhere = {"id": "d", "input": {"message": {"body": "x"}, **EXPORTED}}
    assert site_cases.carried([elsewhere]) == EXPORTED
    assert site_cases.added({"id": "e", "input": {}, **MARK})
    spelt = b'{"added_by":"mendari improve"}\n{"exported_by" : "mendari feedback export"}\n'
    assert site_cases.times_marked(spelt) == 2
    # JSON may put a tab or a line break beside the colon: the mark is still there.
    spaced = b'{"added_by":\t"mendari improve",\n "exported_by"\r\n:\n  "mendari feedback export"}'
    assert site_cases.times_marked(spaced) == 2


def test_no_pack_of_the_repository_holds_one() -> None:
    packs = sorted(path.parent for path in ROOT.glob("*packs/*/pack.yaml"))
    assert len(packs) >= 5 and INBOX_DIR in packs
    for root in packs:
        assert site_cases.marked_files(as_archived(root)) == {}, root.name


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
    assert names_no_case(message, improved.cases)
    assert "learned-" not in message and "decision-" not in message


def test_a_refusal_names_a_few_files_and_counts_the_rest() -> None:
    case = b'{"id": "c", "input": {}, "attributes": {"added_by": "mendari improve"}}\n'
    files = {f"data/part-{number}.jsonl": case * (number + 1) for number in range(7)}
    files["pack.yaml"] = b"id: site/pack\n"
    with pytest.raises(HubError) as refused:
        archive.refuse_site_cases(files)
    assert (
        "(1 in data/part-0.jsonl, 2 in data/part-1.jsonl, 3 in data/part-2.jsonl, "
        "4 in data/part-3.jsonl, 5 in data/part-4.jsonl, and 13 more in 2 other files)"
    ) in str(refused.value)
    del files["data/part-6.jsonl"]
    with pytest.raises(HubError, match=r"5 in data/part-4\.jsonl, and 6 more in 1 other file\)"):
        archive.refuse_site_cases(files)
    archive.refuse_site_cases({"pack.yaml": files["pack.yaml"]})  # none: nothing is raised


def test_a_refusal_is_one_line_whatever_a_file_is_called() -> None:
    marked = b'{"attributes": {"added_by": "mendari improve"}}\n'
    with pytest.raises(HubError) as refused:
        archive.refuse_site_cases({"data/one\ntwo.jsonl": marked})
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
    assert names_no_case(line, improved.cases) and "Traceback" not in line
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
    sent: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["push", "--key", str(key_file), "--registry", "http://127.0.0.1:9"]
    assert main([*argv, str(improved.root)]) == 1
    printed = capsys.readouterr()
    (line,) = printed.err.splitlines()
    assert line.startswith(f"error: {HELD}")
    assert names_no_case(line, improved.cases) and printed.out == ""
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
    quoted = {
        "message": {
            "body": 'one file said "added_by": "mendari improve" on each line, and the '
            'other "exported_by": "mendari feedback export"'
        }
    }
    alike = [
        {**case, "id": "learned-0123456789abcdef"},
        {**case, "id": "ours-1", "attributes": {"source": "review", "decision_id": "decision-1"}},
        {**case, "id": "ours-2", "attributes": {"added_by": "a nurse educator"}},
        {**case, "id": "ours-3", "attributes": {"added_by": True, "by": "mendari improve"}},
        {**case, "id": "ours-4", "input": quoted},
        {**case, "id": "ours-5", "attributes": {"exported_by": "the practice's own tool"}},
        {
            **case,
            "id": "ours-6",
            "attributes": {"exported_by": True, "by": "mendari feedback export"},
        },
    ]
    with train.open("a") as handle:
        handle.writelines(json.dumps(record) + "\n" for record in alike)
    assert site_cases.marked_files(as_archived(root)) == {}
    pack = load_pack(root)
    verified = archive.verify(archive.build(pack, key), trusted(key))
    assert verified.manifest["digest"] == pack.digest
    assert verified.files[TRAIN] == train.read_bytes()


# -- an export is a site's cases too -------------------------------------------------------------


def a_sites_database(path: Path) -> Path:
    """A site's database: two messages the inbox pack sent to a person, each resolved by a
    nurse and rated wrong with the right answer."""
    with AuditStore(path) as store:
        decision = Decision.from_path(INBOX_DIR, store=store)
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
    argv = ["feedback", "export", str(INBOX_DIR), "--db", str(db), "--out", str(out)]
    assert main([*argv, "--source", "reviews"]) == 0
    return out.read_bytes()


@pytest.mark.parametrize("source", ["ratings", "reviews"])
def test_every_record_an_export_writes_says_it_was_exported(
    source: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command's file and the route's answer, of ratings and of reviews: each record is a
    site's case and says so (`attributes.exported_by`), and is read so in the bytes written.
    The command says so too, and where the file belongs."""
    db = a_sites_database(tmp_path / "audit.db")
    out = tmp_path / "export.jsonl"
    argv = ["feedback", "export", str(INBOX_DIR), "--db", str(db), "--out", str(out)]
    assert main([*argv, "--source", source]) == 0
    said = capsys.readouterr().out
    assert "a site's cases" in said and "outside the pack's folder" in said
    served = TestClient(create_app(db_path=db)).get(
        "/api/packs/mendari/patient-inbox-router/feedback/export", params={"source": source}
    )
    assert served.status_code == 200
    for written in (out.read_bytes(), served.content):
        records = [json.loads(line) for line in written.splitlines()]
        assert len(records) == 2
        assert all(record["attributes"].items() >= EXPORTED.items() for record in records)
        assert site_cases.times_marked(written) == 2


def test_a_pack_with_an_export_saved_in_its_folder_is_not_built_or_pushed(
    tmp_path: Path, key_file: Path, sent: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--out` in the pack's own folder: an archive holds every file there, and this one is a
    site's cases. The command says so when it writes the file, and neither `mendari pack build`
    nor `mendari push` makes an archive until the file is out of the folder."""
    root = copy_of_the_inbox(tmp_path / "patient-inbox-router")
    db = a_sites_database(tmp_path / "audit.db")
    saved = root / "reviewed.jsonl"
    export = ["feedback", "export", str(root), "--db", str(db), "--source", "reviews"]
    assert main([*export, "--out", str(saved)]) == 0
    said = capsys.readouterr().out
    assert "a site's cases" in said and "outside the pack's folder" in said
    cases = lines(saved)
    assert len(cases) == 2

    dist = tmp_path / "dist"
    build = ["pack", "build", str(root), "--key", str(key_file), "--out", str(dist)]
    push = ["push", "--key", str(key_file), "--registry", "http://127.0.0.1:9", str(root)]
    for argv in (build, push):
        assert main(argv) == 1
        printed = capsys.readouterr()
        (line,) = printed.err.splitlines()  # one line
        assert line.startswith(f"error: {REFUSED} (2 in reviewed.jsonl)")
        assert names_no_case(line, cases) and printed.out == ""
    assert sent == [] and not dist.exists()

    saved.rename(tmp_path / "reviewed.jsonl")  # kept beside the pack, not in it
    assert main(build) == 0
    assert [path.suffix for path in dist.iterdir()] == [".ohpack"]


def test_exported_cases_are_found_in_whatever_file_of_the_pack_holds_them(
    exported: bytes, tmp_path: Path, key: archive.Ed25519PrivateKey
) -> None:
    """Copied into a dataset the pack tests with, or kept as one JSON document, indented or
    written without spaces: the records still say what they are."""
    root = copy_of_the_inbox(tmp_path / "patient-inbox-router")

    def refused_for(where: str) -> None:
        with pytest.raises(HubError) as refused:
            archive.build(load_pack(root), key)
        assert str(refused.value).startswith(f"{REFUSED} (2 in {where})")

    test = root / TEST
    as_shipped = test.read_bytes()
    test.write_bytes(as_shipped + exported)
    refused_for(TEST)
    test.write_bytes(as_shipped)

    records = [json.loads(line) for line in exported.splitlines()]
    kept = root / "tests" / "from-review.json"
    kept.write_text(json.dumps(records, indent=2, ensure_ascii=False))
    refused_for("tests/from-review.json")
    kept.write_text(json.dumps(records, separators=(",", ":")))
    refused_for("tests/from-review.json")
    kept.unlink()
    pack = load_pack(root)  # with none left, it is built as it always was
    assert archive.verify(archive.build(pack, key), trusted(key)).manifest["digest"] == pack.digest


@pytest.mark.parametrize("dataset", [TRAIN, CALIBRATION])
def test_a_model_fitted_on_exported_cases_says_so(
    dataset: str,
    exported: bytes,
    tmp_path: Path,
    key: archive.Ed25519PrivateKey,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exported cases put into a pack's training or calibration data by hand are still marked,
    and the model `mendari train` then builds was fitted on them, so it says what they said.
    With the cases out again the pack is refused until the model is built without them, which
    here is the model the pack came with."""
    root = copy_of_the_inbox(tmp_path / "patient-inbox-router")
    data = root / dataset
    as_shipped = data.read_bytes()
    data.write_bytes(as_shipped + exported)
    assert main(["train", str(root)]) == 0
    capsys.readouterr()
    training = json.loads((root / MODEL).read_text())["training"]
    assert training.items() >= EXPORTED.items() and "added_by" not in training
    with pytest.raises(HubError) as refused:
        archive.build(load_pack(root), key)
    assert str(refused.value).startswith(f"{REFUSED} (2 in {dataset}, 1 in {MODEL})")

    data.write_bytes(as_shipped)
    with pytest.raises(HubError, match=rf"{REFUSED} \(1 in {MODEL}\)"):
        archive.build(load_pack(root), key)
    assert main(["train", str(root)]) == 0
    capsys.readouterr()
    assert (root / MODEL).read_bytes() == (INBOX_DIR / MODEL).read_bytes()
    archive.build(load_pack(root), key)


def test_a_model_built_from_added_and_exported_cases_says_both(
    improved: Improved, exported: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "patient-inbox-router"
    shutil.copytree(improved.root, root)
    train = root / TRAIN
    train.write_bytes(exported + train.read_bytes())  # before the cases the run added
    assert main(["train", str(root)]) == 0
    capsys.readouterr()
    training = json.loads((root / MODEL).read_text())["training"]
    # In one order, whichever kind of case comes first: the same data gives the same bytes.
    assert list(training.items())[-2:] == [*MARK.items(), *EXPORTED.items()]
    assert site_cases.marked_files(as_archived(root)) == {CALIBRATION: 70, TRAIN: 218, MODEL: 2}


def test_a_runs_report_carries_no_mark_whatever_the_packs_data_holds(
    exported: bytes, tmp_path: Path
) -> None:
    """A run on a pack whose own training data holds exported cases builds its candidates from
    them, so each candidate's model says so. The report still holds counts alone: kept beside
    the pack's files, it stops no archive once the cases are out."""
    root = copy_of_the_inbox(tmp_path / "patient-inbox-router")
    with (root / TRAIN).open("ab") as handle:
        handle.write(exported)
    found = improve(load_pack(root), [load_dataset(HARDER)])
    assert found.model is not None
    assert found.model["training"].items() >= {**MARK, **EXPORTED}.items()
    assert site_cases.times_marked(json.dumps(found.report).encode()) == 0


# -- the cases a run is given stay outside the pack ----------------------------------------------


def test_a_run_is_not_given_cases_kept_in_the_packs_own_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cases a run is given need carry no mark (a site's own file of them, as here, has
    none), a third of them are only held out, and every file in a pack's folder goes into its
    archive. So a run reads none from there, with `--write` or without, however the path is
    spelt."""
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
    improved: Improved,
    exported: bytes,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check before an archive is built is the publisher's own. A client from before it, or
    anything else that can sign, builds the archive all the same: the registry reads what it is
    sent, once it knows who signed it, and stores nothing of it."""
    pack = load_pack(improved.root)
    stranger = archive.load_private_key(archive.generate_key()[0])
    with monkeypatch.context() as without_the_check:
        without_the_check.setattr(archive, "refuse_site_cases", lambda files: None)
        data = archive.build(pack, registry.key)
        by_a_stranger = archive.build(pack, stranger)
    path = f"/v1/packs/mendari/patient-inbox-router/{VERSION}"
    publisher = {"authorization": f"Bearer {registry.token}"}

    answer = registry.client.put(path, content=data, headers=publisher)
    assert answer.status_code == 422, answer.text
    assert answer.json()["detail"].startswith(HELD)
    assert names_no_case(answer.text, improved.cases)
    assert registry.client.get(path).status_code == 404
    assert registry.client.get("/v1/health").json()["packs"] == 0

    # The signature comes first: an archive nobody the namespace trusts signed is not read.
    answer = registry.client.put(path, content=by_a_stranger, headers=publisher)
    assert answer.status_code == 422 and "not trusted" in answer.text
    assert REFUSED not in answer.text

    # The same pack before it learned anything, with an export saved in its folder, is
    # refused too; with the file out of the folder, it is published.
    root = copy_of_the_inbox(tmp_path / "patient-inbox-router")
    (root / "reviewed.jsonl").write_bytes(exported)
    with monkeypatch.context() as without_the_check:
        without_the_check.setattr(archive, "refuse_site_cases", lambda files: None)
        data = archive.build(load_pack(root), registry.key)
    answer = registry.client.put(path, content=data, headers=publisher)
    assert answer.status_code == 422, answer.text
    assert answer.json()["detail"].startswith(f"{REFUSED} (2 in reviewed.jsonl)")
    assert registry.client.get("/v1/health").json()["packs"] == 0
    (root / "reviewed.jsonl").unlink()
    assert registry.publish(load_pack(root)).status_code == 201
