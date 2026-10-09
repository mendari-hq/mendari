# SPDX-License-Identifier: Apache-2.0
"""The registry's regression check on every publish (LP-18g, ADR-0012).

The registry evaluates a version on the publisher's own test set (its manifest's
`evaluation.benchmark_dataset`, a file the pack digest covers) and runs the pack gate
(`evals/gate.py`, as `scripts/pack_gate.py` does in CI) against the newest earlier version's
check. It is what the registry computed itself, so it may be shown, labelled for what it is:
**publisher's own test set: regression check, not a ranking**. A set the publisher chose cannot
rank a pack; only the scoreboard (LP-17) does.

It runs in an isolated job: a child process (`python -I -m mendari.hub.regression DIR`) with an
environment holding no credential or setting but its own, a wall-clock limit, a CPU limit and,
where the system allows, a memory limit; the pack is loaded as a pack from a registry (on its
engine, ADR-0009) and decided by local providers only. A pack whose default providers call a
model is not run (`needs_remote`), nor one with no test set (`no_test_set`); one whose check
fails says so (`failed`), with the error's type only. A regression is shown, never refused: the
version is the publisher's.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FORMAT = "mendari.regression/1"
LABEL = "publisher's own test set: regression check, not a ranking"
TIMEOUT_S = 180
MEMORY_BYTES = 2 * 1024**3
MAX_OUTPUT_BYTES = 1_000_000


def enabled() -> bool:
    """`MENDARI_REGISTRY_REGRESSION`: `on` unless set, or `off` (a registry that does not run
    the check shows none)."""
    value = os.environ.get("MENDARI_REGISTRY_REGRESSION", "on").strip().lower() or "on"
    if value not in ("on", "off"):
        from mendari.errors import MendariError

        raise MendariError("MENDARI_REGISTRY_REGRESSION is on or off")
    return value == "on"


def check(pack_dir: Path, previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """The check of the pack at `pack_dir`, gated against `previous` (the newest earlier
    version's check, or None)."""
    measured = _isolated(pack_dir)
    if measured.get("status") != "measured":
        return {"format": FORMAT, "label": LABEL, **measured}
    base = None
    if isinstance(previous, Mapping) and previous.get("status") == "measured":
        base = previous.get("summary")
    from mendari.evals.gate import gate

    rows, failures = gate(measured["summary"], base, float(measured["tolerance"]))
    return {
        "format": FORMAT,
        "label": LABEL,
        "status": "measured",
        "summary": measured["summary"],
        "compared_with": None if base is None or previous is None else previous.get("version"),
        "rows": rows,
        "regressions": failures,
    }


def _isolated(pack_dir: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as home:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": home,
            "MENDARI_CACHE_DIR": str(Path(home) / "cache"),
            "MENDARI_CONFIG_DIR": str(Path(home) / "config"),
            "LANG": "C.UTF-8",
        }
        try:
            done = subprocess.run(
                [sys.executable, "-I", "-m", "mendari.hub.regression", str(pack_dir)],
                env=env,
                cwd=home,
                capture_output=True,
                timeout=TIMEOUT_S,
                preexec_fn=_limits if os.name == "posix" else None,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"status": "failed", "reason": "time_limit"}
    if done.returncode != 0 or len(done.stdout) > MAX_OUTPUT_BYTES:
        return {"status": "failed", "reason": "the check did not finish"}
    try:
        found = json.loads(done.stdout)
    except ValueError:
        return {"status": "failed", "reason": "the check's answer is not JSON"}
    return found if isinstance(found, dict) else {"status": "failed", "reason": "not a check"}


def _limits() -> None:
    """In the child, before it runs: its CPU time, and its memory where the system bounds it."""
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_S, TIMEOUT_S))
    with contextlib.suppress(ValueError, OSError):  # macOS does not bound it
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))


def summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """What the gate reads of an evaluation report (`mendari.eval/2`), and nothing else: the
    test set's name, size and hash, the primary question's accuracy over n, the false
    automation rate over the automated, and each critical label's detection."""
    primary = str(report["primary"])
    question = report["questions"][primary]
    automation = report["decisions"]["automation"]
    return {
        "primary": primary,
        "dataset": {k: report["dataset"].get(k) for k in ("name", "n", "sha256")},
        "questions": {primary: {"accuracy": question.get("accuracy"), "n": question.get("n")}},
        "decisions": {
            "automation": {
                "false_automation_rate": automation.get("false_automation_rate"),
                "n": automation.get("n"),
            },
            "critical": [
                {
                    k: item.get(k)
                    for k in ("question", "label", "detection_rate", "n", "auto_missed")
                }
                for item in report["decisions"].get("critical", [])
            ],
        },
    }


def _main(argv: list[str]) -> int:
    """The child: evaluate the pack at argv[0] on its own test set, print the summary."""
    from mendari.core.decision import Decision
    from mendari.core.pack import load_pack
    from mendari.evals.harness import evaluation, resolve_dataset

    try:
        pack = load_pack(Path(argv[0]), origin="hub")
        test_set = pack.manifest.evaluation.benchmark_dataset
        if not test_set:
            answer: dict[str, Any] = {"status": "no_test_set"}
        else:
            decision = Decision(pack, origin_cap=False)  # the pack's own policy, measured
            if decision.is_remote:
                answer = {"status": "needs_remote"}
            else:
                report = evaluation(decision, resolve_dataset(pack, test_set)).report
                answer = {
                    "status": "measured",
                    "summary": summary(report),
                    "tolerance": pack.manifest.evaluation.degradation_tolerance,
                }
    except Exception as error:  # the publisher's content: named by its type alone
        answer = {"status": "failed", "reason": type(error).__name__}
    sys.stdout.write(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
