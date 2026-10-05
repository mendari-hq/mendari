"""The repository is public: its working records (tasks/, notes/) and docs describe what was
decided, never how the project's own deployment is configured. History keeps the old values;
these patterns keep them from coming back into the tree."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Each is a deployment setting written down as a value, never a recommendation.
DEPLOYMENT_SPECIFICS = {
    "a spend limit turned off": re.compile(r"DAILY_LIMIT=none|daily_limit: null"),
    "an environment's own setting": re.compile(r"\([Pp]roduction(?: and [Pp]review)?\)"),
    "a limit raised on a deployment": re.compile(r"limit raised to \d+"),
    "a deployed model": re.compile(r"\b[A-Z_]*MODEL=[a-z]"),
    "the access setting": re.compile(r"ssoProtection|all_except_custom_domains"),
    "the open-mode probe": re.compile(r"sign_in: false|open-mode principal"),
}


def _tracked_text() -> dict[str, str]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    files: dict[str, str] = {}
    for name in listed.stdout.decode().split("\0"):
        if not name.endswith((".md", ".yml", ".yaml", ".toml", ".txt")):
            continue
        path = ROOT / name
        if path.is_file():
            files[name] = path.read_text(encoding="utf-8", errors="replace")
    return files


def test_no_deployment_setting_is_written_down() -> None:
    found = [
        f"{name}:{number}: {what}"
        for name, text in _tracked_text().items()
        for number, line in enumerate(text.splitlines(), 1)
        for what, pattern in DEPLOYMENT_SPECIFICS.items()
        if pattern.search(line)
    ]
    assert found == []


def test_the_patterns_find_what_was_scrubbed() -> None:
    scrubbed = [
        "- [x] `MENDARI_JEV_DAILY_LIMIT=none` (no limit, still counted)",
        "run evaluations with no daily limit (`daily_limit: null`)",
        "`LLM_MODEL=anthropic/claude-sonnet-5` and the limit off (production and",
        "MENDARI_PROVIDERS (Production and Preview) = `router=jev`",
        "- [x] Pin a concrete Jev model: `JEV_MODEL=jev-1.13.0` (v0.2).",
        "Jev daily limit raised to 2000 (production) with the user.",
        "project setting was `all_except_custom_domains`, which exempts",
        "(`/api/health`: `sign_in: false`; `/api/auth/me`: an open-mode principal)",
    ]
    for line in scrubbed:
        assert any(p.search(line) for p in DEPLOYMENT_SPECIFICS.values()), line
    advice = "(`MENDARI_JEV_DAILY_LIMIT=2000`, `MENDARI_LLM_DAILY_LIMIT=200` and"
    assert not any(p.search(advice) for p in DEPLOYMENT_SPECIFICS.values())
