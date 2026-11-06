# SPDX-License-Identifier: Apache-2.0
"""The scoreboard end to end (LP-17m, ADR-0013): a result from its run to a signature anyone can
check, through the command line (`main`, as `mendari` runs it, from the repository's root with
the commands docs/operations/scoreboard.md writes) and the hidden-set grading route.

1. `mendari scoreboard run`: a local configuration of the Patient Inbox Router on one of its
   public scoring sets, stored unsigned with its ledger and run record (a runner's store);
2. `mendari scoreboard add`: the result stored in the repository's store beside its inputs,
   which the store's check recomputes byte for byte, re-running its ledger;
3. `mendari scoreboard sign`, with a grading key made here and thrown away, on the key holder's
   machine (not CI): signed in place, its name unchanged, the key never printed; its public key
   listed in `scoreboard/keys.json`;
4. `mendari scoreboard verify`: it verifies with the public key alone, and the store's check
   takes the change against the unsigned base (a valid signature added, nothing else);
5. tampering: one figure changed after signing fails `verify` and the store's check;
6. a hidden set, frozen on the deployment, graded over HTTP with the declared arm
   (`POST /api/evalsets/{set_id}/runs`, LP-17k); its answer stored with `mendari scoreboard add`
   against the pack's declaration, signed and verified as any result;
7. `mendari audit verify --evalsets` passes after the run.

On SQLite and Postgres (`db_target`). Nothing leaves the machine: a socket connection the test
opens to anything but this machine is refused (the Postgres container listens on the loopback
address), and the shell's own systems (a deployment's database, Jev, an LLM, the registry, the
grading key) are removed before anything runs (tests/conftest.py, LIVE_SETTINGS, as
tests/test_isolation.py checks). It takes seconds: a 30-case public set and a six-case hidden
set. Keys are made per test; cases are synthetic or hand-written."""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mendari import Pack
from mendari.cli import main
from mendari.core import clock
from mendari.core.checkpoint import key_id, public_text
from mendari.evals import hidden, method, results_store, signing
from tests.conftest import ROOT
from tests.test_evalset_grading import deployment, started
from tests.test_evalset_scoreboard import DAY, DECLARATION, DECLARED, answered, scored_set
from tests.test_scoreboard_signing import keys_file, seed_of

INBOX = "mendari/patient-inbox-router"
SET = "red_flags_holdout"  # 30 cases: its run takes about a second
LOCAL = "router=naive_bayes"  # with the pack's default safety screen, `rules`
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
CHANGED = "its signature does not hold: it changed after it was signed"


@pytest.fixture
def on_this_machine(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every socket connection refused, and recorded, unless it stays on this machine (a Unix
    socket or the loopback address): what the test tried to reach off it."""
    tried: list[str] = []
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def off_the_machine(sock: socket.socket, address: Any) -> str | None:
        if sock.family == getattr(socket, "AF_UNIX", None):
            return None
        host = str(address[0] if isinstance(address, tuple) else address)
        return None if host in LOOPBACK else host

    def local_connect(sock: socket.socket, address: Any) -> None:
        if (host := off_the_machine(sock, address)) is not None:
            tried.append(host)
            raise AssertionError(f"a connection off the machine, to {host}")
        connect(sock, address)

    def local_connect_ex(sock: socket.socket, address: Any) -> int:
        if (host := off_the_machine(sock, address)) is not None:
            tried.append(host)
            raise AssertionError(f"a connection off the machine, to {host}")
        return connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
    # The positive control: an address off the machine (TEST-NET-1) is refused before any packet.
    with socket.socket() as probe, pytest.raises(AssertionError, match="off the machine"):
        probe.connect(("192.0.2.1", 9))
    assert tried == ["192.0.2.1"]
    tried.clear()
    return tried


def a_store(root: Path) -> results_store.Store:
    """An empty results store at `root`, with the repository's keys file (no key yet)."""
    root.mkdir(parents=True)
    shutil.copy(ROOT / "scoreboard" / "keys.json", root / "keys.json")
    return results_store.Store.at(root)


def files(store: results_store.Store) -> dict[str, bytes]:
    """The store's result files by name, as a base commit would hold them."""
    return {path.stem: path.read_bytes() for path in (store.root / "results").glob("*.json")}


def signed_here(key: Ed25519PrivateKey, path: str, monkeypatch: pytest.MonkeyPatch) -> int:
    """`mendari scoreboard sign path` on the key holder's own machine: not CI (whatever runs this
    test), the key set for that one command and gone after it."""
    for name in signing.CI_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(signing.KEY_ENV, seed_of(key))
    try:
        return main(["scoreboard", "sign", path])
    finally:
        monkeypatch.delenv(signing.KEY_ENV)


def test_a_local_run_to_a_signed_verified_result(
    db_target: str,
    inbox_pack: Pack,
    on_this_machine: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(clock, "today", lambda: DAY)  # every result is measured on DAY
    runner = a_store(tmp_path / "runner" / "scoreboard")
    repository = tmp_path / "repository"
    store = a_store(repository / "scoreboard")
    monkeypatch.chdir(repository)  # the runbook's commands run from the repository's root

    # 1. The run: one local configuration on a public scoring set, stored unsigned.
    argv = ["scoreboard", "run", INBOX, "--dataset", SET, "--config", LOCAL]
    assert main([*argv, "--store", str(runner.root)]) == 0
    (name,) = runner.results()
    assert capsys.readouterr().out.splitlines() == [
        f"stored: {INBOX} {SET} router=naive_bayes,safety_screen=rules as results/{name}.json",
        "then: uv run python scripts/scoreboard_page.py (the page), and commit both",
    ]
    made = runner.root / "results" / f"{name}.json"
    entry = json.loads(made.read_text(encoding="utf-8"))
    assert entry["scope"]["measured_on"] == DAY.isoformat() and "signature" not in entry
    assert entry["scope"]["method"]["sha256"] == method.METHOD_SHA256

    # 2. Added to the repository's store beside its ledger and run record: what they give.
    for kind in (results_store.LEDGERS, results_store.RUNS):
        shutil.copytree(runner.root / kind, store.root / kind)
    assert main(["scoreboard", "add", str(made)]) == 0
    assert capsys.readouterr().out == f"stored: results/{name}.json\n"
    stored = f"scoreboard/results/{name}.json"
    assert Path(stored).read_bytes() == made.read_bytes()
    unsigned = files(store)
    checked = results_store.check(store, base=unsigned)
    assert (checked.problems, checked.signed, checked.recomputed, checked.rerun) == ([], 0, 1, 1)
    assert main(["scoreboard", "verify", stored]) == 1
    assert capsys.readouterr().err == f"error: {stored}: {signing.UNSIGNED}\n"

    # 3. Signed in place with a throwaway key; its public half listed in the keys file.
    key = Ed25519PrivateKey.generate()
    signer, public = key_id(key.public_key()), public_text(key.public_key())
    assert signed_here(key, stored, monkeypatch) == 0
    said = capsys.readouterr().out
    assert said.splitlines() == [
        f"signed {stored} (body {signing.body_sha256(entry)})",
        f"with {signer}, public key {public}: list it once in {signing.KEYS_PATH} with the dates "
        "it is valid for",
    ]
    assert seed_of(key) not in said
    signed = json.loads(Path(stored).read_text(encoding="utf-8"))
    assert {k: v for k, v in signed.items() if k != "signature"} == entry
    assert results_store.name_of(signed) == name  # its name is its body's, which is unchanged
    Path(signing.KEYS_PATH).write_text(
        json.dumps(keys_file(key, valid_from=DAY.isoformat())), encoding="utf-8"
    )

    # 4. Verified with public keys only, and taken by the store's check against the base.
    assert main(["scoreboard", "verify", stored]) == 0
    assert capsys.readouterr().out == (
        f"verified: {stored}, signed with {signer} (valid from {DAY.isoformat()} to no end "
        f"date), under {method.METHOD}\n"
    )
    assert main(["scoreboard", "verify", stored, "--public-key", public]) == 0
    capsys.readouterr()
    checked = results_store.check(store, base=unsigned)
    assert (checked.problems, checked.signed) == ([], 1)

    # 5. Tampering: one figure changed after signing fails both.
    tampered = json.loads(json.dumps(signed))
    tampered["figures"]["accuracy"]["k"] -= 1
    copy = Path("tampered.json")
    copy.write_text(signing.dump(tampered), encoding="utf-8")
    assert main(["scoreboard", "verify", str(copy)]) == 1
    assert capsys.readouterr().err == f"error: {copy}: {CHANGED}\n"
    kept = Path(stored).read_bytes()
    Path(stored).write_text(signing.dump(tampered), encoding="utf-8")
    assert results_store.check(store, base=unsigned).problems == [
        f"results/{name}.json: its body_sha256 is not its body's SHA-256",
        f"results/{name}.json: changed: only a valid signature may be added to an unsigned result",
    ]
    Path(stored).write_bytes(kept)
    assert results_store.check(store, base=unsigned).problems == []

    # 6. A hidden set graded over HTTP with the declared arm, stored against the pack's
    # declaration, signed and verified.
    declaration = store.declaration_path(INBOX)
    declaration.parent.mkdir()
    declaration.write_text(json.dumps(DECLARATION), encoding="utf-8")  # its SHA-256: DECLARED
    keys = deployment(monkeypatch, "memberships")
    with started(db_target, inbox_pack, keys) as world:
        set_id = scored_set(world, urgent=3, routine=3)
        shown = answered(world, set_id, world.grader())
        capsys.readouterr()  # the server's security log: the run, by set id only
        assert shown["scope"]["declared"] == DECLARED and shown["scope"]["set"]["n"] == 6
        assert shown["scope"]["measured_on"] == DAY.isoformat()
        assert (shown["run"]["run"], shown["run"]["runs_left"]) == (1, 2)
        answer = Path("scoreboard-answer.json")
        answer.write_text(json.dumps(shown), encoding="utf-8")
        before_hidden = files(store)
        assert main(["scoreboard", "add", str(answer)]) == 0
        hidden_name = hidden.scoreboard_body_sha256(shown).removeprefix("sha256:")
        assert capsys.readouterr().out == f"stored: results/{hidden_name}.json\n"
        graded = f"scoreboard/results/{hidden_name}.json"
        assert signed_here(key, graded, monkeypatch) == 0
        capsys.readouterr()
        assert main(["scoreboard", "verify", stored, graded]) == 0
        assert capsys.readouterr().out.splitlines() == [
            f"verified: {path}, signed with {signer} (valid from {DAY.isoformat()} to no end "
            f"date), under {method.METHOD}"
            for path in (stored, graded)
        ]
        checked = results_store.check(store, base=before_hidden)
        assert (checked.problems, checked.signed, checked.results) == ([], 2, 2)
        assert checked.kept == [f"{hidden_name}: {results_store.HIDDEN_KEPT}"]
        moved = {
            **json.loads(Path(graded).read_text(encoding="utf-8")),
            "run": {"run": 2, "runs_left": 1},
        }
        assert signing.problem(moved, store.keys()) == CHANGED  # its run is signed too

        # 7. The chain and the sealed rows verify after the run.
        assert main(["audit", "verify", "--evalsets", "--db", world.db]) == 0
        verified = json.loads(capsys.readouterr().out)
        assert verified["ok"] and verified["evalsets"]["frozen"] == 1
    assert on_this_machine == []
