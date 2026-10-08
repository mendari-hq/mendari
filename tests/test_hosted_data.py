# SPDX-License-Identifier: Apache-2.0
"""Hosted data protection (LP-08): nothing kept that cannot be protected or erased.

- The hosted site (`VERCEL`) runs `encrypt` unless told `hash`, and refuses `off`; elsewhere
  sign-in refuses `off` too, set or unset, and the EHR launch without sign-in needs a chosen
  mode (core/phi.py); and a database once kept under `hash` or `encrypt` never takes plaintext
  again.
- A decision recorded under `off` keeps its input inside its chained record. Once the mode is
  `hash` or `encrypt`, that input (and its answers' evidence) is shown only to a reader, and the
  read is chained, as a sealed one is; the audit list never carries it.
- One decision's kept input, evidence and notes are erased on request (`input.erased`).
- Retention runs on a schedule (`/api/internal/retention`, Vercel Cron with CRON_SECRET), keeps
  30 days unless told otherwise, and deletes everything only when asked on the command line.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack, load_pack
from mendari import cli as cli_module
from mendari.cli import main
from mendari.core import audit as audit_module
from mendari.core.decision import DecisionResult
from mendari.core.phi import PhiMode, PhiPolicy
from mendari.errors import MendariError, NoteError
from mendari.server.app import create_app, mounted_routes
from mendari.settings import retention_days
from mendari.standards import audit_fhir
from tests.conftest import (
    HASH_KEY_B64,
    INPUT_HASH_KEY,
    PHI_KEY,
    ROOT,
    encrypt_mode,
    hosted,
    message,
    security_lines,
    signed_in,
)
from tests.test_record_hygiene import LAB_DATE, LAB_VALUE, MRN, evidence, marked_bundle

SECRET = "my neighbour Rosalind Q. Example has chest pain"
URGENT = "Crushing chest pain since an hour, " + SECRET
CRON = "a-cron-secret-of-some-length-0123456789"


@pytest.fixture(scope="module")
def trial() -> Pack:
    return load_pack(ROOT / "packs" / "clinical-trial-prescreener")


def policy(mode: PhiMode) -> PhiPolicy:
    return PhiPolicy(mode=mode, key=PHI_KEY, hash_key=INPUT_HASH_KEY)


def sign_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")


def chained(item: dict[str, Any], event: str) -> list[dict[str, Any]]:
    return [e for e in item["events"] if e["event"] == event]


def as_before_october(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record as releases before 2026-10-02 did: each answer's evidence stays in the chained
    record, whatever the PHI mode (nothing is sealed beside it), and input issues and the
    reasons that refuse over them name a resource by its logical id."""
    monkeypatch.setattr(audit_module, "_take_evidence", lambda record: {})
    monkeypatch.setattr(DecisionResult, "to_record", DecisionResult.to_dict)


# -- the start-up guard ----------------------------------------------------------------------


def test_the_hosted_site_refuses_plaintext_and_runs_encrypt_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("MENDARI_PHI_MODE", "off")
    with pytest.raises(MendariError, match="keeps no input in plaintext") as refused:
        create_app(db_path=tmp_path / "a.db")
    assert "MENDARI_PHI_KEY" in str(refused.value) and "Preview" in str(refused.value)
    monkeypatch.delenv("MENDARI_PHI_MODE")  # the hosted default, encrypt, needs both keys
    for missing in ("MENDARI_PHI_KEY", "MENDARI_INPUT_HASH_KEY"):
        hosted(monkeypatch)
        monkeypatch.delenv(missing)
        with pytest.raises(MendariError, match="runs PHI mode encrypt unless told hash"):
            create_app(db_path=tmp_path / "a.db")
    hosted(monkeypatch)
    health = TestClient(create_app(db_path=tmp_path / "a.db")).get("/api/health").json()
    assert health["phi_mode"] == "encrypt"
    monkeypatch.setenv("MENDARI_PHI_MODE", "hash")  # allowed, with its key
    monkeypatch.delenv("MENDARI_PHI_KEY")
    health = TestClient(create_app(db_path=tmp_path / "b.db")).get("/api/health").json()
    assert health["phi_mode"] == "hash"


def test_sign_in_refuses_plaintext_whatever_the_database(
    db_target: str,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Outside Vercel too, sign-in never starts under `off`, written out or left unset, with or
    without the EHR launch, on SQLite and Postgres alike (LP-08, the owner's choice of
    2026-10-07): people bring other people's cases, and an input kept in plaintext is never
    erased. Even a database only `off` has kept (an open sandbox's) is refused, before it is
    opened; `hash` or `encrypt` starts on it, without the plaintext warning."""
    Decision(inbox_pack, store=AuditStore(db_target, phi=policy("off")))(message("refill please"))
    sign_in_env(monkeypatch)
    for chosen, launch in ((None, False), ("off", False), (" Off ", False), ("off", True)):
        if chosen is None:
            monkeypatch.delenv("MENDARI_PHI_MODE")
        else:
            monkeypatch.setenv("MENDARI_PHI_MODE", chosen)
        if launch:
            monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari")
        with pytest.raises(MendariError) as refused:
            create_app(db_path=db_target)
        assert str(refused.value) == (
            "Sign-in is on, so inputs must be protected: set MENDARI_PHI_MODE=encrypt (with "
            "MENDARI_PHI_KEY and MENDARI_INPUT_HASH_KEY) or hash (with MENDARI_INPUT_HASH_KEY); "
            "off is for a local sandbox without sign-in (docs/deploying-with-phi.md)."
        )
    assert AuditStore(db_target, phi=policy("off")).protected is None  # refused before opening
    for protect in ("hash", "encrypt"):
        encrypt_mode(monkeypatch)
        if protect == "hash":
            monkeypatch.setenv("MENDARI_PHI_MODE", "hash")
            monkeypatch.delenv("MENDARI_PHI_KEY")
        with caplog.at_level(logging.WARNING, logger="mendari.phi"):
            health = TestClient(create_app(db_path=db_target)).get("/api/health").json()
        assert health["sign_in"] is True  # it started, with sign-in on
        assert "plaintext" not in caplog.text
    assert AuditStore(db_target, phi=policy("off")).protected == "hash"  # the first mode stays


def test_the_cli_refuses_sign_in_without_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari serve` refuses at startup, in one line, before it listens."""
    sign_in_env(monkeypatch)

    def listened(*args: object, **kwargs: object) -> None:
        raise AssertionError("the server started")

    monkeypatch.setattr(cli_module, "_run_server", listened)
    assert main(["serve", "--db", str(tmp_path / "a.db")]) == 1
    err = capsys.readouterr().err
    assert "Sign-in is on, so inputs must be protected" in err
    assert len(err.strip().splitlines()) == 1 and "Traceback" not in err
    assert not (tmp_path / "a.db").exists()


def test_an_ehr_launch_needs_a_chosen_mode_without_sign_in_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A launch fetches the patient's record from the EHR: real cases, whoever signs in. Without
    sign-in, `off` written out still starts, with a warning (refusing it is a later decision)."""
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari")
    monkeypatch.delenv("MENDARI_PHI_MODE")
    with pytest.raises(MendariError, match="The EHR launch is on, so choose"):
        create_app(db_path=tmp_path / "a.db")
    monkeypatch.setenv("MENDARI_PHI_MODE", "off")  # written out: a local synthetic sandbox
    with caplog.at_level(logging.WARNING, logger="mendari.phi"):
        assert (
            TestClient(create_app(db_path=tmp_path / "a.db")).get("/api/health").status_code == 200
        )
    assert "the EHR launch is on and PHI mode is off: inputs are kept in plaintext" in caplog.text


def test_vercels_entrypoint_is_hosted_even_without_its_system_variables(tmp_path: Path) -> None:
    """The root `app.py`, which only Vercel loads, applies the hosted checks itself."""
    env = {k: v for k, v in os.environ.items() if k != "VERCEL"}
    env.update(DATABASE_URL=str(tmp_path / "a.db"), MENDARI_PHI_MODE="off")
    code = "import runpy; runpy.run_path('app.py')"
    ran = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert ran.returncode != 0 and "keeps no input in plaintext" in ran.stderr


def test_an_open_local_sandbox_still_starts_without_a_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MENDARI_PHI_MODE")
    health = TestClient(create_app(db_path=tmp_path / "a.db")).get("/api/health").json()
    assert health["phi_mode"] == "off"


def test_a_protected_database_never_takes_plaintext_again(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "a.db"
    assert AuditStore(db, phi=policy("off")).protected is None  # only `off` so far
    sealed = Decision(inbox_pack, store=AuditStore(db, phi=policy("encrypt")))(message(SECRET))
    assert AuditStore(db, phi=policy("hash")).protected == "encrypt"  # the first mode stays
    off = AuditStore(db, phi=policy("off"))
    with pytest.raises(MendariError, match="first kept under PHI mode encrypt"):
        Decision(inbox_pack, store=off)(message(SECRET))
    with pytest.raises(MendariError, match="never takes plaintext again"):
        off.record_feedback(sealed.id, rating="down", reporter="rn-2", note=SECRET)
    off.record_feedback(sealed.id, rating="up", reporter="rn-2")  # no text: allowed
    hashed = Decision(inbox_pack).decide(message(SECRET))
    off.record(hashed, message(SECRET), store_input="hash")  # nothing of the input kept
    assert off.get(sealed.id) is not None and off.verify()["ok"] is True  # reading is fine
    assert SECRET not in json.dumps(off.query())
    monkeypatch.setenv("MENDARI_PHI_MODE", "off")
    with pytest.raises(MendariError, match="first kept under PHI mode encrypt"):
        create_app(db_path=db)
    decide = ["decide", "mendari/patient-inbox-router", "--db", str(db), "--text", SECRET]
    assert main(decide) == 1  # a command run without the deployment's settings
    assert "first kept under PHI mode encrypt" in capsys.readouterr().err
    assert SECRET not in json.dumps(AuditStore(db, phi=policy("encrypt")).query())


def test_the_refusal_comes_before_any_provider_and_sees_a_later_mark(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "a.db"
    early = AuditStore(db, phi=policy("off"))  # a long-running store, opened before the mark
    Decision(inbox_pack, store=early)(message("refill please"))  # synthetic, allowed: unmarked
    AuditStore(db, phi=policy("encrypt"))  # another process protects the database

    def ran(*args: object, **kwargs: object) -> None:
        raise AssertionError("the case reached the schema or a provider")

    monkeypatch.setattr(Decision, "validate", ran)
    with pytest.raises(MendariError, match="first kept under PHI mode encrypt"):
        Decision(inbox_pack, store=early)(message(SECRET))
    assert SECRET not in json.dumps(early.query())


def test_the_signed_in_templates_choose_a_mode() -> None:
    """Every template with sign-in on runs `encrypt`; the compose sandbox's signed-in engine
    reads its keys from the shell or `.env` (never committed, never `${VAR:?}`, which would stop
    a plain `docker compose up` too). Only the open sandbox, with the EHR launch, runs `off`."""
    compose = (ROOT / "docker-compose.yml").read_text()
    engine = compose[compose.index("  engine:") : compose.index("  engine-signin:")]
    signin = compose[compose.index("  engine-signin:") : compose.index("  payer:")]
    assert "MENDARI_OIDC_ISSUER" not in engine and 'MENDARI_PHI_MODE: "off"' in engine
    assert "MENDARI_OIDC_ISSUER" in signin and "MENDARI_PHI_MODE: encrypt" in signin
    assert "MENDARI_PHI_KEY: ${MENDARI_PHI_KEY:-}" in signin
    assert "MENDARI_INPUT_HASH_KEY: ${MENDARI_INPUT_HASH_KEY:-}" in signin
    assert '"off"' not in signin and ":?" not in compose
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()
    assert "MENDARI_PHI_MODE=encrypt" in (ROOT / "deploy/onprem/.env.example").read_text()
    assert "MENDARI_PHI_MODE: encrypt" in (ROOT / "deploy/kubernetes/mendari.yaml").read_text()
    snippets = (ROOT / "scripts/check_doc_snippets.py").read_text()
    assert '"MENDARI_PHI_MODE"] = "encrypt"' in snippets and '= "off"' not in snippets


# -- decisions recorded under `off`, read under `encrypt` ------------------------------------


def test_a_legacy_input_opens_only_for_a_reader_and_the_read_is_chained(
    db_target: str, trial: Pack
) -> None:
    before = AuditStore(db_target, phi=policy("off"))
    result, _ = Decision(trial, store=before).decide_fhir(marked_bundle())
    before.close()
    store = AuditStore(db_target, phi=policy("encrypt"))
    hidden = store.get(result.id)
    assert hidden is not None and hidden["input_restricted"] is True
    assert hidden["record"]["input"] is None and LAB_VALUE not in evidence(hidden["record"])
    assert not chained(hidden, "input.read")
    shown = store.get(result.id, reader="Nurse Kim")
    assert shown is not None and shown["input_restricted"] is False
    assert shown["record"]["input"] is not None and LAB_VALUE in evidence(shown["record"])
    assert [e["payload"]["reader"] for e in chained(shown, "input.read")] == ["Nurse Kim"]
    listed = store.withhold_inputs(store.query())
    assert listed[0]["input_restricted"] is True and listed[0]["record"]["input"] is None
    assert LAB_VALUE not in evidence(listed[0]["record"])
    assert store.verify()["ok"] is True  # nothing stored was changed
    store.close()


@pytest.mark.parametrize("mode", ["hash", "encrypt"])
def test_evidence_an_older_record_kept_opens_only_for_a_reader(
    tmp_path: Path, trial: Pack, monkeypatch: pytest.MonkeyPatch, mode: PhiMode
) -> None:
    store = AuditStore(tmp_path / "a.db", phi=policy(mode))
    with monkeypatch.context() as before:
        as_before_october(before)
        result, _ = Decision(trial, store=store).decide_fhir(marked_bundle())
    hidden = store.get(result.id)
    assert hidden is not None and hidden["input_restricted"] is True
    assert LAB_VALUE not in evidence(hidden["record"]) and not chained(hidden, "input.read")
    shown = store.get(result.id, reader="Nurse Kim")
    assert shown is not None and LAB_VALUE in evidence(shown["record"])
    assert [e["payload"]["reader"] for e in chained(shown, "input.read")] == ["Nurse Kim"]
    listed = store.withhold_inputs(store.query())
    assert LAB_VALUE not in evidence(listed[0]["record"]) and listed[0]["input_restricted"]
    assert store.verify()["ok"] is True


@pytest.mark.parametrize("validation", ["warn", "enforce"])
def test_an_older_records_resource_ids_open_only_for_a_reader(
    tmp_path: Path, trial: Pack, monkeypatch: pytest.MonkeyPatch, validation: str
) -> None:
    store = AuditStore(tmp_path / "a.db", phi=policy("encrypt"))
    with monkeypatch.context() as october:
        as_before_october(october)
        decide = Decision(trial, store=store, fhir_validation=validation)  # type: ignore[arg-type]
        result, _ = decide.decide_fhir(marked_bundle(conforming=False))
    hidden = store.get(result.id)
    assert hidden is not None and MRN not in json.dumps(hidden["record"])
    assert hidden["record"]["input_issues"][0]["resource"] == "Patient"
    listed = store.withhold_inputs(store.query())
    assert MRN not in json.dumps(listed)
    shown = store.get(result.id, reader="Nurse Kim")
    assert shown is not None and MRN in json.dumps(shown["record"]["input_issues"])
    assert chained(shown, "input.read")
    if validation == "enforce":  # the refusal named it too
        assert MRN in " ".join(shown["record"]["reasons"])
        assert "Patient Patient.gender" in " ".join(hidden["record"]["reasons"])
    erased = store.erase_input(result.id, erased_by="dpo@example.org", reason="request")
    assert erased["still_in_chain"] is True
    assert store.verify()["ok"] is True


def test_under_off_or_hash_an_older_records_evidence_follows_the_reader(
    tmp_path: Path, trial: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record sealed under encrypt before 2026-10-02 (its evidence also in itself), read
    later under `off` (as before: nothing withheld) and under `hash` (a reader sees what the
    record holds, and the read is chained; the sealed input stays sealed)."""
    db = tmp_path / "a.db"
    with monkeypatch.context() as october:
        as_before_october(october)
        result, _ = Decision(trial, store=AuditStore(db, phi=policy("encrypt"))).decide_fhir(
            marked_bundle()
        )
    off = AuditStore(db, phi=policy("off")).get(result.id)
    assert off is not None and LAB_VALUE in evidence(off["record"])
    assert not chained(off, "input.read")
    hashed = AuditStore(db, phi=policy("hash"))
    seen = hashed.get(result.id, reader="Nurse Kim")
    assert seen is not None and LAB_VALUE in evidence(seen["record"])
    assert seen["input_restricted"] is True and seen["record"]["input"] is None
    assert [e["payload"]["reader"] for e in chained(seen, "input.read")] == ["Nurse Kim"]
    hidden = hashed.get(result.id)
    assert hidden is not None and LAB_VALUE not in evidence(hidden["record"])


def test_under_off_a_record_still_carries_its_input(tmp_path: Path, trial: Pack) -> None:
    store = AuditStore(tmp_path / "a.db", phi=policy("off"))
    result, _ = Decision(trial, store=store).decide_fhir(marked_bundle())
    item = store.get(result.id)
    assert item is not None and item["record"]["input"] is not None
    assert "input_restricted" not in item and not chained(item, "input.read")
    assert store.withhold_inputs(store.query())[0]["record"]["input"] is not None


def test_the_server_gates_legacy_inputs_by_role(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "a.db"
    decided = Decision(inbox_pack, store=AuditStore(db, phi=policy("off")))(message(URGENT))
    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
    people = AuditStore(db, phi=policy("encrypt"))
    viewer = signed_in(people, role="viewer")
    reviewer = signed_in(people, role="reviewer")
    for url in (f"/api/audit/{decided.id}", "/api/review", "/api/audit"):
        assert "Rosalind" not in client.get(url, headers=viewer).text, url
    for url in (f"/api/audit/{decided.id}", "/api/review"):
        assert "Rosalind" in client.get(url, headers=reviewer).text, url
    assert "Rosalind" not in client.get("/api/audit", headers=reviewer).text  # lists never
    item = client.get(f"/api/audit/{decided.id}", headers=viewer).json()
    readers = [e["payload"]["reader"] for e in chained(item, "input.read")]
    assert readers == ["reviewer@default.example.org"] * 2


def test_exporting_older_inputs_under_hash_is_logged_as_a_read(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "a.db"
    before = AuditStore(db, phi=policy("off"))
    rated = Decision(inbox_pack, store=before)(message(URGENT))
    before.record_feedback(rated.id, rating="down", reporter="rn-2")
    hosted(monkeypatch)
    monkeypatch.setenv("MENDARI_PHI_MODE", "hash")  # as the hosted site may run it
    sign_in_env(monkeypatch)
    client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
    admin = signed_in(AuditStore(db, phi=policy("hash")), role="admin")
    capsys.readouterr()
    exported = client.get(f"/api/packs/{inbox_pack.id}/feedback/export", headers=admin)
    assert exported.status_code == 200 and "Rosalind" in exported.text
    lines = security_lines(capsys.readouterr().out)
    assert [line["ids"] for line in lines if line["event"] == "phi.inputs.read"] == [[rated.id]]


# Marks planted in stored cases: an input's words, a note's, an answer's evidence (a lab value
# and its date) and a resource's logical id (a medical record number).
INPUT_MARK = "ZZINPUTMARK"
NOTE_MARK = "ZZNOTEMARK"
READERS = {"person:reviewer", "person:admin"}  # who may read inputs (`read_input`)
# Read routes the sweep below leaves out, and why: neither serves a stored case.
NOT_SWEPT = {
    "/api/internal/retention": "the scheduled purge: its own tests, with its secret",
    "/api/providers/jev/models": "Jev's model list: needs Jev set up",
}
# Ids for the routes that read one fixed resource.
FIXED_IDS = {
    "/fhir/CodeSystem/{resource_id}": "audit-event",
    "/fhir/NamingSystem/{resource_id}": "decision-id",
    "/fhir/OperationDefinition/{resource_id}": "decide",
}


def marks(text: str) -> list[str]:
    found = [mark for mark in (INPUT_MARK, NOTE_MARK, MRN) if mark in text]
    if re.search(rf"(?<![\d.]){re.escape(LAB_VALUE)} % on {LAB_DATE}", text):
        found.append("evidence")
    return found


def input_reads(store: AuditStore) -> int:
    with store._db.session(exclusive=False) as db:
        row = db.one("SELECT COUNT(*) AS n FROM audit_events WHERE event = 'input.read'")
    return int(row["n"]) if row else 0


def test_no_route_shows_a_stored_case_but_to_a_reader_and_every_read_is_chained(
    db_target: str,
    trial: Pack,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every read route (and FHIR search) but `NOT_SWEPT`, as every kind of principal that can
    read (people in each role, keys in each role, and no one; a Backend Services client holds a
    key's roles, and an EHR launch reads none of these), under `encrypt` with sign-in on, over
    decisions recorded under `off` before the switch, one sealed before 2026-10-02 (evidence and
    a resource's id in its record) and one sealed now, with notes. Each read a reader is shown is
    chained and logged."""
    before = AuditStore(db_target, phi=policy("off"))
    message_off = Decision(inbox_pack, store=before)(message(f"Crushing chest pain, {INPUT_MARK}"))
    before.record_feedback(message_off.id, rating="down", reporter="rn-2", note=NOTE_MARK)
    lab_off, _ = Decision(trial, store=before).decide_fhir(marked_bundle())
    before.close()
    store = AuditStore(db_target, phi=policy("encrypt"))
    with monkeypatch.context() as october:
        as_before_october(october)
        decide = Decision(trial, store=store, fhir_validation="enforce")
        lab_october, _ = decide.decide_fhir(marked_bundle(conforming=False))
    sealed = Decision(inbox_pack, store=store)(message(f"Crushing chest pain, {INPUT_MARK} now"))
    store.resolve_review(sealed.id, final_label="nurse", reviewer="rn-1", note=NOTE_MARK)
    decisions = [message_off.id, lab_off.id, lab_october.id, sealed.id]

    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    app = create_app(db_path=db_target)
    client = TestClient(app, base_url="https://mendari.test")
    who: dict[str, dict[str, str]] = {
        f"person:{role}": signed_in(store, role=role)
        for role in ("viewer", "governance", "reviewer", "admin")
    }
    keys: dict[str, dict[str, Any]] = {}
    for role in ("viewer", "service"):
        key = store.create_api_key(name=f"k-{role}", role=role, org="default", created_by="t")
        keys[role] = key
        who[f"key:{role}"] = {"authorization": f"Bearer {key['key']}"}
    who["anonymous"] = {}
    admin = who["person:admin"]
    found = {
        kind: [
            e["resource"]["id"] for e in client.get(f"/fhir/{kind}", headers=admin).json()["entry"]
        ]
        for kind in ("AuditEvent", "Provenance")
    }

    skip = ("/auth/", "/smart/", "/api/smart/", "/static", "/docs", "/redoc", "/favicon")
    urls: list[tuple[str, str]] = []
    for method, path in sorted(set(mounted_routes(app))):
        if not (method == "GET" or path.endswith("/_search")) or path in ("/", "/openapi.json"):
            continue
        if path.startswith(skip) or path in NOT_SWEPT:
            continue
        filled = [path.replace("{resource_id}", FIXED_IDS.get(path, "{resource_id}"))]
        if path == "/api/monitoring":
            filled = [f"{path}?pack_id={pack.id}" for pack in (inbox_pack, trial)]
        if "{key_id}" in path:  # a key's history: one of the keys above
            filled = [path.replace("{key_id}", keys["service"]["id"])]
        if "{decision_id}" in path or "/Task/{resource_id}" in path:
            filled = [
                path.replace("{decision_id}", d).replace("{resource_id}", d) for d in decisions
            ]
        for kind, ids in found.items():
            if f"/{kind}/{{resource_id}}" in path:
                filled = [path.replace("{resource_id}", i) for i in ids[:6]]
        for pack in (inbox_pack, trial):
            namespace, name = pack.id.split("/")
            if "{namespace}" in path:
                filled += [
                    p.replace("{namespace}", namespace).replace("{name}", name) for p in filled
                ]
        filled = [re.sub(r"\{[^}]+\}", "1", p) for p in filled if "{namespace}" not in p]
        urls += [(method, url) for url in dict.fromkeys(filled)]
    assert len(urls) > 40  # every read route, not a few

    problems: list[str] = []
    answered: set[str] = set()
    capsys.readouterr()
    for method, url in urls:
        for name, headers in who.items():
            count = input_reads(store)
            if method == "GET":
                answer = client.get(url, headers=headers)
            else:
                form = {**headers, "content-type": "application/x-www-form-urlencoded"}
                answer = client.post(url, headers=form, content="")
            if answer.status_code == 200:
                answered.add(url)
            logged = [
                line
                for line in security_lines(capsys.readouterr().out)
                if line["event"] == "phi.inputs.read"
            ]
            shown = marks(answer.text)
            if shown and name not in READERS:
                problems.append(f"{method} {url} showed {shown} to {name}")
            elif shown and input_reads(store) == count:
                problems.append(f"{method} {url} showed {shown} to {name} without a chained read")
            elif shown and not logged:
                problems.append(f"{method} {url} showed {shown} to {name} without a log line")
    assert problems == []
    assert {url for _, url in urls} - answered == set()  # each answers someone: none is idle
    # The readers did see each case: the routes above are not all refusals.
    for decision_id in decisions:
        read = client.get(f"/api/audit/{decision_id}", headers=who["person:reviewer"]).text
        assert marks(read), decision_id
    assert store.verify(notes=True)["ok"] is True
    store.close()


def test_under_hash_a_read_of_what_an_older_sealed_record_holds_is_logged_too(
    tmp_path: Path,
    trial: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sealed under `encrypt` before 2026-10-02 (its evidence in itself too), read under `hash`:
    the sealed input stays sealed, the evidence is shown to a reviewer, and the read is both
    chained and logged."""
    db = tmp_path / "a.db"
    with monkeypatch.context() as october:
        as_before_october(october)
        result, _ = Decision(trial, store=AuditStore(db, phi=policy("encrypt"))).decide_fhir(
            marked_bundle()
        )
    monkeypatch.setenv("MENDARI_PHI_MODE", "hash")
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    sign_in_env(monkeypatch)
    client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
    reviewer = signed_in(AuditStore(db, phi=policy("hash")), role="reviewer")
    capsys.readouterr()
    read = client.get(f"/api/audit/{result.id}", headers=reviewer).json()
    assert LAB_VALUE in evidence(read["record"]) and read["input_restricted"] is True
    assert read["input_read"] is True and chained(read, "input.read")
    lines = security_lines(capsys.readouterr().out)
    assert [line["ids"] for line in lines if line["event"] == "phi.inputs.read"] == [[result.id]]


def test_a_decision_rated_twice_is_logged_as_read_once(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "a.db"
    store = AuditStore(db, phi=policy("encrypt"))
    rated = Decision(inbox_pack, store=store)(message(URGENT))
    store.record_feedback(rated.id, rating="down", reporter="rn-1", note=SECRET)
    store.record_feedback(rated.id, rating="up", reporter="rn-2")
    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
    reviewer = signed_in(store, role="reviewer")
    capsys.readouterr()
    listed = client.get("/api/feedback", headers=reviewer).json()
    assert len([item for item in listed if item["decision_id"] == rated.id]) == 2
    lines = security_lines(capsys.readouterr().out)
    read = [line for line in lines if line["event"] == "phi.inputs.read"]
    assert [(line["ids"], line["count"]) for line in read] == [([rated.id], 1)]


# -- erasing one decision's kept input -------------------------------------------------------


def test_erasing_a_kept_input_and_its_notes_keeps_the_chain(
    db_target: str, trial: Pack, inbox_pack: Pack
) -> None:
    store = AuditStore(db_target, phi=policy("encrypt"))
    result, _ = Decision(trial, store=store).decide_fhir(marked_bundle())
    urgent = Decision(inbox_pack, store=store)(message(URGENT))
    store.resolve_review(urgent.id, final_label="nurse", reviewer="rn-1", note=SECRET)
    for decision_id in (result.id, urgent.id):
        erased = store.erase_input(decision_id, erased_by="dpo@example.org", reason="request")
        assert erased["input"] is True
    assert len(erased["notes"]) == 1
    shown = store.get(urgent.id, reader="Nurse Kim")
    assert shown is not None and shown["record"]["input"] is None
    assert [e["note_status"] for e in chained(shown, "review.resolved")] == ["erased"]
    assert chained(shown, "input.erased")[0]["payload"] == {
        "reason": "request",
        "erased_by": "dpo@example.org",
    }
    lab = store.get(result.id, reader="Nurse Kim")
    assert lab is not None and LAB_VALUE not in evidence(lab["record"])
    with store._db.session(exclusive=False) as db:
        assert db.all("SELECT * FROM decision_inputs") == []
    assert store.verify(notes=True)["ok"] is True
    with pytest.raises(NoteError, match="keeps no input or note"):
        store.erase_input(urgent.id, erased_by="dpo@example.org", reason="request")
    store.close()


def test_what_the_chain_holds_is_said_to_remain(
    tmp_path: Path, trial: Pack, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "a.db"
    before = AuditStore(db, phi=policy("off"))
    legacy = Decision(inbox_pack, store=before)(message(URGENT))
    noted = Decision(inbox_pack, store=before)(message(URGENT))
    before.resolve_review(noted.id, final_label="nurse", reviewer="rn-1", note=SECRET)
    store = AuditStore(db, phi=policy("encrypt"))
    with pytest.raises(NoteError, match="keeps nothing apart from the chain"):
        store.erase_input(legacy.id, erased_by="dpo@example.org", reason="request")
    erased = store.erase_input(noted.id, erased_by="dpo@example.org", reason="request")
    assert (erased["input"], len(erased["notes"]), erased["still_in_chain"]) == (False, 1, True)
    with monkeypatch.context() as october:
        as_before_october(october)
        older, _ = Decision(trial, store=store).decide_fhir(marked_bundle())
    erased = store.erase_input(older.id, erased_by="dpo@example.org", reason="error")
    assert (erased["input"], erased["still_in_chain"]) == (True, True)  # its evidence remains
    fresh = Decision(inbox_pack, store=store)(message(SECRET))
    erased = store.erase_input(fresh.id, erased_by="dpo@example.org", reason="request")
    assert erased["still_in_chain"] is False
    assert store.verify(notes=True)["ok"] is True


def test_what_cannot_be_erased_is_refused(tmp_path: Path, inbox_pack: Pack) -> None:
    store = AuditStore(tmp_path / "a.db", phi=policy("encrypt"))
    with pytest.raises(NoteError, match="no decision"):
        store.erase_input("nope", erased_by="dpo@example.org", reason="request")
    kept = Decision(inbox_pack, store=store)(message(SECRET))
    with pytest.raises(NoteError, match="the reason is one of"):
        store.erase_input(kept.id, erased_by="dpo@example.org", reason="because")
    with pytest.raises(NoteError, match="who erases"):
        store.erase_input(kept.id, erased_by=" ", reason="request")
    assert store.verify()["ok"] is True


def test_the_export_says_an_input_was_erased(tmp_path: Path, inbox_pack: Pack) -> None:
    store = AuditStore(tmp_path / "a.db", phi=policy("encrypt"))
    decided = Decision(inbox_pack, store=store)(message(SECRET))
    store.erase_input(decided.id, erased_by="dpo@example.org", reason="error")
    exported = list(audit_fhir.resources(store.iter_events()))
    erased = next(
        r
        for r in exported
        if r["resourceType"] == "AuditEvent" and r["subtype"][0]["code"] == "input.erased"
    )
    assert erased["action"] == "D" and "error" in json.dumps(erased["entity"])
    assert "dpo@example.org" in json.dumps(erased["agent"])


def test_admins_erase_through_the_api(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    db = tmp_path / "a.db"
    client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
    people = AuditStore(db, phi=policy("encrypt"))
    admin, reviewer = signed_in(people, role="admin"), signed_in(people, role="reviewer")
    decided = client.post(
        f"/api/packs/{inbox_pack.id}/decide", json=message(SECRET), headers=admin
    ).json()
    path = f"/api/audit/{decided['id']}/erase"
    assert client.post(path, json={"reason": "request"}, headers=reviewer).status_code == 403
    assert client.post(path, json={"reason": "whim"}, headers=admin).status_code == 422
    assert (
        client.post("/api/audit/nope/erase", json={"reason": "request"}, headers=admin).status_code
        == 404
    )
    erased = client.post(path, json={"reason": "request"}, headers=admin)
    assert erased.status_code == 200 and erased.json()["erased_by"] == "admin@default.example.org"
    again = client.post(path, json={"reason": "request"}, headers=admin)
    assert again.status_code == 422 and "keeps no input" in again.json()["detail"]
    assert people.verify()["ok"] is True


def test_the_cli_erases(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    encrypt_mode(monkeypatch)
    db = tmp_path / "a.db"
    decided = Decision(inbox_pack, store=AuditStore(db, phi=policy("encrypt")))(message(SECRET))
    erase = ["phi", "erase", "--db", str(db), "--decision", decided.id, "--reason", "request"]
    assert main([*erase, "--by", "a person"]) == 1  # an identifier, as for notes
    assert main([*erase, "--by", "dpo@example.org"]) == 0
    assert f"decision {decided.id}: its input erased" in capsys.readouterr().out
    assert AuditStore(db, phi=policy("encrypt")).verify()["ok"] is True


# -- retention -------------------------------------------------------------------------------


def test_retention_keeps_30_days_unless_told_and_never_0_from_a_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert retention_days() == 30
    monkeypatch.setenv("MENDARI_RETENTION_DAYS", "90")
    assert retention_days() == 90
    for typo in ("0", "-1", "30d", "1.5", "36501", "1000000"):
        monkeypatch.setenv("MENDARI_RETENTION_DAYS", typo)
        with pytest.raises(MendariError, match="1 to 36500"):
            retention_days()


def test_purging_everything_is_asked_for_on_the_command_line(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    encrypt_mode(monkeypatch)
    db = tmp_path / "a.db"
    store = AuditStore(db, phi=policy("encrypt"))
    urgent = Decision(inbox_pack, store=store)(message(URGENT))
    store.resolve_review(urgent.id, final_label="nurse", reviewer="rn-1", note=SECRET)
    purge = ["phi", "purge", "--db", str(db)]
    monkeypatch.setenv("MENDARI_RETENTION_DAYS", "0")
    assert main(purge) == 1  # a setting never purges everything
    monkeypatch.delenv("MENDARI_RETENTION_DAYS")
    assert main(purge) == 0  # 30 days: today's input stays
    assert "0 stored input(s) deleted and 0 note(s) erased, older than 30 days" in (
        capsys.readouterr().out
    )
    assert main([*purge, "--days", "0"]) == 1
    assert "add --yes" in capsys.readouterr().err
    assert main([*purge, "--days", "-1", "--yes"]) == 1
    assert main([*purge, "--days", "1000000"]) == 1  # a date that far back does not exist
    assert main([*purge, "--days", "0", "--yes"]) == 0
    assert "1 stored input(s) deleted and 1 note(s) erased" in capsys.readouterr().out
    assert AuditStore(db, phi=policy("encrypt")).verify(notes=True)["ok"] is True


def test_retention_purges_in_batches_and_chains_a_summary(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_module, "PURGE_BATCH", 2)
    store = AuditStore(db_target, phi=policy("encrypt"))
    assert store.last_purge() is None
    decide = Decision(inbox_pack, store=store)
    for n in range(5):
        urgent = decide(message(f"{URGENT} ({n})"))
        store.resolve_review(urgent.id, final_label="nurse", reviewer="rn-1", note=SECRET)
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decision_inputs SET created_at = '2020-01-01T00:00:00+00:00'")
        db.all("UPDATE notes SET created_at = '2020-01-01T00:00:00+00:00'")
    summary = store.purge(30, source="command")
    assert summary == {"retention_days": 30, "inputs": 5, "notes": 5, "source": "command"}
    last = store.last_purge()
    assert last is not None and {k: last[k] for k in summary} == summary and last["at"]
    assert store.purge(30, source="schedule")["inputs"] == 0  # nothing left; still recorded
    assert store.last_purge()["source"] == "schedule"  # type: ignore[index]
    assert store.verify(notes=True)["ok"] is True
    store.close()


def cron_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, secret: str | None = CRON
) -> TestClient:
    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    if secret is not None:
        monkeypatch.setenv("CRON_SECRET", secret)
    return TestClient(create_app(db_path=tmp_path / "a.db"), base_url="https://mendari.test")


def test_the_scheduled_purge_runs_with_the_cron_secret_only(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = cron_client(tmp_path, monkeypatch)
    store = AuditStore(tmp_path / "a.db", phi=policy("encrypt"))
    old = Decision(inbox_pack, store=store)(message(SECRET))
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decision_inputs SET created_at = '2020-01-01T00:00:00+00:00'")
    new = Decision(inbox_pack, store=store)(message(SECRET))
    route = "/api/internal/retention"
    assert client.get(route).status_code == 401  # no session, no secret
    wrong = {"authorization": "Bearer " + CRON[:-1] + "x"}
    assert client.get(route, headers=wrong).status_code == 401
    admin = signed_in(store, role="admin")
    assert client.get(route, headers=admin).status_code == 401  # a session is not the secret
    ran = client.get(route, headers={"authorization": f"Bearer {CRON}"})
    assert ran.status_code == 200
    assert ran.json() == {"retention_days": 30, "inputs": 1, "notes": 0, "source": "schedule"}
    purged, kept = store.get(old.id, reader="Nurse Kim"), store.get(new.id, reader="Nurse Kim")
    assert purged is not None and purged["record"]["input"] is None
    assert kept is not None and kept["record"]["input"] is not None
    assert store.verify()["ok"] is True
    health = client.get("/api/health", headers=admin).json()
    assert (health["retention_days"], health["retention_scheduled"]) == (30, True)
    assert health["last_purge"]["source"] == "schedule" and health["last_purge"]["inputs"] == 1
    assert "last_purge" not in client.get("/api/health").json()  # signed out: trimmed


def test_without_a_cron_secret_there_is_no_scheduled_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = cron_client(tmp_path, monkeypatch, secret=None)
    assert client.get("/api/internal/retention").status_code == 404
    assert (
        client.get("/api/internal/retention", headers={"authorization": "Bearer "}).status_code
        == 404
    )
    for wrong in ("short", f" {CRON}", f"{CRON}\n"):  # Vercel sends it as stored
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="mendari.phi"):
            client = cron_client(tmp_path, monkeypatch, secret=wrong)  # it still starts
        assert "CRON_SECRET is not used" in caplog.text
        assert client.get("/api/internal/retention").status_code == 404  # the purge is off
        admin = signed_in(AuditStore(tmp_path / "a.db", phi=policy("encrypt")), role="admin")
        assert client.get("/api/health", headers=admin).json()["retention_scheduled"] is False


def test_vercel_schedules_the_purge_daily(tmp_path: Path) -> None:
    crons = json.loads((ROOT / "vercel.json").read_text())["crons"]
    assert crons == [{"path": "/api/internal/retention", "schedule": "0 8 * * *"}]
    paths = {getattr(route, "path", "") for route in create_app(db_path=tmp_path / "a.db").routes}
    assert "/api/internal/retention" in paths
