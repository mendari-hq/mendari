from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from openhealth import Decision, Pack, load_pack

ROOT = Path(__file__).resolve().parents[1]
INBOX_DIR = ROOT / "packs" / "patient-inbox-router"
LOCAL_DATASET = ROOT / "datasets" / "local-site-example.jsonl"


@pytest.fixture(scope="session")
def inbox_pack() -> Pack:
    return load_pack(INBOX_DIR)


@pytest.fixture(scope="session")
def router(inbox_pack: Pack) -> Decision:
    return Decision(inbox_pack)


@pytest.fixture
def pack_copy(tmp_path: Path) -> Callable[..., Path]:
    """Copy the reference pack and apply edits to pack.yaml / policy.yaml before loading."""

    def make(
        manifest: Callable[[dict[str, Any]], None] | None = None,
        policy: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        target = tmp_path / "pack"
        shutil.copytree(INBOX_DIR, target, ignore=shutil.ignore_patterns("data"))
        (target / "data").mkdir()
        for split in ("train", "calibration", "test"):
            # Tiny stand-ins: most tests do not need the real splits.
            source = (INBOX_DIR / "data" / f"{split}.jsonl").read_text().splitlines()[:20]
            (target / "data" / f"{split}.jsonl").write_text("\n".join(source) + "\n")
        for name, edit in (("pack.yaml", manifest), ("policy.yaml", policy)):
            if edit is None:
                continue
            path = target / name
            data = yaml.safe_load(path.read_text())
            edit(data)
            path.write_text(yaml.safe_dump(data, sort_keys=False))
        return target

    return make


def message(body: str, **extra: Any) -> dict[str, Any]:
    return {"message": {"body": body, **extra}}
