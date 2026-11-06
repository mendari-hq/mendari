# SPDX-License-Identifier: Apache-2.0
"""The results store (LP-17f, `mendari.evals.results_store`, `scripts/check_scoreboard.py`;
ADR-0013 and the grading method's sections 11, 12 and 14): a result is stored once, by its body's
SHA-256; nothing stored changes but a valid signature added to an unsigned result; `supersedes`
names an earlier result of the same pack, set, arm and configuration; every committed result is
what its inputs give, and every local ledger on its pack's current digest re-runs to the same
bytes, while one on an older digest is kept and not re-run; only local configurations of the
packs Mendari maintains, on their public scoring sets, are run. Every key here is made at run
time; every set is synthetic."""

from __future__ import annotations

import datetime
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mendari import Pack, load_pack
from mendari.cli import main
from mendari.core import clock
from mendari.core.checkpoint import key_id
from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals import arms, results_store, runs, scoreboard, signing
from mendari.evals.harness import resolve_dataset
from mendari.evals.ledger import canonical
from mendari.evals.results_store import Store
from tests.conftest import calm_jev
from tests.test_scoreboard import build, edited, run_for
from tests.test_scoreboard_signing import keys_file

ROOT = Path(__file__).resolve().parents[1]
INBOX = "mendari/patient-inbox-router"
DATE = datetime.date(2026, 10, 18)
SET = "red_flags_holdout"  # 30 cases: quick to run


@pytest.fixture(scope="module")
def inbox() -> Pack:
    return resolve_pack(INBOX)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    root = tmp_path / "scoreboard"
    root.mkdir()
    (root / "keys.json").write_text(json.dumps(keys_file()), encoding="utf-8")
    return Store(root)


def naive_bayes(pack: Pack) -> arms.Configuration:
    return results_store.configuration_of(pack, ["router=naive_bayes"])


def stored(store: Store, pack: Pack, *, date: datetime.date = DATE, again: bool = False) -> str:
    outcome = results_store.record_run(store, pack, SET, naive_bayes(pack), date=date, again=again)
    assert outcome.action == results_store.STORED
    return outcome.name


def files(store: Store) -> dict[str, bytes]:
    """The store's result files by name, as a base commit would hold them."""
    return {path.stem: path.read_bytes() for path in (store.root / "results").glob("*.json")}


def result_file(store: Store, name: str) -> Path:
    return store.root / "results" / f"{name}.json"


def problems(store: Store, base: dict[str, bytes], **kwargs: Any) -> list[str]:
    return results_store.check(store, base=base, **kwargs).problems


def rewritten(store: Store, name: str, edit: Any) -> dict[str, Any]:
    """The result `name` with `edit` applied to it, written back over its own file."""
    path = result_file(store, name)
    found = json.loads(path.read_text(encoding="utf-8"))
    edit(found)
    path.write_text(signing.dump(found), encoding="utf-8")
    return found


def keyed(store: Store, *keys: Ed25519PrivateKey) -> None:
    (store.root / "keys.json").write_text(json.dumps(keys_file(*keys)), encoding="utf-8")


def test_a_result_is_stored_once_by_its_body_hash(store: Store, inbox: Pack) -> None:
    name = stored(store, inbox)
    path = result_file(store, name)
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["body_sha256"] == f"sha256:{name}" == scoreboard.body_sha256(result)
    assert path.read_text(encoding="utf-8") == signing.dump(result)
    assert result["supersedes"] is None and "signature" not in result
    # Its inputs are stored by their own hashes, beside it.
    ledger = store.ledger(result["inputs"]["ledger"])
    assert (store.root / "ledgers" / ledger.file_name).read_bytes() == ledger.dumps()
    run = store.run(result["inputs"]["run"]["sha256"])
    assert run["ledger"] == ledger.id and len(run["cases"]) == 30
    # Never over another file, and never a second current result of one configuration and set.
    with pytest.raises(MendariError, match="stored already: the store never overwrites"):
        store.add(result)
    before = files(store)
    again = results_store.measure(inbox, SET, naive_bayes(inbox), date=DATE)
    with pytest.raises(MendariError, match=f"{name} is the current result of .*supersedes it"):
        store.add(again.entry)
    assert files(store) == before
    # A configuration still current is kept, not run again.
    outcome = results_store.record_run(store, inbox, SET, naive_bayes(inbox), date=DATE)
    assert (outcome.action, outcome.name) == (results_store.KEPT, name)
    assert files(store) == before
    assert results_store.check(store, base={}).problems == []


def test_an_edited_or_removed_result_fails_the_check(store: Store, inbox: Pack) -> None:
    name = stored(store, inbox)
    base = files(store)
    assert problems(store, base) == []

    def lower_accuracy(found: dict[str, Any]) -> None:
        found["figures"]["accuracy"]["k"] -= 1

    edited = rewritten(store, name, lower_accuracy)
    found = problems(store, base)
    assert f"results/{name}.json: its body_sha256 is not its body's SHA-256" in found
    assert any("changed: only a valid signature may be added" in line for line in found)
    # Its body_sha256 made to match: its name no longer does.
    edited["body_sha256"] = scoreboard.body_sha256(edited)
    result_file(store, name).write_text(signing.dump(edited), encoding="utf-8")
    new_name = edited["body_sha256"].removeprefix("sha256:")
    found = problems(store, base)
    assert any(f"results/{name}.json: its name is not its body's SHA-256" in p for p in found)
    # Stored again under its new name, and the old one taken out: removed, and made up.
    result_file(store, name).unlink()
    result_file(store, new_name).write_text(signing.dump(edited), encoding="utf-8")
    found = problems(store, base)
    assert f"results/{name}.json: removed: the store is append-only" in found[0]
    assert any("it is not what its inputs give" in line for line in found)
    # Its inputs are named by their hashes too: an edited or missing ledger is found.
    result_file(store, new_name).unlink()
    result_file(store, name).write_bytes(base[name])
    assert problems(store, base) == []
    ledger = next((store.root / "ledgers").iterdir())
    ledger.write_bytes(ledger.read_bytes().replace(b'"o":"', b'"o":"x', 1))
    found = problems(store, base)
    assert any("its name is not its id" in line for line in found)
    ledger.unlink()
    assert any("is not in the store (ledgers/" in line for line in problems(store, base))
    # A file the store gives no such name is named, and an input is named by a hash, never a
    # path: a result naming one is refused before anything is read.
    (store.root / "results" / "notes.txt").write_text("x")
    assert "results/notes.txt: not a name the store gives a file" in problems(store, base)
    outside = json.loads(base[name])
    outside["inputs"]["ledger"] = "sha256:../../keys"
    outside["body_sha256"] = scoreboard.body_sha256(outside)
    with pytest.raises(MendariError, match="named by sha256:<64 hex>"):
        results_store.held(outside)


def test_only_adding_a_valid_signature_may_change_a_result(store: Store, inbox: Pack) -> None:
    name = stored(store, inbox)
    base = files(store)
    key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    unsigned = json.loads(base[name])
    signed = signing.sign(unsigned, key)
    assert results_store.name_of(signed) == name  # the body and its name stay as they were
    result_file(store, name).write_text(signing.dump(signed), encoding="utf-8")
    # A key the keys file does not list: refused.
    assert any("a signature was added that does not verify" in p for p in problems(store, base))
    keyed(store, key)
    assert problems(store, base) == []
    found = results_store.check(store, base=base)
    assert (found.results, found.current, found.signed) == (1, 1, 1)
    # A publisher's or a registry's key this machine pinned: refused, even listed.
    pinned = problems(store, base, publisher_ids={key_id(key.public_key())})
    assert any("a publisher's or a registry's key" in line for line in pinned)
    # Once signed, nothing changes: not another signature, not its removal.
    signed_base = files(store)
    keyed(store, key, other)
    result_file(store, name).write_text(signing.dump(signing.sign(unsigned, other)))
    assert any("changed after it was signed" in line for line in problems(store, signed_base))
    result_file(store, name).write_bytes(base[name])
    assert any("changed after it was signed" in line for line in problems(store, signed_base))
    # Nor a signature beside any other change.
    result_file(store, name).write_text(
        signing.dump({**signed, "note": "an extra field"}), encoding="utf-8"
    )
    assert any("only a valid signature may be added" in p for p in problems(store, base))
    # A current result whose key was taken out of the keys file does not verify.
    result_file(store, name).write_text(signing.dump(signed), encoding="utf-8")
    keyed(store, other)
    assert any("its signature does not verify" in line for line in problems(store, signed_base))


def test_supersedes_names_an_earlier_result_of_the_same_entry(
    store: Store, inbox: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Runs on later days, past the first result's stale date: the clock is moved on with them.
    monkeypatch.setattr(clock, "today", lambda: DATE + datetime.timedelta(days=60))
    first = stored(store, inbox)
    base = files(store)
    later = results_store.record_run(
        store, inbox, SET, naive_bayes(inbox), date=DATE + datetime.timedelta(days=1), again=True
    )
    assert (later.action, later.supersedes) == (results_store.STORED, first)
    held = store.results()
    assert held[later.name].result["supersedes"] == f"sha256:{first}"
    assert list(results_store.heads(held)) == [later.name]
    assert results_store.superseded(held) == {first: later.name}
    assert problems(store, base) == []
    # A run past its stale date is run again without being asked, and supersedes the current.
    stale_day = datetime.date.fromisoformat(scoreboard.stale_after(held[later.name].result))
    third = results_store.record_run(
        store, inbox, SET, naive_bayes(inbox), date=stale_day + datetime.timedelta(days=1)
    )
    assert (third.action, third.supersedes) == (results_store.STORED, later.name)
    base = files(store)

    def entry(supersedes: str | None, *, on: str = SET, date: datetime.date = DATE) -> Any:
        return results_store.measure(
            inbox, on, naive_bayes(inbox), date=date, supersedes=supersedes
        ).entry

    with pytest.raises(MendariError, match="which is not stored"):
        store.add(entry("0" * 64))
    with pytest.raises(MendariError, match="supersedes names a result by its body's SHA-256"):
        entry("not-a-hash")
    with pytest.raises(MendariError, match="a result of another pack, set, arm or configuration"):
        store.add(entry(third.name, on="red_flags"))
    with pytest.raises(MendariError, match="measured after it"):
        store.add(entry(third.name, date=DATE))
    with pytest.raises(MendariError, match=f"which {later.name} supersedes already"):
        store.add(entry(first, date=stale_day + datetime.timedelta(days=2)))
    assert files(store) == base
    # Written past `add`: a result superseded twice, and two current results of one entry.
    twice = entry(first, date=stale_day + datetime.timedelta(days=2))
    result_file(store, results_store.name_of(twice)).write_text(signing.dump(twice))
    other = entry(None, date=stale_day + datetime.timedelta(days=2))
    result_file(store, results_store.name_of(other)).write_text(signing.dump(other))
    found = problems(store, base)
    assert any(f"it supersedes {first}, which " in p and "supersedes already" in p for p in found)
    assert any("current results of mendari/patient-inbox-router" in line for line in found)
    # Every superseded result stays, and the page lists it with what superseded it.
    for path in [result_file(store, results_store.name_of(r)) for r in (twice, other)]:
        path.unlink()
    assert problems(store, base) == []
    page = _page_script().page(store)
    assert f"| `{first[:16]}` | `{later.name[:16]}` |" in page
    assert f"| `{later.name[:16]}` | `{third.name[:16]}` |" in page
    assert page.count(f"## {INBOX}: `{SET}`") == 1


def test_a_superseded_result_keeps_the_signature_it_had(store: Store, inbox: Pack) -> None:
    """A key that leaked is taken out of the keys file: what it signed no longer verifies. Its
    results are run again, and the new ones supersede them; the old ones stay, as they were."""
    leaked = Ed25519PrivateKey.generate()
    keyed(store, leaked)
    first = stored(store, inbox)
    path = result_file(store, first)
    path.write_text(signing.dump(signing.sign(json.loads(path.read_bytes()), leaked)))
    base = files(store)
    keyed(store)  # the leaked key is taken out
    assert any("its signature does not verify" in line for line in problems(store, base))
    again = results_store.record_run(store, inbox, SET, naive_bayes(inbox), date=DATE, again=True)
    assert again.supersedes == first
    assert problems(store, base) == []


def test_each_committed_result_is_what_its_inputs_give() -> None:
    """The repository's store: every result recomputes from its inputs, byte for byte, and each
    local ledger on its pack's current digest re-runs to the same bytes (or the result is kept,
    saying why: its pack, set or method changed since)."""
    store = Store.at(ROOT / "scoreboard")
    base = files(store)
    found = results_store.check(store, base=base)
    assert found.problems == []
    assert found.results == len(base) >= 18
    not_recomputed = [line for line in found.kept if "not recomputed" in line]
    assert found.recomputed + len(not_recomputed) == found.results
    assert found.rerun + len(found.kept) >= found.results
    for held in store.results().values():
        assert held.result["scope"]["set"]["kind"] == "public"
        assert "signature" in held.result or results_store.name_of(held.result) == held.name


def test_a_ledger_on_an_older_digest_is_kept_and_not_rerun(
    store: Store, inbox: Pack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = stored(store, inbox)
    base = files(store)
    # The pack changes after the result was stored: another digest, the same sets.
    copy = tmp_path / "inbox-changed"
    shutil.copytree(inbox.root, copy)
    manifest = (copy / "pack.yaml").read_text(encoding="utf-8")
    (copy / "pack.yaml").write_text(f"{manifest}# Changed after its result was stored.\n")
    changed = load_pack(copy)
    assert changed.digest != inbox.digest

    def refused(*_: Any) -> Any:
        raise AssertionError("a ledger on an older digest is never re-run")

    monkeypatch.setattr(results_store, "rerun", refused)
    found = results_store.check(store, base=base, packs=lambda _: changed)
    assert found.problems == []
    assert (found.recomputed, found.rerun) == (1, 0)
    assert found.kept == [f"{name}: its ledger not re-run (stale: pack changed)"]
    # Recomputed from its own pack scope, it is still what its inputs give: an edit is found.
    rewritten(store, name, lambda result: result["figures"]["accuracy"].update(k=0))
    assert problems(store, base, packs=lambda _: changed) != []
    result_file(store, name).write_bytes(base[name])
    # A new result must be on the current digest, so its ledger can be re-run.
    found = results_store.check(store, base={}, packs=lambda _: changed)
    assert any("a new result is measured on its pack's current digest" in p for p in found.problems)
    # Shown as stale on the board, whatever the store holds.
    (table,) = scoreboard.board(
        [store.results()[name].result], {INBOX: scoreboard.Current.of(changed)}
    )["tables"]
    assert "stale: pack changed" in table["entries"][0]["marks"]


def test_a_ledger_made_up_to_fit_its_result_is_found_by_a_new_run(
    store: Store, inbox: Pack
) -> None:
    """A ledger edited by hand, stored under its own id with a run record and a result built
    from it: every name and every figure agrees, and only running the configuration again
    shows it is not what was decided."""
    measured = results_store.measure(inbox, SET, naive_bayes(inbox), date=DATE)

    def flip(cases: list[dict[str, Any]]) -> None:
        cases[0]["o"] = "HUMAN_REVIEW" if cases[0]["o"] == "AUTO" else "AUTO"

    made_up = edited(measured.ledger, flip)
    run = run_for(made_up, measured.run)
    entry = scoreboard.entry(
        made_up,
        resolve_dataset(inbox, SET),
        run,
        pack=scoreboard.PackScope.of(inbox),
        arm=naive_bayes(inbox).arm,
        configuration=naive_bayes(inbox),
        prices=None,
        disclosures=None,
    )
    store.put_ledger(made_up)
    store.put_run(run)
    found = results_store.held(entry)
    assert canonical(results_store.recompute(found, store, inbox)) == canonical(entry)
    refusal = f"results/{found.name}.json ({found.shown}): its ledger is not what a new run gives"
    with pytest.raises(MendariError, match=re.escape(refusal)):
        store.add(entry)
    assert not (store.root / "results").exists()
    # Written past `add`: the check re-runs it, new against the base or stored in it.
    path = result_file(store, found.name)
    path.parent.mkdir()
    path.write_text(signing.dump(entry), encoding="utf-8")
    for base in ({}, files(store)):
        assert problems(store, base) == [f"{refusal} (re-run, a byte differs)"]


def test_a_remote_result_of_one_replicate_is_never_stored_here(
    store: Store, inbox: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote configuration's ledger could not be re-run without a call off the machine, so
    its result is stored only with 3 to 5 replicates and its pack's declaration of it (LP-17h,
    tests/test_scoreboard_runs.py): `add` refuses one of one replicate, and so does the check
    while it is new against the base. Recomputed, its disclosure is the store's disclosures
    file's (pending while there is none), never one the result brings."""
    calm_jev(monkeypatch)
    built = build(INBOX, SET, {"safety_screen": "jev"})
    store.put_ledger(built.ledger)
    store.put_run(built.run)
    made_up = built.entry(disclosures=runs.Disclosures("", {"jev": "Made up by whoever ran it."}))
    found = results_store.held(made_up)
    assert not found.local
    pending = {"status": scoreboard.DISCLOSURE_PENDING, "statements": {"jev": None}}
    assert results_store.recompute(found, store, inbox)["scope"]["disclosure"] == pending
    stated = {"jev": "No commercial relationship."}
    disclosures = {"format": runs.DISCLOSURES_FORMAT, "date": "2026-10-18", "statements": stated}
    (store.root / "disclosures.json").write_text(json.dumps(disclosures), encoding="utf-8")
    assert results_store.recompute(found, store, inbox)["scope"]["disclosure"] == {
        "status": "stated",
        "statements": stated,
    }
    honest = built.entry(disclosures=store.disclosures())
    held = results_store.held(honest)
    assert canonical(results_store.recompute(held, store, inbox)) == canonical(honest)
    # Neither is stored, whatever its disclosure.
    for result in (made_up, honest):
        with pytest.raises(MendariError, match="holds 3 to 5 replicates, not one"):
            store.add(result)
    assert not (store.root / "results").exists()
    # Written past `add`: refused while it is new against the base. One the base holds stays,
    # recomputed with the statements it holds, its ledger never re-run.
    path = result_file(store, held.name)
    path.parent.mkdir()
    path.write_text(signing.dump(honest), encoding="utf-8")
    assert any("holds 3 to 5 replicates, not one" in line for line in problems(store, {}))
    found_now = results_store.check(store, base=files(store))
    assert (found_now.problems, found_now.recomputed, found_now.rerun) == ([], 1, 0)
    assert found_now.kept == [f"{held.name}: its ledger not re-run (a remote configuration)"]
    # A disclosures file that is not one is named.
    (store.root / "disclosures.json").write_text("{}", encoding="utf-8")
    assert any(line.startswith("disclosures.json") for line in problems(store, files(store)))


def test_a_result_measured_after_today_is_refused(store: Store, inbox: Pack) -> None:
    """Dated after today, a result would show a stale date far off, and no honest later run
    could supersede it (it would be measured before it): it is refused while it is new."""
    future = datetime.date(2099, 1, 1)
    # On a machine whose clock is wrong: refused, and nothing left behind.
    with pytest.raises(MendariError, match="measured on 2099-01-01, after today"):
        results_store.record_run(store, inbox, SET, naive_bayes(inbox), date=future)
    assert [path.name for path in store.root.rglob("*") if path.is_file()] == ["keys.json"]
    measured = results_store.measure(inbox, SET, naive_bayes(inbox), date=future)
    store.put_ledger(measured.ledger)
    store.put_run(measured.run)
    with pytest.raises(MendariError, match=r"measured on 2099-01-01, after today \(2026-10-18\)"):
        store.add(measured.entry, today=DATE)
    assert store.add(measured.entry, today=future) == measured.name
    assert any("after today (2026-10-18)" in line for line in problems(store, {}, today=DATE))
    assert problems(store, files(store), today=DATE) == []  # stored before: it stays
    # A day of the calendar, never only its shape.
    no_such_day = json.loads(signing.dump(measured.entry))
    no_such_day["scope"]["measured_on"] = "2026-02-30"
    no_such_day["body_sha256"] = scoreboard.body_sha256(no_such_day)
    with pytest.raises(MendariError, match="names no day it was measured"):
        results_store.held(no_such_day)


def test_the_store_is_run_and_checked_against_the_repositorys_packs(
    store: Store,
    inbox: Pack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A newer copy of a reference pack on `MENDARI_PACKS_PATH` (or pulled from a registry)
    comes first on the search path; the store never reads it."""
    name = stored(store, inbox)
    copy = tmp_path / "elsewhere" / "patient-inbox-router"
    shutil.copytree(inbox.root, copy)
    manifest = (copy / "pack.yaml").read_text(encoding="utf-8")
    newer = re.sub(r"^version: .*$", "version: 99.0.0", manifest, count=1, flags=re.M)
    assert newer != manifest
    (copy / "pack.yaml").write_text(newer, encoding="utf-8")
    monkeypatch.setenv("MENDARI_PACKS_PATH", str(copy.parent))
    assert resolve_pack(INBOX).root == copy.resolve()
    assert results_store.maintained_pack(INBOX).digest == inbox.digest
    found = results_store.check(store, base={})
    assert (found.problems, found.rerun) == ([], 1)
    argv = ["scoreboard", "run", INBOX, "--dataset", SET, "--config", "router=naive_bayes"]
    assert main([*argv, "--store", str(store.root)]) == 0
    assert capsys.readouterr().out.startswith(f"kept: {INBOX} {SET} ")
    assert list(store.results()) == [name]
    with pytest.raises(MendariError, match="not a pack Mendari maintains"):
        results_store.maintained_pack("mendari/no-such-pack")
    with pytest.raises(MendariError, match="not a pack Mendari maintains"):
        results_store.maintained_pack(str(copy))  # a directory is never a pack's id


def test_running_a_remote_configuration_without_replicates_is_refused(
    store: Store,
    inbox: Pack,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside CI, a remote configuration runs 3 to 5 replicates as its arm's declared one
    (LP-17h, tests/test_scoreboard_runs.py); with fewer it is refused before anything runs."""
    for name in signing.CI_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    jev = results_store.configuration_of(inbox, ["router=jev"])
    with pytest.raises(MendariError, match="calls jev off this machine: it runs 3 to 5"):
        results_store.measure(inbox, SET, jev, date=DATE)
    cql = results_store.configuration_of(
        resolve_pack("mendari/clinical-trial-prescreener"), ["structured=cql"]
    )
    assert cql.arm == arms.DETERMINISTIC and not cql.local
    with pytest.raises(MendariError, match="calls cql off this machine: it runs 3 to 5"):
        results_store.refuse_unmeasurable(
            resolve_pack("mendari/clinical-trial-prescreener"), "test", cql
        )
    # A fitting set, a file (so any hidden set) and a pack Mendari does not maintain: refused.
    with pytest.raises(MendariError, match="train fits mendari/patient-inbox-router's models"):
        results_store.refuse_unmeasurable(inbox, "train", naive_bayes(inbox))
    with pytest.raises(MendariError, match="never a file, and no hidden set is run here"):
        results_store.refuse_unmeasurable(inbox, str(ROOT / "datasets" / "x.jsonl"), jev)
    shutil.copytree(inbox.root, tmp_path / "copy")
    with pytest.raises(MendariError, match="not a pack Mendari maintains"):
        results_store.refuse_unmeasurable(load_pack(tmp_path / "copy"), SET, naive_bayes(inbox))
    # From the command line: refused before anything runs, and nothing written.
    argv = ["scoreboard", "run", INBOX, "--dataset", SET, "--store", str(store.root)]
    assert main([*argv, "--every-local", "--dataset", "train"]) == 1
    assert main([*argv, "--config", "router=jev"]) == 1
    assert main([*argv, "--config", "routr=rules"]) == 1
    errors = capsys.readouterr().err.splitlines()
    assert errors[0].startswith("error: train fits")
    assert errors[1] == (
        "error: router=jev,safety_screen=rules calls jev off this machine: it runs 3 to 5 "
        "replicates (--replicates N), not none"
    )
    assert errors[2].startswith("error: mendari/patient-inbox-router has no slot 'routr'")
    assert sorted(path.name for path in store.root.iterdir()) == ["keys.json"]


def test_the_command_line_runs_and_adds_results(
    store: Store, inbox: Pack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["scoreboard", "run", INBOX, "--dataset", SET, "--every-local"]
    assert main([*argv, "--store", str(store.root)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split(" as ")[0] for line in out[:2]] == [
        f"stored: {INBOX} {SET} router=naive_bayes,safety_screen=rules",
        f"stored: {INBOX} {SET} router=rules,safety_screen=rules",
    ]
    assert main([*argv, "--store", str(store.root)]) == 0
    assert all(line.startswith("kept: ") for line in capsys.readouterr().out.splitlines())
    # A result made elsewhere is added once its inputs are stored; never twice.
    elsewhere = Store(tmp_path / "elsewhere")
    shutil.copytree(store.root, elsewhere.root)
    name = sorted(store.results())[0]
    path = result_file(store, name)
    for kept in (elsewhere.root / "results").iterdir():
        kept.unlink()
    assert main(["scoreboard", "add", str(path), "--store", str(elsewhere.root)]) == 0
    assert capsys.readouterr().out == f"stored: results/{name}.json\n"
    assert main(["scoreboard", "add", str(path), "--store", str(elsewhere.root)]) == 1
    assert "stored already" in capsys.readouterr().err
    # --supersedes is part of the body: never set on a signed result.
    signed = tmp_path / "signed.json"
    found = json.loads(path.read_text())
    signed.write_text(signing.dump(signing.sign(found, Ed25519PrivateKey.generate())))
    assert main(["scoreboard", "add", str(signed), "--supersedes", name, "--store", "x"]) == 1
    assert main(["scoreboard", "add", str(signed), "--supersedes", name]) == 1
    errors = capsys.readouterr().err
    assert "no results store at x" in errors and "is signed: its supersedes link" in errors
    assert canonical(json.loads(path.read_text())) == canonical(found)


def test_the_reference_pack_job_checks_the_store_and_the_page() -> None:
    """The checks are steps of CI's required `reference-pack` job, against the pull request's
    base (or the commit a push was made on), with the history fetched to reach it."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    job = workflow["jobs"]["reference-pack"]
    assert job["steps"][0]["with"] == {"persist-credentials": False, "fetch-depth": 0}
    runs = {step.get("run"): step for step in job["steps"]}
    check = runs['uv run python scripts/check_scoreboard.py --base "$BASE"']
    assert check["env"] == {
        "BASE": "${{ github.event.pull_request.base.sha || github.event.before }}"
    }
    assert "uv run python scripts/scoreboard_page.py --check" in runs
    assert not {"if", "continue-on-error"} & {key for step in job["steps"] for key in step}
    # The build writes the page again (a pack change makes its results stale), so the page is
    # checked before it, as committed.
    order = [step.get("run") for step in job["steps"]]
    page = order.index("uv run python scripts/scoreboard_page.py --check")
    assert page < order.index("./scripts/build-reference-pack.sh")
    build = (ROOT / "scripts" / "build-reference-pack.sh").read_text(encoding="utf-8")
    assert build.rstrip().splitlines()[-2] == "uv run python scripts/scoreboard_page.py"


def _page_script() -> Any:
    import importlib.util
    import sys

    path = ROOT / "scripts" / "scoreboard_page.py"
    spec = importlib.util.spec_from_file_location("scoreboard_page_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
