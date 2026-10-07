# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import json
import os
import shutil
from pathlib import Path

import pytest

from mendari import AuditStore
from mendari.cli import EXIT_DEGRADED, main, parse_providers
from mendari.errors import MendariError
from tests.conftest import HASH_KEY_B64, INBOX_DIR, LOCAL_DATASET
from tests.test_record_hygiene import (
    KEYED,
    LAB_DATE,
    LAB_VALUE,
    MRN,
    TRIAL,
    marked_bundle,
    planted,
    stored_text,
)

REFERRAL_DIR = INBOX_DIR.parent / "referral-router"

PACK = "mendari/patient-inbox-router"


def test_validate_and_test(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(INBOX_DIR)]) == 0
    assert main(["test", PACK]) == 0
    assert "0 failed" in capsys.readouterr().out


def test_decide_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["decide", PACK, "--text", "Can I pay my bill online?"]) == 0
    assert json.loads(capsys.readouterr().out)["recommendation"] == "billing"


@pytest.mark.parametrize("mode", ["hash", "encrypt"])
def test_decide_records_under_the_deployments_phi_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    """`decide --db` keeps what the server would: no input or evidence in the chained record
    (`encrypt` seals them apart), a keyed input hash, and no start without the hash key."""
    case = tmp_path / "case.json"
    case.write_text(json.dumps(marked_bundle()), encoding="utf-8")
    db = tmp_path / "a.db"
    decide = ["decide", TRIAL, "--fhir", "--input", str(case), "--db", str(db)]
    monkeypatch.setenv("MENDARI_PHI_MODE", mode)
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.delenv("MENDARI_INPUT_HASH_KEY", raising=False)
    assert main(decide) == 1  # refuses to start
    assert "MENDARI_INPUT_HASH_KEY" in capsys.readouterr().err
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    assert main(decide) == 0
    answered = capsys.readouterr().out
    assert planted(LAB_VALUE, answered)  # the caller's answer is unchanged
    store = AuditStore(db)
    kept = stored_text(store)
    for mark in (LAB_VALUE, LAB_DATE, MRN):
        assert not planted(mark, kept), mark
    [item] = store.query(pack_id=TRIAL)
    record = store.item(item["id"])
    assert record is not None and KEYED.match(record["record"]["input_hash"])


def test_eval_fails_on_degradation_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.json"
    code = main(
        ["eval", PACK, "--dataset", str(LOCAL_DATASET), "--out", str(out), "--fail-on-degradation"]
    )
    assert code == EXIT_DEGRADED
    assert "Performance degradation detected" in capsys.readouterr().out
    assert json.loads(out.read_text())["comparison"]["status"] == "degraded"


def test_errors_are_reported_not_raised(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "mendari/does-not-exist"]) == 1
    assert "error:" in capsys.readouterr().err


def test_provider_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_providers(["router=jev", " safety_screen = rules "]) == {
        "router": "jev",
        "safety_screen": "rules",
    }
    monkeypatch.setenv("MENDARI_PROVIDERS", "router=rules")
    assert parse_providers(None) == {"router": "rules"}
    with pytest.raises(MendariError):
        parse_providers(["router"])


def test_validate_checks_behaviour_test_inputs_against_the_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "pack"
    shutil.copytree(REFERRAL_DIR, root)
    cases = root / "tests" / "cases.yaml"
    quoted = '{reason: "Known stable angina, unchanged for 5 years. Please review medication.",'
    assert quoted in cases.read_text()
    # Unquoted, the comma splits the text into an extra key: a BLOCK instead of a decision.
    cases.write_text(cases.read_text().replace(quoted, quoted.replace('"', "")))
    assert main(["validate", str(root)]) != 0
    error = capsys.readouterr().err
    assert "case 4 (a stable, long-standing problem is not urgent): input" in error
    assert "Additional properties are not allowed" in error
