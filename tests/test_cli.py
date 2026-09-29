from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from openhealth.cli import EXIT_DEGRADED, main, parse_providers
from openhealth.errors import OpenHealthError
from tests.conftest import INBOX_DIR, LOCAL_DATASET

REFERRAL_DIR = INBOX_DIR.parent / "referral-router"

PACK = "openhealth/patient-inbox-router"


def test_validate_and_test(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(INBOX_DIR)]) == 0
    assert main(["test", PACK]) == 0
    assert "0 failed" in capsys.readouterr().out


def test_decide_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["decide", PACK, "--text", "Can I pay my bill online?"]) == 0
    assert json.loads(capsys.readouterr().out)["recommendation"] == "billing"


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
    assert main(["validate", "openhealth/does-not-exist"]) == 1
    assert "error:" in capsys.readouterr().err


def test_provider_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_providers(["router=jev", " safety_screen = rules "]) == {
        "router": "jev",
        "safety_screen": "rules",
    }
    monkeypatch.setenv("OPENHEALTH_PROVIDERS", "router=rules")
    assert parse_providers(None) == {"router": "rules"}
    with pytest.raises(OpenHealthError):
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
