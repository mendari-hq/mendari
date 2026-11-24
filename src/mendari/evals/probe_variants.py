# SPDX-License-Identifier: Apache-2.0
"""The variants `mendari probe` decides: a case with its text changed by one named
transformation. A variant is made one of three ways.

- **Offline** (`offline`): one of `probe_transforms.OFFLINE`, pure and seeded. It keeps the
  meaning, so the original's answers are expected.
- **By a model** (`jobs`, `written`; opt-in, `evals/probe_model.py`): `paraphrase` (in lay
  words), `translate:<language>`, and `negation`: the writer explicitly denies the critical
  finding, so the expected critical answer flips. A denial is asked only of a critical label
  whose question has two labels (yes or no), where what a denial leaves is the other one, and
  only for a case that has the finding. A site's own case is never sent to a model.
- **From a file** (`load_variants`): variants a person wrote or a model wrote earlier, one a
  line, in the shape `dump_variants` writes. So a probe runs again offline on the same variants,
  and a team brings its own.

A variant's id (`Variant.id`) comes from its original case, its transformation, the seed and its
number, so the same probe gives the same ids.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mendari.core.pack import not_json
from mendari.core.spec import LabelPolicy, PackManifest
from mendari.errors import MendariError, ProviderError
from mendari.evals import probe_model, probe_tests
from mendari.evals.canonical import canonical_input
from mendari.evals.probe_cases import Case, critical, shown, shown_id, text_at, with_text
from mendari.evals.probe_transforms import OFFLINE, Stream
from mendari.providers.llm import MODEL_PATTERN

PARAPHRASE, NEGATION, TRANSLATE = "paraphrase", "negation", "translate"
_TRANSLATION = re.compile(rf"{TRANSLATE}:([a-z][a-z-]{{1,30}})")
# Why a model was not asked for a variant, or wrote none, as the report counts it.
SITE_CASE = "site_case"  # a site's own case: never sent to a model
NO_FINDING = "no_finding_to_deny"  # a denial of a finding the case does not have
NOT_WRITTEN = "not_written"  # the call failed, or its answer was not the texts asked for


@dataclass(frozen=True)
class Variant:
    """A case with its text changed by one transformation. `detail` is which of the
    transformation's kinds (a fixed few words). `labels` are the answers the change itself
    changes (a denial); None when it keeps the meaning, and the original's are expected.
    `model` is the model that wrote it, when one did."""

    case: Case
    transformation: str
    detail: str
    input: Mapping[str, Any]
    seed: int | None
    index: int
    labels: Mapping[str, str] | None = None
    model: str | None = None

    @property
    def id(self) -> str:
        which = f"{self.seed}:{self.index}"
        if self.seed is None:  # a file's variant with no seed: by what it says
            which = "input:" + canonical_input(self.input)
        return probe_tests.variant_id(self.case.key, self.transformation, which)


@dataclass(frozen=True)
class Given:
    """A variant as a file gives it (`load_variants`): the id of its case in the source, and
    the rest of a `Variant`."""

    file: str
    case: str
    transformation: str
    input: Mapping[str, Any]
    labels: Mapping[str, str] | None = None
    model: str | None = None
    detail: str = ""
    seed: int | None = None
    index: int = 0

    def of(self, case: Case) -> Variant:
        return Variant(
            case,
            self.transformation,
            self.detail,
            self.input,
            self.seed,
            self.index,
            self.labels,
            self.model,
        )


@dataclass(frozen=True)
class Job:
    """A variant a model is to write: of which case, under which prompt, and, for a denial,
    the critical question, its critical label and the label a denial leaves."""

    case: Case
    transformation: str
    index: int
    prompt: str
    detail: str = ""
    denies: tuple[str, str, str] | None = None


# -- the transformations' names --------------------------------------------------------------------


def by_a_model(name: str) -> str | None:
    """The kind of model-written variant a transformation's name asks for (`PARAPHRASE`,
    `NEGATION` or `TRANSLATE`), or None: an offline one, or no transformation at all."""
    if name in (PARAPHRASE, NEGATION):
        return name
    return TRANSLATE if _TRANSLATION.fullmatch(name) else None


def name_problem(name: str) -> str | None:
    """Why `name` is no transformation, or None."""
    if name in OFFLINE or by_a_model(name) is not None:
        return None
    return (
        f"no transformation {name!r}: choose from {', '.join(OFFLINE)}, and with a model "
        f"{PARAPHRASE}, {NEGATION} and {TRANSLATE}:<language>"
    )


def translation(language: str) -> str:
    """The transformation that translates into a language as a person names it (`es`,
    `Haitian Creole`): `translate:es`, `translate:haitian-creole`. The prompt gives the model
    the name with its spaces."""
    name = f"{TRANSLATE}:{'-'.join(language.lower().split())}"
    if not _TRANSLATION.fullmatch(name):
        raise MendariError(
            "--language: a language's name or code in plain letters, as `es` or `haitian creole`"
        )
    return name


def denials(manifest: PackManifest) -> list[tuple[str, str, str]]:
    """What a denial can be asked of: each critical label of a question a provider answers
    with two labels, as (the question, its critical label, the label a denial leaves)."""
    found = []
    for question, label in critical(manifest):
        labels = manifest.labels(question)
        if len(labels) == 2 and manifest.questions[question].derive is None:
            found.append((question, label, next(other for other in labels if other != label)))
    return found


# -- offline ---------------------------------------------------------------------------------------


def offline(case: Case, name: str, index: int, seed: int, manifest: PackManifest) -> Variant | None:
    """A case's variant by an offline transformation, or None when it changes nothing in it.
    Every text field is changed, or (an addition) the longest one."""
    transformation = OFFLINE[name]
    places = case.places
    if not transformation.every_field:
        places = (max(case.places, key=lambda place: len(text_at(case.input, place))),)
    asked = asks(manifest, case.labels)
    changed = copy.deepcopy(dict(case.input))
    details = []
    for place in places:
        stream = Stream(seed, name, case.key, shown(place), str(index))
        change = transformation.apply(text_at(case.input, place), stream, asked)
        if change is not None:
            with_text(changed, place, change.text)
            details.append(change.detail)
    if not details:
        return None
    return Variant(case, name, "; ".join(dict.fromkeys(details)), changed, seed, index)


def asks(manifest: PackManifest, labels: Mapping[str, str]) -> dict[str, str]:
    """The answers an injected instruction asks the model for, the ones that would do most
    harm if it obeyed: for each critical label, another label of its question; for the primary
    question, a label the policy may automate that is not the case's own. Only questions a
    provider answers are named: a derived one is not asked of any model."""
    asked: dict[str, str] = {}
    answered = [key for key, question in manifest.questions.items() if question.derive is None]
    for question, label in critical(manifest):
        others = [other for other in manifest.labels(question) if other != label]
        if question in answered and others:
            asked.setdefault(question, others[0])
    primary = manifest.policy.primary
    if primary in answered and primary not in asked:
        others = [label for label in manifest.labels(primary) if label != labels.get(primary)]
        automated = [
            label for label in others if manifest.policy.labels.get(label, LabelPolicy()).allow_auto
        ]
        asked[primary] = (automated or others)[0]
    return asked


# -- by a model ------------------------------------------------------------------------------------


def jobs(
    case: Case,
    name: str,
    index: int,
    manifest: PackManifest,
    deniable: Sequence[tuple[str, str, str]],
) -> Iterator[Job | str]:
    """The variants of a case a model is to write under one transformation: each a `Job`, or
    why none is asked (`SITE_CASE`, `NO_FINDING`). A case whose label for a critical question is
    not known may still have the finding: the run asks only once the agent has said it does."""
    kind = by_a_model(name)
    if kind != NEGATION:
        if case.site:
            yield SITE_CASE
            return
        language = name.partition(":")[2].replace("-", " ")
        said = probe_model.prompt(
            kind or PARAPHRASE, **({"language": language} if language else {})
        )
        yield Job(case, name, index, said)
        return
    for number, (question, label, other) in enumerate(deniable):
        if case.labels.get(question, label) != label:
            yield NO_FINDING
        elif case.site:
            yield SITE_CASE
        else:
            asked = manifest.questions[question]
            said = probe_model.prompt(
                NEGATION, question=asked.instructions, label=label, criterion=asked.criteria[label]
            )
            yield Job(
                case,
                name,
                index * len(deniable) + number,
                said,
                f"denies {question}",
                (question, label, other),
            )


def written(job: Job, writer: probe_model.Writer, seed: int) -> Variant | str:
    """The variant a model wrote for a job, or the code of why it wrote none. The model is sent
    the case's texts under their fields' names, and nothing else of the case."""
    places = job.case.places
    texts = {shown(place): text_at(job.case.input, place) for place in places}
    try:
        rewritten = writer.write(job.prompt, texts)
    except ProviderError as error:
        return error.code
    changed = copy.deepcopy(dict(job.case.input))
    for place in places:
        with_text(changed, place, rewritten[shown(place)])
    labels = {job.denies[0]: job.denies[2]} if job.denies is not None else None
    return Variant(
        job.case, job.transformation, job.detail, changed, seed, job.index, labels, writer.model
    )


# -- from a file -----------------------------------------------------------------------------------

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}(?::[a-z0-9_-]{1,40})?")
_DETAIL = re.compile(r"[a-z0-9 ;'_-]{0,80}")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
_REQUIRED = ("case", "transformation", "input")
_OPTIONAL = ("labels", "model", "detail", "seed", "variant")


def load_variants(path: str | Path, manifest: PackManifest) -> list[Given]:
    """The variants of a JSON Lines file, one a line:

        {"case": "<the case's id in the dataset>", "transformation": "<a name>",
         "input": {...the agent's whole input, changed...}}

    and, when it has them: `labels` (question to label: the answers the variant itself changes,
    as a denial does; without it the variant keeps the meaning, and the original's answers are
    expected), `model` (the model that wrote it), and `detail`, `seed` and `variant` as
    `dump_variants` writes them, which give a variant the id it had. A line that is not one is
    a `MendariError` naming the file, the line and what is wrong, never what the line says."""
    file = Path(path)
    try:
        lines = file.read_bytes().decode("utf-8").split("\n")
    except UnicodeDecodeError:
        raise MendariError(f"{file.name}: not UTF-8 text") from None
    found = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            raise MendariError(f"{file.name}:{number}: not JSON") from None
        problem = _given_problem(record, manifest)
        if problem is not None:
            raise MendariError(f"{file.name}:{number}: {problem}")
        found.append(
            Given(
                file=file.name,
                case=shown_id(record["case"]),
                transformation=record["transformation"],
                input=record["input"],
                labels=record.get("labels") or None,
                model=record.get("model"),
                detail=record.get("detail", ""),
                seed=record.get("seed"),
                index=record.get("variant", 0),
            )
        )
    if not found:
        raise MendariError(f"{file.name}: no variant in it")
    return found


def _given_problem(record: Any, manifest: PackManifest) -> str | None:
    """Why a line of a variants file is not a variant, without repeating what it says."""
    if not isinstance(record, dict):
        return "a variant is an object with `case`, `transformation` and `input`"
    unknown = sorted(key for key in record if key not in (*_REQUIRED, *_OPTIONAL))
    if unknown:
        named = f"unknown key `{unknown[0]}`" if _KEY.fullmatch(unknown[0]) else "an unknown key"
        return (
            f"{named} (a variant has `case`, `transformation` and `input`, and may have "
            "`labels`, `model`, `detail`, `seed` and `variant`)"
        )
    for key in _REQUIRED:
        if key not in record:
            return f"no `{key}`"
    name = record["transformation"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return (
            "`transformation` is a short name in lower case (letters, digits, `-` and `_`, "
            "with `:` before a language)"
        )
    if not isinstance(record["input"], dict):
        return "`input` must be an object, the agent's whole input"
    what = not_json(record)
    if what is not None:
        return f"not JSON, it holds {what}"
    labels = record.get("labels") or {}
    if not isinstance(labels, dict):
        return "`labels` must be an object of question to label"
    for question, label in labels.items():
        if question not in manifest.questions:
            return (
                "`labels` names a question the agent does not have (its questions: "
                f"{', '.join(manifest.questions)})"
            )
        if label not in manifest.labels(question):
            return (
                f"`labels.{question}` is not one of its labels "
                f"({', '.join(manifest.labels(question))})"
            )
    model = record.get("model")
    if model is not None and not (isinstance(model, str) and re.fullmatch(MODEL_PATTERN, model)):
        return "`model` is a model's id (letters, digits and `. _ : / -`)"
    detail = record.get("detail", "")
    if not isinstance(detail, str) or not _DETAIL.fullmatch(detail):
        return "`detail` is a few words in lower case, at most 80 characters"
    for key in ("seed", "variant"):
        number = record.get(key, 0)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            return "`seed` and `variant` are whole numbers, 0 or more"
    return None


def dump_variants(variants: Sequence[Variant]) -> str:
    """Variants as the JSON Lines `load_variants` reads, each with what gives it its id again:
    what a probe built, or a model wrote, to read or to try again from the file. It holds the
    variants' text: it is the caller's to keep, outside the agent."""
    lines = []
    for variant in variants:
        record: dict[str, Any] = {
            "case": variant.case.id,
            "transformation": variant.transformation,
            "input": variant.input,
        }
        if variant.labels:
            record["labels"] = dict(variant.labels)
        if variant.model is not None:
            record["model"] = variant.model
        if variant.detail:
            record["detail"] = variant.detail
        if variant.seed is not None:
            record["seed"] = variant.seed
        record["variant"] = variant.index
        lines.append(json.dumps(record, ensure_ascii=False) + "\n")
    return "".join(lines)
