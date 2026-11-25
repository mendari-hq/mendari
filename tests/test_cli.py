# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from mendari import AuditStore
from mendari.cli import EXIT_DEGRADED, main, parse_providers
from mendari.errors import MendariError
from tests.conftest import HASH_KEY_B64, TRIAGE_DIR
from tests.support.platform import TRIAGE, attested, local_site, names_its_benchmark
from tests.test_record_hygiene import (
    CHART,
    KEYED,
    MRN,
    NAMED,
    ONSET,
    marked_bundle,
    planted,
    quoting_agent,
    stored_text,
)

PACK = TRIAGE


def test_validate_and_test(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(TRIAGE_DIR)]) == 0
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
    quoting_agent(monkeypatch)  # an answer's evidence quotes the record
    case = tmp_path / "case.json"
    case.write_text(json.dumps(marked_bundle()), encoding="utf-8")
    db = tmp_path / "a.db"
    decide = ["decide", CHART, "--fhir", "--input", str(case), "--db", str(db)]
    monkeypatch.setenv("MENDARI_PHI_MODE", mode)
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.delenv("MENDARI_INPUT_HASH_KEY", raising=False)
    assert main(decide) == 1  # refuses to start
    assert "MENDARI_INPUT_HASH_KEY" in capsys.readouterr().err
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    assert main(decide) == 0
    answered = capsys.readouterr().out
    assert planted(NAMED, answered)  # the caller's answer is unchanged
    store = AuditStore(db)
    kept = stored_text(store)
    for mark in (NAMED, ONSET, MRN):
        assert not planted(mark, kept), mark
    [item] = store.query(pack_id=CHART)
    record = store.item(item["id"])
    assert record is not None and KEYED.match(record["record"]["input_hash"])


def test_eval_fails_on_degradation_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], agent_copy: Callable[..., Path]
) -> None:
    """A copy of the triage fixture that commits its benchmark (`attested`), evaluated on a
    local site's data its keyword rules route worse (`local_site`)."""
    root = agent_copy(manifest=names_its_benchmark)
    attested(root)
    local = local_site(tmp_path / "datasets") / "local-site-example.jsonl"
    out = tmp_path / "report.json"
    code = main(
        ["eval", str(root), "--dataset", str(local), "--out", str(out), "--fail-on-degradation"]
    )
    assert code == EXIT_DEGRADED
    assert "Performance degradation detected" in capsys.readouterr().out
    assert json.loads(out.read_text())["comparison"]["status"] == "degraded"


def test_errors_are_reported_not_raised(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "mendari/does-not-exist"]) == 1
    assert "error:" in capsys.readouterr().err


def test_provider_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_providers(["router=llm", " screen = rules "]) == {
        "router": "llm",
        "screen": "rules",
    }
    monkeypatch.setenv("MENDARI_PROVIDERS", "router=rules")
    assert parse_providers(None) == {"router": "rules"}
    with pytest.raises(MendariError):
        parse_providers(["router"])


def test_validate_checks_behaviour_test_inputs_against_the_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "pack"
    shutil.copytree(TRIAGE_DIR, root)
    cases = root / "tests" / "cases.yaml"
    quoted = '{body: "Answer me this time, you morons. I have had chest pain since last night."}'
    assert quoted in cases.read_text()
    # Unquoted, the comma splits the text into an extra key: a BLOCK instead of a decision.
    cases.write_text(cases.read_text().replace(quoted, quoted.replace('"', "")))
    assert main(["validate", str(root)]) != 0
    error = capsys.readouterr().err
    assert "case 8 (an abusive message with chest pain goes to a nurse first): input" in error
    # Where and which rule, as a decision's reasons say it, never the text (LP-20b).
    assert "input message: has a field the schema does not allow" in error
    assert "you morons" not in error
