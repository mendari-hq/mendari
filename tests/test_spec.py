# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from mendari import Pack, load_pack
from mendari.core.pack import pack_digest
from mendari.core.registry import resolve_pack
from mendari.errors import PackNotFoundError, PackValidationError
from mendari.hub import archive
from tests.conftest import INBOX_DIR


def test_reference_pack_loads(inbox_pack: Pack) -> None:
    assert inbox_pack.id == "mendari/patient-inbox-router"
    assert inbox_pack.manifest.policy.primary == "destination"
    assert inbox_pack.digest.startswith("sha256:")


def test_resolve_by_id_and_version(inbox_pack: Pack) -> None:
    assert resolve_pack("mendari/patient-inbox-router").root == inbox_pack.root
    pinned = f"mendari/patient-inbox-router@{inbox_pack.version}"
    assert resolve_pack(pinned).version == inbox_pack.version
    with pytest.raises(PackNotFoundError):
        resolve_pack("mendari/patient-inbox-router@9.9.9")


def test_output_schema_lists_primary_labels(inbox_pack: Pack) -> None:
    schema = inbox_pack.output_schema
    assert set(schema["properties"]["recommendation"]["enum"]) == {
        "front_desk",
        "medical_assistant",
        "nurse",
        "physician",
        "billing",
        None,
    }


def test_unknown_question_in_provider_slot(pack_copy: Callable[..., Path]) -> None:
    def edit(m: dict) -> None:
        m["providers"]["router"]["answers"] = ["destination", "missing"]

    with pytest.raises(PackValidationError, match="unknown question 'missing'"):
        load_pack(pack_copy(manifest=edit))


def test_question_answered_twice(pack_copy: Callable[..., Path]) -> None:
    def edit(m: dict) -> None:
        m["providers"]["safety_screen"]["answers"] = ["urgency", "destination"]

    with pytest.raises(PackValidationError, match="answered by both"):
        load_pack(pack_copy(manifest=edit))


def test_rule_recommendation_must_be_a_primary_label(pack_copy: Callable[..., Path]) -> None:
    def edit(p: dict) -> None:
        p["rules"][0]["recommendation"] = "pharmacist"

    with pytest.raises(PackValidationError, match="pharmacist"):
        load_pack(pack_copy(policy=edit))


def test_thresholds_must_be_ordered(pack_copy: Callable[..., Path]) -> None:
    def edit(p: dict) -> None:
        p["thresholds"] = {"auto": 0.5, "review": 0.8}

    with pytest.raises(PackValidationError, match="review must be <="):
        load_pack(pack_copy(policy=edit))


def test_clinical_support_pack_cannot_declare_auto_rules(pack_copy: Callable[..., Path]) -> None:
    def manifest(m: dict) -> None:
        m["risk"]["level"] = 2

    def policy(p: dict) -> None:
        p["rules"][0]["outcome"] = "AUTO"

    with pytest.raises(PackValidationError, match="risk level 2 forbids"):
        load_pack(pack_copy(manifest=manifest, policy=policy))


def test_paths_cannot_escape_the_pack(pack_copy: Callable[..., Path]) -> None:
    def edit(m: dict) -> None:
        m["inputs"]["schema"] = "../../etc/passwd"

    with pytest.raises(PackValidationError, match="escapes the agent's folder"):
        load_pack(pack_copy(manifest=edit))


def test_missing_files_are_reported(pack_copy: Callable[..., Path]) -> None:
    root = pack_copy()
    (root / "fhir" / "mapping.yaml").unlink()
    with pytest.raises(PackValidationError, match=r"missing file 'fhir/mapping\.yaml'"):
        load_pack(root)


def test_digest_tracks_behaviour_but_not_attestations(pack_copy: Callable[..., Path]) -> None:
    root = pack_copy()
    before = pack_digest(root)
    (root / "evals" / "benchmark.json").write_text("{}")
    (root / "DECISION_CARD.md").write_text("changed")
    assert pack_digest(root) == before
    policy = root / "policy.yaml"
    policy.write_text(policy.read_text().replace("auto: 0.90", "auto: 0.91"))
    assert pack_digest(root) != before


# -- a pack reads only its own files: no symbolic link out of it --------------------------------

CANARY = "AWS_SECRET_ACCESS_KEY=canary-outside-the-pack"


@pytest.fixture
def inbox_copy(tmp_path: Path) -> Path:
    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
    return root


@pytest.mark.parametrize("link", ["CHANGELOG.md", "agent.yaml", "logic/extra", "notes"])
def test_a_symbolic_link_out_of_the_pack_is_refused(
    inbox_copy: Path, tmp_path: Path, link: str
) -> None:
    # A CHANGELOG.md link to /proc/self/environ once put the server's environment in the card
    # that GET /api/packs/{id} returns; a link to a directory would do the same for its files.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(CANARY)
    root = inbox_copy
    path = root / link
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    path.symlink_to(outside if link == "notes" else outside / "secret.txt")
    with pytest.raises(PackValidationError) as raised:
        load_pack(root)
    assert raised.value.problems == [f"{link}: a symbolic link that leads outside the agent"]


def test_a_virtualenv_or_cache_in_the_pack_folder_is_not_part_of_the_pack(
    inbox_copy: Path, tmp_path: Path
) -> None:
    """A pack kept in its own repository with a virtualenv in it (`uv venv .venv`, whose
    `bin/python` links to the interpreter) validated on main and was refused here. Hidden entries
    and caches are not part of a pack: never digested, archived or read by a fixed name. A path
    the manifest names through one is refused."""
    root = inbox_copy
    before = pack_digest(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(CANARY)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (root / ".direnv").symlink_to(outside)
    (root / "logic" / ".x").symlink_to(outside / "secret.txt")
    (root / "a" / ".b").mkdir(parents=True)
    (root / "a" / ".b" / "c").symlink_to(outside / "secret.txt")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").symlink_to(outside / "secret.txt")
    pack = load_pack(root)
    assert pack.digest == before
    assert [p.relative_to(root).as_posix() for p in archive.pack_files(root)] == [
        p.relative_to(INBOX_DIR).as_posix() for p in archive.pack_files(INBOX_DIR)
    ]
    with pytest.raises(PackValidationError, match="escapes the agent's folder"):
        pack.path(".direnv/secret.txt")
    manifest = root / "agent.yaml"
    (outside / "cases.yaml").write_text("[]\n")
    manifest.write_text(
        manifest.read_text().replace("tests: tests/cases.yaml", "tests: .direnv/cases.yaml", 1)
    )
    with pytest.raises(PackValidationError, match=r"'\.direnv/cases\.yaml' escapes the agent"):
        load_pack(root)


def test_a_link_loop_is_refused(inbox_copy: Path) -> None:
    (inbox_copy / "loop").symlink_to(inbox_copy / "loop")
    with pytest.raises(PackValidationError, match="loop: a symbolic link that leads outside"):
        load_pack(inbox_copy)


def test_a_symbolic_link_inside_the_pack_is_allowed(inbox_copy: Path) -> None:
    root = inbox_copy
    (root / "notes").symlink_to(root / "logic")
    (root / "HISTORY.md").symlink_to("CHANGELOG.md")
    assert load_pack(root).id == "mendari/patient-inbox-router"


# -- what a pack reads is inside its digest: no path through a hidden entry or a cache ----------


@pytest.mark.parametrize(
    "hidden", [".logic/triage_rules.yaml", "__pycache__/triage_rules.yaml", "./.logic/x.yaml"]
)
def test_a_manifest_path_through_a_hidden_entry_is_refused(inbox_copy: Path, hidden: str) -> None:
    """`config: .logic/triage_rules.yaml` loaded and decided, but `pack_digest` skips hidden
    entries: the rules could change without changing the digest a governance approval is bound
    to. A path the pack reads is refused there, as an archive name with a hidden segment is."""
    root = inbox_copy
    target = root / hidden
    target.parent.mkdir(parents=True, exist_ok=True)
    (root / "logic" / "routing_rules.yaml").rename(target)
    manifest = root / "agent.yaml"
    manifest.write_text(manifest.read_text().replace("logic/routing_rules.yaml", hidden))
    with pytest.raises(PackValidationError) as raised:
        load_pack(root)
    assert raised.value.problems == [
        f"providers.router.variants.rules.config: path {hidden!r} goes through a hidden entry or "
        "__pycache__, which is not part of the agent (nor its digest)"
    ]


@pytest.mark.parametrize("named", ["evals/triage_rules.yaml", "logic/DECISION_CARD.md"])
def test_a_manifest_path_to_an_attestation_is_refused(inbox_copy: Path, named: str) -> None:
    """The same hole through `evals/` and `DECISION_CARD.md`, which the digest leaves out as
    attestations about the pack: they are read by their fixed names, never as its logic."""
    root = inbox_copy
    (root / "logic" / "routing_rules.yaml").rename(root / named)
    manifest = root / "agent.yaml"
    manifest.write_text(manifest.read_text().replace("logic/routing_rules.yaml", named))
    with pytest.raises(PackValidationError) as raised:
        load_pack(root)
    assert raised.value.problems == [
        f"providers.router.variants.rules.config: path {named!r} is in evals/ or a "
        "DECISION_CARD.md, attestations about the agent that are not part of its digest"
    ]


def test_a_link_into_a_hidden_entry_is_refused(inbox_copy: Path) -> None:
    root = inbox_copy
    (root / ".logic").mkdir()
    (root / "logic" / "routing_rules.yaml").rename(root / ".logic" / "routing_rules.yaml")
    (root / "logic" / "routing_rules.yaml").symlink_to(root / ".logic" / "routing_rules.yaml")
    with pytest.raises(PackValidationError, match=r"variants\.rules\.config: path 'logic/routi"):
        load_pack(root)


@pytest.mark.parametrize(
    ("field", "edit"),
    [
        ("policy", lambda m: m.update(policy=".policy.yaml")),
        ("tests", lambda m: m.update(tests="tests/.cases.yaml")),
        ("inputs.schema", lambda m: m["inputs"].update(schema=".schemas/input.schema.json")),
        ("evaluation.datasets.test", lambda m: m["evaluation"]["datasets"].update(test=".t.jsonl")),
        (
            "providers.router.variants.cascade.members.naive_bayes.model_file",
            lambda m: m["providers"]["router"]["variants"]["cascade"]["members"][
                "naive_bayes"
            ].update(model_file=".models/nb.json"),
        ),
    ],
)
def test_the_refusal_names_the_manifest_field(
    pack_copy: Callable[..., Path], field: str, edit: Callable[[dict], None]
) -> None:
    with pytest.raises(PackValidationError, match=f"{re.escape(field)}: path '.*' goes through"):
        load_pack(pack_copy(manifest=edit))


def test_pack_path_refuses_a_hidden_entry_and_shipped_packs_are_unaffected(
    inbox_pack: Pack,
) -> None:
    for relative in [".logic/rules.yaml", "logic/.rules.yaml", "__pycache__/x.pyc"]:
        with pytest.raises(PackValidationError, match="goes through a hidden entry"):
            inbox_pack.path(relative)
    assert inbox_pack.path("./logic/../logic/routing_rules.yaml").is_file()
    for pack_dir in sorted(INBOX_DIR.parent.iterdir()):
        assert load_pack(pack_dir).digest == pack_digest(pack_dir)


# -- a pack reads only files its digest hashes, compared by the file on disk -------------------


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "case-probe"
    probe.write_text("")
    try:
        return (directory / "CASE-PROBE").exists()
    finally:
        probe.unlink()


@pytest.mark.parametrize(
    ("actual", "spelled"),
    [
        ("__pycache__/t.yaml", "__PYCACHE__/t.yaml"),
        ("evals/t.yaml", "EVALS/t.yaml"),
        ("logic/DECISION_CARD.md", "logic/decision_card.md"),
    ],
)
def test_a_case_variant_of_a_left_out_path_is_refused(
    inbox_copy: Path, actual: str, spelled: str
) -> None:
    """On a case-insensitive filesystem (macOS, Windows) `config: EVALS/t.yaml` read
    `evals/t.yaml`, which `pack_digest` skips: the refusals compared spellings, so editing the
    rules changed decisions under the same digest. A file is now accepted by its identity on
    disk, whatever spells it."""
    if not _case_insensitive(inbox_copy):
        pytest.skip("needs a case-insensitive filesystem")
    root = inbox_copy
    before = pack_digest(root)
    target = root / actual
    target.parent.mkdir(parents=True, exist_ok=True)
    (root / "logic" / "routing_rules.yaml").rename(target)
    assert pack_digest(root) != before  # the rules left the digest
    manifest = root / "agent.yaml"
    manifest.write_text(manifest.read_text().replace("logic/routing_rules.yaml", spelled))
    with pytest.raises(PackValidationError) as raised:
        load_pack(root)
    assert raised.value.problems == [
        f"providers.router.variants.rules.config: path {spelled!r} is not one of the files the "
        "agent's digest covers (it leaves out hidden entries, __pycache__, evals/ and "
        "DECISION_CARD.md, however a path spells them)"
    ]


def test_pack_path_refuses_a_case_variant_of_an_attestation(inbox_pack: Pack) -> None:
    if not _case_insensitive(inbox_pack.root / "logic"):
        pytest.skip("needs a case-insensitive filesystem")
    for spelled in ["EVALS/benchmark.json", "decision_card.md", "Evals/../EVALS/benchmark.json"]:
        with pytest.raises(PackValidationError, match="not one of the files the agent's digest"):
            inbox_pack.path(spelled)


@pytest.mark.parametrize(
    ("library", "problem"),
    [
        ("evals/criteria.cql", "is in evals/ or a DECISION_CARD.md"),
        (".logic/criteria.cql", "goes through a hidden entry or __pycache__"),
        ("logic/missing.cql", None),
    ],
)
def test_a_cql_library_is_checked_at_load_naming_the_field(
    tmp_path: Path, library: str, problem: str | None
) -> None:
    """`library:` was not among the manifest paths checked at load, so a CQL library could live
    in `evals/`, outside the digest, on any filesystem."""
    root = tmp_path / "trial"
    shutil.copytree(INBOX_DIR.parent / "clinical-trial-prescreener", root)
    if problem is not None:
        (root / library).parent.mkdir(parents=True, exist_ok=True)
        (root / "logic" / "criteria.cql").rename(root / library)
    manifest = root / "agent.yaml"
    manifest.write_text(
        manifest.read_text().replace("library: logic/criteria.cql", f"library: {library}")
    )
    with pytest.raises(PackValidationError) as raised:
        load_pack(root)
    field = "providers.structured.variants.cql.library"
    if problem is None:
        assert raised.value.problems == [f"missing file {library!r}"]
    else:
        assert len(raised.value.problems) == 1
        assert raised.value.problems[0].startswith(f"{field}: path {library!r} {problem}")


def test_pack_path_accepts_only_a_file_the_digest_hashed(inbox_copy: Path) -> None:
    """Any filesystem: a file the load did not hash (created after it, or under a key the
    manifest check does not list) is refused by `Pack.path`, so no provider reads it."""
    root = inbox_copy
    pack = load_pack(root)
    assert pack.path("logic/routing_rules.yaml").is_file()
    assert not pack.path("logic/not-there.yaml").exists()  # a file to write, or to check for
    (root / "logic" / "later.yaml").write_text("rules: []\n")
    with pytest.raises(PackValidationError, match=r"'logic/later\.yaml' is not one of the files"):
        pack.path("logic/later.yaml")
    with pytest.raises(PackValidationError, match=r"is in evals/ or a DECISION_CARD\.md"):
        pack.path("evals/benchmark.json")
    assert pack.attestation("evals/benchmark.json").is_file()
    assert pack.attestation("DECISION_CARD.md").is_file()
    with pytest.raises(PackValidationError, match=r"is not in evals/ or a DECISION_CARD\.md"):
        pack.attestation("logic/routing_rules.yaml")
    with pytest.raises(PackValidationError, match="goes through a hidden entry"):
        pack.attestation("evals/.x.json")


def test_the_benchmark_must_be_an_attestation(inbox_copy: Path) -> None:
    """`evaluation.benchmark` is the one manifest path that must be outside the digest: a
    published benchmark is about the pack, and publishing one must not change its identity."""
    manifest = inbox_copy / "agent.yaml"
    text = manifest.read_text().replace(
        "benchmark: evals/benchmark.json", "benchmark: logic/benchmark.json"
    )
    manifest.write_text(text)
    with pytest.raises(PackValidationError) as raised:
        load_pack(inbox_copy)
    assert raised.value.problems == [
        "evaluation.benchmark: path 'logic/benchmark.json' is not in evals/ or a "
        "DECISION_CARD.md: an attestation about the agent is kept outside its digest"
    ]


def test_every_shipped_pack_reads_only_digested_files() -> None:
    """Every file a shipped pack opens while it loads, builds every provider variant (and its
    FHIR mapping), reads its tests and decides them is one `pack_digest` hashes, by the file on
    disk. Watched with an audit hook on `open`, independently of `Pack.path`."""
    for pack_dir in sorted(INBOX_DIR.parent.iterdir()):
        root = pack_dir.resolve()
        digested = [
            file
            for file in root.rglob("*")
            if file.is_file()
            and not any(
                p.startswith(".") or p == "__pycache__" for p in file.relative_to(root).parts
            )
            and file.relative_to(root).parts[0] != "evals"
            and file.name != "DECISION_CARD.md"
        ]
        opened = _opened_under(root, partial(_exercise, root))
        assert opened, pack_dir.name
        for path in opened:
            assert any(os.path.samefile(path, file) for file in digested), (pack_dir.name, path)


def _exercise(root: Path) -> None:
    from mendari.core.decision import Decision
    from mendari.evals.pack_tests import load_cases
    from mendari.providers import build_provider
    from mendari.standards.fhir import FhirAdapter

    pack = load_pack(root)
    for slot, spec in pack.manifest.providers.items():
        for variant in spec.variants:
            build_provider(pack, slot, variant)
    if pack.manifest.inputs.fhir:
        FhirAdapter.for_pack(pack)
    run = Decision(pack)
    for case in load_cases(pack):
        run.decide(case["input"])


_WATCHED: list[tuple[Path, list[Path]]] = []
_HOOKED: list[bool] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event != "open" or not _WATCHED or not isinstance(args[0], str | Path):
        return
    root, opened = _WATCHED[-1]
    path = Path(args[0])
    if path.is_absolute() and path.is_relative_to(root):
        opened.append(path)


def _opened_under(root: Path, action: Callable[[], None]) -> list[Path]:
    if not _HOOKED:  # an audit hook cannot be removed: added once, idle unless watching
        sys.addaudithook(_audit)
        _HOOKED.append(True)
    opened: list[Path] = []
    _WATCHED.append((root, opened))
    try:
        action()
    finally:
        _WATCHED.pop()
    return opened


def test_the_card_reads_the_changelog_only_inside_the_pack(
    inbox_copy: Path, tmp_path: Path
) -> None:
    from mendari.core.card import render_card

    secret = tmp_path / "secret.txt"
    secret.write_text(CANARY)
    root = inbox_copy
    pack = load_pack(root)
    (root / "CHANGELOG.md").unlink()
    (root / "CHANGELOG.md").symlink_to(secret)  # after the load's own check
    with pytest.raises(PackValidationError, match="escapes the agent's folder"):
        render_card(pack)


def test_card_write_never_writes_through_a_symbolic_link(
    inbox_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mendari.cli import main

    root = inbox_copy
    (root / "notes.md").write_text("mine\n")
    (root / "DECISION_CARD.md").unlink()
    (root / "DECISION_CARD.md").symlink_to("notes.md")
    assert main(["card", str(root), "--write"]) != 0
    assert "symbolic link" in capsys.readouterr().err
    assert (root / "notes.md").read_text() == "mine\n"
