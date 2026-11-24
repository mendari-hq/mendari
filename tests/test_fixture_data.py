# SPDX-License-Identifier: Apache-2.0
"""The fixture agents' data is generated, never written by hand and never a person's: each
committed file is what its generator writes (`tests/support/fixture_data.py`), byte for byte.
The bundled demo agent's data is held to its own script the same way
(`scripts/demo_agent_data.py --check`, in CI's `demo-agent` job)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import fixture_data

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("kind", sorted(fixture_data.KINDS))
def test_the_committed_data_is_what_its_generator_writes(kind: str, tmp_path: Path) -> None:
    _, agent, local = fixture_data.KINDS[kind]
    assert agent.is_dir() and local.is_file(), kind
    written = tmp_path / "local.jsonl"
    fixture_data.generate(kind, tmp_path / "agent", written)
    assert written.read_bytes() == local.read_bytes(), local.name
    splits = sorted((tmp_path / "agent" / "data").iterdir())
    assert splits, kind
    for split in splits:
        assert split.read_bytes() == (agent / "data" / split.name).read_bytes(), split.name


def test_every_fixture_agent_with_generated_data_has_a_kind() -> None:
    """One generator per fixture agent, and each kind names an agent and an example that exist."""
    agents = {agent for _, agent, _ in fixture_data.KINDS.values()}
    assert agents == {path for path in fixture_data.AGENTS.iterdir() if path.is_dir()}
    examples = {local for _, _, local in fixture_data.KINDS.values()}
    assert examples == set(fixture_data.DATASETS.glob("*.jsonl"))


def test_the_command_writes_where_it_is_told_and_runs_as_a_module(tmp_path: Path) -> None:
    """`python -m tests.support.fixture_data --kind <kind>`: the command a maintainer runs."""
    local = tmp_path / "out" / "local.jsonl"
    where = ["--agent-dir", str(tmp_path / "agent"), "--local-out", str(local)]
    ran = subprocess.run(
        [sys.executable, "-m", "tests.support.fixture_data", "--kind", "guard", *where],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stderr
    assert local.is_file() and (tmp_path / "agent" / "data" / "test.jsonl").is_file()
    assert f"-> {local}" in ran.stdout
    with pytest.raises(SystemExit):  # a kind is always named: nothing is written by default
        fixture_data.main([])
