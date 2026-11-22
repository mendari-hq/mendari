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
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from mendari.errors import HubError, MendariError

FORMAT = "mendari.regression/1"
LABEL = "publisher's own test set: regression check, not a ranking"
TIMEOUT_S = 180
MEMORY_BYTES = 2 * 1024**3
FILE_BYTES = 64 * 1024**2  # the largest file the child may write
MAX_OUTPUT_BYTES = 1_000_000
# How many checks run at once in one registry process; past it a publish is told to try again.
CONCURRENT = 2
_SLOTS = threading.BoundedSemaphore(CONCURRENT)


def enabled() -> bool:
    """`MENDARI_REGISTRY_REGRESSION`: `on` unless set, or `off` (a registry that does not run
    the check shows none)."""
    value = os.environ.get("MENDARI_REGISTRY_REGRESSION", "on").strip().lower() or "on"
    if value not in ("on", "off"):
        raise MendariError("MENDARI_REGISTRY_REGRESSION is on or off")
    return value == "on"


@contextlib.contextmanager
def slot() -> Iterator[None]:
    """One of `CONCURRENT` checks at once, or a 503 to try again: a check holds a worker for up
    to `TIMEOUT_S`, so a burst of publishes cannot hold them all."""
    if not _SLOTS.acquire(blocking=False):
        raise HubError("the registry is checking other versions: publish again shortly", 503)
    try:
        yield
    finally:
        _SLOTS.release()


def check(pack_dir: Path, previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """The check of the pack at `pack_dir`, gated against `previous` (the newest earlier
    version's measured check, with its version and whether it is recalled, or None). The gate
    uses the earlier check's tolerance, so a version cannot widen the margin it is held to, and
    a test set that changed is said to, not compared."""
    measured = _isolated(pack_dir)
    if measured.get("status") != "measured":
        return {"format": FORMAT, "label": LABEL, **measured}
    shown: dict[str, Any] = {
        "format": FORMAT,
        "label": LABEL,
        "status": "measured",
        "summary": measured.get("summary"),
        "tolerance": measured.get("tolerance"),
        "compared_with": None,
        "rows": [],
        "regressions": [],
    }
    if not isinstance(previous, Mapping) or previous.get("status") != "measured":
        return shown
    shown["compared_with"] = previous.get("version")
    shown["compared_with_recalled"] = bool(previous.get("recalled"))
    try:
        before, now = previous["summary"], measured["summary"]
        if before["dataset"].get("sha256") != now["dataset"].get("sha256"):
            shown["regressions"] = [
                f"the test set is not the one {previous.get('version')} was checked on: "
                "not compared"
            ]
            return shown
        tolerance = previous.get("tolerance")
        tolerance = float(tolerance if tolerance is not None else measured["tolerance"])
        from mendari.evals.gate import gate

        shown["rows"], shown["regressions"] = gate(now, before, tolerance)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        # The comparison failed on what the two checks hold: said so, never a refused publish.
        shown["regressions"] = ["the two checks could not be compared"]
    return shown


def rerun(pack_dir: Path, configuration: Mapping[str, str], dataset: str) -> dict[str, Any]:
    """The counts of the pack at `pack_dir` run on its dataset `dataset` with `configuration`
    (slot -> variant, every slot), in the isolated child: `{"status": "measured", "counts":
    ...}`, or `{"status": "failed", "reason": ...}` (`needs_remote` for a configuration that
    calls anything off the machine, which the child never runs)."""
    return _isolated(
        pack_dir, "--evaluation", dataset, json.dumps(dict(configuration), sort_keys=True)
    )


def _boot() -> str:
    """The child's first line: the parent's own import path, then this module as `__main__`.
    `-I` keeps the child from reading PYTHONPATH, user site-packages and the working
    directory, which is the point; but on a host that finds the installed packages through
    PYTHONPATH (Vercel's Python runtime) it then cannot import Mendari at all. So the child
    gets the parent's resolved path, absolute directories only, as code, never from its
    environment."""
    path = [entry for entry in sys.path if entry and os.path.isabs(entry)]
    return (
        "import runpy, sys; "
        f"sys.path[:] = {path!r}; "
        "runpy.run_module('mendari.hub.regression', run_name='__main__', alter_sys=True)"
    )


def _isolated(pack_dir: Path, *extra: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as home:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": home,
            "MENDARI_CACHE_DIR": str(Path(home) / "cache"),
            "MENDARI_CONFIG_DIR": str(Path(home) / "config"),
            "LANG": "C.UTF-8",
        }
        try:
            child = subprocess.Popen(
                [sys.executable, "-I", "-c", _boot(), str(pack_dir), *extra],
                env=env,
                cwd=home,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return {"status": "failed", "reason": "the check could not start"}
        try:
            # Its limits are its own (`_limits`, first thing in `_main`); this bounds the wait.
            killed = threading.Event()

            def stop() -> None:
                killed.set()
                child.kill()

            timer = threading.Timer(TIMEOUT_S, stop)
            timer.start()
            try:
                assert child.stdout is not None
                out = child.stdout.read(MAX_OUTPUT_BYTES + 1)
                child.stdout.close()
                code = child.wait()
            finally:
                timer.cancel()
            timed_out = killed.is_set()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    if timed_out:
        return {"status": "failed", "reason": "time_limit"}
    if code != 0 or len(out) > MAX_OUTPUT_BYTES:
        return {"status": "failed", "reason": "the check did not finish"}
    try:
        found = json.loads(out)
    except ValueError:
        return {"status": "failed", "reason": "the check's answer is not JSON"}
    return found if isinstance(found, dict) else {"status": "failed", "reason": "not a check"}


def _limits() -> None:
    """In the child, before the pack is loaded: its CPU time, the files it may write, and its
    memory where the system bounds it (not macOS)."""
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_S, TIMEOUT_S))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_BYTES, FILE_BYTES))
    with contextlib.suppress(ValueError, OSError):
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


def _rerun_main(pack_dir: str, dataset: str, configuration: str) -> dict[str, Any]:
    """In the child: a publisher's evaluation run again (`rerun`)."""
    from mendari.core.decision import Decision
    from mendari.core.pack import load_pack
    from mendari.evals.harness import evaluation, resolve_dataset
    from mendari.evals.share import counted_report

    pack = load_pack(Path(pack_dir), origin="hub")
    if dataset not in pack.manifest.evaluation.datasets:  # never a path: the pack's own sets
        return {"status": "failed", "reason": "no_such_dataset"}
    decision = Decision(pack, providers=json.loads(configuration), origin_cap=False)
    if decision.is_remote:
        return {"status": "needs_remote"}
    run = evaluation(decision, resolve_dataset(pack, dataset))
    return {"status": "measured", "counts": counted_report(run.ledger)}


def _main(argv: list[str]) -> int:
    """The child: evaluate the pack at argv[0] on its own test set, print the summary; or, with
    `--evaluation DATASET CONFIGURATION`, run a publisher's evaluation again (`rerun`)."""
    from mendari.core.decision import Decision
    from mendari.core.pack import load_pack
    from mendari.evals.harness import evaluation, resolve_dataset

    if os.name == "posix":
        _limits()  # before anything of the pack is read
    if len(argv) == 4 and argv[1] == "--evaluation":
        try:
            again = _rerun_main(argv[0], argv[2], argv[3])
        except Exception as error:  # the publisher's content: named by its type alone
            again = {"status": "failed", "reason": type(error).__name__}
        sys.stdout.write(json.dumps(again))
        return 0
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
