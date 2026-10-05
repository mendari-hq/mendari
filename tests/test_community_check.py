"""`mendari community-check` (plan B4, B6): every rule CI enforces on a community pack, each on a
pack that breaks only it, in a throwaway git repository shaped like Mendari's
(`community-packs/<name>/`, `community-packs/MAINTAINERS.yaml`). The pack is what
`mendari new --community` writes, which passes as it is."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from mendari.cli import main
from mendari.community import maintainers, phi_scan, regex_safety
from mendari.community.check import Report, check, has_entry, pr_scope_problems
from mendari.errors import MendariError
from tests.conftest import ROOT

NAME = "refill-router"
PACK_ID = f"community/{NAME}"
SORTER = ROOT / "tests" / "fixtures" / "community-packs" / "visit-sorter"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout


def commit(repo: Path, message: str = "change") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def write_maintainers(repo: Path, entries: list[dict[str, Any]]) -> None:
    path = repo / "community-packs" / "MAINTAINERS.yaml"
    path.write_text(yaml.safe_dump({"packs": entries}, sort_keys=False))


@pytest.fixture(scope="module")
def scaffold(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`mendari new --community`, once: tests copy it."""
    target = tmp_path_factory.mktemp("scaffold") / NAME
    assert main(["new", str(target), "--community", "--publisher", "Test Clinic"]) == 0
    return target


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository whose `main` has an empty MAINTAINERS.yaml and no packs, on a branch off it.
    A test that wants the base to hold its commits so far moves `main` there."""
    root = tmp_path / "repo"
    (root / "community-packs").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    write_maintainers(root, [])
    (root / "README.md").write_text("A repository.\n")
    commit(root, "base")
    git(root, "checkout", "-q", "-b", "change")  # `main` stays the base
    return root


@pytest.fixture
def new_pack(repo: Path, scaffold: Path) -> Path:
    """A new community pack, added (with its MAINTAINERS entry) since `main`."""
    pack = repo / "community-packs" / NAME
    shutil.copytree(scaffold, pack)
    write_maintainers(repo, [{"id": PACK_ID, "maintainers": ["octocat"]}])
    commit(repo, "add the pack")
    return pack


def run(pack: Path, base: str | None = "main", **kwargs: Any) -> Report:
    return check(pack, base=base, **kwargs)


def messages(report: Report, rule: str) -> list[str]:
    return [f.message for f in report.findings if f.rule == rule and not f.warning]


def has(report: Report, rule: str, text: str) -> bool:
    return any(text in message for message in messages(report, rule))


def edit_manifest(pack: Path, change: Callable[[dict[str, Any]], None]) -> None:
    path = pack / "pack.yaml"
    manifest = yaml.safe_load(path.read_text())
    change(manifest)
    path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))


def regenerate(pack: Path) -> None:
    """What the author does after a change: the benchmark and the card again."""
    out = pack / "evals" / "benchmark.json"
    assert (
        main(["eval", str(pack), "--dataset", "examples", "--baseline", "none", "--out", str(out)])
        == 0
    )
    assert main(["card", str(pack), "--write"]) == 0


# -- the scaffold passes ------------------------------------------------------------------------


def test_a_new_community_pack_passes_as_written(new_pack: Path) -> None:
    report = run(new_pack)
    assert report.passed, report.text()
    assert report.pack_id == PACK_ID and not report.warnings
    assert report.text().endswith("passed: ready for review")
    markdown = report.markdown()
    assert "community-check passed" in markdown
    assert "| Metric | main | this change |" in markdown and "A new pack" in markdown
    assert "<details><summary>Decision Card</summary>" in markdown
    assert report.card is not None and report.card.startswith("# Decision Card: Refill router")


def test_the_scaffold_has_the_community_defaults(scaffold: Path) -> None:
    manifest = yaml.safe_load((scaffold / "pack.yaml").read_text())
    assert manifest["id"] == PACK_ID and manifest["license"] == "Apache-2.0"
    assert manifest["publisher"] == {"name": "Test Clinic", "type": "community"}
    assert manifest["audit"] == {"store_input": "hash"}
    assert manifest["providers"]["reader"]["use"] == "rules"
    assert manifest["evaluation"]["benchmark"] == "evals/benchmark.json"
    assert manifest["evaluation"]["critical"] == [{"question": "urgent_symptoms", "label": "yes"}]
    assert "## 0.1.0" in (scaffold / "CHANGELOG.md").read_text()
    assert (scaffold / "DECISION_CARD.md").is_file() and (
        scaffold / "evals/benchmark.json"
    ).is_file()
    assert not list(scaffold.rglob("*{{*")) and "{{" not in (scaffold / "CHANGELOG.md").read_text()


def test_without_a_base_the_change_itself_is_not_checked(new_pack: Path) -> None:
    report = run(new_pack, base=None)
    assert report.passed, report.text()
    assert report.not_checked and "--base" in report.not_checked[0]


def test_new_community_derives_and_checks_the_id(tmp_path: Path) -> None:
    assert main(["new", str(tmp_path / "Bad_Name"), "--community"]) == 1
    assert main(["new", str(tmp_path / "sorter"), "--community", "--id", "community/other"]) == 1
    assert not (tmp_path / "sorter").exists()
    assert main(["new", str(tmp_path / "no-id")]) == 1  # a plain pack still needs --id


# -- scope ---------------------------------------------------------------------------------------


def test_a_change_outside_the_pack_folder_fails(new_pack: Path, repo: Path) -> None:
    (repo / "src.py").write_text("print('hello')\n")
    commit(repo)
    (repo / "README.md").write_text("Changed.\n")  # not committed yet: counted too
    (repo / "notes.txt").write_text("mine\n")  # untracked outside the packs: not in the change
    report = run(new_pack)
    assert has(report, "scope", "README.md, src.py")
    assert has(report, "scope", "open a separate pull request")


def test_editing_another_packs_maintainers_entry_fails(new_pack: Path, repo: Path) -> None:
    write_maintainers(
        repo,
        [
            {"id": PACK_ID, "maintainers": ["octocat"]},
            {
                "id": "community/other",
                "maintainers": ["someone"],
                "removed": "2026-10-01",
                "reason": "unmaintained",
            },
        ],
    )
    commit(repo)
    git(repo, "branch", "-f", "main")  # the base now has the other entry
    write_maintainers(repo, [{"id": PACK_ID, "maintainers": ["octocat"]}])  # tombstone dropped
    assert has(run(new_pack), "scope", "community/other")
    assert not has(run(new_pack, owner=True), "scope", "community/other")


def test_a_nested_pack_and_a_folder_outside_community_packs_fail(
    new_pack: Path, tmp_path: Path, scaffold: Path
) -> None:
    shutil.copytree(SORTER, new_pack / "extra")
    assert has(run(new_pack), "scope", "remove extra/pack.yaml")
    elsewhere = tmp_path / "elsewhere" / NAME
    shutil.copytree(scaffold, elsewhere)
    assert has(run(elsewhere, base=None), "scope", "lives in community-packs/<name>/")


def test_the_pull_request_scope() -> None:
    pack = ["community-packs/a/pack.yaml", "community-packs/a/data/x.jsonl"]
    maintainers_file = "community-packs/MAINTAINERS.yaml"
    assert pr_scope_problems(["src/mendari/cli.py"], owner=False) == []  # no community pack
    assert pr_scope_problems([*pack, maintainers_file], owner=False) == []
    (two,) = pr_scope_problems([*pack, "community-packs/b/pack.yaml"], owner=False)
    assert "2 community packs (a, b)" in two
    (outside,) = pr_scope_problems(
        [*pack, "src/mendari/cli.py", "community-packs/README.md"], owner=False
    )
    assert "src/mendari/cli.py, community-packs/README.md" in outside
    (alone,) = pr_scope_problems([maintainers_file], owner=False)
    assert "only together with the pack it lists" in alone
    assert pr_scope_problems(["community-packs/README.md"], owner=False) == []  # docs only
    assert pr_scope_problems([*pack, "src/mendari/cli.py"], owner=True) == []


# -- identity ------------------------------------------------------------------------------------


def test_the_id_must_match_the_folder(new_pack: Path) -> None:
    edit_manifest(new_pack, lambda m: m.update(id="community/something-else"))
    assert has(run(new_pack), "identity", f"has the id {PACK_ID}")


def test_the_folder_name_is_checked(repo: Path, scaffold: Path) -> None:
    pack = repo / "community-packs" / "Refill_Router"
    shutil.copytree(scaffold, pack)
    assert has(run(pack, base=None), "identity", "lowercase letters, digits and dashes")


def test_a_removed_id_is_never_used_again(new_pack: Path, repo: Path) -> None:
    write_maintainers(
        repo,
        [
            {
                "id": PACK_ID,
                "maintainers": ["octocat"],
                "removed": "2026-09-01",
                "reason": "unmaintained",
            }
        ],
    )
    commit(repo)
    git(repo, "branch", "-f", "main")
    # Dropping the tombstone in the change does not help: the base's still counts.
    write_maintainers(repo, [{"id": PACK_ID, "maintainers": ["octocat"]}])
    assert has(run(new_pack), "identity", "was removed on 2026-09-01")


def test_a_pack_needs_its_maintainers(new_pack: Path, repo: Path) -> None:
    write_maintainers(repo, [])
    assert has(run(new_pack), "identity", f"add an entry for {PACK_ID}")
    (repo / "community-packs" / "MAINTAINERS.yaml").write_text("packs: [{id: x}]\n")
    assert has(run(new_pack), "identity", "`maintainers` must list one or more GitHub user names")
    (repo / "community-packs" / "MAINTAINERS.yaml").unlink()
    assert has(run(new_pack), "identity", "MAINTAINERS.yaml is missing")


def test_an_id_another_folder_claims_is_refused(new_pack: Path, repo: Path) -> None:
    shutil.copytree(new_pack, repo / "community-packs" / "copy")
    assert has(run(new_pack), "identity", "already used by community-packs/copy/")


def test_a_terminology_collision_is_refused(repo: Path) -> None:
    # community/visit-sorter's question `topic` and community/visit's question `sorter_topic`
    # both become the CodeSystem id community-visit-sorter-topic.
    sorter = repo / "community-packs" / "visit-sorter"
    shutil.copytree(SORTER, sorter)
    other = repo / "community-packs" / "visit"
    shutil.copytree(SORTER, other)
    for path in other.rglob("*.yaml"):
        text = path.read_text().replace("topic", "sorter_topic")
        path.write_text(text.replace("community/visit-sorter", "community/visit"))
    report = run(sorter, base=None)
    assert has(report, "identity", "collides with another pack's")
    assert has(report, "identity", "community-visit-sorter-topic")


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda m: m.update(name="patient inbox  router"),
            "is the name of mendari/patient-inbox-router, a pack Mendari maintains",
        ),
        (
            lambda m: m.update(name="Patient-Inbox Router."),
            "is the name of mendari/patient-inbox-router, a pack Mendari maintains",
        ),
        (lambda m: m["publisher"].update(name="Mendari reference packs"), "names mendari"),
        (lambda m: m["publisher"].update(name="M\u0435ndari reference packs"), "names mendari"),
        (
            lambda m: m["publisher"].update(name="M\u0435ndari reference packs"),
            "mixes Latin letters with Cyrillic ones",
        ),
        (lambda m: m["publisher"].update(name="Mendar\u0131 packs"), "names mendari"),
        (lambda m: m["publisher"].update(name="\uff2d\uff45ndari"), "names mendari"),
        (lambda m: m.update(name="Open-Health refills"), "names openhealth"),
    ],
    ids=[
        "maintained-name",
        "maintained-name-punctuated",
        "mendari-publisher",
        "cyrillic-e",
        "mixed-alphabets",
        "dotless-i",
        "fullwidth",
        "old-name",
    ],
)
def test_a_community_pack_cannot_pass_for_one_mendari_maintains(
    new_pack: Path, change: Callable[[dict[str, Any]], None], expected: str
) -> None:
    edit_manifest(new_pack, change)
    assert has(run(new_pack, base=None), "identity", expected)


def test_a_community_packs_own_name_may_carry_accents(new_pack: Path) -> None:
    """Folding names to compare them refuses look-alikes, not names in other languages."""
    edit_manifest(new_pack, lambda m: m.update(name="Clínica: tri des visites"))
    report = run(new_pack, base=None)
    assert not has(report, "identity", "name 'Cl")


def test_a_community_packs_fhir_systems_live_under_its_own_url(repo: Path) -> None:
    """Before, CI passed a pack whose performer system was the inbox router's: its definitions
    were then served in Mendari's CodeSystem."""
    pack = repo / "community-packs" / "inbox-copy"
    shutil.copytree(ROOT / "packs" / "patient-inbox-router", pack)
    edit_manifest(pack, lambda m: m.update(id="community/inbox-copy"))
    report = run(pack, base=None)
    assert has(report, "identity", "task.performer_system is 'https://mendari.co/fhir/CodeSystem")
    assert has(report, "identity", "https://mendari.co/packs/community/inbox-copy/")


def test_the_repositorys_maintainers_file_matches_its_packs() -> None:
    """Every pack in community-packs/ has an entry, and every entry but a tombstone has its
    pack: what a contributor's pull request adds keeps this true, so it never fails CI."""
    assert_maintainers_match(ROOT / "community-packs")


def assert_maintainers_match(base: Path) -> None:
    entries = maintainers.parse((base / "MAINTAINERS.yaml").read_bytes())
    folders = {path.parent.name for path in base.glob("*/pack.yaml")}
    listed = {
        pack_id.removeprefix("community/") for pack_id, e in entries.items() if not e.tombstone
    }
    assert all(pack_id.startswith("community/") for pack_id in entries)
    assert listed == folders, (sorted(listed), sorted(folders))
    gone = {pack_id.removeprefix("community/") for pack_id, e in entries.items() if e.tombstone}
    assert not gone & folders


@pytest.mark.parametrize("removed_before", [False, True], ids=["as-committed", "after-a-removal"])
def test_a_contributors_maintainers_entry_keeps_the_repository_check_green(
    tmp_path: Path, scaffold: Path, removed_before: bool
) -> None:
    """The guide's steps (a pack and its entry) pass; a pack without its entry, or an entry
    without its pack, does not. A tombstone stays, including one already in the file (the
    owner removed a pack before this contribution): it stays a tombstone."""
    base = tmp_path / "community-packs"
    shutil.copytree(ROOT / "community-packs", base)
    shutil.copytree(scaffold, base / NAME)
    if removed_before:
        earlier = yaml.safe_load((base / "MAINTAINERS.yaml").read_text())["packs"] or []
        removal = {"id": "community/gone", "maintainers": ["y"], "removed": "2026-10-05"}
        write_maintainers(tmp_path, [*earlier, {**removal, "reason": "unmaintained"}])
    rows = yaml.safe_load((base / "MAINTAINERS.yaml").read_text())["packs"] or []
    write_maintainers(tmp_path, [*rows, {"id": PACK_ID, "maintainers": ["janedoe"]}])
    assert_maintainers_match(base)
    gone = {"id": "community/old", "maintainers": ["x"], "removed": "2026-10-04", "reason": "r"}
    write_maintainers(tmp_path, [*rows, {"id": PACK_ID, "maintainers": ["janedoe"]}, gone])
    assert_maintainers_match(base)
    write_maintainers(tmp_path, rows)
    with pytest.raises(AssertionError):
        assert_maintainers_match(base)


def test_the_maintainers_format() -> None:
    entries = maintainers.parse(
        b"packs:\n  - {id: community/a, maintainers: [octocat]}\n"
        b'  - {id: community/b, maintainers: [x-y], removed: "2026-10-04", reason: gone}\n'
    )
    assert not entries["community/a"].tombstone and entries["community/b"].tombstone
    for bad in (
        b"packs: {}\n",
        b"packs: [{id: a, maintainers: [-bad]}]\n",
        b"packs: [{id: a, maintainers: [x], extra: 1}]\n",
        b"packs: [{id: a, maintainers: [x]}, {id: a, maintainers: [y]}]\n",
    ):
        with pytest.raises(MendariError):
            maintainers.parse(bad)


# -- the manifest --------------------------------------------------------------------------------


BLOCK_RULE = {
    "name": "no-refills",
    "when": {"question": "request", "labels": ["refill"]},
    "outcome": "BLOCK",
    "reason": "Refills are refused.",
}


def _llm_default(m: dict[str, Any]) -> None:
    m["providers"]["reader"]["variants"]["llm"] = {"type": "llm"}
    m["providers"]["reader"]["use"] = "llm"


def _pinned_model(m: dict[str, Any]) -> None:
    m["providers"]["reader"]["variants"]["llm"] = {"type": "llm", "model": "example/model"}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda m: m.update(license="MIT"), "a community pack is `license: Apache-2.0`"),
        (lambda m: m["publisher"].update(type="verified"), "says `type: community`"),
        (lambda m: m.update(audit={"store_input": "full"}), "`audit: {store_input: hash}`"),
        (_llm_default, "the default variant 'llm' calls a service"),
        (_pinned_model, "providers.reader.variants.llm: remove `model`"),
        (lambda m: m["evaluation"].update(degradation_tolerance=0.1), "at most 0.05"),
        (lambda m: m["risk"].update(level=2), "at most risk level 1"),
        (lambda m: m["policy"]["rules"].append(BLOCK_RULE), "'no-refills' has outcome BLOCK"),
    ],
    ids=[
        "licence",
        "publisher",
        "store-input",
        "remote-default",
        "pinned-model",
        "tolerance",
        "risk",
        "block-rule",
    ],
)
def test_the_manifest_policy(
    new_pack: Path, change: Callable[[dict[str, Any]], None], expected: str
) -> None:
    edit_manifest(new_pack, change)
    report = run(new_pack, base=None)
    assert has(report, "manifest", expected), report.text()
    if change is _llm_default:  # nothing decides with a remote default, so nothing calls out
        assert any("once the default providers are local" in n for n in report.not_checked)


# -- correctness ---------------------------------------------------------------------------------


def test_a_failing_behaviour_test_fails(new_pack: Path) -> None:
    cases = new_pack / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text().replace("recommendation: appointment", "recommendation: refill")
    )
    assert has(run(new_pack, base=None), "correctness", "an appointment request goes to scheduling")


def test_each_critical_label_needs_a_test_case(new_pack: Path) -> None:
    cases = new_pack / "tests" / "cases.yaml"
    kept = yaml.safe_load(cases.read_text())[:2]  # drop the chest-pain case
    cases.write_text(yaml.safe_dump(kept))
    assert has(run(new_pack, base=None), "correctness", "critical label urgent_symptoms=yes")


def test_a_pack_that_does_not_load_says_why(new_pack: Path) -> None:
    edit_manifest(new_pack, lambda m: m["policy"].update(primary="nothing"))
    report = run(new_pack, base=None)
    assert has(report, "correctness", "does not load")
    assert report.card is None


# -- evaluation ----------------------------------------------------------------------------------


def _examples(pack: Path) -> list[dict[str, Any]]:
    lines = (pack / "data" / "examples.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _write_examples(pack: Path, records: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(r) + "\n" for r in records)
    (pack / "data" / "examples.jsonl").write_text(text)


def test_a_critical_label_needs_five_benchmark_cases(new_pack: Path) -> None:
    records = [r for r in _examples(new_pack) if r["id"] not in ("ex-13", "ex-14", "ex-15")]
    _write_examples(new_pack, records)
    regenerate(new_pack)
    assert has(run(new_pack, base=None), "evaluation", "3 case(s) of the critical label")


def test_a_critical_label_found_too_rarely_fails(new_pack: Path) -> None:
    missed = [
        {
            "id": f"miss-{i}",
            "input": {"message": {"body": text}},
            "labels": {"request": "other", "urgent_symptoms": "yes"},
        }
        for i, text in enumerate(["My lips are turning blue.", "I fainted twice today."])
    ]
    _write_examples(new_pack, _examples(new_pack) + missed)
    regenerate(new_pack)
    report = run(new_pack, base=None)
    assert has(report, "evaluation", "urgent_symptoms=yes is found in 75% of its cases")
    assert report.passed is False and not messages(report, "honest-numbers")


def test_a_pack_must_declare_a_critical_label(new_pack: Path) -> None:
    def no_critical(m: dict[str, Any]) -> None:
        del m["evaluation"]["critical"]

    edit_manifest(new_pack, no_critical)
    assert has(run(new_pack, base=None), "evaluation", "declare at least one critical label")


def test_a_regression_fails_with_the_bases_tolerance(new_pack: Path, repo: Path) -> None:
    git(repo, "branch", "-f", "main")  # the base now has the pack
    rules = new_pack / "logic" / "rules.yaml"
    rules.write_text(rules.read_text().replace("refill|renew|", ""))  # misses some refills
    # The change sets its own, looser tolerance; the base's 0.03 still applies.
    edit_manifest(new_pack, lambda m: m["evaluation"].update(degradation_tolerance=0.05))
    edit_manifest(new_pack, lambda m: m.update(version="0.2.0"))
    changelog = new_pack / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text().replace("## 0.1.0", "## 0.2.0\n\n- Fewer rules.\n\n## 0.1.0")
    )
    regenerate(new_pack)
    report = run(new_pack)
    assert has(report, "evaluation", "regression against the base (tolerance 3%, from the base)")
    assert has(report, "evaluation", "request accuracy fell")
    assert report.metrics is not None and "A new pack" not in report.metrics


def test_an_update_that_follows_the_rules_passes(new_pack: Path, repo: Path) -> None:
    git(repo, "branch", "-f", "main")
    rules = new_pack / "logic" / "rules.yaml"
    rules.write_text(rules.read_text().replace("ran out", "ran out|all out"))
    edit_manifest(new_pack, lambda m: m.update(version="0.1.1"))
    changelog = new_pack / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text().replace("## 0.1.0", "## 0.1.1\n\n- All out.\n\n## 0.1.0")
    )
    regenerate(new_pack)
    report = run(new_pack)
    assert report.passed, report.text()
    assert report.metrics is not None and "No regression" in report.metrics


def test_an_update_cannot_hide_a_regression_by_editing_its_own_benchmark(
    new_pack: Path, repo: Path
) -> None:
    """Before, the update below passed: it drops the `breathing` rule and the two benchmark
    cases only that rule caught, so on its own dataset every floor holds. On the base's, it
    finds 4 of 6 critical cases."""
    git(repo, "branch", "-f", "main")
    rules = new_pack / "logic" / "rules.yaml"
    rules.write_text(
        "".join(line for line in rules.read_text().splitlines(True) if "breathing" not in line)
    )
    kept = [r for r in _examples(new_pack) if r["id"] not in ("ex-11", "ex-13")]
    chest = {"message": {"body": "Crushing chest pain right now."}}
    kept.append(
        {"id": "ex-16", "input": chest, "labels": {"request": "other", "urgent_symptoms": "yes"}}
    )
    _write_examples(new_pack, kept)
    edit_manifest(new_pack, lambda m: m.update(version="0.2.0"))
    changelog = new_pack / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text().replace("## 0.1.0", "## 0.2.0\n\n- Fewer rules.\n\n## 0.1.0")
    )
    regenerate(new_pack)
    report = run(new_pack)
    assert not report.passed
    assert has(
        report,
        "evaluation",
        "on the base's benchmark dataset: the critical label urgent_symptoms=yes is found in 67%",
    )
    assert has(report, "evaluation", "on the base's benchmark dataset: regression against the base")
    gone = [f.message for f in report.warnings if f.rule == "evaluation"]
    assert any("ex-11, ex-13 are gone" in message for message in gone)


def test_an_update_keeps_the_bases_critical_labels(new_pack: Path, repo: Path) -> None:
    git(repo, "branch", "-f", "main")
    edit_manifest(new_pack, lambda m: m["evaluation"].update(critical=[]))
    assert has(
        run(new_pack), "evaluation", "the base declares the critical label urgent_symptoms=yes"
    )


# -- honest numbers ------------------------------------------------------------------------------


def test_a_faked_benchmark_fails(new_pack: Path) -> None:
    path = new_pack / "evals" / "benchmark.json"
    benchmark = json.loads(path.read_text())
    benchmark["questions"]["request"]["accuracy"] = 0.5
    path.write_text(json.dumps(benchmark, indent=2) + "\n")
    report = run(new_pack, base=None)
    assert has(report, "honest-numbers", "questions.request.accuracy")
    assert has(report, "honest-numbers", "--baseline none --out")


def test_a_benchmark_only_differing_in_id_time_and_latency_passes(new_pack: Path) -> None:
    path = new_pack / "evals" / "benchmark.json"
    benchmark = json.loads(path.read_text())
    benchmark["id"] = "another"
    benchmark["latency_ms"] = {"mean": 9.0, "p50": 9.0, "p95": 9.0, "max": 9.0}
    path.write_text(json.dumps(benchmark, indent=2) + "\n")
    assert main(["card", str(new_pack), "--write"]) == 0
    assert run(new_pack, base=None).passed


def test_an_edited_or_missing_card_fails(new_pack: Path) -> None:
    card = new_pack / "DECISION_CARD.md"
    card.write_text(card.read_text().replace("Front-desk and nursing staff", "Anyone"))
    assert has(run(new_pack, base=None), "honest-numbers", "is not the card the pack renders")
    card.unlink()
    assert has(run(new_pack, base=None), "honest-numbers", "DECISION_CARD.md is missing")


def test_a_stale_benchmark_after_a_rule_change_fails(new_pack: Path) -> None:
    rules = new_pack / "logic" / "rules.yaml"
    rules.write_text(rules.read_text().replace("ran out", "ran out|all out"))
    report = run(new_pack, base=None)
    assert has(report, "honest-numbers", "pack.digest")  # the benchmark is of the old pack


def _bayes_pack(repo: Path) -> Path:
    """A community pack whose default is a trained naive Bayes model."""
    pack = repo / "community-packs" / "visit-sorter"
    shutil.copytree(SORTER, pack)
    texts = {
        "billing": ["question about my bill", "my insurance payment", "the bill is wrong"],
        "scheduling": ["reschedule my appointment", "cancel my appointment", "move my visit"],
        "other": ["where do I park", "thank you doctor", "what are your hours"],
    }
    rows = [
        {"input": {"message": text}, "labels": {"topic": label}}
        for label, items in texts.items()
        for text in items
    ]
    (pack / "data").mkdir()
    for split in ("train", "calibration"):
        (pack / "data" / f"{split}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def bayes(m: dict[str, Any]) -> None:
        m["providers"]["reader"] = {
            "answers": ["topic"],
            "use": "bayes",
            "variants": {
                "bayes": {
                    "type": "naive_bayes",
                    "model_file": "models/topic.json",
                    "text_fields": ["message"],
                }
            },
        }
        m["evaluation"] = {
            "datasets": {"train": "data/train.jsonl", "calibration": "data/calibration.jsonl"}
        }

    edit_manifest(pack, bayes)
    assert main(["train", str(pack)]) == 0
    return pack


def test_a_composites_trained_member_must_be_what_training_gives(repo: Path) -> None:
    """Before, only top-level naive_bayes variants were trained again: a cascade's members'
    model could be anything (trained elsewhere, on real messages), and its vocabulary was not
    scanned."""
    pack = _bayes_pack(repo)
    member = {"type": "naive_bayes", "model_file": "models/topic.json", "text_fields": ["message"]}
    cascade = {"type": "cascade", "accept_at": 0.9, "members": {"a": member, "b": dict(member)}}
    edit_manifest(
        pack,
        lambda m: m["providers"]["reader"].update(use="both", variants={"both": cascade}),
    )
    assert main(["train", str(pack)]) == 0
    report = run(pack, base=None)
    assert not messages(report, "honest-numbers") and not report.warnings
    model = pack / "models" / "topic.json"
    loaded = json.loads(model.read_text())
    loaded["tokens"]["123-45-6789"] = loaded["tokens"][next(iter(loaded["tokens"]))]
    model.write_text(json.dumps(loaded, separators=(",", ":")) + "\n")
    report = run(pack, base=None)
    assert has(report, "honest-numbers", "models/topic.json is not what `mendari train` gives")
    (phi,) = [f.message for f in report.warnings if f.rule == "phi"]
    assert "models/topic.json: vocabulary word" in phi and "123-45" not in phi


def test_a_trained_model_must_be_what_training_gives(repo: Path) -> None:
    pack = _bayes_pack(repo)
    assert not messages(run(pack, base=None), "honest-numbers")
    model = pack / "models" / "topic.json"
    loaded = json.loads(model.read_text())
    loaded["temperature"] = 0.5
    model.write_text(json.dumps(loaded, separators=(",", ":")) + "\n")
    report = run(pack, base=None)
    assert has(report, "honest-numbers", "models/topic.json is not what `mendari train` gives")


# -- patterns ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [r"(a+)+$", r"(\w+\s?)+$", r"x(a*)*y", r"(a|a)*$", r"(a|aa)+$", r"(.|\s)*$", r"(ab|a?b)+c"],
)
def test_the_static_check_refuses_backtracking_shapes(pattern: str) -> None:
    assert regex_safety.static_problems(pattern, re.IGNORECASE)


@pytest.mark.parametrize(
    "pattern",
    [
        r"refill|renew|running (low|out)|ran out",
        r"(can'?t|trouble|hard to) breathe?|short(ness)? of breath",
        r"(refill|renew)+",
        r"(ab|a)*c",
        r"(?:x|xy)+$",
        r"(\w|\d)+$",  # `re` turns single characters into one set: no alternation runs
        r"(?:a++)+$",  # possessive: never backtracks
        r"^\d{3}-\d{4}$",
    ],
)
def test_the_static_check_allows_ordinary_patterns(pattern: str) -> None:
    assert regex_safety.static_problems(pattern, re.IGNORECASE) == []


def test_the_probe_times_out_slow_patterns_in_another_process() -> None:
    jobs = [
        regex_safety.Job("fast", r"\bchest (pain|pressure)", re.IGNORECASE),
        regex_safety.Job("nested", r"(a+)+$", 0),
        regex_safety.Job("polynomial", r"a*a*a*a*a*b", 0),  # the static check misses it
        regex_safety.Job("also fast", r"refill|renew", re.IGNORECASE),
    ]
    assert [job.where for job in regex_safety.probe(jobs)] == ["nested", "polynomial"]


def test_a_backtracking_rule_pattern_fails(new_pack: Path) -> None:
    rules = new_pack / "logic" / "rules.yaml"
    # Bounded repeats are not the static check's: the probe times them.
    text = rules.read_text().replace('"refill|renew|', '"(re+)+fill|a{0,3000}a{0,3000}z|renew|')
    rules.write_text(text)
    patterns = messages(run(new_pack, base=None), "patterns")
    assert len(patterns) == 1 and "inside another one" in patterns[0]
    assert "questions.request.labels.refill[0] (refill)" in patterns[0]
    rules.write_text(text.replace("(re+)+fill|", ""))
    (slow,) = messages(run(new_pack, base=None), "patterns")
    assert "took more than 1 s on a 10,000-character text" in slow


@pytest.mark.parametrize(
    "pattern",
    [
        r"\d*\d*x",
        r"(?:ab|cd|ef|gh|ij|kl|mn|op)\d*\d*\d*\d*x",  # the probe-padding bypass
        r"(?:\d\d)*(?:\d\d)*x",  # repeated groups, not single characters
        r"q[0-9]z(?:00)*(?:00)*(?:00)*x",
        r"a*a*a*a*a*b",
        r"[a-z]+\s*[a-z]+$",  # only what both can match (or nothing) between them
        r"(\w+)(\w*)!",  # groups do not hide the sequence
        r"\d+0\d+x",  # a mandatory piece the first can also match
    ],
)
def test_the_static_check_refuses_quantifiers_one_after_another(pattern: str) -> None:
    (problem,) = regex_safety.static_problems(pattern, re.IGNORECASE)
    assert "one after another" in problem


@pytest.mark.parametrize(
    "pattern",
    [
        r"\d+\.\d+",  # a piece neither matches fixes the split
        r"[a-z]+ [a-z]+",
        r"\d+\s*(mg|ml)",
        r"chest.*pain",
        r".*foo.*",  # nothing after the second can fail: the first try matches
        r"\w+@\w+",
        r"\d*x",  # the guide's rewrites
        r"[a-z]+\s+[a-z]+$",
        r"cough\w*(?:[^\w.!?\n]+\w+){0,3}?[^\w.!?\n]+blood",  # a negated class is exact
    ],
)
def test_the_sequence_check_allows_quantifiers_something_separates(pattern: str) -> None:
    assert regex_safety.static_problems(pattern, re.IGNORECASE) == []


def test_the_reference_packs_patterns_pass_the_static_check() -> None:
    """The guide points at them (the inbox router's red-flag screen) as the example."""
    from mendari.core.pack import authored_patterns
    from mendari.core.registry import SearchPath, discover

    packs = discover([SearchPath(ROOT / "packs", "maintained")]).packs
    found = [
        (pack.id, where)
        for pack in packs
        for where, text, flags in authored_patterns(pack)
        if regex_safety.static_problems(text, flags)
    ]
    assert len(packs) == 5 and found == []


def test_padding_a_pattern_with_pieces_does_not_hide_its_slow_part_from_the_probe() -> None:
    """Before, the probe kept the first 8 pieces and the first 4 leading texts: eight literal
    alternatives pushed the default '0' out, and a slow branch after the fourth was never
    reached."""
    padded = r"(?:ab|cd|ef|gh|ij|kl|mn|op)\d*\d*\d*\d*x"
    atoms, prefixes = regex_safety.pump_atoms(padded, re.IGNORECASE)
    assert "0" in atoms and prefixes[:8] == ["ab", "cd", "ef", "gh", "ij", "kl", "mn", "op"]
    fifth = r"ab\d|cd\d|ef\d|gh\d|ij\d*\d*\d*x"
    assert "ij" in regex_safety.pump_atoms(fifth, re.IGNORECASE)[1]
    jobs = [regex_safety.Job(p, p, re.IGNORECASE) for p in (padded, fifth)]
    assert [job.where for job in regex_safety.probe(jobs)] == [padded, fifth]


def test_the_probe_reaches_a_slow_part_behind_a_character_class() -> None:
    """Before, inputs led up to a pumped part with literal texts only: `q[0-9]z` was never
    matched, so the slow part after it was never reached (this one takes minutes on its text;
    the static check leaves bounded repeats to the probe)."""
    behind = r"q[0-9]z0{0,3000}0{0,3000}0{0,3000}x"
    assert regex_safety.static_problems(behind, re.IGNORECASE) == []
    assert "q0z" in regex_safety.pump_atoms(behind)[1]
    jobs = [regex_safety.Job(behind, behind, 0), regex_safety.Job("fast", r"q[0-9]z0+x", 0)]
    assert [job.where for job in regex_safety.probe(jobs)] == [behind]


def test_a_pattern_too_big_to_probe_is_refused(new_pack: Path) -> None:
    rules = new_pack / "logic" / "rules.yaml"
    many = "|".join(f"(?:q{i})+" for i in range(100))  # 101 x 103 x 2 inputs
    rules.write_text(rules.read_text().replace('"refill|renew|', f'"{many}|refill|renew|'))
    (problem,) = messages(run(new_pack, base=None), "patterns")
    assert "too many pieces for the probe" in problem


# -- hygiene -------------------------------------------------------------------------------------


def test_size_file_count_binaries_links_and_evals(new_pack: Path) -> None:
    data = new_pack / "data"
    (data / "big.txt").write_text("x" * (2 * 1024 * 1024 + 1))
    for i in range(200):
        (data / f"n{i}.txt").write_text("note\n")
    (data / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0")
    (data / "link.jsonl").symlink_to(data / "examples.jsonl")
    (new_pack / "evals" / "leaderboard.json").write_text("{}\n")
    report = run(new_pack, base=None)
    assert has(report, "hygiene", "at most 200")
    assert has(report, "hygiene", "a community pack is at most 2 MB")
    assert has(report, "hygiene", "remove data/photo.png")
    assert has(report, "hygiene", "replace the symbolic links data/link.jsonl")
    assert has(report, "hygiene", "remove evals/leaderboard.json")


def test_the_changelog_needs_an_entry_for_the_version(new_pack: Path) -> None:
    (new_pack / "CHANGELOG.md").write_text("# Changelog\n")
    assert has(run(new_pack, base=None), "hygiene", "needs an entry for version 0.1.0")
    assert has_entry("## [0.2.0] - 2026-10-04\n", "0.2.0")
    assert has_entry("intro\n### v1.0.0\n", "1.0.0")
    assert not has_entry("## 0.2.01\n", "0.2.0") and not has_entry("0.2.0\n", "0.2.0")


def test_a_change_without_a_version_bump_fails(new_pack: Path, repo: Path) -> None:
    git(repo, "branch", "-f", "main")
    rules = new_pack / "logic" / "rules.yaml"
    rules.write_text(rules.read_text().replace("ran out", "ran out|all out"))
    regenerate(new_pack)
    assert has(run(new_pack), "hygiene", "the base has 0.1.0, this change 0.1.0")
    edit_manifest(new_pack, lambda m: m.update(version="0.1.1"))
    regenerate(new_pack)
    assert has(run(new_pack), "hygiene", "needs an entry for version 0.1.1")


def test_evaluating_again_needs_no_bump(new_pack: Path, repo: Path) -> None:
    git(repo, "branch", "-f", "main")
    regenerate(new_pack)  # a new benchmark id and time: outside the digest, as is the card
    report = run(new_pack)
    assert report.passed, report.text()


# -- PHI warnings --------------------------------------------------------------------------------


def test_phi_lookalikes_are_warnings_that_never_quote_the_text(new_pack: Path) -> None:
    row = {
        "id": "phi",
        "input": {"message": {"body": "SSN 123-45-6789, call 617-555-2368"}},
        "labels": {"request": "other", "urgent_symptoms": "no"},
    }
    _write_examples(new_pack, [*_examples(new_pack), row])
    regenerate(new_pack)
    report = run(new_pack, base=None)
    assert report.passed, report.text()  # warnings only
    kinds = " ".join(f.message for f in report.warnings)
    assert "data/examples.jsonl line 16 looks like a Social Security number" in kinds
    assert "a phone number" in kinds
    assert "123-45-6789" not in kinds and "617-555-2368" not in kinds


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("SSN 123-45-6789", "a Social Security number"),
        ("Call me at (617) 555-2368", "a phone number"),
        ("MRN: 00482913", "a medical record number"),
        ("Jane Smith, DOB 03/14/1962", "a date of birth next to a name"),
        ("born 1962-03-14", "a date of birth next to a name"),
        ("write to jane.smith@gmail.com", "an email address"),
    ],
)
def test_phi_heuristics_find(text: str, kind: str) -> None:
    assert [hit.kind.split(" (")[0] for hit in phi_scan.scan(text)] == [kind]


@pytest.mark.parametrize(
    "text",
    [
        "Call 617-555-0123 any time",  # the fictional range
        "write to front.desk@example.org or a@clinic.example",
        "My appointment is on 2026-10-04 at 9",
        "I need 30 tablets of lisinopril 10 mg",
    ],
)
def test_phi_heuristics_leave_synthetic_text_alone(text: str) -> None:
    assert phi_scan.scan(text) == []


# -- the command line and CI ---------------------------------------------------------------------


def test_the_command_exits_one_on_an_error(
    new_pack: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["community-check", str(new_pack), "--base", "main"]) == 0
    assert "passed: ready for review" in capsys.readouterr().out
    edit_manifest(new_pack, lambda m: m.update(license="MIT"))
    assert main(["community-check", str(new_pack), "--markdown"]) == 1
    out = capsys.readouterr().out
    assert "community-check failed" in out and "- **Error** (manifest):" in out
    assert main(["community-check", str(new_pack), "--base=--not-a-commit"]) == 1
    assert "is not a commit" in capsys.readouterr().out


def _script() -> ModuleType:
    path = ROOT / "scripts" / "community_check.py"
    spec = importlib.util.spec_from_file_location("community_check", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pull_request(repo: Path, scaffold: Path) -> Iterator[Path]:
    """A branch off `main` adding the pack, as a pull request's head."""
    git(repo, "checkout", "-q", "-b", "contribution")
    shutil.copytree(scaffold, repo / "community-packs" / NAME)
    write_maintainers(repo, [{"id": PACK_ID, "maintainers": ["octocat"]}])
    commit(repo, "add the pack")
    yield repo


def test_the_ci_script_checks_the_pull_request(
    pull_request: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _script()
    summary = tmp_path / "summary.md"
    assert script.run(pull_request, "main", owner=False, summary=summary) == 0
    assert "`community/refill-router`: community-check passed" in summary.read_text()
    (pull_request / "src.py").write_text("x = 1\n")
    commit(pull_request)
    assert script.run(pull_request, "main", owner=False) == 1
    assert "open a separate pull request for: src.py" in capsys.readouterr().out
    assert script.run(pull_request, "main", owner=True) == 0  # the owner may


def test_the_ci_script_passes_a_pull_request_without_community_packs(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "README.md").write_text("Changed.\n")
    commit(repo)
    summary = tmp_path / "summary.md"
    assert _script().run(repo, "HEAD~1", owner=False, summary=summary) == 0
    assert "no community pack changes" in capsys.readouterr().out
    assert summary.read_text() == "No community pack changes.\n"


def test_a_removed_pack_needs_its_tombstone(
    pull_request: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(pull_request, "branch", "-f", "main")
    shutil.rmtree(pull_request / "community-packs" / NAME)
    commit(pull_request, "remove the pack")
    script = _script()
    assert script.run(pull_request, "main", owner=True) == 1
    assert "as a tombstone" in capsys.readouterr().out
    write_maintainers(
        pull_request,
        [
            {
                "id": PACK_ID,
                "maintainers": ["octocat"],
                "removed": "2026-10-04",
                "reason": "unmaintained",
            }
        ],
    )
    commit(pull_request)
    assert script.run(pull_request, "main", owner=True) == 0
    # Only the owner removes a pack: before, anyone's pull request could, with a tombstone.
    capsys.readouterr()
    assert script.run(pull_request, "main", owner=False) == 1
    assert "only the repository's owner removes a community pack" in capsys.readouterr().out


def test_the_owner_is_the_pull_requests_author_from_this_repository() -> None:
    is_owner = _script().is_owner
    assert is_owner("Junebugg1214", "junebugg1214", "Junebugg1214/mendari", "Junebugg1214/mendari")
    assert not is_owner("attacker", "Junebugg1214", "attacker/mendari", "Junebugg1214/mendari")
    # The owner's name on a fork's branch (anyone may push there) is not the owner's change.
    assert not is_owner("Junebugg1214", "Junebugg1214", "attacker/mendari", "Junebugg1214/mendari")
    assert not is_owner("", "Junebugg1214", "", "Junebugg1214/mendari")


def test_a_pack_added_as_a_git_submodule_fails(
    pull_request: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before, a gitlink at community-packs/<name> (one slash) was 'no community pack
    changes': its files are another repository's, which nothing checks."""
    git(pull_request, "branch", "-f", "main")
    head = git(pull_request, "rev-parse", "HEAD").strip()
    (pull_request / ".gitmodules").write_text(
        '[submodule "evil"]\n\tpath = community-packs/evil\n\turl = https://example.org/evil\n'
    )
    git(pull_request, "add", ".gitmodules")
    git(pull_request, "update-index", "--add", "--cacheinfo", f"160000,{head},community-packs/evil")
    git(pull_request, "commit", "-q", "-m", "a submodule")
    capsys.readouterr()
    for owner in (False, True):
        assert _script().run(pull_request, "main", owner=owner) == 1
        out = capsys.readouterr().out
        assert "holds a git submodule (community-packs/evil)" in out
        assert "no community pack changes" not in out
    assert pr_scope_problems([".gitmodules", "community-packs/evil"], owner=False)


def test_a_submodule_inside_a_pack_fails(new_pack: Path, repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD").strip()
    git(
        repo, "update-index", "--add", "--cacheinfo", f"160000,{head},community-packs/{NAME}/vendor"
    )
    assert has(run(new_pack, base=None), "hygiene", f"community-packs/{NAME}/vendor")
