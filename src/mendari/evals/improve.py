# SPDX-License-Identifier: Apache-2.0
"""`mendari improve`: a pack's classifier learns from new labelled cases, and what it learned is
kept only when it is measurably better and no less safe.

The cases are a dataset file: what reviewers decided (`mendari feedback export --source
reviews`), rated decisions someone labelled, or cases a site labelled itself. A run:

1. **Takes the cases it can use**: labelled for the classifier's question with one of its
   labels, with an input the pack accepts, and not already among the pack's own data. A case
   is what the classifier reads of it: its words, in order, in the fields the classifier reads
   (`providers/naive_bayes.py` `tokenize`). So the same message received twice, or with another
   time stamp, another channel or other capitals, is one case; given with two labels, it is
   left out; and a message the pack's own data already holds is known whatever else its record
   says.
2. **Sets some aside before it looks at any.** Each case falls by a hash of what the classifier
   reads of it, so it falls the same way in every run: 30% are held out, 15% calibrate, the rest
   are learned from. A held-out case is never learned from, in this run or a later one.
3. **Builds candidates**: the pack's model retrained on its own training data and the cases to
   learn from, once with the features the model has and once with the other set
   (`providers/naive_bayes.py`: words alone, or words and runs of characters).
4. **Judges each against the model in the pack**, on this machine, with the pack's own policy:
   on every dataset the pack declares but the two it trains on, and on the held-out cases. A
   candidate has regressed when, on any of them, the eval gate fails (`evals/gate.py`: the
   primary question's accuracy and the false automation rate within the pack's tolerance, every
   critical label still detected and none newly automated), it gets fewer cases right than the
   model in the pack by more than chance (the cases only one of the two gets right, exact
   McNemar, p < 0.05), or it is confidently wrong more often (answers at or above the pack's
   automation threshold that are wrong: as a share of such answers, above the model in the
   pack's by more than the tolerance even at the lower end of its exact 95% interval, or on
   more cases than the model in the pack by more than chance); or when a behaviour test that
   passes stops passing. It is better when it has not regressed and gets
   more of the held-out cases right than the model in the pack does, by more than chance.
5. **Keeps nothing by itself.** The report says which candidate is better, if any. Only
   `apply` (`--write`) writes, and only into a pack of the caller's own (`not_writable`): the
   model, and the cases it learned from and calibrated on at the end of the pack's own training
   and calibration datasets, so the pack's data still gives the model it holds and `mendari
   train` rebuilds it byte for byte. All three files change, or none does.

A pack it wrote to is a changed pack: a new digest, to be given a new version, tested, and
approved wherever a digest is approved, as any change is. Nothing here serves it or approves it.

It also holds a site's cases now, which may be patient text, and its model holds their words.
Every case a run adds says so (`attributes.added_by`, `core/site_cases.py`), `apply` writes none
that does not, and the model built from them says so too (`evals/training.py` `fit`). No
archive is made of a pack that holds either (`hub/archive.py`), so `mendari pack build` and
`mendari push` refuse it, and a registry refuses one sent to it all the same. The file a run
was given is a site's cases too, a third of them only held out, and it need carry no mark (an
export's records do, cases a site labelled itself do not): the command reads none from the
pack's own folder (`kept_in_the_pack`).

The report (`mendari.improvement/1`) holds counts and rates, never a case's text or id. The
held-out comparison is paired: the cases the two models disagree on, with an exact McNemar
p-value, so a reader can tell a real gain from a few cases' luck. Two candidates are judged on
the same held-out cases, so the better one's margin is a little kind to it.

Reviewed cases are the ones a pack sent to a person, labelled by someone who saw its suggestion.
They are fair for comparing two models on the same cases. They are not the pack's accuracy on
everything it sees.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mendari.core.decision import Decision
from mendari.core.pack import Pack, load_pack, pack_digest
from mendari.core.registry import is_within, origin_of
from mendari.core.site_cases import ADDED_BY, IMPROVE, MARKS, added
from mendari.errors import MendariError
from mendari.evals import stats
from mendari.evals.canonical import canonical_input
from mendari.evals.gate import gate
from mendari.evals.harness import Dataset, Evaluation, evaluation, load_dataset, resolve_dataset
from mendari.evals.ledger import Ledger
from mendari.evals.pack_tests import run_pack_tests
from mendari.evals.result import CONFIDENCE, PREDICTED, TRUTH
from mendari.evals.training import built_with, fit, model_files, text_fields, written
from mendari.hub.archive import pack_files
from mendari.providers.base import text_from
from mendari.providers.naive_bayes import CharacterNgrams, tokenize

FORMAT = "mendari.improvement/1"
HELD_OUT = "held out"  # the new cases a run never learns from, as its report names them
HELD_OUT_SHARE = 0.30
CALIBRATION_SHARE = 0.15
# Fewer held-out cases than this cannot tell a better model from a lucky one.
MIN_HELD_OUT = 30
# A difference between two models on the same cases counts when its exact McNemar p-value is
# below this: a gain on the held-out cases, or a loss on any dataset.
ALPHA = 0.05
CHARACTER_NGRAMS: CharacterNgrams = (3, 5)  # the runs a candidate with character features counts
BETTER, NOT_BETTER, REGRESSED, TOO_FEW = "better", "not_better", "regressed", "too_few_cases"
LEFT_OUT = (
    "not_labelled",
    "refused",
    "nothing_to_read",
    "already_in_pack",
    "conflicting",
    "repeated",
)
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # a source, or a decision's id: never free text


@dataclass(frozen=True)
class Improvement:
    """A run's report, and what `apply` would write: the chosen candidate's model, and the
    cases it learned from and calibrated on, for the datasets named. `held_out_ids` are the ids
    of the given records held out (every record of a case), for the caller alone: the report
    names no case."""

    report: dict[str, Any]
    held_out_ids: tuple[str, ...] = ()
    model: dict[str, Any] | None = None
    learned: tuple[dict[str, Any], ...] = ()
    calibration: tuple[dict[str, Any], ...] = ()
    train_dataset: str = "train"
    calibration_dataset: str = "calibration"

    @property
    def verdict(self) -> str:
        return str(self.report["verdict"])


@dataclass(frozen=True)
class _Cases:
    learned: list[dict[str, Any]]
    calibration: list[dict[str, Any]]
    held_out: list[dict[str, Any]]
    held_out_ids: list[str]
    given: int
    left_out: dict[str, int]

    def counts(self) -> dict[str, Any]:
        return {
            "given": self.given,
            "learned": len(self.learned),
            "calibration": len(self.calibration),
            "held_out": len(self.held_out),
            "left_out": dict(self.left_out),
        }


def improve(
    pack: Pack,
    given: Sequence[Dataset],
    *,
    slot: str | None = None,
    train: str = "train",
    calibration: str = "calibration",
) -> Improvement:
    """Judge what the pack's classifier (of `slot`, when several slots have one) would gain from
    the cases `given` (the module's docstring). Nothing is written. A pack with no classifier,
    or one that cannot be judged on this machine, is a `MendariError`."""
    if train == calibration:
        raise MendariError(
            f"{pack.id}: a model is calibrated on cases it did not learn from: the training and "
            "calibration datasets are two"
        )
    slot, variant, config = _target(pack, slot)
    # As the pack holds it: a path that leaves the pack is refused here, before anything runs.
    model_file = pack.path(str(config["model_file"])).relative_to(pack.root).as_posix()
    (question,) = pack.manifest.providers[slot].answers
    current = _decision(pack, slot, variant)
    cases = _new_cases(pack, current, question, text_fields(config), given)
    report: dict[str, Any] = {
        "format": FORMAT,
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "slot": slot,
        "question": question,
        "model_file": model_file,
        "providers": {name: info.variant for name, info in current.provider_info.items()},
        "datasets": {"train": train, "calibration": calibration},
        "shares": {"held_out": HELD_OUT_SHARE, "calibration": CALIBRATION_SHARE},
        "cases": cases.counts(),
        "candidates": [],
        "chosen": None,
    }
    held_out_ids = tuple(cases.held_out_ids)
    if not cases.learned or len(cases.held_out) < MIN_HELD_OUT:
        report["verdict"] = TOO_FEW
        report["reason"] = (
            "no new case to learn from"
            if not cases.learned
            else f"{len(cases.held_out)} held-out cases: at least {MIN_HELD_OUT} are needed to "
            "tell a better model from a lucky one"
        )
        return Improvement(report, held_out_ids)

    datasets = [
        resolve_dataset(pack, name)
        for name in pack.manifest.evaluation.datasets
        if name not in (train, calibration)
    ]
    datasets.append(_dataset(HELD_OUT, cases.held_out))
    now = {dataset.name: evaluation(current, dataset) for dataset in datasets}
    passing = {outcome.name for outcome in run_pack_tests(current) if outcome.passed}
    features_now = built_with(pack, model_file)
    report["current"] = {
        "model": current.provider_info[slot].model,
        "features": _features(features_now),
        "tests": {"passed": len(passing)},
    }
    learn_from = [*resolve_dataset(pack, train).records, *cases.learned]
    calibrate_on = [*resolve_dataset(pack, calibration).records, *cases.calibration]
    models = []
    for features in (features_now, None if features_now else CHARACTER_NGRAMS):
        model = fit(pack, slot, config, learn_from, calibrate_on, features)
        models.append(model)
        report["candidates"].append(
            _judged(pack, model, model_file, {slot: variant}, datasets, now, passing, question)
        )
    better = [
        index for index, found in enumerate(report["candidates"]) if found["verdict"] == BETTER
    ]
    if not better:
        verdicts = {found["verdict"] for found in report["candidates"]}
        report["verdict"] = REGRESSED if verdicts == {REGRESSED} else NOT_BETTER
        return Improvement(report, held_out_ids)
    # The most held-out cases gained; of two alike, the candidate built as the pack's model is.
    chosen = max(
        better,
        key=lambda index: (
            report["candidates"][index]["held_out"]["gained"]
            - report["candidates"][index]["held_out"]["lost"],
            -index,
        ),
    )
    report["chosen"] = chosen
    report["verdict"] = BETTER
    return Improvement(
        report,
        held_out_ids,
        model=models[chosen],
        learned=tuple(cases.learned),
        calibration=tuple(cases.calibration),
        train_dataset=train,
        calibration_dataset=calibration,
    )


def not_writable(pack: Pack) -> str | None:
    """Why a run is never kept in this pack, or None: `apply` writes only into a pack of the
    caller's own. A pack among those this release bundles (its data comes from its generator),
    in the repository's community packs (what it learned from would be published with it) or
    in the registry's cache (a verified copy) is not changed in place, by where it is on disk
    (`core.registry.origin_of`), whatever it was loaded as."""
    where = {pack.origin, origin_of(pack.root)} - {"local"}
    if not where:
        return None
    what = {
        "maintained": "a reference pack of this release",
        "community": "a community pack of this repository",
        "hub": "a pack pulled from a registry",
        "store": "a version in a pack store",
    }[sorted(where)[0]]
    return f"{what} is not changed in place: copy it, and improve the copy"


def kept_in_the_pack(pack: Pack, cases: str | Path) -> bool:
    """Whether a file of cases is in the pack's own folder, by the file it is, however its path
    is spelt. The cases a run is given need carry no mark (an export's do, a site's own file of
    them does not), and a third of them are only held out, never added; and every file in a
    pack's folder goes into its archive. So the command reads none from there (`cli.py`
    `cmd_improve`): a site's cases stay outside the pack until a run adds the ones it learned
    from, marked."""
    return is_within(cases, pack.root)


def apply(pack: Pack, found: Improvement) -> list[Path]:
    """Keep what a run found better: add the cases it learned from and calibrated on to the end
    of the pack's own training and calibration datasets, and write the model. The pack's data
    then gives the model it holds (`mendari train` rebuilds it byte for byte). Returns what it
    wrote. All of it is written, or nothing: each file is written whole beside the pack first,
    then moved into place with its own permissions, and if one cannot be moved the ones already
    moved are put back. A run that found nothing better, a pack that is not the caller's own
    (`not_writable`), a pack that is no longer the one the run judged (so a run is kept once),
    and a case that does not say a run added it (the mark that keeps the pack out of an
    archive, `hub/archive.py`) are each a `MendariError`, and nothing is written."""
    if found.model is None:
        raise MendariError(f"nothing to keep: no candidate was better ({found.verdict})")
    if (problem := not_writable(pack)) is not None:
        raise MendariError(f"nothing to keep: {problem}")
    # The pack as it is on disk now, not as it was when loaded.
    if pack_digest(pack.root) != found.report["pack"]["digest"]:
        raise MendariError("nothing to keep: the pack has changed since this run judged it")
    declared = pack.manifest.evaluation.datasets
    contents: list[tuple[Path, bytes, bytes]] = []  # the file, what it holds, what it will
    for name, records in (
        (found.train_dataset, found.learned),
        (found.calibration_dataset, found.calibration),
    ):
        if name not in declared:
            raise MendariError(
                f"nothing to keep: {name!r} is not one of the pack's datasets, and what a run "
                "learned from goes into the pack, so that its data gives the model it holds"
            )
        if not all(added(case) for case in records):
            # Read as an archive's builder reads it (`core/site_cases.py`): the one writer checks
            # the mark, so no pack holds a case of a run's that nothing would recognise.
            raise MendariError(
                "nothing to keep: a case this run would add does not say `mendari improve` "
                f"added it (`attributes.{ADDED_BY}`), and that is what keeps a pack that holds "
                "a site's cases from being published"
            )
        if records:
            target = pack.path(declared[name])
            held = target.read_bytes()
            ends = b"" if held.endswith(b"\n") or not held else b"\n"
            contents.append((target, held, held + ends + _lines(records)))
    model = pack.path(str(found.report["model_file"]))
    contents.append((model, model.read_bytes(), written(found.model).encode()))
    moved: list[tuple[Path, bytes]] = []
    with tempfile.TemporaryDirectory(dir=pack.root.parent, prefix=".mendari-improve-") as beside:
        staged = []
        for number, (target, _, content) in enumerate(contents):
            ready = Path(beside) / str(number)
            ready.write_bytes(content)
            shutil.copymode(target, ready)
            staged.append(ready)
        try:
            for ready, (target, held, _) in zip(staged, contents, strict=True):
                os.replace(ready, target)
                moved.append((target, held))
        except OSError as error:
            for target, held in moved:  # as they were: the pack is whole, or untouched
                target.write_bytes(held)
            raise MendariError(
                f"nothing was kept: {contents[len(moved)][0].name} could not be written "
                f"({type(error).__name__}), and the pack is as it was"
            ) from None
    return [target for target, _, _ in contents]


def text(report: Mapping[str, Any]) -> str:
    """The report in words, for a terminal."""
    pack, cases = report["pack"], report["cases"]
    left = ", ".join(
        f"{count} {name.replace('_', ' ')}" for name, count in cases["left_out"].items() if count
    )
    lines = [
        f"Improve {pack['id']}@{pack['version']}: {report['slot']} ({report['question']}), "
        f"{report['model_file']}",
        f"New cases: {cases['given']} given, {cases['learned']} to learn from, "
        f"{cases['calibration']} to calibrate on, {cases['held_out']} held out"
        + (f" (left out: {left})" if left else ""),
    ]
    if report["verdict"] == TOO_FEW:
        return "\n".join([*lines, f"Not kept: {report['reason']}."])
    lines.append("Candidates, each against the model in the pack:")
    for found in report["candidates"]:
        held = found["held_out"]
        lines.append(
            f"  {_named(found['features'])}: {held['candidate']} of {held['n']} held out right "
            f"(now {held['current']}): {held['gained']} gained, {held['lost']} lost "
            f"(p = {held['p']:.3g}); {found['verdict'].replace('_', ' ')}"
        )
        lines += [f"    regression: {problem}" for problem in found["regressions"]]
    if report["chosen"] is None:
        why = {
            REGRESSED: "every candidate regressed",
            NOT_BETTER: "no candidate was shown to get more of the held-out cases right",
        }[report["verdict"]]
        return "\n".join([*lines, f"Not kept: {why}."])
    chosen = report["candidates"][report["chosen"]]
    return "\n".join([*lines, f"Better: {_named(chosen['features'])}."])


def _target(pack: Pack, slot: str | None) -> tuple[str, str, dict[str, Any]]:
    """(slot, variant, config) of the one classifier to improve: the variant is the slot's
    variant that holds it, a composite's when only a member does."""
    found = model_files(pack, slot)
    if not found:
        where = f" in slot {slot!r}" if slot else ""
        raise MendariError(f"{pack.id}: no naive_bayes variant{where} to improve")
    if len(found) > 1:
        named = ", ".join(f"{name} ({config['model_file']})" for name, _, config in found)
        raise MendariError(f"{pack.id}: several classifiers: {named}: name one slot with --slot")
    slot_name, variant, config = found[0]
    return slot_name, variant.partition(".")[0], config


def _decision(pack: Pack, slot: str, variant: str) -> Decision:
    """The pack deciding with the classifier, its other slots as it ships, and its own policy
    alone (`origin_cap=False`, as `mendari eval`): on this machine, or a `MendariError`."""
    decision = Decision(pack, providers={slot: variant}, origin_cap=False)
    for name, provider in decision.providers.items():
        if provider.remote:
            raise MendariError(
                f"{pack.id}: {name}={provider.info.variant} calls a model, and a run is judged "
                "on this machine alone: improve a pack whose other slots decide locally"
            )
    return decision


def _read(inputs: Mapping[str, Any], fields: list[str]) -> str:
    """What the classifier reads of an input: its words and pairs of words, in order, from the
    fields it reads, in their plain form (`tokenize`). Two inputs it reads alike are one case,
    whatever else their records hold; one with no word to read is none."""
    return "\x1f".join(tokenize(text_from(inputs, fields)))


def _new_cases(
    pack: Pack, decision: Decision, question: str, fields: list[str], given: Sequence[Dataset]
) -> _Cases:
    """The cases of `given` a run can use, each as the pack's datasets hold a case, split by a
    hash of what the classifier reads of it (`_read`); and how many it left out, by why
    (`LEFT_OUT`)."""
    labels = set(pack.manifest.labels(question))
    in_pack = {
        _read(record["input"], fields)
        for name in pack.manifest.evaluation.datasets
        for record in resolve_dataset(pack, name).records
    }
    left_out = dict.fromkeys(LEFT_OUT, 0)
    by_reading: dict[str, dict[str, list[dict[str, Any]]]] = {}
    total = 0
    for dataset in given:
        for record in dataset.records:
            total += 1
            answers = record.get("labels")
            label = answers.get(question) if isinstance(answers, Mapping) else None
            if not isinstance(label, str) or label not in labels:
                left_out["not_labelled"] += 1
            elif decision.validate(record["input"]):
                left_out["refused"] += 1
            elif not (read := _read(record["input"], fields)):
                left_out["nothing_to_read"] += 1
            elif read in in_pack:
                left_out["already_in_pack"] += 1
            else:
                by_reading.setdefault(read, {}).setdefault(label, []).append(record)
    cases = _Cases([], [], [], [], total, left_out)
    hashed = sorted((hashlib.sha256(read.encode()).hexdigest(), read) for read in by_reading)
    for digest, read in hashed:
        labelled = by_reading[read]
        records = [record for same in labelled.values() for record in same]
        if len(labelled) > 1:  # read alike, labelled two ways: neither is learned
            left_out["conflicting"] += len(records)
            continue
        left_out["repeated"] += len(records) - 1
        (label,) = labelled
        # Of records read alike, the one kept does not depend on the order they were given in.
        kept = min(records, key=lambda record: canonical_input(record["input"]))
        # A case a run adds says so, from here on: the model records the cases it was built
        # from whole, so the mark is on them before any model is, and what `apply` writes is
        # what `mendari train` reads. It is what keeps the pack out of an archive.
        attributes = {ADDED_BY: IMPROVE, "source": _name(kept, "source") or "given"}
        if (decision_id := _name(kept, "decision_id")) is not None:
            attributes["decision_id"] = decision_id  # to find the case again (an erasure)
        case = {
            "id": f"learned-{digest[:16]}",
            "input": kept["input"],
            "labels": {question: label},
            "attributes": attributes,
        }
        share = int(digest[:8], 16) / 2**32
        if share < HELD_OUT_SHARE:
            cases.held_out.append(case)
            cases.held_out_ids.extend(str(record["id"]) for record in records)
        elif share < HELD_OUT_SHARE + CALIBRATION_SHARE:
            cases.calibration.append(case)
        else:
            cases.learned.append(case)
    return cases


def _name(record: Mapping[str, Any], key: str) -> str | None:
    """A record's `attributes.<key>` (where a case came from: `review`, `outcome`, `feedback`;
    the decision it was), when it is a short name and so never free text; else None."""
    attributes = record.get("attributes")
    value = attributes.get(key) if isinstance(attributes, Mapping) else None
    return value if isinstance(value, str) and _NAME.fullmatch(value) else None


def _lines(records: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()


def _dataset(name: str, records: Sequence[Mapping[str, Any]]) -> Dataset:
    """Cases held in memory as a dataset, hashed and keyed as a file of them would be."""
    return load_dataset(Path(name), name=name, raw=_lines(records), label=name)


def _judged(
    pack: Pack,
    model: Mapping[str, Any],
    model_file: str,
    providers: Mapping[str, str],
    datasets: Sequence[Dataset],
    now: Mapping[str, Evaluation],
    passing: set[str],
    question: str,
) -> dict[str, Any]:
    """One candidate against the model in the pack: the pack as it would be with it, in a
    temporary copy, on every dataset and behaviour test."""
    tolerance = pack.manifest.evaluation.degradation_tolerance
    at = _threshold(pack, question)
    with tempfile.TemporaryDirectory() as scratch:
        candidate = Decision(
            _copy_with(pack, model_file, model, Path(scratch) / pack.root.name),
            providers=dict(providers),
            origin_cap=False,
        )
        compared: list[dict[str, Any]] = []
        regressions: list[str] = []
        for dataset in datasets:
            judged = evaluation(candidate, dataset)
            current = now[dataset.name]
            rows, failures = gate(judged.report, current.report, tolerance)
            paired = _paired(current.ledger, judged.ledger, question)
            if paired["lost"] > paired["gained"] and paired["p"] < ALPHA:
                failures.append(
                    f"got fewer right: {paired['lost']} lost, {paired['gained']} gained "
                    f"(p = {paired['p']:.3g})"
                )
            confident = _confident(current.ledger, judged.ledger, question, at)
            was, sure = confident["current"], confident["candidate"]
            if _at_least(sure) > _share(was) + tolerance:
                failures.append(
                    f"confident and wrong more often: {sure['wrong']} of {sure['n']} confident "
                    f"answers (the model in the pack: {was['wrong']} of {was['n']})"
                )
            if confident["newly_wrong"] > confident["no_longer_wrong"] and confident["p"] < ALPHA:
                failures.append(
                    f"confident and wrong on more cases: {confident['newly_wrong']} newly, "
                    f"{confident['no_longer_wrong']} no longer (p = {confident['p']:.3g})"
                )
            regressions += [f"{dataset.name}: {failure}" for failure in failures]
            compared.append(
                {
                    "dataset": dataset.name,
                    "cases": len(dataset.records),
                    "sha256": dataset.sha256,
                    "gate": [
                        {
                            "metric": row["metric"],
                            "current": row["main"],
                            "candidate": row["change"],
                            "current_n": row["main_n"],
                            "candidate_n": row["change_n"],
                        }
                        for row in rows
                    ],
                    "paired": paired,
                    "confident": confident,
                }
            )
        outcomes = run_pack_tests(candidate)
        broken = sorted(o.name for o in outcomes if not o.passed and o.name in passing)
        regressions += [f"behaviour test {name!r} no longer passes" for name in broken]
        held_out = next(found["paired"] for found in compared if found["dataset"] == HELD_OUT)
        gained = held_out["gained"] > held_out["lost"] and held_out["p"] < ALPHA
        return {
            "features": _features(model.get("character_ngrams")),
            "model": candidate.provider_info[next(iter(providers))].model,
            # How it was trained, without the marks a model built from a site's cases
            # carries: a report holds no case, and kept beside a pack's files it must stop no
            # archive.
            "training": {k: v for k, v in model["training"].items() if k not in MARKS},
            "temperature": model["temperature"],
            "held_out": held_out,
            "datasets": compared,
            "tests": {
                "passed": sum(outcome.passed for outcome in outcomes),
                "failed": len(broken),
            },
            "regressions": regressions,
            "verdict": REGRESSED if regressions else BETTER if gained else NOT_BETTER,
        }


def _copy_with(pack: Pack, model_file: str, model: Mapping[str, Any], root: Path) -> Pack:
    """The pack as it would be with `model` at `model_file`, copied to `root`: the files that
    make up a pack (`hub.archive.pack_files`: nothing hidden, no cache), each by its content
    alone, without its evaluation ledgers, which a candidate decides without."""
    for source in pack_files(pack.root):
        relative = source.relative_to(pack.root)
        if relative.parts[:2] == ("evals", "ledgers"):
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (root / model_file).write_text(written(model), encoding="utf-8")
    return load_pack(root, origin=pack.origin)


def _threshold(pack: Pack, question: str) -> Callable[[str], float]:
    """The confidence at which the pack's policy would automate an answer of `question`: the
    label's own `auto_threshold` for the primary question, else `thresholds.auto`."""
    policy = pack.manifest.policy

    def at(label: str) -> float:
        own = policy.labels.get(label) if question == policy.primary else None
        if own is None or own.auto_threshold is None:
            return policy.thresholds.auto
        return float(own.auto_threshold)

    return at


def _confident(
    current: Ledger, candidate: Ledger, question: str, at: Callable[[str], float]
) -> dict[str, Any]:
    """The two models' labelled answers to `question` at or above the confidence the pack would
    automate at: how many each gave and how many of those were wrong, and the cases only one of
    them was confidently wrong on (`newly_wrong` for the candidate, `no_longer_wrong`), with
    their exact McNemar p-value."""

    def wrong(entry: Sequence[Any] | None) -> bool | None:
        """Confidently wrong, confidently right (False), or not confident or not labelled."""
        if entry is None or None in (entry[TRUTH], entry[PREDICTED], entry[CONFIDENCE]):
            return None
        if entry[CONFIDENCE] < at(entry[PREDICTED]):
            return None
        return bool(entry[PREDICTED] != entry[TRUTH])

    counts = {"current": {"n": 0, "wrong": 0}, "candidate": {"n": 0, "wrong": 0}}
    newly = no_longer = 0
    for before, after in zip(current.cases, candidate.cases, strict=True):
        was, is_now = wrong(before["q"].get(question)), wrong(after["q"].get(question))
        for side, found in (("current", was), ("candidate", is_now)):
            counts[side]["n"] += found is not None
            counts[side]["wrong"] += bool(found)
        newly += bool(is_now) and not was
        no_longer += bool(was) and not is_now
    return {
        **counts,
        "newly_wrong": newly,
        "no_longer_wrong": no_longer,
        "p": stats.mcnemar_exact(no_longer, newly),
    }


def _share(confident: Mapping[str, int]) -> float:
    return confident["wrong"] / confident["n"] if confident["n"] else 0.0


def _at_least(confident: Mapping[str, int]) -> float:
    """The least the share of confident answers that are wrong can be, given how few there may
    be: the lower end of its exact 95% interval. One wrong answer of fourteen is not a share."""
    if not confident["n"]:
        return 0.0
    return stats.clopper_pearson(confident["wrong"], confident["n"])[0]


def _paired(current: Ledger, candidate: Ledger, question: str) -> dict[str, Any]:
    """The two models on the same held-out cases: how many each got right, the cases only one
    got right (`gained` by the candidate, `lost` by it), and their exact McNemar p-value."""
    n = right_now = right_then = gained = lost = 0
    for before, after in zip(current.cases, candidate.cases, strict=True):
        if before["c"] != after["c"]:
            raise MendariError("the two runs are not of the same cases")
        old, new = before["q"].get(question), after["q"].get(question)
        if old is None or new is None or old[TRUTH] is None:
            continue
        was, is_now = old[PREDICTED] == old[TRUTH], new[PREDICTED] == new[TRUTH]
        n += 1
        right_now += was
        right_then += is_now
        gained += is_now and not was
        lost += was and not is_now
    return {
        "n": n,
        "current": right_now,
        "candidate": right_then,
        "gained": gained,
        "lost": lost,
        "p": stats.mcnemar_exact(lost, gained),
    }


def _features(character_ngrams: Any) -> list[int] | None:
    """A model's features as the report holds them: its `character_ngrams`, or None (words)."""
    return list(character_ngrams) if character_ngrams else None


def _named(features: Sequence[int] | None) -> str:
    if not features:
        return "words alone"
    return f"words and runs of {features[0]} to {features[1]} characters"
