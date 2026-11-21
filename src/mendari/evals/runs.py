# SPDX-License-Identifier: Apache-2.0
"""What a scoreboard entry (`mendari.evals.scoreboard`, LP-17d) needs beside its ledger: the run
record (`mendari.run/1`), the dated price table (`mendari.prices/1`) and the disclosures
(`mendari.disclosures/1`). Each is a closed schema, checked when it is read.

- **The run record** holds what a ledger does not, and what no rebuild reproduces: the
  replicate's number, the engine's version, the date, the ledger it is of (its id), the models
  each model type reported, and per case, in the ledger's order, its latency and each model
  type's input and output tokens. It never holds a key, an endpoint or case text. Its bytes are
  its canonical JSON and a newline, and its SHA-256 is of those bytes. `agrees` says whether it
  is of a ledger: the ledger's id, its number of cases, per case token sums that are the
  ledger's `u` (or more, on a case where a provider call failed), and the models the ledger's
  answers name.
- **The price table** is per model type and model id, in USD per 1,000 input tokens and per
  1,000 output tokens, with its date and source. It is cited by the SHA-256 of its bytes. It is
  a file outside Mendari's code (`scoreboard/prices/<date>.json`): one read from the package, or
  from a pack's directory, is refused, so no price built into a provider or set by a pack is
  ever used.
- **The disclosures** give, per remote provider type, the statement of any commercial
  relationship between Mendari and that provider (`scoreboard/disclosures.json`).
- **The declarations** (`mendari.declared/1`, LP-17h, `scoreboard/declared/<pack name>.json`):
  per arm, the one remote configuration run on public sets with replicates, and the one local
  configuration a hidden set grades, committed before any run. A result cites the SHA-256 of the
  declaration's bytes.
- **Replicates** (LP-17h; the method's section 10): `replicates` gives, from a remote
  configuration's 3 to 5 runs over one set, each figure's spread (its minimum to maximum across
  them) and the case flip rate: k of n cases whose primary prediction or outcome differs between
  any two, matched by ledger hash, with its interval. Replicate 1 is the headline.
- **The canary** (the method's section 12): `draw` is a seeded draw of a set's cases by their
  ledger hashes, `canary_flips` counts the drawn cases whose primary prediction or outcome
  differs from replicate 1's on the same case, and `drift` says whether that rate's lower bound
  is above the upper bound of the replicates' flip rate.

Nothing here reads a clock, a setting or the network.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

import mendari
from mendari import __version__
from mendari.core.decision import Decision, DecisionResult
from mendari.core.origin import COMPOSITE_PROVIDER_TYPES, LOCAL_PROVIDER_TYPES
from mendari.core.spec import is_pack_id
from mendari.errors import MendariError
from mendari.evals import arms, method, stats
from mendari.evals.ledger import Ledger, canonical
from mendari.evals.result import ERROR, PREDICTED
from mendari.evals.share import JOINERS
from mendari.providers import MODEL_TYPES, PROVIDER_TYPES

RUN_FORMAT = "mendari.run/1"
PRICES_FORMAT = "mendari.prices/1"
DISCLOSURES_FORMAT = "mendari.disclosures/1"
DECLARED_FORMAT = "mendari.declared/1"
REMOTE, LOCAL = "remote", "local"  # a declared arm's configurations
HEADLINE = 1  # the replicate an entry's figures are of, as declared before the run
NOT_ESTIMABLE = "not_estimable"
_DRAW = b"mendari-canary-draw-v1\x00"  # domain-separates the canary's draw
# The provider types that may call something off the machine, and so need a disclosure.
REMOTE_TYPES = frozenset(PROVIDER_TYPES) - LOCAL_PROVIDER_TYPES - COMPOSITE_PROVIDER_TYPES
MAX_CASES = 1_000_000
MAX_TOKENS = 10**9  # per case and type
MAX_LATENCY_MS = 10**9
MAX_USD_PER_1000 = 1_000.0
LATENCY_DIGITS = 3  # as a decision's record keeps it
# A model id as a provider reports it (`share`'s pattern): never a URL.
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,127}")
_SHA256 = r"^sha256:[0-9a-f]{64}$"
_DATE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


def _model_type(kind: str) -> str:
    if kind not in MODEL_TYPES:
        raise ValueError(f"must be a model type ({', '.join(sorted(MODEL_TYPES))})")
    return kind


def _remote_type(kind: str) -> str:
    if kind not in REMOTE_TYPES:
        raise ValueError(f"must be a remote provider type ({', '.join(sorted(REMOTE_TYPES))})")
    return kind


def _model_id(model: str) -> str:
    if not _MODEL.fullmatch(model) or "://" in model:
        raise ValueError("must be a model id as its provider reports it, never a URL")
    return model


def _a_date(text: str) -> str:
    datetime.date.fromisoformat(text)  # a ValueError names no value
    return text


def _sorted_unique(models: list[str]) -> list[str]:
    if models != sorted(set(models)) or not models:
        raise ValueError("must be one or more model ids, sorted, each once")
    return models


ModelType = Annotated[str, AfterValidator(_model_type)]
RemoteType = Annotated[str, AfterValidator(_remote_type)]
ModelId = Annotated[str, AfterValidator(_model_id)]
Date = Annotated[str, Field(pattern=_DATE), AfterValidator(_a_date)]
Tokens = Annotated[
    list[Annotated[int, Field(ge=0, le=MAX_TOKENS)]], Field(min_length=2, max_length=2)
]
Usd = Annotated[float, Field(ge=0.0, le=MAX_USD_PER_1000, allow_inf_nan=False)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RunCase(_Closed):
    latency_ms: Annotated[float, Field(ge=0.0, le=MAX_LATENCY_MS, allow_inf_nan=False)]
    tokens: dict[ModelType, Tokens]


class RunRecord(_Closed):
    format: Literal["mendari.run/1"]
    replicate: Annotated[int, Field(ge=1, le=method.MAX_REPLICATES)]
    engine: Annotated[str, Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")]
    date: Date
    ledger: Annotated[str, Field(pattern=_SHA256)]
    models: dict[ModelType, Annotated[list[ModelId], AfterValidator(_sorted_unique)]]
    cases: Annotated[list[RunCase], Field(min_length=1, max_length=MAX_CASES)]


class Price(_Closed):
    input_per_1000: Usd
    output_per_1000: Usd


class PriceTable(_Closed):
    format: Literal["mendari.prices/1"]
    date: Date
    source: Annotated[str, Field(min_length=1, max_length=500)]
    prices: dict[ModelType, Annotated[dict[ModelId, Price], Field(min_length=1)]]


class DisclosureFile(_Closed):
    format: Literal["mendari.disclosures/1"]
    date: Date
    statements: dict[RemoteType, Annotated[str, Field(min_length=1, max_length=2000)]]


def _arm(arm: str) -> str:
    if arm not in arms.ARMS:
        raise ValueError(f"must be an arm ({', '.join(arms.ARMS)})")
    return arm


def _pack_id(text: str) -> str:
    if not is_pack_id(text):
        raise ValueError("must be a pack id (namespace/name)")
    return text


ArmId = Annotated[str, AfterValidator(_arm)]
_NAME = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$"  # a slot's or a variant's name
Providers = Annotated[
    dict[Annotated[str, Field(pattern=_NAME)], Annotated[str, Field(pattern=_NAME)]],
    Field(min_length=1),
]


class DeclaredArm(_Closed):
    remote: Providers | None = None
    local: Providers | None = None

    @model_validator(mode="after")
    def _one_at_least(self) -> DeclaredArm:
        if self.remote is None and self.local is None:
            raise ValueError("an arm declares a remote configuration, a local one, or both")
        return self


class DeclaredFile(_Closed):
    format: Literal["mendari.declared/1"]
    pack: Annotated[str, AfterValidator(_pack_id)]
    date: Date
    arms: Annotated[dict[ArmId, DeclaredArm], Field(min_length=1)]


# What a refusal may name of where it fails: the schemas' own fields, the types, the arms, and
# pydantic's mark for a dict's key. Anything else in a location is a key the input chose (a
# model id, an extra field, a slot), and may be a URL, a secret or case text: it is named
# `<key>`, never echoed.
_SCHEMAS = (RunCase, RunRecord, Price, PriceTable, DisclosureFile, DeclaredArm, DeclaredFile)
_NAMED = frozenset(
    {"[key]", *MODEL_TYPES, *REMOTE_TYPES, *arms.ARMS}.union(
        *(schema.model_fields for schema in _SCHEMAS)
    )
)


def _checked(model: type[BaseModel], data: Any, where: str) -> BaseModel:
    """`data` as `model`, or a `MendariError` that names where it fails, never a value: a key the
    input chose is `<key>` (`_NAMED`)."""
    try:
        return model.model_validate(data)
    except ValidationError as error:
        first = error.errors()[0]
        at = ".".join(
            str(part) if isinstance(part, int) or part in _NAMED else "<key>"
            for part in first["loc"]
        )
        raise MendariError(f"{where}: {at or 'the top level'}: {first['msg']}") from None


# --------------------------------------------------------------------------------------------
# The run record
# --------------------------------------------------------------------------------------------


def check_run(record: Any, *, where: str = "run record") -> dict[str, Any]:
    """`record` checked against `mendari.run/1`'s closed schema: a plain copy of it, or a
    `MendariError` naming the first field that fails."""
    if not isinstance(record, Mapping):
        raise MendariError(f"{where}: not a {RUN_FORMAT} record")
    _checked(RunRecord, dict(record), where)
    copy: dict[str, Any] = json.loads(canonical(record))
    return copy


def dumps_run(record: Mapping[str, Any]) -> bytes:
    """A run record's bytes: its canonical JSON (sorted keys, no spaces, no NaN) and a newline."""
    return (canonical(check_run(record)) + "\n").encode()


def loads_run(data: bytes, *, where: str = "run record") -> dict[str, Any]:
    """A run record from its bytes, refused unless they are its canonical form, so that its
    SHA-256 is of the record and nothing else."""
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise MendariError(f"{where}: not JSON") from None
    checked = check_run(record, where=where)
    if dumps_run(checked) != data:
        raise MendariError(f"{where}: not in canonical form (sorted keys, no spaces, a newline)")
    return checked


def run_sha256(record: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(dumps_run(record)).hexdigest()


def disagreement(record: Mapping[str, Any], ledger: Ledger) -> str | None:
    """Why `record` is not the run record of `ledger`, or None when it is: another ledger's id,
    another number of cases, a case whose tokens are not the ledger's, or models that are not
    the ledger's answers'. Names case numbers and types, never a case or a model.

    - **Tokens:** a case's tokens, summed over its model types, are the ledger's `u` (input and
      output tokens). On a case where a provider call failed they may be more: a call whose
      answer was refused still spent its tokens, and only the run record counts them (`u` holds
      the answers' tokens). Its cost is unknown either way.
    - **Models:** every model the record names is one the ledger's answers name (an answer's
      model, or a composite member's in its `member:model`), and every model the ledger's
      answers name is the record's, or a deterministic provider's (`_deterministic`)."""
    run = check_run(record)
    if run["ledger"] != ledger.id:
        return f"it is the run record of another ledger ({run['ledger']}, not {ledger.id})"
    if len(run["cases"]) != len(ledger.cases):
        return f"it holds {len(run['cases'])} cases and the ledger {len(ledger.cases)}"
    for number, (case, line) in enumerate(zip(run["cases"], ledger.cases, strict=True), start=1):
        spent = [sum(pair[side] for pair in case["tokens"].values()) for side in (0, 1)]
        answered = [line["u"][1], line["u"][2]]
        failed = any(found[ERROR] is not None for found in line["q"].values())
        more = all(found >= held for found, held in zip(spent, answered, strict=True))
        if spent != answered and not (failed and more):
            return f"case {number}'s tokens are not the ledger's"
    return _models_problem(run, ledger)


def agrees(record: Mapping[str, Any], ledger: Ledger) -> bool:
    """Whether `record` is the run record of `ledger` (`disagreement`)."""
    return disagreement(record, ledger) is None


# What a composite writes for a member that reported no model: `member:None`
# (`CompositeProvider.combined`) or `member:<its type>` (a vote's). Neither is a model.
_PLACEHOLDERS = frozenset({"None", *PROVIDER_TYPES})


_DETERMINISTIC_PREFIXES = ("rules@", "naive_bayes@", "criteria@", "dosing@", "cql:")


def _deterministic(model: str) -> bool:
    """Whether `model` is a deterministic provider's: its type (with a CQL library's name), "@"
    and its pack file's fingerprint (`rules@…`, `cql:<library>@…`, `providers.base.fingerprint`).
    Agent ids (`agent_id@version`) are remote providers, not deterministic."""
    return model.startswith(_DETERMINISTIC_PREFIXES) and "@" in model


def _composites(header: Mapping[str, Any]) -> list[tuple[str, frozenset[str]]]:
    """Each composite slot in a ledger's header: how it joins its members' `member:model`, and
    its members' names, from its own model as the header lists it."""
    found = []
    for provider in header["providers"].values():
        if not isinstance(provider, Mapping):
            continue
        joiner, model = JOINERS.get(str(provider.get("type"))), provider.get("model")
        if joiner is not None and isinstance(model, str):
            names = frozenset(part.partition(":")[0] for part in model.split(joiner))
            found.append((joiner, names))
    return found


def _readings(model: str, composites: Sequence[tuple[str, frozenset[str]]]) -> list[list[str]]:
    """The ways a model a ledger's answer names reads as a composite's answer: for each composite
    whose members it names, those members' models."""
    found = []
    for joiner, names in composites:
        parts = [part.partition(":") for part in model.split(joiner)]
        if all(colon and name in names for name, colon, _ in parts):
            found.append([member_model for _, _, member_model in parts])
    return found


def _models_problem(run: Mapping[str, Any], ledger: Ledger) -> str | None:
    """Why `run`'s models are not the ones `ledger`'s answers name (`disagreement`), or None."""
    composites = _composites(ledger.header)
    answered: dict[str, list[list[str]]] = {}
    for model in ledger.header["models"]:
        if not isinstance(model, str):
            return "the ledger's answers name a model that is not a model id"
        answered[model] = _readings(model, composites)
    named = set(answered) | {m for found in answered.values() for one in found for m in one}
    for kind, models in run["models"].items():
        if not set(models) <= named:
            return f"its {kind} models are not ones the ledger's answers name"
    claimed = {model for models in run["models"].values() for model in models}

    def kept(member: str) -> bool:
        return member in claimed or _deterministic(member) or member in _PLACEHOLDERS

    for model, readings in answered.items():
        alone = model in claimed or (_deterministic(model) and not readings)
        if not alone and not any(all(kept(member) for member in one) for one in readings):
            return "the ledger's answers name a model of a model type it does not list"
    return None


def record(
    decision: Decision,
    ledger: Ledger,
    results: Sequence[DecisionResult],
    *,
    date: datetime.date,
    replicate: int = 1,
    engine: str = __version__,
) -> dict[str, Any]:
    """The run record of a run of `decision` whose `results` (in the dataset's order) `ledger`
    was built from: each result's latency (to 3 places) and its model types' tokens
    (`DecisionResult.usage`), and the models those types reported in its answers. A composite's
    answer names its member and the member's model (`member:model`, joined as the composite
    joins them): a member of a model type counts as that type. A model whose answers a
    composite never kept is not reported, so a cost that needs it is unknown, never guessed.
    Refused when it would not agree with `ledger` (`disagreement`)."""
    if decision.pack.digest != ledger.header["pack"]["digest"]:
        raise MendariError("the decision is not of the ledger's pack (another digest)")
    if len(results) != len(ledger.cases):
        raise MendariError(f"{len(results)} results for a ledger of {len(ledger.cases)} cases")
    cases = []
    reported: dict[str, set[str]] = {}
    for result in results:
        tokens = {
            kind: [metered.input_tokens, metered.output_tokens]
            for kind, metered in sorted(result.usage.items())
            if kind in MODEL_TYPES and metered.calls
        }
        latency = round(result.latency_ms, LATENCY_DIGITS)
        cases.append({"latency_ms": float(latency), "tokens": tokens})
        for kind, model in _answer_models(decision, result):
            reported.setdefault(kind, set()).add(model)
    run = check_run(
        {
            "format": RUN_FORMAT,
            "replicate": replicate,
            "engine": engine,
            "date": date.isoformat(),
            "ledger": ledger.id,
            "models": {kind: sorted(models) for kind, models in sorted(reported.items())},
            "cases": cases,
        }
    )
    problem = disagreement(run, ledger)
    if problem is not None:
        raise MendariError(f"the run record would not agree with its ledger: {problem}")
    return run


def _answer_models(decision: Decision, result: DecisionResult) -> set[tuple[str, str]]:
    """(model type, model) for each model a result's answers name."""
    found: set[tuple[str, str]] = set()
    for answer in result.answers.values():
        info = answer.provider
        if not info.model:
            continue
        if info.type in MODEL_TYPES:
            found.add((info.type, info.model))
            continue
        joiner = JOINERS.get(info.type)
        members = getattr(decision.providers.get(info.slot), "members", None)
        if joiner is None or not isinstance(members, Mapping):
            continue
        for part in info.model.split(joiner):
            name, _, model = part.partition(":")
            kind = getattr(members.get(name), "type", None)
            # A member that reported no model is written `member:None` (`combined`), or
            # `member:<type>` (a vote's): neither is a model.
            if kind in MODEL_TYPES and model and model not in ("None", kind):
                found.add((str(kind), model))
    return found


# --------------------------------------------------------------------------------------------
# The price table
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prices:
    """A dated price table (`mendari.prices/1`): USD per 1,000 input and per 1,000 output tokens,
    per model type and model id, and the SHA-256 of the bytes it was read from."""

    date: str
    source: str
    rates: Mapping[tuple[str, str], tuple[float, float]]
    sha256: str

    def rate(self, kind: str, model: str) -> tuple[float, float] | None:
        return self.rates.get((kind, model))


def prices(raw: bytes, *, where: str = "price table") -> Prices:
    """A price table from its bytes (JSON), cited by their SHA-256."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise MendariError(f"{where}: not JSON") from None
    table = _checked(PriceTable, data, where)
    assert isinstance(table, PriceTable)
    rates = {
        (kind, model): (float(price.input_per_1000), float(price.output_per_1000))
        for kind, models in table.prices.items()
        for model, price in models.items()
    }
    return Prices(table.date, table.source, rates, hashlib.sha256(raw).hexdigest())


def load_prices(path: str | Path) -> Prices:
    """A price table from a file outside Mendari's code: one in the package (or the repository's
    `src/`) or in a pack's directory is refused before it is read, so no built-in price and no
    price a pack sets is ever used."""
    return read_prices(path)[1]


def read_prices(path: str | Path) -> tuple[bytes, Prices]:
    """`load_prices`, with the bytes the table was read from (which the results store keeps,
    under `prices/<date>.json`, for a result priced from it)."""
    where = Path(path)
    if _inside_code_or_pack(where):
        raise MendariError(
            f"{where.name}: a price table is never read from Mendari's code or a pack "
            "(keep it under scoreboard/prices/)"
        )
    try:
        raw = where.read_bytes()
    except OSError as error:
        raise MendariError(
            f"{where.name}: cannot read the price table ({error.strerror})"
        ) from None
    return raw, prices(raw, where=where.name)


def _inside_code_or_pack(path: Path) -> bool:
    resolved = path.resolve()
    package = Path(mendari.__file__).resolve().parent
    code = [package, package.parent] if package.parent.name == "src" else [package]
    if any(resolved.is_relative_to(root) for root in code):
        return True
    return any((parent / "pack.yaml").is_file() for parent in resolved.parents)


# --------------------------------------------------------------------------------------------
# The disclosures
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Disclosures:
    """Per remote provider type, the statement of any commercial relationship between Mendari and
    that provider (`mendari.disclosures/1`)."""

    date: str
    statements: Mapping[str, str]


def disclosures(raw: bytes, *, where: str = "disclosures") -> Disclosures:
    """The disclosures from their bytes (JSON)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise MendariError(f"{where}: not JSON") from None
    found = _checked(DisclosureFile, data, where)
    assert isinstance(found, DisclosureFile)
    return Disclosures(found.date, dict(sorted(found.statements.items())))


def load_disclosures(path: str | Path) -> Disclosures:
    where = Path(path)
    try:
        raw = where.read_bytes()
    except OSError as error:
        raise MendariError(
            f"{where.name}: cannot read the disclosures ({error.strerror})"
        ) from None
    return disclosures(raw, where=where.name)


# --------------------------------------------------------------------------------------------
# The declarations
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Declaration:
    """A pack's declared configurations (`mendari.declared/1`), per arm: the one remote
    configuration run on public sets with replicates (`remote`), and the one local configuration
    a hidden set grades (`local`), each slot to variant, or None. Cited by the SHA-256 of the
    bytes it was read from."""

    pack: str
    date: str
    arms: Mapping[str, Mapping[str, Mapping[str, str] | None]]
    sha256: str

    def declared(self, arm: str, where: str) -> dict[str, str] | None:
        """The configuration declared for `arm` and `where` (`REMOTE` or `LOCAL`), or None."""
        found = self.arms.get(arm, {}).get(where)
        return None if found is None else dict(found)


def declaration(raw: bytes, *, where: str = "declaration") -> Declaration:
    """A declaration from its bytes (JSON), cited by their SHA-256."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise MendariError(f"{where}: not JSON") from None
    found = _checked(DeclaredFile, data, where)
    assert isinstance(found, DeclaredFile)
    declared = {
        arm: {REMOTE: chosen.remote, LOCAL: chosen.local} for arm, chosen in found.arms.items()
    }
    return Declaration(found.pack, found.date, declared, hashlib.sha256(raw).hexdigest())


def declaration_problem(
    declared: Declaration, configurations: Sequence[arms.Configuration]
) -> str | None:
    """Why `declared` is not a declaration of the pack whose `configurations` these are (every
    one, `arms.configurations`), or None: each configuration it names must be one of them, in
    the arm it is declared for, remote where it says remote and local where it says local."""
    for arm, chosen in sorted(declared.arms.items()):
        for where, providers in sorted(chosen.items()):
            if providers is None:
                continue
            found = [c for c in configurations if c.providers == dict(providers)]
            written = ",".join(f"{slot}={providers[slot]}" for slot in sorted(providers))
            if not found:
                return f"its {where} configuration of {arm}, {written}, is not one of the pack's"
            if found[0].arm != arm:
                return f"its {where} configuration of {arm}, {written}, is in {found[0].arm}"
            if found[0].local != (where == LOCAL):
                kind = "local" if found[0].local else "remote"
                return f"its {where} configuration of {arm}, {written}, is {kind}"
    return None


# --------------------------------------------------------------------------------------------
# Replicates, and the canary
# --------------------------------------------------------------------------------------------


def _primary_readings(ledger: Ledger) -> dict[str, tuple[Any, str]]:
    """Each case's primary prediction and outcome, by its ledger hash."""
    primary = ledger.header["primary"]
    found = {}
    for case in ledger.cases:
        answered = case["q"].get(primary)
        found[case["c"]] = (None if answered is None else answered[PREDICTED], case["o"])
    return found


def _same_run_of(first: Ledger, other: Ledger) -> bool:
    """Whether two ledgers are runs of one configuration of one pack over one set."""
    one, two = first.header, other.header
    variants = [{s: p["variant"] for s, p in h["providers"].items()} for h in (one, two)]
    return (
        one["pack"] == two["pack"]
        and one["dataset"] == two["dataset"]
        and one["primary"] == two["primary"]
        and variants[0] == variants[1]
    )


def replicates(
    ledgers: Sequence[Ledger],
    records: Sequence[Mapping[str, Any]],
    figures: Sequence[Mapping[str, float | None]],
) -> dict[str, Any]:
    """The replicates of one configuration over one set (the method's section 10): `ledgers`,
    their run `records` and each one's `figures` (a figure's name to its estimate, None where it
    has none), replicate 1 first. Gives their count, the headline (replicate 1), the case flip
    rate (k of n cases whose primary prediction or outcome differs between any two replicates,
    matched by ledger hash, with its 95% interval), each figure's spread (its minimum to maximum
    across the replicates that have it, a description with no interval) and each model type's
    models across them. Refused, naming replicates by number and never a case: more than
    `MAX_REPLICATES`, replicates out of order, a record that is not its ledger's, or a ledger of
    another pack, set or configuration, or of other cases."""
    if not 1 <= len(ledgers) <= method.MAX_REPLICATES:
        raise MendariError(f"{len(ledgers)} replicates: 1 to {method.MAX_REPLICATES}")
    if not len(ledgers) == len(records) == len(figures):
        raise MendariError("a replicate needs its ledger, its run record and its figures")
    readings = [_primary_readings(ledger) for ledger in ledgers]
    models: dict[str, set[str]] = {}
    for number, (ledger, record) in enumerate(zip(ledgers, records, strict=True), start=1):
        run = check_run(record, where=f"replicate {number}'s run record")
        if run["replicate"] != number:
            raise MendariError(
                f"replicate {number}'s run record is replicate {run['replicate']}: replicate "
                f"{HEADLINE} is the headline, and the others follow it in order"
            )
        problem = disagreement(run, ledger)
        if problem is not None:
            raise MendariError(f"replicate {number}'s run record is not its ledger's: {problem}")
        if not _same_run_of(ledgers[0], ledger):
            raise MendariError(f"replicate {number} is of another pack, set or configuration")
        if set(readings[number - 1]) != set(readings[0]):
            raise MendariError(f"replicate {number} holds other cases than replicate {HEADLINE}")
        for kind, found in run["models"].items():
            models.setdefault(kind, set()).update(found)
    flipped = sum(1 for case in readings[0] if len({read[case] for read in readings}) > 1)
    spread: dict[str, Any] = {}
    for name in sorted({name for found in figures for name in found}):
        held = [float(v) for found in figures if (v := found.get(name)) is not None]
        spread[name] = (
            {
                "min": round(min(held), stats.DIGITS),
                "max": round(max(held), stats.DIGITS),
                "replicates": len(held),
            }
            if held
            else {"status": NOT_ESTIMABLE}
        )
    return {
        "count": len(ledgers),
        "headline": HEADLINE,
        "flip_rate": stats.proportion(flipped, len(readings[0]), method.LEVEL).to_dict(),
        "spread": spread,
        "models": {kind: sorted(found) for kind, found in sorted(models.items())},
    }


def draw(case_hashes: Sequence[str], size: int, seed: int) -> list[int]:
    """The positions, in the set's order, of a seeded draw of `size` of a set's cases: those
    whose SHA-256 of a domain tag, the seed and the case's ledger hash is smallest. The same
    set, size and seed give the same draw on every platform, and never a case's text."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise MendariError("a canary's seed is a whole number, 0 or more")
    if not 1 <= size <= len(case_hashes):
        raise MendariError(f"a draw of {size} from {len(case_hashes)} cases")

    def order(position: int) -> bytes:
        tag = _DRAW + str(seed).encode() + b"\x00" + case_hashes[position].encode()
        return hashlib.sha256(tag).digest()

    return sorted(sorted(range(len(case_hashes)), key=order)[:size])


def canary_flips(canary: Ledger, headline: Ledger) -> tuple[int, int]:
    """k of n: the canary's cases whose primary prediction or outcome differs from replicate 1's
    on the same case, matched by ledger hash. Refused unless every canary case is one of the
    headline's, and both are runs of one configuration of one pack."""
    one, two = canary.header, headline.header
    variants = [{s: p["variant"] for s, p in h["providers"].items()} for h in (one, two)]
    if (one["pack"], one["dataset"]["sha256"], one["primary"], variants[0]) != (
        two["pack"],
        two["dataset"]["sha256"],
        two["primary"],
        variants[1],
    ):
        raise MendariError("the canary is not of its replicates' pack, set and configuration")
    drawn, held = _primary_readings(canary), _primary_readings(headline)
    if not set(drawn) <= set(held):
        raise MendariError("the canary holds a case its replicates do not")
    return sum(1 for case, read in drawn.items() if held[case] != read), len(drawn)


def drift(canary: tuple[int, int], replicated: tuple[int, int]) -> bool:
    """Whether a canary's flip rate (k of n) shows drift: its 95% interval's lower bound is above
    the upper bound of the replicates' flip rate's 95% interval (the method's section 12),
    decided from k and n, never from a stored (rounded) bound."""
    low = stats.clopper_pearson(*canary, method.LEVEL)[0]
    high = stats.clopper_pearson(*replicated, method.LEVEL)[1]
    return low > high
