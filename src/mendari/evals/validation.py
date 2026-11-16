# SPDX-License-Identifier: Apache-2.0
"""One validation function for a Decision Pack (LP-20b).

`mendari validate`, the community check, the registry's publish and the pack store all call
`validate` or `problems` here, so a pack one of them takes the others take too. It is what
`load_pack` does not check: the CDS Hooks templates and provider configs held to the rules a
pack the deployment does not own is refused for (an owned pack loads with them as warnings),
every provider variant built, the FHIR mapping loaded, the behaviour tests' inputs against the
input schema, and every declared dataset read and checked.

A problem has a kind, the pack file it is in and a place in that file (a manifest key, a case
number, a case id), so the store and the dashboard can show it; its text is the line
`mendari validate` has always printed. No problem holds a case's text: inputs are checked by
schema rule and named by case number or id.

A pack the deployment does not own (`Pack.bounded`) is checked on the engine it runs on: its
inputs within the limits and the CPU budget a decision gives it (`core/patterns.py`)."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Literal

from mendari.core import input_schema
from mendari.core.decision import Decision, _schema_rule, _where
from mendari.core.origin import bounded_reader, shape_problem
from mendari.core.pack import MANIFEST, Pack, manifest_template_problems
from mendari.core.patterns import OutOfTime, time_budget
from mendari.errors import MendariError, PackValidationError

Kind = Literal["manifest", "provider", "fhir", "tests", "dataset"]

# At most this many problems are named per dataset; the rest are counted.
MAX_DATASET_PROBLEMS = 10


@dataclass(frozen=True)
class Problem:
    """Why a pack is not valid. `file` is a path inside the pack, `place` where in it (a
    manifest key such as `providers.rules.variants.default`, `case 3`, `case c-12`, or empty
    for the whole file), `text` the line `mendari validate` prints. Never a case's text."""

    kind: Kind
    file: str
    place: str
    text: str

    def __str__(self) -> str:
        return self.text

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "file": self.file, "place": self.place, "text": self.text}


def problems(pack: Pack) -> list[Problem]:
    """Every problem `validate` would refuse `pack` for; an empty list for a valid pack. A
    provider that cannot be built and a FHIR mapping that does not load are problems here, not
    exceptions, so a caller holding someone else's files (the registry, the store) shows them."""
    return _collect(pack, raising=False)


def validate(pack: Pack) -> None:
    """`mendari validate`: a `PackValidationError` listing every problem, or the error a
    provider build or the FHIR mapping raised, as the command has always printed them."""
    found = _collect(pack, raising=True)
    if found:
        raise PackValidationError(pack.id, [str(problem) for problem in found])


def _collect(pack: Pack, *, raising: bool) -> list[Problem]:
    from mendari.providers import build_provider, config_problems

    # Every template's and variant's problems, each naming it; the trained files are read
    # whatever the pack was loaded with. Then what only a build finds: the deployment's model
    # settings.
    found = [
        _manifest_problem("manifest", text) for text in manifest_template_problems(pack.manifest)
    ] + [_manifest_problem("provider", text) for text in config_problems(pack)]
    if found:
        return found
    for slot, spec in pack.manifest.providers.items():
        for variant in spec.variants:
            try:
                build_provider(pack, slot, variant)
            except Exception as error:
                if raising:
                    raise
                where = f"providers.{slot}.variants.{variant}"
                return [Problem("provider", MANIFEST, where, f"{where}: {_said(error)}")]
    if pack.manifest.inputs.fhir:
        from mendari.standards.fhir import FhirAdapter

        # Parse and check the mapping, not only that it exists; its Task text bare, whoever
        # owns the pack.
        try:
            FhirAdapter.for_pack(pack, bare=True)
        except Exception as error:
            if raising:
                raise
            return [Problem("fhir", str(pack.manifest.inputs.fhir), "", _said(error))]
    return input_problems(pack) + dataset_problems(pack)


def _said(error: Exception) -> str:
    """An error's own words when it is one of Mendari's (they never quote a case), else only its
    type: the pack's content may be someone else's."""
    if isinstance(error, MendariError):
        return str(error)
    return f"it cannot be built ({type(error).__name__})"


def _manifest_problem(kind: Kind, text: str) -> Problem:
    # Each starts with the manifest key it is about (`providers.x.variants.y: …`,
    # `cds_hooks.card.summary …`).
    head = text.split(None, 1)[0] if text.strip() else ""
    return Problem(kind, MANIFEST, head.rstrip(":"), text)


def input_problems(pack: Pack) -> list[Problem]:
    """Behaviour-test inputs must match the input schema, unless the test expects a BLOCK;
    otherwise the test decides a BLOCK and fails confusingly (e.g. unquoted YAML flow-mapping
    text split into extra keys). Each problem names the case by number and name, the place in
    the input and the schema's rule, never the input's value."""
    from mendari.evals.pack_tests import load_cases

    bounded = pack.bounded  # on the community engine, as in a decision
    reader = bounded_reader(pack.id, pack.origin)
    validator = input_schema.validator(pack.input_schema, formats=False, community=bounded)
    tests = str(pack.manifest.tests)
    found: list[Problem] = []
    for number, case in enumerate(load_cases(pack), start=1):
        if (case.get("expect") or {}).get("outcome") == "BLOCK":
            continue  # a test of the input contract itself
        place = f"case {number}"
        where = f"{tests} {place} ({case.get('name', 'unnamed')})"

        def problem(text: str, place: str = place, where: str = where) -> Problem:
            return Problem("tests", tests, place, f"{where}: {text}")

        shape = shape_problem(case.get("input"), reader) if bounded else None
        if shape is not None:
            found.append(problem(f"its input is too large: {shape}"))
            continue
        try:  # such a pack's schema runs within the time budget a decision gives it
            with time_budget() if bounded else nullcontext():
                for error in validator.iter_errors(case.get("input")):
                    # Where and which rule, in the schema's words, never the value (as a
                    # decision's reasons are, `Decision.validate`).
                    rule = _schema_rule(error, bounded)
                    found.append(problem(f"input {_where(error.path)}: {rule}"))
        except OutOfTime as error:
            found.append(problem(f"checking its input against the schema stopped: {error}"))
        except (RecursionError, OverflowError):
            found.append(
                problem(
                    "the schema cannot check its input: it is nested too deep, or holds a "
                    "number too large"
                )
            )
    return found


def dataset_problems(pack: Pack) -> list[Problem]:
    """Every dataset `evaluation.datasets` declares reads as JSON Lines, each input matches the
    input schema as a decision checks it (within its limits and budget for a bounded pack,
    `Decision.validate`), and each record's labels are the pack's questions and labels
    (`record_problems`). Problems name the file and the case id, never its text."""
    from mendari.evals.harness import record_problems, resolve_dataset

    decision = Decision(pack)
    found: list[Problem] = []
    for name, path in pack.manifest.evaluation.datasets.items():
        file = str(path)
        try:
            dataset = resolve_dataset(pack, name)
        except MendariError as error:
            found.append(Problem("dataset", file, "", str(error)))
            continue
        problems = [
            Problem("dataset", file, _case_place(text, dataset.name), f"{file}: {text}")
            for text in record_problems(pack.manifest, dataset)
        ]
        for record in dataset.records:
            refused = decision.validate(record["input"])
            if refused:
                place = f"case {record['id']}"
                problems.append(
                    Problem("dataset", file, place, f"{file} {place}: input {refused[0]}")
                )
        found += problems[:MAX_DATASET_PROBLEMS]
        if len(problems) > MAX_DATASET_PROBLEMS:
            more = len(problems) - MAX_DATASET_PROBLEMS
            found.append(Problem("dataset", file, "", f"{file}: and {more} more problems"))
    return found


def _case_place(text: str, dataset: str) -> str:
    """`case <id>` from a `record_problems` line (`<dataset> case <id>: …`), else empty."""
    head = text.split(": ", 1)[0]
    prefix = f"{dataset} "
    return head[len(prefix) :] if head.startswith(prefix + "case ") else ""
