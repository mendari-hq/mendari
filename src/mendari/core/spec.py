# SPDX-License-Identifier: Apache-2.0
"""Agent manifest, spec versions 0.1 to 0.4.

A pack declares *questions* (a fixed set of labels, each with human-readable criteria).
Providers answer questions with probability distributions; the policy turns answers into
one of four outcomes. Spec 0.2 adds `boolean` and `score` question types and *derived*
questions computed from other answers by a decision table. See docs/spec/agent-manifest-0.2.md.
Spec 0.3 changes the FHIR mapping (docs/spec/agent-manifest-0.3.md), which
`standards/fhir.py` reads and checks, and adds two card keys (§5: `refused` and
`cds_hooks.card.also_show`); its manifest is otherwise a 0.2 manifest. Spec 0.4 adds `actions`
(docs/spec/agent-manifest-0.4.md): what an agent may ask to have done besides answer, each with
an effect; what an effect needs before it is released is the runtime's (`core/policy.py`). It
also adds written output: `outputs` (what an agent writes), `rubrics` (how a clinician and a
model judge grade it) and `evaluation.grades` (clinicians' grades in a file).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal, TypeGuard

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mendari.core.templates import (
    display_choices,
    longest,
    template_problems,
    unknown_placeholders,
)

SPEC_VERSIONS = ("0.1", "0.2", "0.3", "0.4")  # every spec version this engine reads, in order
SPEC_VERSION = SPEC_VERSIONS[-1]  # the newest
MAX_VERSION_CHARS = 64
MAX_DERIVE_ROWS = 10

# Actions (spec 0.4 section 2): how many an agent declares, and the bounds of each.
MAX_ACTIONS = 16
MAX_ACTION_NAME_CHARS = 64
MAX_ACTION_DESCRIPTION = 300
MAX_ACTION_SCHEMA_CHARS = 4_000  # one arguments schema, as JSON
MAX_ACTION_SCHEMA_DEPTH = 4  # objects inside objects
MAX_ACTION_PROPERTIES = 32
MAX_ACTION_ENUM = 100
MAX_ACTION_ENUM_CHARS = 200
MAX_ACTIONS_CHARS = 32_000  # the declared actions as one request sends them, as JSON

# Written output (spec 0.4 sections 16 to 19): the texts an agent writes and the rubrics they
# are graded against.
MAX_OUTPUTS = 8
MAX_OUTPUT_CHARS = 8_000  # one written text; a response stays under the agent contract's bytes
DEFAULT_OUTPUT_CHARS = 4_000
MAX_OUTPUT_DESCRIPTION = 500
MAX_RUBRICS = 8
MAX_CRITERIA = 16  # one judge call grades an output: as many questions as one call takes
MAX_LEVELS = 7
MAX_CONTEXT_FIELDS = 32
# A model's name, as `providers/llm.py` `MODEL_PATTERN` (which imports this module).
JUDGE_MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
# The engine's floors for believing a judge (`evals/written.py` holds the rule): its weighted
# kappa with clinicians, and for a must-not-fail criterion the share of clinician-found failures
# it found too, each by its lower bound. A manifest may raise one, never lower it.
AGREEMENT_FLOOR = 0.60
MUST_PASS_RECALL_FLOOR = 0.80
# The most of the outputs a judge may fail to grade (of those clinicians graded, and of the
# rest) and still be believed. A ceiling: a manifest may lower it, never raise it.
MAX_JUDGE_ERRORS = 0.05


def at_least(version: str, wanted: str) -> bool:
    """Whether spec `version` is `wanted` or newer, by their place in `SPEC_VERSIONS`. Every
    check of "this feature needs spec X" asks this, never equality: a later version keeps what
    an earlier one added. A version this engine does not read is none of them."""
    if version not in SPEC_VERSIONS:
        return False
    return SPEC_VERSIONS.index(version) >= SPEC_VERSIONS.index(wanted)


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
        re.compile(r"ama-assn\.org/go/cpt|" + _oid("2.16.840.1.113883.6.12"), re.IGNORECASE),
    ),
    "http://snomed.info/sct": (
        "SNOMED CT (SNOMED International)",
        re.compile(r"snomed\.info/(?:x?sct|id/)|" + _oid("2.16.840.1.113883.6.96"), re.IGNORECASE),
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
            + _oid("2.16.840.1.113883.6.165"),
            re.IGNORECASE,
        ),
    ),
    "https://codesystem.x12.org": (
        "X12 code lists (X12)",
        re.compile(
            r"(?:codesystem|valueset)\.x12\.org|x12\.org/codes/|"
            + _THO
            + r"X12\.|"
            + _oid("2.16.840.1.113883.6.255"),
            re.IGNORECASE,
        ),
    ),
    "http://www.ada.org/cdt": (
        "CDT (American Dental Association)",
        re.compile(
            r"ada\.org/cdt|" + _THO + r"CD2(?![\w-])|" + _oid("2.16.840.1.113883.6.13"),
            re.IGNORECASE,
        ),
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
    "agent": frozenset({"endpoint_env", "token_env", "api_key_env"}),
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
        "an agent may not choose the environment variables a provider reads its endpoint, model "
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


class Effect(StrEnum):
    """What a declared action does (spec 0.4 section 2.1), mildest first: an action that fits
    two takes the later one. The author's declaration, as the risk level is; what each needs
    before an action is released is the runtime's (`core/policy.py` `EFFECT_GATES`)."""

    READ = "read"  # reads data and changes nothing
    DRAFT = "draft"  # prepares something a person sees before it takes effect
    WRITE = "write"  # changes the chart or another system of record
    CONTACT = "contact"  # sends something to a patient, or to anyone outside the care team
    ORDER = "order"  # places an order or a prescription, or changes treatment


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
        # A `criteria` or `levels` that is not a mapping is left as written, for the field's own
        # check to name ("Input should be a valid dictionary"), never a raw error.
        if data.get("type") == "boolean":
            given = data.get("criteria") or {}
            if not isinstance(given, dict):
                return data
            extra = set(given) - {"yes", "no"}
            if extra:
                raise ValueError(f"a boolean question only has labels yes/no, not {sorted(extra)}")
            data["criteria"] = {"yes": given.get("yes", "Yes."), "no": given.get("no", "No.")}
        elif data.get("type") == "score" and "criteria" not in data:
            levels = data.get("levels") or {}
            if not isinstance(levels, dict):
                return data
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

    @model_validator(mode="after")
    def _label_thresholds_ordered(self) -> Policy:
        for label, lp in self.labels.items():
            if lp.auto_threshold is not None and lp.auto_threshold < self.thresholds.review:
                raise ValueError(
                    f"policy.labels.{label}.auto_threshold must be >= thresholds.review"
                )
        return self


class CriticalLabel(_Strict):
    """A (question, label) whose true cases a person must see: never automated in the pack's
    default deployment, and under a governance approval only with the correct primary
    recommendation (spec section 8). Tracked in every eval."""

    question: str
    label: str


class RubricGrades(_Strict):
    """Clinicians' grades of a rubric in a file of the agent (`mendari.written-grades/1`): the
    dataset whose outputs they graded, and the file."""

    dataset: str
    file: str = Field(min_length=1, max_length=500)


class Evaluation(_Strict):
    datasets: dict[str, str] = {}
    benchmark: str | None = None
    benchmark_dataset: str | None = None
    critical: list[CriticalLabel] = []
    subgroups: list[str] = []
    degradation_tolerance: float = Field(default=0.03, gt=0.0, lt=1.0)
    # Clinicians' grades of written output, by rubric (spec 0.4 section 19).
    grades: dict[str, RubricGrades] = {}


class AuditConfig(_Strict):
    store_input: Literal["full", "hash"] = "full"


# The JSON Schema keywords an action's arguments schema may use (spec 0.4 section 2.2): no
# pattern, format, `$ref` or combinator, so checking arguments a model wrote costs no more than
# reading them.
ACTION_SCHEMA_KEYWORDS = frozenset(
    {
        "type", "properties", "required", "additionalProperties", "items", "enum", "const",
        "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minItems", "maxItems", "title", "description",
    }
)  # fmt: skip
_ACTION_TYPES = ("object", "array", "string", "integer", "number", "boolean")
_ACTION_PROPERTY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_ACTION_COUNTS = ("minLength", "maxLength", "minItems", "maxItems")
_ACTION_BOUNDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")


def _scalar(value: Any) -> bool:
    """A value an `enum` or a `const` may hold: no object and no list, a text of bounded length."""
    if isinstance(value, str):
        return len(value) <= MAX_ACTION_ENUM_CHARS
    return value is None or isinstance(value, bool | int | float)


def action_schema_problems(schema: Any) -> list[str]:
    """Why `schema` is not an arguments schema spec 0.4 section 2.2 allows, or nothing: the
    closed keyword set, each keyword's own shape, and the bounds. Walked with a stack, never by
    recursion: a manifest is another party's file. Each problem names where in the schema,
    never a value."""
    if not isinstance(schema, dict):
        return ["it must be a mapping"]
    try:
        size = len(json.dumps(schema, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return ["it must hold only what JSON carries"]
    if size > MAX_ACTION_SCHEMA_CHARS:
        return [f"it is longer than {MAX_ACTION_SCHEMA_CHARS:,} characters as JSON"]
    problems: list[str] = []
    stack: list[tuple[str, Any, int]] = [("(root)", schema, 1)]
    while stack and len(problems) < 10:
        where, node, depth = stack.pop()
        if not isinstance(node, dict):
            problems.append(f"{where}: a schema is a mapping")
            continue
        unknown = sorted(str(key) for key in set(node) - ACTION_SCHEMA_KEYWORDS)
        if unknown:
            shown = ", ".join(key[:40] for key in unknown[:5])
            problems.append(f"{where}: keywords an arguments schema may not use: {shown}")
            continue
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in _ACTION_TYPES:
            problems.append(f"{where}: `type` is one of {', '.join(_ACTION_TYPES)}")
            continue
        problems.extend(f"{where}: {problem}" for problem in _keyword_problems(node))
        if kind == "object":
            if depth > MAX_ACTION_SCHEMA_DEPTH:
                problems.append(f"{where}: objects nest at most {MAX_ACTION_SCHEMA_DEPTH} deep")
                continue
            if node.get("additionalProperties") is not False:
                problems.append(f"{where}: an object sets `additionalProperties: false`")
            properties = node.get("properties", {})
            if not isinstance(properties, dict) or len(properties) > MAX_ACTION_PROPERTIES:
                problems.append(
                    f"{where}: `properties` is a mapping of at most {MAX_ACTION_PROPERTIES}"
                )
                continue
            named = [key for key in properties if isinstance(key, str)]
            if len(named) != len(properties) or not all(_ACTION_PROPERTY.match(k) for k in named):
                problems.append(f"{where}: a property's name matches {_ACTION_PROPERTY.pattern}")
                continue
            required = node.get("required", [])
            if not isinstance(required, list) or not all(
                isinstance(key, str) and key in properties for key in required
            ):
                problems.append(f"{where}: `required` names only the object's properties")
            inside = "" if where == "(root)" else f"{where}."
            stack.extend((f"{inside}{key}", properties[key], depth + 1) for key in named)
        elif "properties" in node or "required" in node or "additionalProperties" in node:
            problems.append(f"{where}: only an object has properties")
        if kind == "array":
            if "items" in node:
                stack.append((f"{where}[]", node["items"], depth))
        elif "items" in node:
            problems.append(f"{where}: only an array has `items`")
    return problems[:10]


def _keyword_problems(node: Mapping[str, Any]) -> list[str]:
    """What is wrong with one schema's own keywords (`action_schema_problems`), each named."""
    problems = []
    for key in ("title", "description"):
        if key in node and not isinstance(node[key], str):
            problems.append(f"`{key}` is a text")
    for key in _ACTION_COUNTS:
        count = node.get(key)
        if key in node and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            problems.append(f"`{key}` is a whole number, 0 or more")
    for key in _ACTION_BOUNDS:
        bound = node.get(key)
        if key in node and (isinstance(bound, bool) or not isinstance(bound, int | float)):
            problems.append(f"`{key}` is a number")
    if "enum" in node:
        values = node["enum"]
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_ACTION_ENUM:
            problems.append(f"`enum` lists 1 to {MAX_ACTION_ENUM} values")
        elif not all(_scalar(value) for value in values):
            problems.append(
                "an `enum` value is no object or list, and a text of at most "
                f"{MAX_ACTION_ENUM_CHARS} characters"
            )
    if "const" in node and not _scalar(node["const"]):
        problems.append("`const` is no object or list")
    return problems


class ActionSpec(_Strict):
    """One action an agent may propose (spec 0.4 section 2): what it does in words a reviewer
    reads (shown on the Decision Card and sent to the agent as written), its effect, and
    optionally a JSON Schema for its arguments. `allow_auto: false` only tightens: the action is
    then never released without a person, whatever its effect. No key loosens the runtime's rule
    (`core/policy.py`): `allow_auto: true` is the default and changes nothing, on an `order` too
    (a dumped manifest writes the default, and must load)."""

    description: str = Field(min_length=1, max_length=MAX_ACTION_DESCRIPTION)
    effect: Effect
    allow_auto: bool = True
    json_schema: dict[str, Any] | None = Field(default=None, alias="schema")

    @field_validator("description")
    @classmethod
    def _described(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an action needs a description a person can read")
        return value

    @field_validator("json_schema")
    @classmethod
    def _arguments_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        problems = action_schema_problems(value)
        if problems:
            raise ValueError("schema: " + "; ".join(problems))
        return value

    def request(self, name: str) -> dict[str, Any]:
        """The action as a request to the agent declares it (spec 0.4 section 4.1)."""
        declared: dict[str, Any] = {
            "name": name,
            "description": self.description,
            "effect": self.effect.value,
        }
        if self.json_schema is not None:
            declared["schema"] = self.json_schema
        return declared


_FIELD = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}(?:\.[A-Za-z0-9_][A-Za-z0-9_-]{0,63}){0,7}\Z")


class WrittenOutput(_Strict):
    """A text an agent writes (spec 0.4 section 16): a draft reply, a summary, a note. It comes
    from the agent behind `slot`, in its response, or it is already in the input at `input` (a
    dotted field, as the guard's `draft`). Exactly one of the two. `description` is what the
    agent is asked for and what a grader is told the text is."""

    description: str = Field(min_length=1, max_length=MAX_OUTPUT_DESCRIPTION)
    slot: str | None = None
    input: str | None = None
    max_chars: int = Field(default=DEFAULT_OUTPUT_CHARS, ge=1, le=MAX_OUTPUT_CHARS)

    @model_validator(mode="after")
    def _one_source(self) -> WrittenOutput:
        if not self.description.strip():
            raise ValueError("an output needs a description a person can read")
        if (self.slot is None) == (self.input is None):
            raise ValueError("an output names exactly one of `slot` and `input`")
        if self.input is not None and not _FIELD.match(self.input):
            raise ValueError("`input` is a dotted field of the input, such as `draft`")
        return self

    @property
    def source(self) -> str:
        """Where the text comes from, as reports say it: `slot:<slot>` or `input:<field>`."""
        return f"slot:{self.slot}" if self.slot is not None else f"input:{self.input}"

    def request(self, name: str) -> dict[str, Any]:
        """The output as a request to the agent declares it (spec 0.4 section 17)."""
        return {"name": name, "description": self.description, "max_chars": self.max_chars}


class RubricCriterion(Question):
    """One criterion of a rubric (spec 0.4 section 18): a `boolean` or a `score` question, read
    by the clinician and by the judge in the same words. On a yes/no criterion `yes` passes: it
    is worded so that yes is good. A `score` criterion names `pass_from`, the lowest level that
    passes; its scores are distinct, and a higher score is better. `must_pass` marks a criterion
    that must not fail: a judge is believed on it only when it finds the failures clinicians
    find."""

    must_pass: bool = False
    pass_from: str | None = None

    @model_validator(mode="after")
    def _scale(self) -> RubricCriterion:
        if self.type == "choice":
            raise ValueError("a criterion is `boolean` or `score`, never `choice`")
        if self.derive is not None:
            raise ValueError("a criterion is graded, never derived")
        if self.type == "boolean":
            if self.pass_from is not None:
                raise ValueError("a yes/no criterion takes no `pass_from`: `yes` passes")
            return self
        levels = self.levels or {}
        if not 2 <= len(levels) <= MAX_LEVELS:
            raise ValueError(f"a score criterion has 2 to {MAX_LEVELS} levels")
        if len({level.score for level in levels.values()}) != len(levels):
            raise ValueError("a score criterion's levels have distinct scores")
        if self.pass_from is None or self.pass_from not in levels:
            raise ValueError("a score criterion names `pass_from`, the lowest level that passes")
        return self

    def scale(self) -> list[tuple[str, float]]:
        """The levels with their scores, lowest first; `no` 0 and `yes` 1 on a yes/no."""
        if self.type == "boolean":
            return [("no", 0.0), ("yes", 1.0)]
        levels = self.levels or {}
        return sorted(((label, level.score) for label, level in levels.items()), key=_by_score)

    def passing(self) -> str:
        """The lowest level that passes."""
        return "yes" if self.type == "boolean" else str(self.pass_from)

    def passes(self, level: str) -> bool:
        """Whether a grade of `level` passes; a level the criterion does not have never does."""
        scores = dict(self.scale())
        return level in scores and scores[level] >= scores[self.passing()]


def _by_score(level: tuple[str, float]) -> float:
    return level[1]


class JudgeConfig(_Strict):
    """The model judge of a rubric (spec 0.4 section 18.2): the model settings an `llm` variant
    takes, and a fixture of canned grades for when no judge model is set. The endpoint, the
    credentials and the default model are the deployment's."""

    model: str | None = Field(default=None, pattern=JUDGE_MODEL_PATTERN)
    timeout_ms: int = Field(default=30_000, ge=1000, le=120_000)
    max_tokens: int = Field(default=4096, ge=64, le=32_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    mock_file: str | None = Field(default=None, min_length=1, max_length=500)


class RubricFloors(_Strict):
    """A rubric's own floors: each may raise the engine's (`AGREEMENT_FLOOR`,
    `MUST_PASS_RECALL_FLOOR`), never lower it."""

    agreement: float | None = Field(default=None, ge=0.0, lt=1.0)
    must_pass_recall: float | None = Field(default=None, ge=0.0, lt=1.0)
    # A ceiling, so tightening it is lowering it: at most the engine's `MAX_JUDGE_ERRORS`.
    max_judge_errors: float | None = Field(default=None, ge=0.0, le=1.0)


class Rubric(_Strict):
    """How one written output is graded (spec 0.4 section 18): its criteria, the input fields a
    grader reads beside the text (none unless named: minimum necessary), the judge and the
    floors."""

    output: str
    context_fields: list[str] = Field(default=[], max_length=MAX_CONTEXT_FIELDS)
    criteria: dict[str, RubricCriterion] = Field(min_length=1, max_length=MAX_CRITERIA)
    judge: JudgeConfig = JudgeConfig()
    floors: RubricFloors = RubricFloors()

    @field_validator("context_fields")
    @classmethod
    def _fields(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("`context_fields` names a field more than once")
        if not all(_FIELD.match(field) for field in value):
            raise ValueError("`context_fields` are dotted fields of the input")
        return value

    def agreement_floor(self) -> float:
        return AGREEMENT_FLOOR if self.floors.agreement is None else self.floors.agreement

    def must_pass_recall_floor(self) -> float:
        own = self.floors.must_pass_recall
        return MUST_PASS_RECALL_FLOOR if own is None else own

    def max_judge_errors(self) -> float:
        own = self.floors.max_judge_errors
        return MAX_JUDGE_ERRORS if own is None else own


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
    spec_version: Literal["0.1", "0.2", "0.3", "0.4"]
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
    # What the agent may ask to have done besides answer (spec 0.4), by name, in the manifest's
    # order. Empty: none, as for every agent before 0.4.
    actions: dict[str, ActionSpec] = {}
    # What the agent writes, and how it is graded (spec 0.4): empty for an agent that only
    # answers, as every agent before 0.4.
    outputs: dict[str, WrittenOutput] = {}
    rubrics: dict[str, Rubric] = {}

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
                        "are licensed: give a licence_note saying the terms the agent uses it under"
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
        if newer and not at_least(m.spec_version, "0.3"):
            problems.append(f"{newer} use spec 0.3 card features; set spec_version: '0.3'")

    second_signal = second_signal_keys(m)
    if second_signal and not at_least(m.spec_version, "0.4"):
        problems.append(f"{second_signal} use spec 0.4 features; set spec_version: '0.4'")

    for critical in m.evaluation.critical:
        critical_question = m.questions.get(critical.question)
        if critical_question is None or critical.label not in critical_question.criteria:
            problems.append(f"evaluation.critical {critical.question}={critical.label} is unknown")
    if (
        m.evaluation.benchmark_dataset
        and m.evaluation.benchmark_dataset not in m.evaluation.datasets
    ):
        problems.append("evaluation.benchmark_dataset must name one of evaluation.datasets")
    problems.extend(action_problems(m))
    problems.extend(written_problems(m))
    return problems


def _named(kind: str, name: str) -> str | None:
    """Why `name` is no key of an output, a rubric or a criterion, or None."""
    if _KEY.match(name) and len(name) <= MAX_ACTION_NAME_CHARS:
        return None
    return (
        f"{kind} {name[:40]!r} must match {_KEY.pattern}, at most {MAX_ACTION_NAME_CHARS} "
        "characters"
    )


def written_problems(m: PackManifest) -> list[str]:
    """What spec 0.4 holds `outputs`, `rubrics` and `evaluation.grades` to beyond their own
    shapes: the names, how many, what each refers to, the floors and the spec version. An empty
    mapping is the default a dumped manifest carries: it counts as left out."""
    sections = (
        ("outputs", m.outputs),
        ("rubrics", m.rubrics),
        ("evaluation.grades", m.evaluation.grades),
    )
    used = [key for key, value in sections if value]
    if not used:
        return []
    problems = []
    if not at_least(m.spec_version, "0.4"):
        problems.append(f"{used} use spec 0.4 features; set spec_version: '0.4'")
    if len(m.outputs) > MAX_OUTPUTS:
        problems.append(f"an agent declares at most {MAX_OUTPUTS} outputs")
    if len(m.rubrics) > MAX_RUBRICS:
        problems.append(f"an agent declares at most {MAX_RUBRICS} rubrics")
    for name, output in m.outputs.items():
        if problem := _named("output name", name):
            problems.append(problem)
        if output.slot is None:
            continue
        slot = m.providers.get(output.slot)
        if slot is None:
            problems.append(f"outputs.{name[:40]}.slot names unknown slot {output.slot[:40]!r}")
        elif not any(variant.type == "agent" for variant in slot.variants.values()):
            problems.append(
                f"outputs.{name[:40]}.slot: slot {output.slot!r} has no variant of type `agent`; "
                "only an agent writes an output (a composite's member does not count)"
            )
    for name, rubric in m.rubrics.items():
        if problem := _named("rubric name", name):
            problems.append(problem)
        if rubric.output not in m.outputs:
            problems.append(
                f"rubrics.{name[:40]}.output {rubric.output[:40]!r} is not a declared output"
            )
        for key in rubric.criteria:
            if problem := _named(f"rubrics.{name[:40]}: criterion key", key):
                problems.append(problem)
        floors = (("agreement", AGREEMENT_FLOOR), ("must_pass_recall", MUST_PASS_RECALL_FLOOR))
        for floor, lowest in floors:
            own = getattr(rubric.floors, floor)
            if own is not None and own < lowest:
                problems.append(
                    f"rubrics.{name[:40]}.floors.{floor} is below the engine's {lowest}: a "
                    "manifest may raise a floor, never lower it"
                )
        ceiling = rubric.floors.max_judge_errors
        if ceiling is not None and ceiling > MAX_JUDGE_ERRORS:
            problems.append(
                f"rubrics.{name[:40]}.floors.max_judge_errors is above the engine's "
                f"{MAX_JUDGE_ERRORS}: a manifest may lower this ceiling, never raise it"
            )
    for name, grades in m.evaluation.grades.items():
        if name not in m.rubrics:
            problems.append(f"evaluation.grades.{name[:40]} is not a declared rubric")
        if grades.dataset not in m.evaluation.datasets:
            problems.append(
                f"evaluation.grades.{name[:40]}.dataset must name one of evaluation.datasets"
            )
    return problems


def action_problems(m: PackManifest) -> list[str]:
    """What spec 0.4 holds the `actions` section to beyond each action's own shape: the names,
    how many, how long they are together as one request sends them, and the spec version. An
    empty `actions` is the default a dumped manifest carries: it counts as left out."""
    if not m.actions:
        return []
    problems = []
    for name in m.actions:
        if not _KEY.match(name) or len(name) > MAX_ACTION_NAME_CHARS:
            shown = name if len(name) <= MAX_ACTION_NAME_CHARS else name[:40] + "..."
            problems.append(
                f"action name {shown!r} must match {_KEY.pattern}, at most "
                f"{MAX_ACTION_NAME_CHARS} characters"
            )
    if len(m.actions) > MAX_ACTIONS:
        problems.append(f"an agent declares at most {MAX_ACTIONS} actions")
    sent = json.dumps([spec.request(name) for name, spec in m.actions.items()])
    if len(sent) > MAX_ACTIONS_CHARS:
        problems.append(
            f"actions are longer than {MAX_ACTIONS_CHARS:,} characters together, as a request "
            "sends them"
        )
    if not at_least(m.spec_version, "0.4"):
        problems.append("['actions'] use spec 0.4 features; set spec_version: '0.4'")
    return problems


def variant_configs(m: PackManifest) -> Iterator[tuple[str, str, str | None, dict[str, Any]]]:
    """Each provider variant of a manifest, and each member of a composite after it, as
    (slot, variant, member, config): the config as written, with its `type`. A member that is
    not a mapping is left out (its load says so)."""
    for slot_name, slot in m.providers.items():
        for name, variant in slot.variants.items():
            extra = variant.model_extra or {}
            yield slot_name, name, None, {"type": variant.type, **extra}
            members = extra.get("members")
            if isinstance(members, dict):
                for key, member in members.items():
                    if isinstance(member, dict):
                        yield slot_name, name, str(key), dict(member)


def second_signal_keys(m: PackManifest) -> list[str]:
    """Where a manifest uses what spec 0.4 adds to a provider variant (ADR-0024), in a variant
    or a composite's member: an `agent`'s `calibration`, `repeat` on an `agent` or an `llm`,
    and the `check` composite."""
    found = []
    for slot, name, member, config in variant_configs(m):
        at = f"providers.{slot}.variants.{name}"
        if member is not None:
            at += f".members.{member}"
        kind = config.get("type")
        if kind == "check":
            found.append(f"{at} (type check)")
        if kind in ("agent", "llm") and "repeat" in config:
            found.append(f"{at}.repeat")
        if kind == "agent" and "calibration" in config:
            found.append(f"{at}.calibration")
    return found


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
