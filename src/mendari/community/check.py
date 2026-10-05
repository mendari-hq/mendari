"""`mendari community-check`: what CI enforces on a community pack, before anyone reviews it.

One entry point for the command line and CI (`scripts/community_check.py`, the `pack-check` job
of the `packs` workflow). Each rule says, in plain words, what to fix:

- **scope**: the change touches only `community-packs/<name>/` (and `MAINTAINERS.yaml`, for
  that pack's own entry); no `pack.yaml` below the pack's own.
- **identity**: the folder name and the id `community/<name>` agree; the id is not taken by
  another pack, nor removed (a tombstone in `MAINTAINERS.yaml`); the pack has maintainers; its
  terminology (FHIR CodeSystem ids and urls) collides with no other pack's.
- **manifest**: licence Apache-2.0, `publisher.type: community`, `audit.store_input: hash`,
  local default providers, no pinned model, `degradation_tolerance` at most 0.05, risk level at
  most 1.
- **correctness**: the pack validates and passes its behaviour tests, with at least one test
  case for each critical label.
- **evaluation**: a benchmark dataset with at least 5 cases of each critical label (at least
  one is declared); no critical case automated and each critical label found at least 90% of
  the time; an update does not regress against the base's benchmark, within the base's
  tolerance (never the change's own).
- **honest-numbers**: the committed benchmark and Decision Card are what the pack gives when
  evaluated again (ignoring the report's id, time and latency), and a trained model is what
  `mendari train` gives, byte for byte.
- **patterns**: no pattern that may backtrack for seconds (`regex_safety`).
- **hygiene**: at most 2 MB and 200 files, regular text files only (a trained model aside),
  no files in `evals/` but the benchmark, and a `CHANGELOG.md` entry for the version; a change
  to what the pack decides with raises the version.
- **phi**: data and tests that look like a real person's details (warnings).

The limits are constants here, so a pull request cannot set its own bar: the scope rule keeps
a community pull request from changing this file.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from mendari.community import maintainers, phi_scan, regex_safety
from mendari.community.gitbase import COMMUNITY_DIR, Base, toplevel
from mendari.core.origin import (
    COMMUNITY_NAME,
    COMMUNITY_NAMESPACE,
    MAX_COMMUNITY_RISK,
    is_local,
)
from mendari.core.pack import MANIFEST, Pack, authored_patterns, load_pack, pack_digest, parse_yaml
from mendari.core.spec import Evaluation
from mendari.errors import MendariError, PackValidationError

MAX_TOLERANCE = 0.05
MIN_CRITICAL_CASES = 5
MIN_CRITICAL_DETECTION = 0.9
MAX_PACK_BYTES = 2 * 1024 * 1024
MAX_PACK_FILES = 200
# Fields of an evaluation report that differ on every run.
VOLATILE_REPORT_KEYS = ("id", "created_at", "latency_ms")
CHANGELOG = "CHANGELOG.md"
CARD = "DECISION_CARD.md"
SHOWN = 10  # at most this many paths in one message


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    warning: bool = False


@dataclass
class Report:
    """What `check` found for one pack folder."""

    folder: str
    pack_id: str | None = None
    findings: list[Finding] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)
    metrics: str | None = None  # the metrics table (Markdown), from the eval gate
    card: str | None = None  # the Decision Card the pack renders

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if not f.warning]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.warning]

    @property
    def passed(self) -> bool:
        return not self.errors

    def error(self, rule: str, message: str) -> None:
        self.findings.append(Finding(rule, message))

    def warn(self, rule: str, message: str) -> None:
        self.findings.append(Finding(rule, message, warning=True))

    def text(self) -> str:
        title = self.folder + (f" ({self.pack_id})" if self.pack_id else "")
        lines = [f"community-check {title}"]
        lines += [f"  error    [{f.rule}] {f.message}" for f in self.errors]
        lines += [f"  warning  [{f.rule}] {f.message}" for f in self.warnings]
        lines += [f"  not checked: {item}" for item in self.not_checked]
        if self.passed:
            extra = f"; look at the {len(self.warnings)} warning(s)" if self.warnings else ""
            lines.append(f"passed: ready for review{extra}")
        else:
            lines.append(f"{len(self.errors)} error(s): fix them, then run this again")
        return "\n".join(lines)

    def markdown(self) -> str:
        verdict = "passed" if self.passed else "failed"
        lines = [f"## `{self.pack_id or self.folder}`: community-check {verdict}", ""]
        lines += [f"- **Error** ({f.rule}): {_inline(f.message)}" for f in self.errors]
        lines += [f"- Warning ({f.rule}): {_inline(f.message)}" for f in self.warnings]
        lines += [f"- Not checked: {_inline(item)}" for item in self.not_checked]
        if self.passed and not self.warnings:
            lines.append("Every check passed.")
        lines.append("")
        if self.metrics:
            lines += [self.metrics, ""]
        if self.card:
            lines += ["<details><summary>Decision Card</summary>", "", self.card, "", "</details>"]
        return "\n".join(lines) + "\n"


def _inline(text: str) -> str:
    return text.replace("\n", " ")


@dataclass
class _Context:
    """What the rules share about one pack folder."""

    root: Path  # the pack folder
    report: Report
    base: Base | None = None
    folder: str = ""  # the pack folder, relative to the repository (git paths)
    maintainers_path: str = ""  # MAINTAINERS.yaml, relative to the repository
    pack: Pack | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # pack.yaml as written
    owner: bool = False

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def community_dir(self) -> Path:
        return self.root.parent

    @property
    def shown(self) -> str:
        return self.report.folder


def check(
    pack_dir: str | Path, *, base: str | None = None, scope: bool = True, owner: bool = False
) -> Report:
    """Every community-pack rule on the folder `pack_dir` (`community-packs/<name>`). With
    `base` (a git commit), the change since it is checked too: `scope` (the files it touches)
    and the update rules. `owner`: the repository's owner made the change, who may edit
    `MAINTAINERS.yaml` beyond the pack's own entry."""
    root = Path(pack_dir).resolve()
    report = Report(folder=_display(pack_dir))
    ctx = _Context(root=root, report=report, owner=owner)
    if root.parent.name != COMMUNITY_DIR:
        report.error(
            "scope",
            f"a community pack lives in {COMMUNITY_DIR}/<name>/; move this folder there",
        )
    if not (root / MANIFEST).is_file():
        report.error("correctness", f"there is no {MANIFEST} in {report.folder}")
        return report
    if base is not None:
        _open_base(ctx, base)
    else:
        report.not_checked.append(
            "the change itself (scope, version bump, regressions): pass --base <commit>, "
            "e.g. --base origin/main"
        )
    _hygiene_files(ctx)
    _nested_manifests(ctx)
    if scope and ctx.base is not None:
        _scope(ctx)
    _load(ctx)
    _identity(ctx)
    _phi(ctx)
    if ctx.pack is None:
        return report
    _manifest_policy(ctx)
    _changelog(ctx)
    _patterns(ctx)
    if not _local_defaults(ctx.pack):
        # Deciding would call a service, at whoever runs this check's expense.
        report.not_checked.append(
            "behaviour tests, evaluation and benchmark: they run once the default providers "
            "are local"
        )
        return report
    if _correctness(ctx) is not None:
        _evaluation(ctx)
    _retrained(ctx)
    return report


def _local_defaults(pack: Pack) -> bool:
    return all(
        is_local(
            {"type": slot.variants[slot.use].type, **(slot.variants[slot.use].model_extra or {})}
        )
        for slot in pack.manifest.providers.values()
    )


def _display(pack_dir: str | Path) -> str:
    path = Path(pack_dir)
    try:
        return path.resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def _open_base(ctx: _Context, ref: str) -> None:
    top = toplevel(ctx.root)
    if top is None:
        ctx.report.error("scope", "--base needs the pack inside a git repository")
        return
    try:
        ctx.base = Base(top, ref)
    except MendariError as error:
        ctx.report.error("scope", f"cannot read the base {ref!r}: {error}")
        return
    ctx.folder = ctx.root.relative_to(top.resolve()).as_posix()
    ctx.maintainers_path = (
        (ctx.community_dir / maintainers.FILE).relative_to(top.resolve()).as_posix()
    )


# -- scope ---------------------------------------------------------------------------------------


def pr_scope_problems(changed: list[str], *, owner: bool) -> list[str]:
    """Why a pull request that changes `changed` (repository paths) is out of scope for a
    community pack, or []: it changes a community pack (or `MAINTAINERS.yaml`) and anything
    outside one `community-packs/<name>/` folder (`MAINTAINERS.yaml` aside). The owner may
    change more."""
    prefix = COMMUNITY_DIR + "/"
    community = [
        path
        for path in changed
        if path.startswith(prefix) and (path.count("/") >= 2 or path == prefix + maintainers.FILE)
    ]
    if owner or not community:
        return []  # the owner's change, or none to a pack (community-packs/README.md is docs)
    folders = sorted({path.split("/")[1] for path in community if path.count("/") >= 2})
    outside = [
        path
        for path in changed
        if not path.startswith(prefix)
        or (path.count("/") == 1 and path != prefix + maintainers.FILE)
    ]
    problems = []
    if len(folders) > 1:
        problems.append(
            f"this pull request changes {len(folders)} community packs ({', '.join(folders)}): "
            "change one pack per pull request"
        )
    if not folders and prefix + maintainers.FILE in changed:
        problems.append(
            f"{prefix}{maintainers.FILE} changes only together with the pack it lists; the "
            "repository's owner makes other changes to it"
        )
    if outside:
        problems.append(
            "a community pack's pull request changes only its own folder "
            f"({prefix}<name>/, and {prefix}{maintainers.FILE}); open a separate pull request "
            f"for: {_listed(outside)}"
        )
    return problems


def _listed(paths: list[str]) -> str:
    shown = ", ".join(paths[:SHOWN])
    return shown + (f" and {len(paths) - SHOWN} more" if len(paths) > SHOWN else "")


def _scope(ctx: _Context) -> None:
    assert ctx.base is not None
    try:
        changed = ctx.base.changed()
    except MendariError as error:
        ctx.report.error("scope", f"cannot list the files this change touches: {error}")
        return
    inside = ctx.folder + "/"
    outside = [p for p in changed if not p.startswith(inside) and p != ctx.maintainers_path]
    if outside:
        ctx.report.error(
            "scope",
            f"this change also touches files outside {inside}: {_listed(outside)}. A community "
            f"pack's pull request changes only its own folder (and its entry in "
            f"{COMMUNITY_DIR}/{maintainers.FILE}); open a separate pull request for the rest",
        )


def _nested_manifests(ctx: _Context) -> None:
    nested = sorted(
        path.relative_to(ctx.root).as_posix()
        for path in ctx.root.rglob(MANIFEST)
        if path.parent != ctx.root
    )
    if nested:
        ctx.report.error(
            "scope",
            f"a community pack has one {MANIFEST}, at the top of its folder; remove "
            f"{_listed(nested)} (one pack per folder in {COMMUNITY_DIR}/)",
        )


# -- loading and identity ------------------------------------------------------------------------


def _load(ctx: _Context) -> None:
    try:
        raw = parse_yaml((ctx.root / MANIFEST).read_bytes(), MANIFEST, ctx.shown)
    except (OSError, MendariError) as error:
        ctx.report.error("correctness", f"{MANIFEST} cannot be read: {error}")
        return
    ctx.raw = raw if isinstance(raw, dict) else {}
    if isinstance(ctx.raw.get("id"), str):
        ctx.report.pack_id = ctx.raw["id"]
    try:
        ctx.pack = load_pack(ctx.root, origin="community")
    except PackValidationError as error:
        for problem in error.problems:
            ctx.report.error("correctness", f"the pack does not load: {problem}")
    except MendariError as error:
        ctx.report.error("correctness", f"the pack does not load: {error}")
    except Exception as error:  # the pack's content: named by type, as the registry does
        ctx.report.error("correctness", f"the pack does not load ({type(error).__name__})")


def _identity(ctx: _Context) -> None:
    expected = f"{COMMUNITY_NAMESPACE}/{ctx.name}"
    if not COMMUNITY_NAME.match(ctx.name):
        ctx.report.error(
            "identity",
            f"the folder name {ctx.name!r} must be lowercase letters, digits and dashes, "
            "starting with a letter or digit",
        )
    pack_id = ctx.raw.get("id")
    if pack_id != expected:
        ctx.report.error(
            "identity",
            f"the id is {pack_id!r}; a pack in {COMMUNITY_DIR}/{ctx.name}/ has the id {expected}",
        )
    _maintainers(ctx, expected)
    _unique(ctx, expected)
    if ctx.pack is not None and pack_id == expected:
        _terminology(ctx)


def _read_maintainers(ctx: _Context) -> dict[str, maintainers.Entry] | None:
    path = ctx.community_dir / maintainers.FILE
    if not path.is_file():
        ctx.report.error(
            "identity",
            f"{COMMUNITY_DIR}/{maintainers.FILE} is missing; it lists every community pack's "
            "maintainers",
        )
        return None
    try:
        return maintainers.parse(path.read_bytes(), f"{COMMUNITY_DIR}/{maintainers.FILE}")
    except MendariError as error:
        ctx.report.error("identity", str(error))
        return None


def _maintainers(ctx: _Context, pack_id: str) -> None:
    head = _read_maintainers(ctx)
    base_entries: dict[str, maintainers.Entry] = {}
    if ctx.base is not None:
        try:
            data = ctx.base.read(ctx.maintainers_path)
            if data is not None:
                base_entries = maintainers.parse(data, f"the base's {maintainers.FILE}")
        except MendariError as error:
            ctx.report.error("identity", str(error))
    tombstone = next(
        (
            entry
            for entries in (base_entries, head or {})
            if (entry := entries.get(pack_id)) is not None and entry.tombstone
        ),
        None,
    )
    if tombstone is not None:
        ctx.report.error(
            "identity",
            f"{pack_id} was removed on {tombstone.removed}, and a removed id is never used again "
            f"(approvals and audit records key on it): choose another name",
        )
    elif head is not None and pack_id not in head:
        ctx.report.error(
            "identity",
            f"add an entry for {pack_id} to {COMMUNITY_DIR}/{maintainers.FILE}, with the GitHub "
            "user names of the people who will answer for it: "
            f"`- {{id: {pack_id}, maintainers: [your-github-name]}}`",
        )
    if ctx.base is None or ctx.owner or head is None:
        return
    changed = sorted(
        entry_id
        for entry_id in set(base_entries) | set(head)
        if entry_id != pack_id and base_entries.get(entry_id) != head.get(entry_id)
    )
    if changed:
        ctx.report.error(
            "scope",
            f"this change edits other packs' entries in {maintainers.FILE} ({_listed(changed)}); "
            "change only your pack's entry",
        )


def _unique(ctx: _Context, pack_id: str) -> None:
    claims: list[str] = []
    for other in sorted(ctx.community_dir.glob(f"*/{MANIFEST}")):
        if other.parent.resolve() == ctx.root:
            continue
        try:
            raw = parse_yaml(other.read_bytes(), MANIFEST, str(other))
        except (OSError, MendariError):
            continue
        if isinstance(raw, dict) and raw.get("id") == pack_id:
            claims.append(f"{COMMUNITY_DIR}/{other.parent.name}/")
    claims += [
        str(pack.root) for pack in _maintained(str(_maintained_dir(ctx))) if pack.id == pack_id
    ]
    if claims:
        ctx.report.error(
            "identity", f"the id {pack_id} is already used by {_listed(claims)}: choose another"
        )


def _maintained_dir(ctx: _Context) -> Path:
    from mendari.core import registry

    own = ctx.community_dir.parent / "packs"
    if own.is_dir():
        return own
    return next((path for path in registry._BUNDLED_DIRS if path.is_dir()), own)


@cache
def _maintained(directory: str) -> tuple[Pack, ...]:
    """The maintained packs in `directory` (they do not change while a process runs)."""
    from mendari.core.registry import SearchPath, discover

    if not Path(directory).is_dir():
        return ()
    return tuple(discover([SearchPath(Path(directory), "maintained")]).packs)


def _terminology(ctx: _Context) -> None:
    from mendari.core.registry import SearchPath, discover
    from mendari.standards import terminology

    assert ctx.pack is not None
    community = discover([SearchPath(ctx.community_dir, "community")]).packs
    others = [
        *_maintained(str(_maintained_dir(ctx))),
        *(pack for pack in community if pack.root != ctx.root),
    ]
    try:
        terminology.resources(others)
    except Exception:
        ctx.report.not_checked.append(
            "terminology collisions: the packs already in the repository collide with each other"
        )
        return
    try:
        terminology.resources([*others, ctx.pack])
    except MendariError as error:
        ctx.report.error(
            "identity",
            f"the pack's FHIR terminology collides with another pack's: {error}. Give its "
            "systems (e.g. a performer system) their own urls",
        )
    except Exception as error:
        ctx.report.error("identity", f"the pack's FHIR terminology cannot be built ({error})")


# -- the manifest --------------------------------------------------------------------------------


def _variant_configs(pack: Pack) -> Iterator[tuple[str, dict[str, Any]]]:
    """(where, config) for every provider variant and composite member."""
    for slot_name, slot in pack.manifest.providers.items():
        for name, variant in slot.variants.items():
            config = {"type": variant.type, **(variant.model_extra or {})}
            yield f"providers.{slot_name}.variants.{name}", config
            members = config.get("members")
            if isinstance(members, dict):
                for member_name, member in members.items():
                    if isinstance(member, dict):
                        yield f"providers.{slot_name}.variants.{name}.members.{member_name}", member


def _manifest_policy(ctx: _Context) -> None:
    assert ctx.pack is not None
    m = ctx.pack.manifest
    fail = ctx.report.error
    if m.license != "Apache-2.0":
        fail("manifest", f"license is {m.license!r}; a community pack is `license: Apache-2.0`")
    if m.publisher.type != "community":
        fail(
            "manifest",
            f"publisher.type is {m.publisher.type!r}; a community pack says `type: community`",
        )
    if m.audit.store_input != "hash":
        fail(
            "manifest",
            "a community pack keeps only a hash of its inputs: set `audit: {store_input: hash}`",
        )
    for slot_name, slot in m.providers.items():
        variant = slot.variants[slot.use]
        if not is_local({"type": variant.type, **(variant.model_extra or {})}):
            fail(
                "manifest",
                f"providers.{slot_name}: the default variant {slot.use!r} calls a service; make "
                "the default a local one (rules, criteria, dosing, naive_bayes, or a composite "
                "of those). The deployment can opt a pack in to others by name",
            )
    for where, config in _variant_configs(ctx.pack):
        if "model" in config:
            fail(
                "manifest",
                f"{where}: remove `model`; the deployment chooses which model a community pack "
                "uses",
            )
    tolerance = m.evaluation.degradation_tolerance
    if tolerance > MAX_TOLERANCE:
        fail(
            "manifest",
            f"evaluation.degradation_tolerance is {tolerance}; a community pack's is at most "
            f"{MAX_TOLERANCE}",
        )
    if m.risk.level > MAX_COMMUNITY_RISK:
        fail(
            "manifest",
            f"risk level {int(m.risk.level)}: a community pack is at most risk level "
            f"{int(MAX_COMMUNITY_RISK)} (clinical information). Higher levels need a clinical "
            "review the pull-request route does not have yet",
        )


# -- correctness ---------------------------------------------------------------------------------


def _correctness(ctx: _Context) -> bool | None:
    """Validate and run the behaviour tests; True if they pass, None if they could not run."""
    from mendari.core.decision import Decision
    from mendari.evals.pack_tests import load_cases, run_pack_tests, validate_pack

    assert ctx.pack is not None
    pack = ctx.pack
    try:
        validate_pack(pack)
    except PackValidationError as error:
        for problem in error.problems:
            ctx.report.error("correctness", f"`mendari validate`: {problem}")
    except MendariError as error:
        ctx.report.error("correctness", f"`mendari validate`: {error}")
    if pack.manifest.tests is None:
        ctx.report.error(
            "correctness",
            "add behaviour tests: `tests: tests/cases.yaml`, with a case for each critical label",
        )
        return None
    try:
        cases = load_cases(pack)
        outcomes = run_pack_tests(Decision(pack))
    except MendariError as error:
        ctx.report.error("correctness", f"the behaviour tests cannot run: {error}")
        return None
    failed = [outcome for outcome in outcomes if not outcome.passed]
    for outcome in failed:
        ctx.report.error(
            "correctness",
            f"behaviour test {outcome.name!r} fails: {'; '.join(outcome.failures)}",
        )
    primary = pack.manifest.policy.primary
    for critical in pack.manifest.evaluation.critical:
        if not any(
            _tests_label(case, critical.question, critical.label, primary) for case in cases
        ):
            ctx.report.error(
                "correctness",
                f"add a behaviour test for the critical label {critical.question}="
                f"{critical.label}: a case with `expect: {{answers: {{{critical.question}: "
                f"{critical.label}}}}}`",
            )
    return not failed


def _tests_label(case: Mapping[str, Any], question: str, label: str, primary: str) -> bool:
    expect = case.get("expect") or {}
    if (expect.get("answers") or {}).get(question) == label:
        return True
    return question == primary and expect.get("recommendation") == label


# -- evaluation and honest numbers -------------------------------------------------------------


def _evaluation(ctx: _Context) -> None:
    from mendari.core.decision import Decision
    from mendari.evals import evaluate, resolve_dataset
    from mendari.evals.gate import gate, markdown

    assert ctx.pack is not None
    pack = ctx.pack
    evaluation = pack.manifest.evaluation
    fail = ctx.report.error
    if not evaluation.critical:
        fail(
            "evaluation",
            "declare at least one critical label under `evaluation.critical`: a "
            "(question, label) whose cases must never be missed or automated",
        )
    if evaluation.benchmark_dataset is None or evaluation.benchmark is None:
        fail(
            "evaluation",
            "set `evaluation.benchmark_dataset` (a dataset in `evaluation.datasets`) and "
            "`evaluation.benchmark: evals/benchmark.json`, the report CI checks",
        )
        return
    try:
        dataset = resolve_dataset(pack, evaluation.benchmark_dataset)
    except MendariError as error:
        fail("evaluation", f"the benchmark dataset cannot be read: {error}")
        return
    for critical in evaluation.critical:
        count = sum(
            record.get("labels", {}).get(critical.question) == critical.label
            for record in dataset.records
        )
        if count < MIN_CRITICAL_CASES:
            fail(
                "evaluation",
                f"the benchmark dataset has {count} case(s) of the critical label "
                f"{critical.question}={critical.label}; add at least {MIN_CRITICAL_CASES}",
            )
    try:
        report = evaluate(Decision(pack), dataset)
    except MendariError as error:
        fail("evaluation", f"the pack cannot be evaluated: {error}")
        return
    for item in report["decisions"]["critical"]:
        name = f"{item['question']}={item['label']}"
        if item["auto_missed"]:
            fail("evaluation", f"{item['auto_missed']} critical case(s) of {name} were automated")
        rate = item["detection_rate"]
        if rate is not None and rate < MIN_CRITICAL_DETECTION:
            fail(
                "evaluation",
                f"the critical label {name} is found in {rate:.0%} of its cases; it must be at "
                f"least {MIN_CRITICAL_DETECTION:.0%}",
            )
    base_report, tolerance = _base_benchmark(ctx)
    rows, failures = gate(report, base_report, tolerance)
    for failure in failures:
        fail(
            "evaluation",
            f"regression against the base (tolerance {tolerance:.0%}, from the base): {failure}",
        )
    ctx.report.metrics = markdown(pack.id, rows, failures, new=base_report is None)
    _honest_numbers(ctx, report)


def _base_benchmark(ctx: _Context) -> tuple[dict[str, Any] | None, float]:
    """The base's benchmark for this pack, and the tolerance its manifest set (capped at
    `MAX_TOLERANCE`); (None, the cap) for a new pack or without a base."""
    if ctx.base is None:
        return None, MAX_TOLERANCE
    try:
        manifest_bytes = ctx.base.read(f"{ctx.folder}/{MANIFEST}")
        if manifest_bytes is None:
            return None, MAX_TOLERANCE
        raw = parse_yaml(manifest_bytes, MANIFEST, f"the base's {ctx.folder}")
        evaluation = raw.get("evaluation") if isinstance(raw, dict) else None
        evaluation = evaluation if isinstance(evaluation, dict) else {}
        default = Evaluation.model_fields["degradation_tolerance"].default
        given = evaluation.get("degradation_tolerance", default)
        tolerance = min(float(given), MAX_TOLERANCE) if isinstance(given, int | float) else 0.0
        relative = evaluation.get("benchmark")
        if not isinstance(relative, str):
            ctx.report.not_checked.append("regressions: the base's pack has no benchmark")
            return None, tolerance
        data = ctx.base.read(f"{ctx.folder}/{relative}")
        loaded = json.loads(data) if data is not None else None
    except (MendariError, ValueError) as error:
        ctx.report.error("evaluation", f"the base's benchmark cannot be read: {error}")
        return None, MAX_TOLERANCE
    if not isinstance(loaded, dict):
        ctx.report.not_checked.append("regressions: the base's pack has no benchmark")
        return None, tolerance
    return loaded, tolerance


def _honest_numbers(ctx: _Context, report: dict[str, Any]) -> None:
    from mendari.core.card import render_card

    assert ctx.pack is not None
    pack = ctx.pack
    evaluation = pack.manifest.evaluation
    relative = str(evaluation.benchmark)
    regenerate = (
        f"regenerate it: `mendari eval {ctx.shown} --dataset {evaluation.benchmark_dataset} "
        f"--baseline none --out {ctx.shown}/{relative}`"
    )
    committed, problem = pack.attestation_json(relative)
    if committed is None:
        why = f" ({problem})" if problem else ""
        ctx.report.error(
            "honest-numbers", f"{relative} is missing or unreadable{why}: {regenerate}"
        )
    else:
        differences = _differences(_stable(report), _stable(committed))
        if differences:
            ctx.report.error(
                "honest-numbers",
                f"{relative} is not what the pack gives when evaluated again (differs at "
                f"{_listed(differences)}): {regenerate}",
            )
    shown = dict(report)
    if committed is not None:
        shown.update({key: committed[key] for key in VOLATILE_REPORT_KEYS if key in committed})
    with _copy(ctx.root) as copy:
        target = copy / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(shown, indent=2) + "\n", encoding="utf-8")
        card = render_card(load_pack(copy, origin="community"))
    ctx.report.card = card
    card_path = ctx.root / CARD
    rewrite = f"render it again after the benchmark: `mendari card {ctx.shown} --write`"
    if not card_path.is_file():
        ctx.report.error("honest-numbers", f"{CARD} is missing: {rewrite}")
    elif card_path.read_text(encoding="utf-8") != card:
        ctx.report.error("honest-numbers", f"{CARD} is not the card the pack renders: {rewrite}")


def _retrained(ctx: _Context) -> None:
    from mendari.evals.training import train_naive_bayes

    assert ctx.pack is not None
    if not any(variant.type == "naive_bayes" for _, variant in _variants(ctx.pack)):
        return
    with _copy(ctx.root) as copy:
        try:
            trained = train_naive_bayes(load_pack(copy, missing_models_ok=True, origin="community"))
        except (MendariError, KeyError, ValueError) as error:
            ctx.report.error(
                "honest-numbers",
                f"the trained model cannot be trained again ({error}): `mendari train` reads "
                "the datasets named `train` and `calibration` in `evaluation.datasets`",
            )
            return
        for item in trained:
            relative = item.path.relative_to(copy.resolve()).as_posix()
            committed = ctx.root / relative
            if not committed.is_file() or committed.read_bytes() != item.path.read_bytes():
                ctx.report.error(
                    "honest-numbers",
                    f"{relative} is not what `mendari train` gives from the pack's data, byte for "
                    f"byte: retrain it with `mendari train {ctx.shown}`",
                )


def _variants(pack: Pack) -> Iterator[tuple[str, Any]]:
    for slot_name, slot in pack.manifest.providers.items():
        for name, variant in slot.variants.items():
            yield f"{slot_name}.{name}", variant


@contextmanager
def _copy(root: Path) -> Iterator[Path]:
    """A throwaway copy of a pack folder, to write a regenerated file into."""
    with tempfile.TemporaryDirectory(prefix="mendari-community-check-") as temp:
        target = Path(temp) / root.name
        # The pack loaded, so any link in it leads inside it: the copy holds the files.
        shutil.copytree(root, target, ignore_dangling_symlinks=True)
        yield target


def _stable(report: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key not in VOLATILE_REPORT_KEYS}


def _differences(new: Any, old: Any, path: str = "") -> list[str]:
    """Where two JSON values differ (floats equal to within rounding)."""
    here = path or "the top"
    if isinstance(new, dict) and isinstance(old, dict):
        found = []
        for key in sorted(set(new) | set(old), key=str):
            where = f"{path}.{key}" if path else str(key)
            if key not in new or key not in old:
                found.append(where)
            else:
                found += _differences(new[key], old[key], where)
        return found
    if isinstance(new, list) and isinstance(old, list):
        if len(new) != len(old):
            return [here]
        return [
            d
            for i, (a, b) in enumerate(zip(new, old, strict=True))
            for d in _differences(a, b, f"{path}[{i}]")
        ]
    numbers = (int, float)
    if (
        isinstance(new, numbers)
        and isinstance(old, numbers)
        and not isinstance(new, bool)
        and not isinstance(old, bool)
    ):
        return [] if math.isclose(new, old, rel_tol=1e-9, abs_tol=1e-12) else [here]
    return [] if new == old else [here]


# -- patterns ------------------------------------------------------------------------------------


def _patterns(ctx: _Context) -> None:
    assert ctx.pack is not None
    jobs = []
    for where, text, flags in authored_patterns(ctx.pack):
        problems = regex_safety.static_problems(text, flags)
        for problem in problems:
            ctx.report.error("patterns", f"{where}: {problem}")
        if not problems:
            jobs.append(regex_safety.Job(where, text, flags))
    try:
        slow = regex_safety.probe(jobs)
    except MendariError as error:
        ctx.report.error("patterns", f"the patterns could not be timed: {error}")
        return
    for job in slow:
        ctx.report.error(
            "patterns",
            f"{job.where}: took more than {regex_safety.TIME_LIMIT_S:g} s on a "
            f"{regex_safety.INPUT_CHARS:,}-character text built from its own pieces; it "
            "backtracks too much. Simplify it (fewer optional or repeated parts)",
        )


# -- hygiene -------------------------------------------------------------------------------------


def _hygiene_files(ctx: _Context) -> None:
    fail = ctx.report.error
    links: list[str] = []
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(ctx.root):
        here = Path(directory)
        for name in [*dirnames, *filenames]:
            path = here / name
            if path.is_symlink():
                links.append(path.relative_to(ctx.root).as_posix())
            elif name in filenames:
                files.append(path)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
    if links:
        fail(
            "hygiene",
            f"a community pack holds regular files only; replace the symbolic links "
            f"{_listed(sorted(links))}",
        )
    size = sum(path.stat().st_size for path in files)
    if len(files) > MAX_PACK_FILES:
        fail("hygiene", f"{len(files)} files; a community pack has at most {MAX_PACK_FILES}")
    if size > MAX_PACK_BYTES:
        fail(
            "hygiene",
            f"{size / 1024 / 1024:.1f} MB; a community pack is at most "
            f"{MAX_PACK_BYTES // 1024 // 1024} MB. Keep the benchmark dataset to what it needs",
        )
    models = _model_files(ctx)
    binary = sorted(
        relative
        for path in files
        if (relative := path.relative_to(ctx.root).as_posix()) not in models and not _is_text(path)
    )
    if binary:
        fail(
            "hygiene",
            f"a community pack holds text files only (a trained model aside); remove "
            f"{_listed(binary)}",
        )
    evals = sorted(
        relative
        for path in files
        if (relative := path.relative_to(ctx.root).as_posix()).startswith("evals/")
        and relative != _benchmark_path(ctx)
    )
    if evals:
        fail(
            "hygiene",
            f"evals/ holds only the benchmark, which CI checks; remove {_listed(evals)} (numbers "
            "CI cannot reproduce do not go on the Decision Card)",
        )


def _raw_manifest(ctx: _Context) -> dict[str, Any]:
    try:
        raw = parse_yaml((ctx.root / MANIFEST).read_bytes(), MANIFEST, ctx.shown)
    except (OSError, MendariError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _benchmark_path(ctx: _Context) -> str | None:
    evaluation = _raw_manifest(ctx).get("evaluation")
    benchmark = evaluation.get("benchmark") if isinstance(evaluation, dict) else None
    return benchmark if isinstance(benchmark, str) else None


def _model_files(ctx: _Context) -> set[str]:
    """The trained models the manifest names (`model_file` of a naive_bayes variant)."""
    found: set[str] = set()
    providers = _raw_manifest(ctx).get("providers")
    for slot in (providers or {}).values() if isinstance(providers, dict) else ():
        variants = slot.get("variants") if isinstance(slot, dict) else None
        for variant in (variants or {}).values() if isinstance(variants, dict) else ():
            configs = [variant]
            members = variant.get("members") if isinstance(variant, dict) else None
            if isinstance(members, dict):
                configs += list(members.values())
            for config in configs:
                if isinstance(config, dict) and config.get("type") == "naive_bayes":
                    model = config.get("model_file")
                    if isinstance(model, str):
                        found.add(Path(model).as_posix().removeprefix("./"))
    return found


def _is_text(path: Path) -> bool:
    data = path.read_bytes()
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


_ENTRY = "^#{{2,3}}\\s*\\[?v?{version}\\]?(?:\\s|$)"


def has_entry(changelog: str, version: str) -> bool:
    """Whether a CHANGELOG has a heading for `version` (`## 0.2.0`, `## [0.2.0] - date`)."""
    pattern = _ENTRY.format(version=re.escape(version))
    return re.search(pattern, changelog, re.MULTILINE) is not None


def _changelog(ctx: _Context) -> None:
    from mendari.core.registry import version_key

    assert ctx.pack is not None
    version = ctx.pack.version
    path = ctx.root / CHANGELOG
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    if not has_entry(text, version):
        ctx.report.error(
            "hygiene",
            f"{CHANGELOG} needs an entry for version {version}: a `## {version}` heading and "
            "what changed",
        )
    if ctx.base is None:
        return
    try:
        before = ctx.base.files(ctx.folder)
    except MendariError as error:
        ctx.report.error("hygiene", f"the base's pack cannot be read: {error}")
        return
    if not before:
        return  # a new pack
    with tempfile.TemporaryDirectory(prefix="mendari-community-base-") as temp:
        for relative, content in before.items():
            target = Path(temp) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        base_digest = pack_digest(Path(temp))
    if base_digest == pack_digest(ctx.root):
        return
    raw = _parsed(before.get(MANIFEST))
    base_version = raw.get("version") if isinstance(raw.get("version"), str) else None
    if base_version is None or version_key(version) <= version_key(base_version):
        ctx.report.error(
            "hygiene",
            f"what the pack decides with changed, so its version goes up (the base has "
            f"{base_version}, this change {version}): MAJOR for label changes, MINOR for "
            "behaviour, PATCH for wording",
        )
    elif has_entry(before.get(CHANGELOG, b"").decode("utf-8", "replace"), version):
        ctx.report.error("hygiene", f"{CHANGELOG}: the entry for {version} was already there")


def _parsed(data: bytes | None) -> dict[str, Any]:
    if data is None:
        return {}
    try:
        raw = parse_yaml(data, MANIFEST, "the base's pack")
    except MendariError:
        return {}
    return raw if isinstance(raw, dict) else {}


# -- PHI -----------------------------------------------------------------------------------------


def _phi(ctx: _Context) -> None:
    for folder in ("data", "tests"):
        for path in sorted((ctx.root / folder).rglob("*")):
            if not path.is_file() or path.is_symlink() or not _is_text(path):
                continue
            relative = path.relative_to(ctx.root).as_posix()
            for hit in phi_scan.scan(path.read_text(encoding="utf-8")):
                ctx.report.warn(
                    "phi",
                    f"{relative} line {hit.line} looks like {hit.kind}. Data must be synthetic: "
                    "change it, or say in the pull request why it is made up",
                )
