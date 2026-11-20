# SPDX-License-Identifier: Apache-2.0
"""Decision Pack manifest, spec versions 0.1, 0.2 and 0.3.

A pack declares *questions* (a fixed set of labels, each with human-readable criteria).
Providers answer questions with probability distributions; the policy turns answers into
one of four outcomes. Spec 0.2 adds `boolean` and `score` question types and *derived*
questions computed from other answers by a decision table. See docs/spec/decision-pack-0.2.md.
Spec 0.3 changes the FHIR mapping (docs/spec/decision-pack-0.3.md), which
`standards/fhir.py` reads and checks, and adds two card keys (§5: `refused` and
`cds_hooks.card.also_show`); its manifest is otherwise a 0.2 manifest.
"""

from __future__ import annotations

import re
from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal, TypeGuard

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mendari.core.templates import (
    display_choices,
    longest,
    template_problems,
    unknown_placeholders,
)

SPEC_VERSION = "0.3"  # the newest spec version this engine reads
MAX_VERSION_CHARS = 64
MAX_DERIVE_ROWS = 10

_PACK_ID = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
# An SPDX license expression (`license`, at most `MAX_LICENSE_CHARS`): identifiers (or
# `LicenseRef-…`, optionally after `DocumentRef-…:`), `AND`, `OR`, `WITH` an exception, and
# parentheses. The syntax is checked, not the identifiers against SPDX's list.
MAX_LICENSE_CHARS = 200
_SPDX_TOKEN = re.compile(r"\(|\)|[^\s()]+")
_SPDX_ID = re.compile(
    r"^(?:(?:DocumentRef-[A-Za-z0-9.-]+:)?LicenseRef-[A-Za-z0-9.-]+|[A-Za-z0-9][A-Za-z0-9.-]*\+?)$"
)
_SPDX_OPERATORS = frozenset({"AND", "OR", "WITH"})


def _oid(oid: str, *, children: bool = False) -> str:
    """An OID as files spell it, bare or as `urn:oid:`, never a longer OID it begins (unless
    `children`: then its sub-OIDs too, as MedDRA's translations are numbered)."""
    child = r"(?:\.\d+)?" if children else ""
    return r"(?<![\d.])" + re.escape(oid) + child + r"(?!\d|\.\d)"


# Code systems whose codes are licensed, not free to redistribute everywhere: a pack that names
# one (in `code_systems`, or by its url or OID anywhere in its files) declares it with a
# `licence_note` saying the terms it is used under (LP-14). A bare code value names no system:
# declaring the system it comes from is the author's to do. Canonical url -> (name, spellings),
# the spellings taken from HL7's terminology registry (THO) and the systems' own urls.
_THO = r"terminology\.hl7\.org/CodeSystem/"
LICENSED_CODE_SYSTEMS: dict[str, tuple[str, re.Pattern[str]]] = {
    "http://www.ama-assn.org/go/cpt": (
        "CPT (American Medical Association)",
        re.compile(r"ama-assn\.org/go/cpt|" + _oid("2.16.840.1.113883.6.12")),
    ),
    "http://snomed.info/sct": (
        "SNOMED CT (SNOMED International)",
        re.compile(r"snomed\.info/(?:x?sct|id/)|" + _oid("2.16.840.1.113883.6.96")),
    ),
    "http://terminology.hl7.org/CodeSystem/mdr": (
        "MedDRA (ICH)",
        re.compile(
            _THO
            + r"(?:mdr|(?:MTH)?MDR[A-Z]*)(?![\w-])|meddra\.org|"
            + _oid("2.16.840.1.113883.6.163", children=True)
            + "|"
            + _oid("2.16.840.1.113883.6.164")
            + "|"
            + _oid("2.16.840.1.113883.6.165")
        ),
    ),
    "https://codesystem.x12.org": (
        "X12 code lists (X12)",
        re.compile(
            r"(?:codesystem|valueset)\.x12\.org|x12\.org/codes/|"
            + _THO
            + r"X12\.|"
            + _oid("2.16.840.1.113883.6.255")
        ),
    ),
    "http://www.ada.org/cdt": (
        "CDT (American Dental Association)",
        re.compile(r"ada\.org/cdt|" + _THO + r"CD2(?![\w-])|" + _oid("2.16.840.1.113883.6.13")),
    ),
}
# Text that every spelling above contains, with no slash (a file may escape slashes): a file
# holding none of them is not searched.
LICENSED_HINTS = (
    b"ama-assn.org",
    b"snomed.info",
    b"113883.6.",
    b"terminology.hl7.org",
    b"meddra",
    b"x12.org",
    b"ada.org",
)
# A slash as files may escape it: JSON's `\/` (any number of backslashes), `%2F`, `&#47;`.
_ESCAPED_SLASH = re.compile(r"\\+/|%2[Ff]|&#(?:47|[xX]0*2[Ff]);")


def licensed_systems(text: str) -> set[str]:
    r"""The licensed code systems `text` names, by canonical url. An escaped slash (`\/`,
    `%2F`, `&#47;`) is read as a slash, so `http:\/\/snomed.info\/sct` names SNOMED CT."""
    text = _ESCAPED_SLASH.sub("/", text)
    return {url for url, (_, spelled) in LICENSED_CODE_SYSTEMS.items() if spelled.search(text)}


def is_spdx_expression(text: str) -> bool:
    """Whether `text` is an SPDX license expression (syntax only), read without recursion."""
    tokens = _SPDX_TOKEN.findall(text)
    if not tokens:
        return False
    depth = 0
    position = 0
    expect_term = True  # a term (an id or an opening parenthesis) comes next, else an operator
    while position < len(tokens):
        token = tokens[position]
        if expect_term:
            if token == "(":
                depth += 1
            elif token not in _SPDX_OPERATORS and token != ")" and _SPDX_ID.match(token):
                expect_term = False
                if position + 1 < len(tokens) and tokens[position + 1] == "WITH":
                    exception = tokens[position + 2] if position + 2 < len(tokens) else ""
                    if exception in _SPDX_OPERATORS or not _SPDX_ID.match(exception):
                        return False
                    position += 2
            else:
                return False
        elif token == ")" and depth > 0:
            depth -= 1
        elif token in {"AND", "OR"}:
            expect_term = True
        else:
            return False
        position += 1
    return not expect_term and depth == 0


def readable(key: str) -> str:
    """A key as words: `medical_assistant` reads "Medical assistant"."""
    words = key.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def is_pack_id(text: object) -> TypeGuard[str]:
    """`namespace/name`, lowercase letters, digits and dashes (the manifest's `id`)."""
    return isinstance(text, str) and bool(_PACK_ID.match(text))


def is_version(text: object) -> TypeGuard[str]:
    """A semantic version (the manifest's `version`) of at most `MAX_VERSION_CHARS`."""
    return isinstance(text, str) and len(text) <= MAX_VERSION_CHARS and bool(_SEMVER.match(text))


def version_key(version: str) -> tuple[tuple[int, str], ...]:
    """Orders versions by their numbers, as integers would, without converting them: `int()`
    refuses more than 4,300 digits, and a version string comes from a pack or a registry."""
    parts = (part.lstrip("0") for part in re.findall(r"\d+", version.split("-")[0]))
    return tuple((len(part), part) for part in parts)


# Variant keys that once named the environment variables a remote provider reads its endpoint,
# model or key from. Those variables are the deployment's (spec section 5), so a pack that sets
# one is invalid, whatever its spec version: it could send any process secret anywhere.
DEPLOYMENT_ENV_KEYS: dict[str, frozenset[str]] = {
    "jev": frozenset({"api_key_env", "connector_env", "endpoint_env", "model_env"}),
    "llm": frozenset({"api_key_env", "base_url_env", "local_key_env", "model_env"}),
}


def deployment_env_problem(config: Any) -> str | None:
    """Why a provider config (a variant's, or a composite member's) is refused, if its `type`
    is `jev` or `llm` and it names any of `DEPLOYMENT_ENV_KEYS`; None otherwise. Never echoes
    the values."""
    if not isinstance(config, dict):
        return None
    chosen = sorted(set(config) & DEPLOYMENT_ENV_KEYS.get(str(config.get("type")), frozenset()))
    if not chosen:
        return None
    return (
        "a pack may not choose the environment variables a provider reads its endpoint, model "
        "or key from; these are the deployment's. Remove: " + ", ".join(chosen)
    )


class Outcome(StrEnum):
    """What the runtime is allowed to do with a decision."""

    AUTO = "AUTO"  # act without a human in the loop
    HUMAN_REVIEW = "HUMAN_REVIEW"  # a human confirms or corrects the recommendation
    ABSTAIN = "ABSTAIN"  # no recommendation; hand off to the existing manual workflow
    BLOCK = "BLOCK"  # the action must not happen (invalid input or a blocking policy rule)


class RiskLevel(IntEnum):
    ADMINISTRATIVE = 0
    CLINICAL_INFORMATION = 1
    CLINICAL_SUPPORT = 2
    PATIENT_PREDICTION = 3
    TIME_CRITICAL = 4


class Priority(StrEnum):
    """FHIR R4 request-priority codes, so decisions map onto Task.priority unchanged."""

    ROUTINE = "routine"
    URGENT = "urgent"
    ASAP = "asap"
    STAT = "stat"


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, allow_inf_nan=False
    )


class Publisher(_Strict):
    name: str
    type: Literal["community", "verified", "institutional"] = "community"
    url: str | None = None


class Risk(_Strict):
    level: RiskLevel
    category: str
    rationale: str


class Evidence(_Strict):
    title: str
    url: str | None = None
    note: str | None = None


class CodeSystemUse(_Strict):
    """A code system the pack's inputs, rules or data use (`code_systems`): its canonical url, its
    name and, for a licensed one (`LICENSED_CODE_SYSTEMS`), the terms the pack uses it under."""

    url: str = Field(min_length=1, max_length=500, pattern=r"^(?:https?://|urn:)\S+$")
    name: str = Field(min_length=1, max_length=200)
    licence_note: str | None = Field(default=None, min_length=1, max_length=1000)


# What a pack's criteria, examples and datasets were built from (`card.provenance`).
Provenance = Literal["synthetic", "de_identified", "site_records"]


class Card(_Strict):
    """Human-facing Decision Card content that cannot be derived from the rest of the pack."""

    intended_use: str
    intended_users: list[str] = Field(min_length=1)
    patient_population: str
    out_of_scope: list[str] = []
    known_failure_modes: list[str] = []
    evidence: list[Evidence] = []
    # Every surface derives the pack's status line from this (`core/status.py`); a pack never
    # writes that line itself. None: not stated.
    provenance: Provenance | None = None


class Inputs(_Strict):
    json_schema: str = Field(alias="schema")
    fhir: str | None = None


class ScoreLevel(_Strict):
    score: float
    criterion: str = Field(min_length=1)


class DeriveRow(_Strict):
    """One decision-table row: `{any: L, then: X}`, `{all: L, then: X}` or `{else: X}`.

    `L` is a label or a list of labels (`any: ["no", unknown]`: a member is `no` or `unknown`).
    `in: [members]` limits a row to some of the members, for tables whose members answer in
    different directions (e.g. criteria where `no` fails, and exclusions where `yes` fails).
    `when: {member: [labels], ...}` makes a row count only while each named member's answer is
    one of its labels, e.g. a document only one specialty requires:
    `{any: "no", in: [ecg_on_file], when: {specialty: [cardiology]}, then: return_for_info}`.
    """

    any: str | list[str] | None = None
    all: str | list[str] | None = None
    then: str | None = None
    otherwise: str | None = Field(default=None, alias="else")
    scope: list[str] | None = Field(default=None, alias="in", min_length=1)
    when: dict[str, list[str]] | None = None

    @model_validator(mode="after")
    def _shape(self) -> DeriveRow:
        if self.otherwise is not None:
            if self.any is not None or self.all is not None or self.then is not None:
                raise ValueError("an `else` row takes no other keys")
            if self.scope is not None:
                raise ValueError("an `else` row applies to all members; it takes no `in`")
            if self.when is not None:
                raise ValueError("an `else` row applies always; it takes no `when`")
        elif (self.any is None) == (self.all is None) or self.then is None:
            raise ValueError(
                "a row is {any: <label>, then: <label>}, {all: <label>, then: <label>} "
                "or {else: <label>}"
            )
        if self.otherwise is None and not self.labels:
            raise ValueError("`any` / `all` need at least one label")
        if self.when is not None and (not self.when or not all(self.when.values())):
            raise ValueError(
                "`when` is {<member>: [<label>, ...], ...}: each with one or more labels"
            )
        return self

    @property
    def result(self) -> str:
        return self.otherwise if self.otherwise is not None else str(self.then)

    @property
    def labels(self) -> frozenset[str]:
        """The labels `any` / `all` match."""
        value = self.any if self.any is not None else self.all
        if value is None:
            return frozenset()
        return frozenset([value] if isinstance(value, str) else value)

    @property
    def gates(self) -> dict[str, frozenset[str]]:
        """The `when` conditions, member -> labels; empty for a row that always counts."""
        return {member: frozenset(labels) for member, labels in (self.when or {}).items()}

    def applies_to(self, member: str) -> bool:
        return self.scope is None or member in self.scope

    def describe(self) -> str:
        if self.otherwise is not None:
            return f"else -> {self.otherwise}"
        value = self.any if self.any is not None else self.all
        label = value if isinstance(value, str) else "|".join(value or [])
        kind = "any" if self.any is not None else "all"
        within = f" in [{', '.join(self.scope)}]" if self.scope else ""
        gate = " and ".join(
            f"{member} in [{', '.join(labels)}]" for member, labels in (self.when or {}).items()
        )
        return f"{kind} {label}{within}{f' when {gate}' if gate else ''} -> {self.then}"


class Derive(_Strict):
    """A question computed from other questions' answers: the first matching row wins."""

    members: list[str] = Field(alias="from", min_length=1)
    table: list[DeriveRow] = Field(min_length=1)

    @model_validator(mode="after")
    def _table(self) -> Derive:
        if len(set(self.members)) != len(self.members):
            raise ValueError("derive `from` lists a question more than once")
        if self.table[-1].otherwise is None:
            raise ValueError("the last row of a derive table must be {else: <label>}")
        if any(row.otherwise is not None for row in self.table[:-1]):
            raise ValueError("only the last row may be an `else` row")
        if len(self.table) - 1 > MAX_DERIVE_ROWS:
            raise ValueError(f"a derive table has at most {MAX_DERIVE_ROWS} rows plus `else`")
        for row in self.table:
            outside = sorted(set(row.scope or []) - set(self.members))
            if outside:
                raise ValueError(f"row `in` names questions not in `from`: {outside}")
        gated = {member for row in self.table for member in row.gates}
        if gated - set(self.members):
            raise ValueError(
                f"`when` names questions not in `from`: {sorted(gated - set(self.members))}"
            )
        return self


class Question(_Strict):
    type: Literal["choice", "boolean", "score"] = "choice"
    instructions: str = Field(min_length=1)
    criteria: dict[str, str]
    levels: dict[str, ScoreLevel] | None = None
    derive: Derive | None = None
    # What a person reads instead of the key and the labels (`core/status.py` `title`,
    # `Question.name_of`): optional, and a readable form of the key when absent.
    display: str | None = Field(default=None, min_length=1, max_length=80)
    names: dict[str, Annotated[str, Field(min_length=1, max_length=80)]] = {}

    @model_validator(mode="before")
    @classmethod
    def _implied_criteria(cls, data: Any) -> Any:
        """`boolean` questions always have labels yes/no; `score` labels come from levels."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if data.get("type") == "boolean":
            given = data.get("criteria") or {}
            extra = set(given) - {"yes", "no"}
            if extra:
                raise ValueError(f"a boolean question only has labels yes/no, not {sorted(extra)}")
            data["criteria"] = {"yes": given.get("yes", "Yes."), "no": given.get("no", "No.")}
        elif data.get("type") == "score" and "criteria" not in data:
            levels = data.get("levels") or {}
            data["criteria"] = {
                label: level.get("criterion", "") if isinstance(level, dict) else ""
                for label, level in levels.items()
            }
        return data

    @field_validator("criteria")
    @classmethod
    def _labels(cls, criteria: dict[str, str]) -> dict[str, str]:
        if not 2 <= len(criteria) <= 255:
            raise ValueError("a question needs between 2 and 255 labels")
        for label, description in criteria.items():
            if not _KEY.match(label):
                raise ValueError(f"label {label!r} must match {_KEY.pattern}")
            if not description.strip():
                raise ValueError(f"label {label!r} needs a non-empty criterion")
        return criteria

    @model_validator(mode="after")
    def _named_labels(self) -> Question:
        unknown = sorted(set(self.names) - set(self.criteria))
        if unknown:
            raise ValueError(f"`names` names labels the question does not have: {unknown}")
        return self

    def name_of(self, label: str) -> str:
        """What a person reads for one of this question's labels."""
        return self.names.get(label) or readable(label)

    @model_validator(mode="after")
    def _levels(self) -> Question:
        if self.type == "score":
            if not self.levels or set(self.levels) != set(self.criteria):
                raise ValueError("a score question needs `levels` for exactly its labels")
        elif self.levels is not None:
            raise ValueError("only score questions have `levels`")
        return self

    @property
    def labels(self) -> list[str]:
        """Labels in declaration order; score questions are ordered by score."""
        levels = self.levels
        if self.type == "score" and levels:
            return sorted(levels, key=lambda label: levels[label].score)
        return list(self.criteria)

    def expected_score(self, probabilities: dict[str, float]) -> float | None:
        levels = self.levels
        if self.type != "score" or not levels:
            return None
        return sum(p * levels[label].score for label, p in probabilities.items() if label in levels)


class ProviderVariant(BaseModel):
    """One concrete way to answer a slot's questions. Extra keys are provider-specific config."""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: str

    @model_validator(mode="after")
    def _no_deployment_env(self) -> ProviderVariant:
        """Checked at load, for the variant and each composite member (one level deep)."""
        extra = self.model_extra or {}
        members = extra.get("members")
        configs = [{"type": self.type, **extra}]
        if isinstance(members, dict):
            configs += [member for member in members.values() if isinstance(member, dict)]
        for config in configs:
            problem = deployment_env_problem(config)
            if problem is not None:
                raise ValueError(problem)
        return self


class ProviderSlot(_Strict):
    answers: list[str] = Field(min_length=1)
    use: str
    variants: dict[str, ProviderVariant] = Field(min_length=1)

    @model_validator(mode="after")
    def _default_exists(self) -> ProviderSlot:
        if self.use not in self.variants:
            raise ValueError(f"default variant {self.use!r} is not one of {sorted(self.variants)}")
        return self


class Condition(_Strict):
    question: str
    labels: list[str] = []
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # The total probability of `labels`, whichever label is most likely: acts on a meaningful
    # chance of a critical label (e.g. P(urgent) >= 0.35), not only when that label wins.
    min_probability: float | None = Field(default=None, gt=0.0, le=1.0)
    # Score questions: inclusive bounds on the expected score.
    min_score: float | None = None
    max_score: float | None = None

    @model_validator(mode="after")
    def _something_to_match(self) -> Condition:
        if not self.labels and self.min_score is None and self.max_score is None:
            raise ValueError("a condition needs `labels`, `min_score` or `max_score`")
        if self.min_probability is not None:
            if not self.labels:
                raise ValueError("`min_probability` needs the `labels` it applies to")
            if self.min_confidence:
                raise ValueError("use `min_probability` or `min_confidence`, not both")
        return self


class PolicyRule(_Strict):
    name: str
    when: Condition
    outcome: Outcome
    recommendation: str | None = None
    priority: Priority | None = None
    reason: str


class Thresholds(_Strict):
    auto: float = Field(ge=0.0, le=1.0)
    review: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> Thresholds:
        if self.review > self.auto:
            raise ValueError("thresholds.review must be <= thresholds.auto")
        return self


class LabelPolicy(_Strict):
    allow_auto: bool = True
    auto_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class Policy(_Strict):
    primary: str
    thresholds: Thresholds
    rules: list[PolicyRule] = []
    labels: dict[str, LabelPolicy] = {}
    # AUTO only while these slots run one of these variants, e.g. a safety screen that includes
    # a model: {triage_screen: [rules_and_jev, rules_and_llm]}. Other variants still decide,
    # but every decision goes to a person.
    automation_requires: dict[str, list[str]] = {}
    fallback: str
    default_priority: Priority = Priority.ROUTINE


class CriticalLabel(_Strict):
    """A (question, label) whose true cases a person must see: never automated in the pack's
    default deployment, and under a governance approval only with the correct primary
    recommendation (spec section 8). Tracked in every eval."""

    question: str
    label: str


class Evaluation(_Strict):
    datasets: dict[str, str] = {}
    benchmark: str | None = None
    benchmark_dataset: str | None = None
    critical: list[CriticalLabel] = []
    subgroups: list[str] = []
    degradation_tolerance: float = Field(default=0.03, gt=0.0, lt=1.0)


class AuditConfig(_Strict):
    store_input: Literal["full", "hash"] = "full"


# The most characters a CDS Hooks card's summary has: CDS Hooks 2.0 says under 140 (Inferno's CRD
# kit fails a summary of 140), checked at load for every text a pack's summary can give
# (`card_summary_problems`).
SUMMARY_CHARS = 139


# The card key of a decision that refused its input (a BLOCK before any provider ran), for a card
# template that names it (spec 0.3); one that does not reads such a decision as `none`, as before.
REFUSED = "refused"


class RuleCard(_Strict):
    """A card for a decision a policy rule matched whose recommendation `show` leaves out (spec
    0.3): its own summary (a template, as `summaries` are) and indicator."""

    summary: str = Field(min_length=1)
    indicator: Literal["info", "warning", "critical"] = "warning"


class CdsCard(_Strict):
    """Card wording for one pack. Keys are the primary question's labels, `none` when the
    decision has no recommendation, or `refused` (spec 0.3) when it refused its input;
    `{recommendation}` and `{priority}` are filled in."""

    summaries: dict[str, str] = Field(min_length=1)
    indicators: dict[str, Literal["info", "warning", "critical"]] = {}
    # Recommendations that produce a card at all; e.g. a patient-view service stays silent for
    # patients who are clearly not eligible. None: every decision produces a card.
    show: list[str] | None = None
    # Policy rules (by name) whose match still shows a card for a recommendation `show` leaves
    # out, in the rule's own words (spec 0.3); a recommendation `show` names keeps its own card.
    also_show: dict[str, RuleCard] = {}
    suggestion: str = "Create a task"

    def names_refused(self) -> bool:
        """Whether the template words refused input apart from `none`."""
        return REFUSED in {*self.summaries, *self.indicators, *(self.show or [])}

    def summary_templates(self) -> dict[str, str]:
        """Every summary a card can take, by where the manifest writes it."""
        return {f"summaries.{key}": text for key, text in self.summaries.items()} | {
            f"also_show.{rule}.summary": card.summary for rule, card in self.also_show.items()
        }


class GuardConfig(_Strict):
    """An API guard in front of an LLM (spec 0.2): `draft` is the input field holding the reply
    being checked, and a decision releases it only when it is AUTO with a `release` label."""

    draft: str = Field(min_length=1)
    release: list[str] = Field(min_length=1)


class CoverageInformation(_Strict):
    """Da Vinci CRD `coverage-information` for one recommendation of an order hook (spec 0.2):
    what the pack's answer tells the EHR about the order's coverage. Codes are CRD's."""

    covered: Literal["covered", "not-covered", "conditional"]
    pa_needed: Literal["no-auth", "auth-needed", "satisfied", "performpa", "conditional"] | None = (
        None
    )
    doc_needed: list[Literal["clinical", "admin", "patient", "conditional"]] = []
    doc_purpose: list[Literal["withpa", "withclaim", "withorder", "retain-doc", "OTH"]] = []
    info_needed: list[
        Literal["performer", "location", "timeframe", "contract", "detail-code", "OTH"]
    ] = []
    reason: str | None = Field(default=None, max_length=500)  # why, in words (no case data)


class QuestionnaireConfig(_Strict):
    """Da Vinci DTR for an order-hook pack whose primary question is derived (spec 0.2): the
    documentation the payer asks for is every member that can turn the decision into `missing`
    for the order's `context` (e.g. the service and the payer)."""

    missing: str  # the recommendation that means documentation is missing
    context: list[str] = Field(min_length=1)  # questions that pick the requirement set
    title: str = Field(min_length=1, max_length=200)


class CdsHooksConfig(_Strict):
    hook: str
    title: str
    description: str
    prefetch: dict[str, str] = {}
    card: CdsCard | None = None
    # Order hooks only: the order comes back with CRD coverage information (a system action) for
    # these recommendations. Needs a `coverage` prefetch (the patient's Coverage).
    coverage_information: dict[str, CoverageInformation] = {}
    # DTR: `$questionnaire-package` serves a questionnaire of the documentation the payer asks for.
    questionnaire: QuestionnaireConfig | None = None


# A CDS Hooks prefetch template (`cds_hooks.prefetch`): a read (`Type/id`, `Type/{{context.x}}`)
# or a search (`Type?query`) relative to the EHR's FHIR server, and nothing else. Its query holds
# no `/`, `?`, `#`, backslash, whitespace or control character, and `prefetch_problems` refuses
# `..` anywhere.
PREFETCH = re.compile(
    r"^[A-Z][A-Za-z]{0,63}(/(\{\{context\.[A-Za-z][A-Za-z0-9]*\}\}|[A-Za-z0-9.-]{1,64}))?"
    r"(\?[^/?#\s\\\x00-\x1f\x7f]*)?$"
)
_PREFETCH_SHOWN = 60  # how much of a refused template a problem shows


def prefetch_problems(hooks: CdsHooksConfig | None) -> list[str]:
    """Each prefetch template that is not a relative read or search on the EHR's FHIR server
    (`PREFETCH`): an absolute URL, `//` or a leading `/`, `.` or `..`, an operation
    (`$everything`), a fragment (`#`), whitespace. `core/pack.py` refuses a pack the deployment
    does not own for one at load, and warns of one in an owned pack; the shipped packs' all
    pass."""
    if hooks is None:
        return []
    problems = []
    for key, template in sorted(hooks.prefetch.items()):
        segments = template.partition("?")[0].split("/")
        if PREFETCH.fullmatch(template) and ".." not in template and "." not in segments:
            continue
        shown = template if len(template) <= _PREFETCH_SHOWN else template[:_PREFETCH_SHOWN] + "..."
        problems.append(
            f"cds_hooks.prefetch.{key} {shown!r} is not a read or search relative to the EHR's "
            "FHIR server (`Type/<id>`, `Type/{{context.<name>}}` or `Type?<query>`, with no "
            "`..`, `$` operation, `#` or whitespace)"
        )
    return problems


class PackManifest(_Strict):
    spec_version: Literal["0.1", "0.2", "0.3"]
    id: str
    version: str
    name: str
    summary: str
    publisher: Publisher
    license: str = Field(max_length=MAX_LICENSE_CHARS)  # an SPDX license expression
    risk: Risk
    card: Card
    inputs: Inputs
    code_systems: list[CodeSystemUse] = []
    questions: dict[str, Question] = Field(min_length=1)
    providers: dict[str, ProviderSlot] = Field(min_length=1)
    policy: Policy
    evaluation: Evaluation = Evaluation()
    tests: str | None = None
    audit: AuditConfig = AuditConfig()
    cds_hooks: CdsHooksConfig | None = None
    guard: GuardConfig | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not is_pack_id(value):
            raise ValueError("id must look like 'namespace/name' (lowercase, digits, dashes)")
        return value

    @field_validator("license")
    @classmethod
    def _license(cls, value: str) -> str:
        if not is_spdx_expression(value):
            raise ValueError(
                "license must be an SPDX license expression, e.g. Apache-2.0 or MIT OR Apache-2.0"
            )
        return value

    @field_validator("code_systems")
    @classmethod
    def _code_systems(cls, value: list[CodeSystemUse]) -> list[CodeSystemUse]:
        urls = [use.url for use in value]
        repeated = sorted({url for url in urls if urls.count(url) > 1})
        if repeated:
            raise ValueError(f"code_systems names {repeated} more than once")
        for use in value:
            for url in licensed_systems(use.url):
                if use.licence_note is None:
                    raise ValueError(
                        f"code system {use.url} is {LICENSED_CODE_SYSTEMS[url][0]}, whose codes "
                        "are licensed: give a licence_note saying the terms the pack uses it under"
                    )
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not is_version(value):
            raise ValueError(
                f"version must be semantic (MAJOR.MINOR.PATCH), at most {MAX_VERSION_CHARS} "
                "characters"
            )
        return value

    @model_validator(mode="after")
    def _cross_references(self) -> PackManifest:
        problems = cross_reference_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def labels(self, question: str) -> list[str]:
        return self.questions[question].labels

    @property
    def derived(self) -> dict[str, Question]:
        return {key: q for key, q in self.questions.items() if q.derive is not None}


def cross_reference_problems(m: PackManifest) -> list[str]:
    """Consistency rules that a JSON-shape check cannot express."""
    problems: list[str] = []
    for key in m.questions:
        if not _KEY.match(key):
            problems.append(f"question key {key!r} must match {_KEY.pattern}")
    derived = {key for key, q in m.questions.items() if q.derive is not None}
    if m.spec_version == "0.1":
        newer = [k for k, q in m.questions.items() if q.type != "choice" or q.derive is not None]
        newer += [
            f"rule {rule.name}"
            for rule in m.policy.rules
            if rule.when.min_probability is not None
            or rule.when.min_score is not None
            or rule.when.max_score is not None
        ]
        if m.guard is not None:
            newer.append("guard")
        newer += [
            f"question {k} display or names" for k, q in m.questions.items() if q.display or q.names
        ]
        if m.card.provenance is not None:
            newer.append("card.provenance")
        if newer:
            problems.append(f"{newer} use spec 0.2 features; set spec_version: '0.2'")

    answered_by: dict[str, str] = {}
    for slot_name, slot in m.providers.items():
        for question in slot.answers:
            if question in derived:
                problems.append(f"derived question {question!r} cannot be answered by a slot")
            elif question not in m.questions:
                problems.append(
                    f"provider slot {slot_name!r} answers unknown question {question!r}"
                )
            elif question in answered_by:
                problems.append(
                    f"question {question!r} is answered by both {answered_by[question]!r} "
                    f"and {slot_name!r}"
                )
            else:
                answered_by[question] = slot_name
    for question in m.questions:
        if question not in answered_by and question not in derived:
            problems.append(f"question {question!r} is not answered by any provider slot")
    problems.extend(_derive_problems(m, derived))

    policy = m.policy
    if policy.primary not in m.questions:
        return [*problems, f"policy.primary {policy.primary!r} is not a question"]
    primary_labels = set(m.questions[policy.primary].criteria)

    for rule in policy.rules:
        condition = rule.when
        target = m.questions.get(condition.question)
        if target is None:
            problems.append(
                f"rule {rule.name!r} references unknown question {condition.question!r}"
            )
        else:
            unknown = set(condition.labels) - set(target.criteria)
            if unknown:
                problems.append(f"rule {rule.name!r} references unknown labels {sorted(unknown)}")
            bounded = condition.min_score is not None or condition.max_score is not None
            if bounded and target.type != "score":
                problems.append(f"rule {rule.name!r}: score bounds need a score question")
        if rule.recommendation is not None and rule.recommendation not in primary_labels:
            problems.append(
                f"rule {rule.name!r} recommends {rule.recommendation!r}, "
                f"which is not a label of {policy.primary!r}"
            )
        if rule.outcome is Outcome.AUTO and m.risk.level >= RiskLevel.CLINICAL_SUPPORT:
            problems.append(
                f"rule {rule.name!r} requests AUTO, which risk level {int(m.risk.level)} forbids"
            )
    for label in policy.labels:
        if label not in primary_labels:
            problems.append(f"policy.labels has unknown label {label!r}")
    if m.guard is not None:
        unknown_release = sorted(set(m.guard.release) - set(primary_labels))
        if unknown_release:
            problems.append(f"guard.release names unknown recommendations {unknown_release}")
    for slot_name, required in policy.automation_requires.items():
        provider_slot = m.providers.get(slot_name)
        if provider_slot is None:
            problems.append(f"policy.automation_requires names unknown slot {slot_name!r}")
        elif not required or set(required) - set(provider_slot.variants):
            not_variants = sorted(set(required) - set(provider_slot.variants))
            problems.append(
                f"policy.automation_requires.{slot_name} must list variants of the slot"
                + (f"; unknown: {not_variants}" if not_variants else "")
            )

    hooks = m.cds_hooks
    if hooks is not None and hooks.coverage_information:
        unknown_labels = sorted(set(hooks.coverage_information) - set(primary_labels))
        if unknown_labels:
            problems.append(f"cds_hooks.coverage_information names unknown labels {unknown_labels}")
        if hooks.hook not in ("order-select", "order-sign"):
            problems.append("cds_hooks.coverage_information is for order-select or order-sign")
        if "coverage" not in hooks.prefetch:
            problems.append("cds_hooks.coverage_information needs a `coverage` prefetch")
    dtr = hooks.questionnaire if hooks is not None else None
    if dtr is not None:
        if m.questions[policy.primary].derive is None:
            problems.append("cds_hooks.questionnaire needs a derived primary question")
        if dtr.missing not in primary_labels:
            problems.append(f"cds_hooks.questionnaire.missing {dtr.missing!r} is not a label")
        unknown_context = sorted(set(dtr.context) - set(m.questions))
        if unknown_context:
            problems.append(f"cds_hooks.questionnaire.context names unknown {unknown_context}")

    card = m.cds_hooks.card if m.cds_hooks is not None else None
    if card is not None:
        allowed = set(primary_labels) | {"none", REFUSED}
        named = set(card.summaries) | set(card.indicators) | set(card.show or [])
        unknown_keys = sorted(named - allowed)
        if unknown_keys:
            problems.append(f"cds_hooks.card names unknown recommendations {unknown_keys}")
        if REFUSED in primary_labels and card.names_refused():
            problems.append(
                f"cds_hooks.card: {REFUSED!r} is a label of {policy.primary!r} and the key of "
                "refused input"
            )
        rules = {rule.name for rule in policy.rules}
        unknown_rules = sorted(set(card.also_show) - rules)
        if unknown_rules:
            problems.append(f"cds_hooks.card.also_show names unknown policy rules {unknown_rules}")
        if card.also_show and card.show is None:
            problems.append("cds_hooks.card.also_show needs a `show` list (every card shows)")
        for where, summary in card.summary_templates().items():
            placeholders = unknown_placeholders(summary, m.questions)
            if placeholders:
                problems.append(f"cds_hooks.card.{where} uses unknown placeholders {placeholders}")
        # An empty `also_show` is the default a dumped manifest carries: it counts as left out.
        newer = (["cds_hooks.card.also_show"] if card.also_show else []) + (
            [f"cds_hooks.card key {REFUSED!r}"] if card.names_refused() else []
        )
        if newer and m.spec_version in ("0.1", "0.2"):
            problems.append(f"{newer} use spec 0.3 card features; set spec_version: '0.3'")

    for critical in m.evaluation.critical:
        critical_question = m.questions.get(critical.question)
        if critical_question is None or critical.label not in critical_question.criteria:
            problems.append(f"evaluation.critical {critical.question}={critical.label} is unknown")
    if (
        m.evaluation.benchmark_dataset
        and m.evaluation.benchmark_dataset not in m.evaluation.datasets
    ):
        problems.append("evaluation.benchmark_dataset must name one of evaluation.datasets")
    return problems


def card_summary_problems(m: PackManifest) -> list[str]:
    """Each card summary (`cds_hooks.card.summaries`) that can read longer than `SUMMARY_CHARS`
    once its placeholders are filled as a card fills them, with display names
    (`templates.fill_display`): every expansion is measured, through its longest
    (`templates.longest`). A summary that is not bare is `template_problems`' to report."""
    card = m.cds_hooks.card if m.cds_hooks is not None else None
    if card is None or m.policy.primary not in m.questions:
        return []
    choices = display_choices(m)
    problems = []
    for where, summary in card.summary_templates().items():
        if template_problems(summary, m.questions):
            continue
        most = len(longest(summary, choices))
        if most > SUMMARY_CHARS:
            problems.append(
                f"cds_hooks.card.{where} can read {most} characters with display names; "
                f"a card's summary is at most {SUMMARY_CHARS} (CDS Hooks: under 140)"
            )
    return problems


def _derive_problems(m: PackManifest, derived: set[str]) -> list[str]:
    problems = []
    for key in sorted(derived):
        spec = m.questions[key].derive
        if spec is None:
            continue
        for member in spec.members:
            if member not in m.questions or member in derived:
                problems.append(f"{key}: derive member {member!r} must be a non-derived question")
        for row in spec.table:
            scoped = [q for q in spec.members if row.applies_to(q) and q in m.questions]
            labels = {lab for q in scoped for lab in m.questions[q].criteria}
            for label in sorted(row.labels - labels):
                problems.append(f"{key}: row label {label!r} is not a label of its members")
            if row.result not in m.questions[key].criteria:
                problems.append(f"{key}: row result {row.result!r} is not one of its labels")
            for member, gate_labels in row.gates.items():
                if member not in m.questions:
                    continue
                unknown = sorted(gate_labels - set(m.questions[member].criteria))
                if unknown:
                    problems.append(f"{key}: `when` labels {unknown} are not labels of {member!r}")
    return problems
