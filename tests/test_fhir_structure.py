# SPDX-License-Identifier: Apache-2.0
"""The structural R4 gate (standards/fhir_structure.py) and the file it reads.

jsonschema is the oracle: the same derived schema, run by a JSON Schema implementation, must give
the walker's verdicts on the fixtures, the sandbox and Da Vinci samples, and their mistyped
variants. `uv run pytest -m slow` also runs the gate over HL7's R4 examples, when cached.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import re
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft6Validator, ValidationError, validators

from mendari.core.decision import Decision
from mendari.core.spec import Outcome
from mendari.errors import FhirStructureError
from mendari.server.app import create_app
from mendari.standards import fhir_structure, us_core
from mendari.standards.fhir_structure import check
from mendari.standards.us_core import Issue
from tests.support.sandbox import EHR_PROFILE, INBOX, REFERRAL, seed_bundle
from tests.test_fhir_errors import WRONG_TYPES, element_paths, replaced

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "us-core"
EXAMPLES = ROOT / "var" / "fhir-packages" / "hl7.fhir.r4.examples-4.0.1.tgz"
SECRET = "Synthetic secret: chest pain since noon"  # must never come back in an issue
REFERRAL_PACK = "mendari/referral-router"
EXTENSION = "https://mendari.co/fhir/test/x"
DIAGNOSTICS = re.compile(
    r"(?P<element>_?[A-Za-z][A-Za-z0-9]*|an element|resourceType) ("
    r"must be (a string|an object|an array|a boolean|a number)"
    r"|is not an element of [A-Za-z_]+ in R4"
    r"|is missing|is not an R4 resource type"
    r"|does not have the [A-Za-z0-9]+ format"
    r"|is required"
    r"|is not a code from its required value set)"
    r"|and \d+ more issues?"
)


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def patient(**elements: Any) -> dict[str, Any]:
    return {"resourceType": "Patient", "id": "p1", **elements}


def found(document: Any) -> list[tuple[str, str, str]]:
    return [(i.code, i.expression, i.diagnostics) for i in check(document)]


# -- the derived file ---------------------------------------------------------------------------


def derived() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(fhir_structure.DATA.read_text(encoding="utf-8"))
    return data


def schemas(definitions: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for name, definition in definitions.items():
        yield name, definition
        for element in definition.get("properties", {}).values():
            yield name, element
            if "items" in element:
                yield name, element["items"]


def test_the_file_is_hl7s_r4_schema_pinned_with_its_patches_listed() -> None:
    data = derived()
    assert data["source"]["url"] == "https://hl7.org/fhir/R4/fhir.schema.json.zip"
    assert data["source"]["sha256"] == (
        "2230406893b4cf002a4ee1e5e2bbeca22ac5d2d4931b3e9ef7b9594bbc376a01"
    )
    assert len(data["source"]["patches"]) == 7  # the 5 patches, the resource choice, descriptions
    assert len(data["resourceTypes"]) == 146
    assert {"Patient", "Bundle", "Parameters", "Communication"} <= set(data["resourceTypes"])


def test_the_file_uses_only_the_keywords_the_walker_supports() -> None:
    definitions = derived()["definitions"]
    for name, schema in schemas(definitions):
        assert set(schema) <= fhir_structure.KEYWORDS, (name, set(schema))
        assert schema.get("additionalProperties", False) is False, name
        assert schema.get("type", "string") in {
            "string",
            "object",
            "array",
            "boolean",
            "number",
            "integer",
        }
        if "$ref" in schema:
            assert set(schema) == {"$ref"}, name  # draft 6 ignores a $ref's siblings anyway
            assert schema["$ref"].removeprefix("#/definitions/") in definitions, name
        if "pattern" in schema:
            re.compile(schema["pattern"])
    assert definitions["ResourceList"] == {"resourceChoice": "resourceType"}


def test_the_patches() -> None:
    definitions = derived()["definitions"]
    assert definitions["Meta"]["type"] == "object"  # 1
    assert definitions["Meta"]["properties"]["_profile"]["items"] == {
        "$ref": "#/definitions/Element"
    }  # 2
    assert definitions["string"]["pattern"].startswith("(?a)")  # 3
    assert not definitions["code"]["pattern"].startswith("(?a)")
    assert definitions["positiveInt"] | {"pattern": ""} == {  # 4
        "type": "integer",
        "minimum": 1,
        "maximum": 2**31 - 1,
        "pattern": "",
    }
    # Only the keyword was removed: elements called `description` are still elements.
    assert definitions["Account"]["properties"]["description"] == {"$ref": "#/definitions/string"}


def test_the_generator_is_reproducible_from_the_cached_download() -> None:
    script = load_script("fhir_r4_schema")
    if not script.CACHE.exists():
        pytest.skip("run scripts/fhir_r4_schema.py once to cache HL7's schema")
    out = script.build(script.download())
    text = json.dumps(out, sort_keys=True, separators=(",", ":")) + "\n"
    assert text == fhir_structure.DATA.read_text(encoding="utf-8")


# -- jsonschema as the oracle -------------------------------------------------------------------


def load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def oracle() -> Callable[[Any], list[ValidationError]]:
    """Draft 6 over the derived file, with the resource choice as a keyword and FHIR JSON's
    rules for null padding and integers."""
    data = derived()
    definitions = data["definitions"]
    resource_types = set(data["resourceTypes"])

    def resource_choice(
        validator: Any, _: Any, instance: Any, __: Any
    ) -> Iterator[ValidationError]:
        if not validator.is_type(instance, "object"):
            yield ValidationError("not a resource")
            return
        kind = instance.get("resourceType")
        if not isinstance(kind, str) or kind not in resource_types:
            yield ValidationError("not an R4 resource type")
            return
        yield from validator.descend(instance, {"$ref": f"#/definitions/{kind}"})

    def padded(items: dict[str, Any]) -> bool:
        name = items.get("$ref", "").removeprefix("#/definitions/")
        target = definitions.get(name, items)
        return name == "Element" or ("properties" not in target and "resourceChoice" not in target)

    def items(validator: Any, schema: Any, instance: Any, _: Any) -> Iterator[ValidationError]:
        if not validator.is_type(instance, "array"):
            return
        for index, item in enumerate(instance):
            if item is None and padded(schema):
                continue
            yield from validator.descend(item, schema, path=index)

    integers = Draft6Validator.TYPE_CHECKER.redefine(
        "integer", lambda _, value: isinstance(value, int) and not isinstance(value, bool)
    )
    cls = validators.extend(
        Draft6Validator,
        {"resourceChoice": resource_choice, "items": items},
        type_checker=integers,
    )
    validator = cls({"definitions": definitions, "resourceChoice": "resourceType"})
    return lambda document: list(validator.iter_errors(document))


def verdict_of_oracle(errors: list[ValidationError]) -> tuple[bool, bool]:
    """(rejected, flagged): `required` and a string outside an `enum` are findings, not 400s."""
    findings = [
        e
        for e in errors
        if e.validator == "required" or (e.validator == "enum" and isinstance(e.instance, str))
    ]
    return len(findings) < len(errors), bool(errors)


def verdict(issues: list[Issue]) -> tuple[bool, bool]:
    return bool(fhir_structure.blocking(issues)), bool(issues)


@pytest.fixture(scope="module")
def jsonschema_check() -> Callable[[Any], list[ValidationError]]:
    return oracle()


@pytest.fixture(scope="module")
def samples(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The US Core fixtures, and what `scripts/sandbox_fhir_sample.py` and
    `scripts/davinci_sample.py` write for the HL7 validator in CI."""
    names = ("inbox.json", "referral.json", "prior-auth.json", "trial.json")
    documents = {name: load(name) for name in names}
    documents["sandbox-inbox"] = seed_bundle(INBOX, 20)
    documents["sandbox-referral"] = seed_bundle(REFERRAL, 40)
    documents["sandbox-ehr-profile"] = seed_bundle(EHR_PROFILE, 0)  # LP-23e
    davinci = tmp_path_factory.mktemp("davinci")
    # As CI gives it: the prior-authorization agent of the test fixtures, by its directory.
    agent = ROOT / "tests" / "fixtures" / "reference-agents" / "prior-auth-preflight"
    load_script("davinci_sample").main(davinci, agent)
    for path in sorted(davinci.glob("*.json")):
        documents[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return documents


def test_the_samples_are_clean_for_both(
    samples: dict[str, Any], jsonschema_check: Callable[[Any], list[ValidationError]]
) -> None:
    # 12 Da Vinci samples: PAS with every X12 code, only required, and a request that needs
    # attention with its completed QuestionnaireResponse; DTR with a warning too; CRD's
    # `indeterminate` coverage over a record that could not be read (LP-23d); and the sandbox's
    # vendor-shaped EHR profile (LP-23e)
    assert len(samples) == 19
    for name, document in samples.items():
        assert check(document) == [], name
        assert jsonschema_check(document) == [], name


def test_the_walker_and_jsonschema_agree_on_mistyped_fixtures(
    samples: dict[str, Any], jsonschema_check: Callable[[Any], list[ValidationError]]
) -> None:
    """Every element of the four US Core fixtures, replaced by each wrong JSON type."""
    compared = 0
    for name in ("inbox.json", "referral.json", "prior-auth.json", "trial.json"):
        seed = samples[name]
        for path in element_paths(seed):
            for wrong in WRONG_TYPES:
                mistyped = replaced(seed, path, wrong)
                theirs = verdict_of_oracle(jsonschema_check(mistyped))
                assert verdict(check(mistyped)) == theirs, (name, path, wrong)
                compared += 1
    assert compared > 1500


def test_the_walker_and_jsonschema_agree_on_mistyped_samples(
    samples: dict[str, Any], jsonschema_check: Callable[[Any], list[ValidationError]]
) -> None:
    """The sandbox and Da Vinci samples: one resource of each type in each, every element
    replaced by one wrong JSON type in turn (all four would take seconds and find nothing new)."""
    compared = 0
    for name, document in samples.items():
        if name.endswith(".json"):
            continue  # the fixtures, above
        for resource in one_of_each_type(document):
            for index, path in enumerate(element_paths(resource)):
                mistyped = replaced(resource, path, WRONG_TYPES[index % len(WRONG_TYPES)])
                theirs = verdict_of_oracle(jsonschema_check(mistyped))
                assert verdict(check(mistyped)) == theirs, (name, resource["resourceType"], path)
                compared += 1
    assert compared > 700


def one_of_each_type(document: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [e["resource"] for e in document.get("entry", [])] or [document]
    first: dict[str, dict[str, Any]] = {}
    for resource in entries:
        first.setdefault(resource["resourceType"], resource)
    return list(first.values())


def test_the_issue_codes_and_their_vocabulary() -> None:
    issues = found(
        patient(
            gender="F",
            text={"status": "generated"},
            birthDate="1st of May",
            active="yes",
            photo=[{"size": -1}],
            nickname="x",
        )
    )
    assert issues == [
        ("code-invalid", "Patient.gender", "gender is not a code from its required value set"),
        ("required", "Patient.text.div", "div is required"),
        ("value", "Patient.birthDate", "birthDate does not have the date format"),
        ("structure", "Patient.active", "active must be a boolean"),
        ("value", "Patient.photo[0].size", "size does not have the unsignedInt format"),
        ("structure", "Patient.nickname", "nickname is not an element of Patient in R4"),
    ]
    assert all(DIAGNOSTICS.fullmatch(diagnostics) for *_, diagnostics in issues)


def test_every_diagnostic_on_the_mistyped_fixtures_is_from_the_vocabulary(
    samples: dict[str, Any],
) -> None:
    seed = samples["referral.json"]
    for path in element_paths(seed):
        for wrong in WRONG_TYPES:
            for issue in check(replaced(seed, path, wrong)):
                assert DIAGNOSTICS.fullmatch(issue.diagnostics), issue


# -- FHIR JSON rules ----------------------------------------------------------------------------


def test_null_padding_is_valid_and_a_null_element_is_not() -> None:
    absent = {"url": us_core.DATA_ABSENT, "valueCode": "unknown"}
    extended = [None, {"extension": [absent]}]
    padded = patient(
        meta={"profile": ["http://example.org/p", None], "_profile": extended},
        name=[{"given": ["Ann", None], "_given": extended}],
    )
    assert check(padded) == []
    assert found(patient(gender=None)) == [
        ("structure", "Patient.gender", "gender must be a string")
    ]
    assert found(patient(meta=None)) == [("structure", "Patient.meta", "meta must be an object")]
    assert found(patient(name=[None])) == [
        ("structure", "Patient.name[0]", "name must be an object")
    ]


@pytest.mark.parametrize("value", [True, False])
def test_booleans_are_not_numbers(value: bool) -> None:
    for element in ("valueInteger", "valueDecimal", "valuePositiveInt", "valueUnsignedInt"):
        resource = patient(extension=[{"url": EXTENSION, element: value}])
        assert found(resource) == [
            ("structure", f"Patient.extension[0].{element}", f"{element} must be a number")
        ]


@pytest.mark.parametrize(
    ("element", "value", "valid"),
    [
        ("valueInteger", -3, True),
        ("valueInteger", 1.0, False),  # FHIR integers have no decimal point
        ("valueInteger", 1.5, False),
        ("valueInteger", 2**31, False),  # 32 bits
        ("valuePositiveInt", 0, False),
        ("valueUnsignedInt", 0, True),
        ("valueUnsignedInt", -1, False),
        ("valueDecimal", 1, True),
        ("valueDateTime", "2026-10-01T12:00:00Z", True),
        ("valueDateTime", "2026-10-01T12:00:00", False),  # a time needs its zone
        ("valueString", "No\u00a0break", True),  # (?a): a no-break space is not whitespace here
        ("valueString", "Soft\x0bbreak", True),  # a word processor's line break: a warning only
        ("valueMarkdown", "Page\x0cbreak", True),  # a scanned report's page break, likewise
        ("valueCode", " padded", False),
    ],
)
def test_primitive_formats(element: str, value: Any, valid: bool) -> None:
    """Each verdict is the HL7 validator's (6.10.4, R4)."""
    issues = found(patient(extension=[{"url": EXTENSION, element: value}]))
    if valid:
        assert issues == []
    else:
        primitive = element.removeprefix("value")
        primitive = primitive[0].lower() + primitive[1:]
        assert issues == [
            (
                "value",
                f"Patient.extension[0].{element}",
                f"{element} does not have the {primitive} format",
            )
        ]


def test_the_walk_is_iterative() -> None:
    nested: dict[str, Any] = {"url": EXTENSION, "valueString": "x"}
    for _ in range(1500):
        nested = {"url": EXTENSION, "extension": [nested]}
    assert check(patient(extension=[nested])) == []
    innermost = nested
    while "extension" in innermost:
        innermost = innermost["extension"][0]
    innermost["valueString"] = 7
    [issue] = check(patient(extension=[nested]))
    assert issue.expression == "Patient" + ".extension[0]" * 1501 + ".valueString"
    deep_list: Any = [patient()]
    for _ in range(5000):
        deep_list = [deep_list]
    assert check(deep_list) == []


# -- where resources are, and what an issue names -----------------------------------------------


def test_expressions_restart_at_each_resource() -> None:
    contained = {"resourceType": "Medication", "id": "m1", "code": "x"}
    parameters = {
        "resourceType": "Parameters",
        "parameter": [{"name": "p", "resource": patient(gender=7)}],
    }
    bundle = {
        "resourceType": "Bundle",
        "id": "b1",
        "type": "collection",
        "entry": [
            {"resource": patient(contained=[contained])},
            {"resource": parameters},
            {"resource": "Patient"},
            {"resource": {"id": "x"}},
        ],
    }
    issues = check(bundle)
    assert [(i.resource, i.expression, i.diagnostics) for i in issues] == [
        ("Medication/m1", "Medication.code", "code must be an object"),
        ("Patient/p1", "Patient.gender", "gender must be a string"),
        ("Bundle/b1", "Bundle.entry[2].resource", "resource must be an object"),
        ("Bundle/b1", "Bundle.entry[3].resource", "resourceType is missing"),
    ]
    assert issues[0].profile == "http://hl7.org/fhir/StructureDefinition/Medication"


def test_the_top_level_is_a_resource_a_list_or_a_prefetch_map() -> None:
    broken = patient(gender=7)
    prefetch = {
        "patient": broken,
        "unfilled": None,
        "search": {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": broken}]},
    }
    assert [i.expression for i in check(prefetch)] == ["Patient.gender"]  # the same issue once
    assert [i.expression for i in check([prefetch, broken])] == ["Patient.gender"]
    assert found({"resourceType": "Banana"}) == [
        ("structure", "resourceType", "resourceType is not an R4 resource type")
    ]
    assert found({"resourceType": ["Patient"]}) == [
        ("structure", "resourceType", "resourceType must be a string")
    ]
    assert check("Patient") == [] and check({}) == []  # no resource: the mapping reports that


def test_unknown_keys_are_named_only_when_they_look_like_element_names() -> None:
    issues = found(patient(**{"_nickname": {}, SECRET: 1, "x" * 65: 1, "a\n": 1}))
    assert issues == [
        ("structure", "Patient._nickname", "_nickname is not an element of Patient in R4"),
        ("structure", "Patient", "an element is not an element of Patient in R4"),  # once
    ]
    [backbone] = check({"resourceType": "Bundle", "type": "collection", "entry": [{"oops": 1}]})
    assert backbone.diagnostics == "oops is not an element of Bundle_Entry in R4"


@pytest.mark.parametrize("resource_id", [SECRET, "x" * 65, "p1\n", 7])
def test_a_resource_is_labelled_by_its_id_only_when_it_is_an_id(resource_id: Any) -> None:
    [*_, issue] = check({**patient(gender=7), "id": resource_id})
    assert issue.resource == "Patient"


def test_issues_never_contain_values_or_client_text(samples: dict[str, Any]) -> None:
    """A marked synthetic string as every value, as an unknown key at every level, as a
    resource type and as an id: it is never in an issue."""
    seed = samples["referral.json"]
    texts: list[str] = []
    for path in element_paths(seed):
        texts += [json.dumps([i.to_dict() for i in check(replaced(seed, path, SECRET))])]
        parent = path[:-1]
        node: Any = seed
        for key in parent:
            node = node[key]
        if isinstance(node, dict):
            keyed = replaced(seed, (*parent, SECRET), 1)
            texts += [json.dumps([i.to_dict() for i in check(keyed)])]
    texts += [json.dumps([i.to_dict() for i in check({"resourceType": SECRET, "id": SECRET})])]
    assert texts and not any("Synthetic secret" in text for text in texts)


def test_after_100_issues_one_issue_counts_the_rest() -> None:
    resource = patient(**{f"unknown{n}": 1 for n in range(100)}, gender="F", active="x")
    issues = check(resource)
    assert len(issues) == 101
    assert issues[-1].diagnostics == "and 2 more issues"
    # The count's code is a blocking one when any of the rest blocks, so the input is a 400.
    assert (issues[-1].code, issues[-1].expression) == ("structure", "Patient.active")
    findings = check(patient(**{f"unknown{n}": 1 for n in range(100)}, gender="F"))
    assert (findings[-1].code, findings[-1].diagnostics) == ("code-invalid", "and 1 more issue")


def test_a_profile_issue_replaces_the_same_base_issue() -> None:
    resource = patient(gender="F", text={"status": "generated"})
    base, profiled = check(resource), us_core.validate(resource)
    assert [(i.code, i.expression) for i in base] == [
        ("code-invalid", "Patient.gender"),
        ("required", "Patient.text.div"),
    ]
    assert ("code-invalid", "Patient.gender") in [(i.code, i.expression) for i in profiled]
    merged = fhir_structure.merge(base, profiled)
    assert [(i.expression, i.profile.rsplit("/", 1)[-1]) for i in merged] == [
        *[(i.expression, "us-core-patient") for i in profiled],
        ("Patient.text.div", "Patient"),  # US Core says nothing about the narrative
    ]


# -- decisions ----------------------------------------------------------------------------------


def referral(**changes: Any) -> dict[str, Any]:
    bundle = copy.deepcopy(load("referral.json"))
    found_patient = next(
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
    )
    found_patient.update(changes)
    return bundle


def test_a_malformed_primitive_is_a_structure_error_in_every_mode() -> None:
    for mode in ("off", "warn", "enforce"):
        with pytest.raises(FhirStructureError) as raised:
            Decision.from_hub(REFERRAL_PACK, fhir_validation=mode).decide_fhir(
                referral(birthDate="May 1st")
            )
        assert raised.value.issues == [
            {
                "code": "value",
                "expression": "Patient.birthDate",
                "diagnostics": "birthDate does not have the date format",
            }
        ]


def test_base_r4_findings_follow_fhir_validation() -> None:
    bundle = referral(text={"status": "generated"})  # a narrative without its div
    off, _ = Decision.from_hub(REFERRAL_PACK, fhir_validation="off").decide_fhir(bundle)
    warn, _ = Decision.from_hub(REFERRAL_PACK, fhir_validation="warn").decide_fhir(bundle)
    assert off.input_issues == ()
    assert [(i["expression"], i["code"], i["profile"]) for i in warn.input_issues] == [
        ("Patient.text.div", "required", "http://hl7.org/fhir/StructureDefinition/Patient")
    ]
    assert (warn.outcome, warn.recommendation) == (off.outcome, off.recommendation)
    enforced, _ = Decision.from_hub(REFERRAL_PACK, fhir_validation="enforce").decide_fhir(bundle)
    assert enforced.outcome is Outcome.BLOCK and enforced.answers == {}
    assert enforced.reasons == (
        "input rejected: not R4 Patient: Patient/ref-p1 Patient.text.div: div is required",
    )


def test_each_rejection_names_its_own_profile() -> None:
    bundle = referral(text={"status": "generated"})
    patient_resource = next(
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
    )
    patient_resource.pop("name")
    result, _ = Decision.from_hub(REFERRAL_PACK, fhir_validation="enforce").decide_fhir(bundle)
    assert result.reasons == (
        "input rejected: not US Core 6.1.0: Patient/ref-p1 Patient.name: name is required "
        "(US Core Patient Profile)",
        "input rejected: not R4 Patient: Patient/ref-p1 Patient.text.div: div is required",
    )


def test_an_issue_keeps_its_code_in_the_operation_outcome(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "s.db"), raise_server_exceptions=False)
    url = f"/api/packs/{REFERRAL_PACK}/fhir/decide"
    response = client.post(url, json=referral(birthDate="May 1st", nickname="Al"))
    assert response.status_code == 400
    assert [(i["code"], i["expression"]) for i in response.json()["issue"]] == [
        ("value", ["Patient.birthDate"]),
        ("structure", ["Patient.nickname"]),
    ]


# -- HL7's R4 examples --------------------------------------------------------------------------


@pytest.mark.slow
def test_hl7s_r4_examples() -> None:
    """5,309 examples: three are rejected, and the HL7 validator rejects them too (codes with a
    no-break space in CodeSystem v2-0550, alone and in a Bundle, and an id of 67 characters)."""
    if not EXAMPLES.exists():
        pytest.skip("hl7.fhir.r4.examples-4.0.1.tgz is not in var/fhir-packages")
    rejected, flagged, total = [], [], 0
    with tarfile.open(EXAMPLES) as archive:
        for member in archive.getmembers():
            if not member.name.endswith(".json") or member.name.endswith(
                ("package.json", ".index.json")
            ):
                continue
            handle = archive.extractfile(member)
            assert handle is not None
            document = json.load(io.TextIOWrapper(handle, encoding="utf-8"))
            if not isinstance(document, dict) or "resourceType" not in document:
                continue
            total += 1
            issues = check(document)
            if fhir_structure.blocking(issues):
                rejected.append(Path(member.name).name)
            elif issues:
                flagged.append(Path(member.name).name)
    assert total == 5309
    assert sorted(rejected) == [
        "Bundle-v2-valuesets.json",
        "CodeSystem-v2-0550.json",
        "SearchParameter-questionnaireresponse-extensions-QuestionnaireResponse-item-subject.json",
    ]
    assert flagged == []


def test_an_extensible_binding_takes_codes_beyond_the_example_list() -> None:
    """R4 binds Expression.language as extensible; CQL and SDC use media types the schema's list
    leaves out, and that is valid R4."""
    expression = {"language": "text/cql-identifier", "expression": "Eligible"}
    extension = {"url": "http://hl7.org/fhir/StructureDefinition/cqf-expression"}
    resource = patient(extension=[{**extension, "valueExpression": expression}])
    assert check(resource) == []


def test_a_required_canonical_may_be_carried_by_its_extension_alone() -> None:
    """Like every primitive, a required canonical is present when only `_x` is."""
    absent = {"extension": [{"url": us_core.DATA_ABSENT, "valueCode": "unknown"}]}
    report = {
        "resourceType": "MeasureReport",
        "id": "r1",
        "status": "complete",
        "type": "summary",
        "_measure": absent,
        "period": {"start": "2026-01-01"},
    }
    assert check(report) == []


R4_CORE = Path(__file__).parents[1] / "var" / "fhir-packages" / "hl7.fhir.r4.core-4.0.1.tgz"


@pytest.mark.skipif(not R4_CORE.exists(), reason="hl7.fhir.r4.core is not cached in var/")
def test_every_enumeration_is_a_required_binding_in_r4() -> None:
    """A code outside an enumeration is reported as outside a required value set, so each one
    must be a required binding in R4 itself (the schema enumerated one extensible binding)."""
    strength: dict[str, str] = {}
    with tarfile.open(R4_CORE) as archive:
        for member in archive.getmembers():
            if not (member.isfile() and member.name.startswith("package/StructureDefinition-")):
                continue
            handle = archive.extractfile(member)
            definition = json.loads(handle.read()) if handle else {}
            if definition.get("derivation") == "constraint":
                continue
            for element in definition.get("snapshot", {}).get("element", []):
                if element.get("binding"):
                    strength[element["path"]] = element["binding"]["strength"]
    unresolved = []
    for name, definition in fhir_structure.rules().definitions.items():
        root, *backbones = name.split("_")
        for element, schema in definition.get("properties", {}).items():
            if "enum" not in (schema.get("items") or schema):
                continue
            tail = ".".join([b[0].lower() + b[1:] for b in backbones] + [element])
            found = {
                strength[p] for p in strength if p.split(".")[0] == root and p.endswith("." + tail)
            }
            if not found:
                unresolved.append(f"{root}.{tail}")  # the schema's alias for a backbone
            assert found <= {"required"}, (root, tail, found)
    assert unresolved == ["CapabilityStatement.interaction1.code"]  # rest.interaction: required
