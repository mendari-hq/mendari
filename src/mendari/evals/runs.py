# SPDX-License-Identifier: Apache-2.0
"""What a scoreboard entry (`mendari.evals.scoreboard`, LP-17d) needs beside its ledger: the run
record (`mendari.run/1`), the dated price table (`mendari.prices/1`) and the disclosures
(`mendari.disclosures/1`). Each is a closed schema, checked when it is read.

- **The run record** holds what a ledger does not, and what no rebuild reproduces: the
  replicate's number, the engine's version, the date, the ledger it is of (its id), the models
  each model type reported, and per case, in the ledger's order, its latency and each model
  type's input and output tokens. It never holds a key, an endpoint or case text. Its bytes are
  its canonical JSON and a newline, and its SHA-256 is of those bytes. `agrees` says whether it
  is of a ledger: the ledger's id, its number of cases, and per case token sums that are the
  ledger's `u`.
- **The price table** is per model type and model id, in USD per 1,000 input tokens and per
  1,000 output tokens, with its date and source. It is cited by the SHA-256 of its bytes. It is
  a file outside Mendari's code (`scoreboard/prices/<date>.json`): one read from the package, or
  from a pack's directory, is refused, so no price built into a provider or set by a pack is
  ever used.
- **The disclosures** give, per remote provider type, the statement of any commercial
  relationship between Mendari and that provider (`scoreboard/disclosures.json`).

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

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

import mendari
from mendari import __version__
from mendari.core.decision import Decision, DecisionResult
from mendari.core.origin import COMPOSITE_PROVIDER_TYPES, LOCAL_PROVIDER_TYPES
from mendari.errors import MendariError
from mendari.evals import method
from mendari.evals.ledger import Ledger, canonical
from mendari.evals.share import JOINERS
from mendari.providers import MODEL_TYPES, PROVIDER_TYPES

RUN_FORMAT = "mendari.run/1"
PRICES_FORMAT = "mendari.prices/1"
DISCLOSURES_FORMAT = "mendari.disclosures/1"
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


def _checked(model: type[BaseModel], data: Any, where: str) -> BaseModel:
    """`data` as `model`, or a `MendariError` that names where it fails, never a value."""
    try:
        return model.model_validate(data)
    except ValidationError as error:
        first = error.errors()[0]
        at = ".".join(str(part) for part in first["loc"]) or "the top level"
        raise MendariError(f"{where}: {at}: {first['msg']}") from None


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
    another number of cases, or a case whose tokens, summed over its model types, are not the
    ledger's `u` (input and output tokens). Names case numbers, never a case."""
    run = check_run(record)
    if run["ledger"] != ledger.id:
        return f"it is the run record of another ledger ({run['ledger']}, not {ledger.id})"
    if len(run["cases"]) != len(ledger.cases):
        return f"it holds {len(run['cases'])} cases and the ledger {len(ledger.cases)}"
    for number, (case, line) in enumerate(zip(run["cases"], ledger.cases, strict=True), start=1):
        spent = [sum(pair[0] for pair in case["tokens"].values())]
        spent.append(sum(pair[1] for pair in case["tokens"].values()))
        if spent != [line["u"][1], line["u"][2]]:
            return f"case {number}'s tokens are not the ledger's"
    return None


def agrees(record: Mapping[str, Any], ledger: Ledger) -> bool:
    """Whether `record` is the run record of `ledger` (`disagreement`)."""
    return disagreement(record, ledger) is None


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
    return prices(raw, where=where.name)


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
