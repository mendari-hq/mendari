# SPDX-License-Identifier: Apache-2.0
"""The fixture agents' synthetic data, written from their generators: each agent's public splits
(`data/<split>.jsonl`) and its local-site example. No real PHI: every case is generated, and the
committed files are what the generators write (the fixture agents' own tests hold them to it).

    uv run python -m tests.support.fixture_data --kind inbox

It writes into the repository's fixtures unless told where (`--agent-dir`, `--local-out`). The
bundled demo agent's data has its own script, `scripts/demo_agent_data.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from tests.support import (
    synthetic_guard,
    synthetic_inbox,
    synthetic_prior_auth,
    synthetic_referral,
    synthetic_trial,
)

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "tests" / "fixtures" / "reference-agents"
DATASETS = ROOT / "tests" / "fixtures" / "datasets"
# kind: (its generator, its agent's folder, its local-site example).
KINDS: dict[str, tuple[ModuleType, Path, Path]] = {
    "inbox": (
        synthetic_inbox,
        AGENTS / "patient-inbox-router",
        DATASETS / "local-site-example.jsonl",
    ),
    "trial": (
        synthetic_trial,
        AGENTS / "clinical-trial-prescreener",
        DATASETS / "trial-local-site-example.jsonl",
    ),
    "referral": (
        synthetic_referral,
        AGENTS / "referral-router",
        DATASETS / "referral-local-site-example.jsonl",
    ),
    "prior-auth": (
        synthetic_prior_auth,
        AGENTS / "prior-auth-preflight",
        DATASETS / "prior-auth-local-site-example.jsonl",
    ),
    "guard": (synthetic_guard, AGENTS / "ai-guard", DATASETS / "guard-local-site-example.jsonl"),
}


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def generate(kind: str, agent_dir: Path | None = None, local_out: Path | None = None) -> None:
    """Write the kind's public splits under `agent_dir`'s `data/` and its local-site example to
    `local_out` (default: the fixtures'). It takes a folder, not an agent: an agent cannot load
    before its data exists."""
    generator, default_dir, default_local = KINDS[kind]
    data_dir = (agent_dir or default_dir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for split, records in generator.public_splits().items():
        write_jsonl(data_dir / f"{split}.jsonl", records)
        print(f"{len(records):>5} -> {data_dir / f'{split}.jsonl'}")
    local = generator.local_site()
    local_path = local_out or default_local
    local_path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(local_path, local)
    print(f"{len(local):>5} -> {local_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--kind", choices=sorted(KINDS), required=True)
    parser.add_argument("--agent-dir", type=Path, help="default: the kind's fixture agent")
    parser.add_argument("--local-out", type=Path, help="default: the kind's local-site example")
    args = parser.parse_args(argv)
    generate(args.kind, args.agent_dir, args.local_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
