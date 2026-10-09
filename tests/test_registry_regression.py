# SPDX-License-Identifier: Apache-2.0
"""The registry's regression check on every publish (LP-18g, `hub/regression.py`): the pack gate
on the publisher's own test set, in an isolated job, shown as what it is."""

from __future__ import annotations

import json
import shutil
import subprocess
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


def _mislabel(root: Path) -> None:
    """A test set whose destinations are wrong for a third of its cases."""
    path = root / "data" / "test.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    labels = sorted({row["labels"]["destination"] for row in rows})
    for number, row in enumerate(rows):
        if number % 3 == 0:
            current = row["labels"]["destination"]
            row["labels"]["destination"] = next(label for label in labels if label != current)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


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

    worse = registry.publish(_copy(tmp_path, "9.9.8", _mislabel))
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


def test_the_check_runs_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A child process with none of the parent's settings or credentials, limited in time."""
    monkeypatch.setenv("LLM_API_KEY", "a-credential-the-child-never-sees")
    monkeypatch.setenv("MENDARI_REGISTRY_ADMIN_TOKEN", "another")
    seen: dict[str, Any] = {}

    def run(args: list[str], **options: Any) -> Any:
        seen.update(options, args=args)
        return subprocess.CompletedProcess(args, 0, stdout=b'{"status": "no_test_set"}')

    monkeypatch.setattr(regression.subprocess, "run", run)
    assert regression.check(tmp_path, None)["status"] == "no_test_set"
    assert seen["args"][1:4] == ["-I", "-m", "mendari.hub.regression"]
    assert set(seen["env"]) == {"PATH", "HOME", "MENDARI_CACHE_DIR", "MENDARI_CONFIG_DIR", "LANG"}
    assert seen["timeout"] == regression.TIMEOUT_S and seen["cwd"] != str(Path.cwd())

    def slow(args: list[str], **options: Any) -> Any:
        raise subprocess.TimeoutExpired(args, regression.TIMEOUT_S)

    monkeypatch.setattr(regression.subprocess, "run", slow)
    assert regression.check(tmp_path, None) == {
        "format": regression.FORMAT,
        "label": regression.LABEL,
        "status": "failed",
        "reason": "time_limit",
    }


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
