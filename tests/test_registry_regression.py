# SPDX-License-Identifier: Apache-2.0
"""The registry's regression check on every publish (LP-18g, `hub/regression.py`): the pack gate
on the publisher's own test set, in an isolated job, shown as what it is."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari.core.pack import Pack, load_pack
from mendari.errors import MendariError
from mendari.hub import regression
from tests.conftest import Registry
from tests.test_registry import INBOX, _at_version


@pytest.fixture(autouse=True)
def _checking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "on")


def _copy(tmp_path: Path, version: str, edit: Any = None) -> Pack:
    pack = _at_version(tmp_path / version, INBOX, version)
    if edit is not None:
        edit(pack.root)
    return load_pack(pack.root)


def _worse(root: Path) -> None:
    """The same test set, routed by the pack's keyword rules instead of its model."""
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["providers"]["router"]["use"] = "rules"
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))


def test_a_publish_is_checked_on_its_own_test_set_and_shown_as_no_ranking(
    registry: Registry, tmp_path: Path
) -> None:
    first = registry.publish(_copy(tmp_path, "9.9.7"))
    assert first.status_code == 201, first.text
    shown = registry.client.get(f"/v1/packs/{INBOX.id}/9.9.7").json()["checked"]["regression"]
    assert shown["label"] == "publisher's own test set: regression check, not a ranking"
    assert shown["status"] == "measured" and shown["compared_with"] is None
    assert shown["regressions"] == []
    assert shown["test_set"]["name"] == "test" and shown["test_set"]["n"] == 600
    assert shown["accuracy"]["n"] == 600 and 590 <= shown["accuracy"]["k"] <= 600

    worse = registry.publish(_copy(tmp_path, "9.9.8", _worse))
    assert worse.status_code == 201, worse.text  # shown, never refused: it is the publisher's
    second = registry.client.get(f"/v1/packs/{INBOX.id}/9.9.8").json()["checked"]["regression"]
    assert second["compared_with"] == "9.9.7"
    assert second["regressions"] == ["destination accuracy fell by more than 3%"]
    assert second["accuracy"]["k"] < shown["accuracy"]["k"]
    # Not a figure anywhere a ranking would read one.
    figures = registry.client.get(f"/v1/packs/{INBOX.id}").json()["latest"]["figures"]
    assert figures["benchmark"] is None and figures["leaderboard"] is None


def test_a_pack_without_a_test_set_says_so(registry: Registry, tmp_path: Path) -> None:
    def no_test_set(root: Path) -> None:
        manifest = yaml.safe_load((root / "pack.yaml").read_text())
        del manifest["evaluation"]["benchmark_dataset"]
        manifest["evaluation"].pop("benchmark", None)
        (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    assert registry.publish(_copy(tmp_path, "9.9.7", no_test_set)).status_code == 201
    shown = registry.client.get(f"/v1/packs/{INBOX.id}/9.9.7").json()["checked"]["regression"]
    assert shown["status"] == "no_test_set" and "accuracy" not in shown


class FakeChild:
    """A child process as `subprocess.Popen` gives it: an answer, or none until it is killed."""

    started: list[dict[str, Any]] = []

    def __init__(self, args: list[str], answer: bytes | None, **options: Any) -> None:
        FakeChild.started.append({"args": args, **options})
        self.answer, self.killed = answer, threading.Event()
        self.stdout = self
        self.returncode: int | None = None

    def read(self, limit: int) -> bytes:
        if self.answer is None:
            self.killed.wait(10)
            return b""
        return self.answer[:limit]

    def close(self) -> None:
        pass

    def kill(self) -> None:
        self.killed.set()
        self.returncode = -9

    def wait(self) -> int:
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode


def _fake(monkeypatch: pytest.MonkeyPatch, answer: bytes | None) -> None:
    FakeChild.started.clear()
    monkeypatch.setattr(
        regression.subprocess, "Popen", lambda args, **o: FakeChild(args, answer, **o)
    )


def test_the_check_runs_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A child process with none of the parent's settings or credentials, no input, its errors
    dropped and its answer read to a bound; killed at the time limit."""
    monkeypatch.setenv("LLM_API_KEY", "a-credential-the-child-never-sees")
    monkeypatch.setenv("MENDARI_REGISTRY_ADMIN_TOKEN", "another")
    _fake(monkeypatch, b'{"status": "no_test_set"}')
    assert regression.check(tmp_path, None)["status"] == "no_test_set"
    (seen,) = FakeChild.started
    assert seen["args"][1:3] == ["-I", "-c"]
    # The parent's own import path, as code (a host may find packages only via PYTHONPATH,
    # which -I ignores); never a relative entry, so never the working directory.
    boot = seen["args"][3]
    assert "run_module('mendari.hub.regression', run_name='__main__'" in boot
    assert "''" not in boot.split("sys.path[:] = ")[1].split("; runpy")[0]
    assert set(seen["env"]) == {"PATH", "HOME", "MENDARI_CACHE_DIR", "MENDARI_CONFIG_DIR", "LANG"}
    assert seen["cwd"] != str(Path.cwd()) and "preexec_fn" not in seen
    assert seen["stdin"] == seen["stderr"] == subprocess.DEVNULL
    _fake(monkeypatch, b"x" * (regression.MAX_OUTPUT_BYTES + 10))
    assert regression.check(tmp_path, None)["reason"] == "the check did not finish"
    monkeypatch.setattr(regression, "TIMEOUT_S", 0.2)
    _fake(monkeypatch, None)
    assert regression.check(tmp_path, None) == {
        "format": regression.FORMAT,
        "label": regression.LABEL,
        "status": "failed",
        "reason": "time_limit",
    }


def _summary(accuracy: float, sha: str = "a" * 64) -> dict[str, Any]:
    return {
        "primary": "destination",
        "dataset": {"name": "test", "n": 600, "sha256": sha},
        "questions": {"destination": {"accuracy": accuracy, "n": 600}},
        "decisions": {"automation": {"false_automation_rate": None, "n": 0}, "critical": []},
    }


def _measured(monkeypatch: pytest.MonkeyPatch, accuracy: float, tolerance: float, sha: str) -> None:
    answer = {"status": "measured", "summary": _summary(accuracy, sha), "tolerance": tolerance}
    _fake(monkeypatch, json.dumps(answer).encode())


def test_the_margin_and_the_test_set_are_the_earlier_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The LP-18g review: a version could widen its own tolerance to hide a fall, or change to
    an easier test set and read as no regression."""
    before = {
        "status": "measured",
        "summary": _summary(0.99),
        "tolerance": 0.03,
        "version": "1.0.0",
    }
    _measured(monkeypatch, 0.90, 0.99, "a" * 64)  # a fall of 9 points, its own margin 99
    widened = regression.check(tmp_path, before)
    assert widened["regressions"] == ["destination accuracy fell by more than 3%"]
    _measured(monkeypatch, 0.995, 0.03, "b" * 64)
    changed = regression.check(tmp_path, before)
    assert changed["regressions"] == [
        "the test set is not the one 1.0.0 was checked on: not compared"
    ]
    _measured(monkeypatch, 0.99, 0.03, "a" * 64)
    broken = regression.check(
        tmp_path, {**before, "summary": {"dataset": {"sha256": "a" * 64}, "questions": []}}
    )
    assert broken["status"] == "measured"  # shown, never a refused publish
    assert broken["regressions"] == ["the two checks could not be compared"]


def test_the_baseline_is_the_newest_measured_check_recalled_or_not(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert registry.publish(_copy(tmp_path, "9.9.6")).status_code == 201

    def no_test_set(root: Path) -> None:
        manifest = yaml.safe_load((root / "pack.yaml").read_text())
        del manifest["evaluation"]["benchmark_dataset"]
        manifest["evaluation"].pop("benchmark", None)
        (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    assert registry.publish(_copy(tmp_path, "9.9.7", no_test_set)).status_code == 201
    worse = registry.publish(_copy(tmp_path, "9.9.8", _worse))
    assert worse.status_code == 201
    shown = registry.client.get(f"/v1/packs/{INBOX.id}/9.9.8").json()["checked"]["regression"]
    assert shown["compared_with"] == "9.9.6"  # not 9.9.7, which measured nothing
    assert shown["regressions"] == ["destination accuracy fell by more than 3%"]


def test_a_burst_of_publishes_is_told_to_try_again(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = [regression._SLOTS.acquire(blocking=False) for _ in range(regression.CONCURRENT)]
    try:
        busy = registry.publish(_copy(tmp_path, "9.9.6"))
        assert busy.status_code == 503 and "publish again shortly" in busy.text
    finally:
        for taken in held:
            if taken:
                regression._SLOTS.release()
    assert registry.publish(_copy(tmp_path / "second", "9.9.6")).status_code == 201
    _fake(monkeypatch, b'{"status": "no_test_set"}')
    again = registry.publish(_copy(tmp_path / "again", "9.9.6"))
    assert again.status_code == 409 and FakeChild.started == []  # refused before any check


def test_the_raw_check_is_never_served_beside_its_figure(
    registry: Registry, tmp_path: Path
) -> None:
    assert registry.publish(_copy(tmp_path, "9.9.6")).status_code == 201
    for version in (
        registry.client.get(f"/v1/packs/{INBOX.id}/9.9.6").json(),
        registry.client.get("/v1/search").json()[0],
    ):
        assert "regression" not in version
        assert "summary" not in version["checked"]["regression"]


def test_a_check_that_fails_names_only_the_errors_type(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    shutil.copytree(INBOX.root, root)
    (root / "data" / "test.jsonl").write_text('{"id": "x", "input": "Jane Doe chest pain"}\n')
    shown = regression.check(root, None)
    assert shown["status"] == "failed"
    assert "Jane" not in json.dumps(shown)


def test_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "off")
    assert regression.enabled() is False
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "sometimes")
    with pytest.raises(MendariError, match="on or off"):
        regression.enabled()


def test_a_setting_that_is_not_one_is_the_operators_never_the_packs(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry whose MENDARI_REGISTRY_REGRESSION is neither on nor off answers 503 with what
    the setting must be, before any work: never "the pack does not load", which sent the
    publisher to fix a pack with nothing wrong with it. Nothing is published."""
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "sometimes")
    refused = registry.publish(_copy(tmp_path, "9.9.7"))
    assert refused.status_code == 503, refused.text
    detail = refused.json()["detail"]
    assert "cannot check versions" in detail and "on or off" in detail
    assert "does not load" not in detail
    assert registry.client.get(f"/v1/packs/{INBOX.id}/9.9.7").status_code == 404
