# SPDX-License-Identifier: Apache-2.0
"""The results store (LP-17f, ADR-0013; the grading method's sections 11, 12 and 14): scoreboard
results, and what a public set's result is recomputed from, kept append-only in the repository
under `scoreboard/`, outside the wheel, the sdist and the site.

    scoreboard/
      results/<sha256>.json   a result, named by its body's SHA-256 (`scoreboard.body_sha256`,
                              without `sha256:`), in the form `signing.dump` writes
      ledgers/<sha256>.jsonl  a ledger (`mendari.ledger/1`), named by its id (`Ledger.file_name`)
      runs/<sha256>.json      a run record (`mendari.run/1`), named by the SHA-256 of its bytes
      prices/<date>.json      a dated price table (`mendari.prices/1`), cited by its SHA-256
      canaries/<sha256>.json  a canary (`mendari.canary/1`, LP-17h), named by its body's SHA-256
      declared/<name>.json    a pack's declared configurations (`mendari.declared/1`, LP-17h)
      keys.json               the public grading keys (`mendari.grading-keys/1`, LP-17e)
      disclosures.json        the disclosures (`mendari.disclosures/1`): none yet, so every
                              remote type's statement is pending

A result may name the earlier result it supersedes (`supersedes`, in its body): one of the same
pack, set, arm and configuration, measured no later. Both stay in the store; the later is the
current one (`heads`), and one pack, set, arm and configuration has one current result at most.
Nothing stored changes: `add` refuses to overwrite, and `check` (`scripts/check_scoreboard.py`,
in the required `reference-pack` job) refuses a result removed or changed against a base commit,
except a valid signature added to an unsigned result whose body stays the same. It recomputes
every public result from its stored inputs, and re-runs every local ledger on its pack's current
digest. A result of an older digest is recomputed and kept, never re-run; one whose set or
method changed is kept as it is. A new result must be on its pack's current digest, set and
method, and measured no later than today, so that all of it is checked; a new remote one must
hold its replicates and its pack's declaration of it (its ledgers could not be re-run without a
call off the machine, so they are recomputed from, never re-run). The packs are the
ones Mendari maintains, read from the bundled packs alone (`maintained_pack`: the repository's
`packs/` in a checkout), never from `MENDARI_PACKS_PATH` or the registry cache.

A hidden set's result (`mendari.hidden_scoreboard/1`, LP-17k) is the grading route's answer,
stored with `add` as it was answered: one result per pack and set (its arms are in it), named by
its body's SHA-256, which leaves its run block out, so a second read of the same set is the same
result. It must fit its closed schema (`hidden.scoreboard_problem`), be of a pack Mendari
maintains and, when new, under the current method, measured no later than today, in effect as
the method is that day, and citing its pack's declaration as the store holds it, whose `local`
configurations are the arms it graded; under the current method, it must agree with its own figures
(`hidden.scoreboard_disagreement`: intervals, evidence, grades, banner, ranking, draft). It is
kept as it is: no ledger of a hidden set is kept, so its k and n are the owner's attestation,
and their signature covers it with its run's number.

`measure` runs one configuration of a maintained pack over one of its public scoring sets (every
set it declares but its fitting sets, `train` and `calibration`), and gives its ledger, its run
record (each case's latency and tokens, from its `DecisionResult`) and its entry. It refuses a
fitting set and anything but a set the pack names, so no file and no hidden set.

A remote configuration (LP-17h; the method's sections 10 and 12) is run by hand, never in CI
(`CI` or `GITHUB_ACTIONS` set), in 3 to 5 replicates, and only when it is its arm's remote
configuration in the pack's declaration (`declared/<pack name>.json`), committed before the run:
the result cites the declaration's SHA-256, and its figures are replicate 1's. Its result is
stored with every replicate's ledger and run record; the check recomputes it from them, and
never re-runs a remote ledger, which would call off the machine. A canary (`run_canary`) is a
seeded draw of 30 cases from a pack's `test` set, run once by hand on a remote configuration
whose current result there holds replicates, and stored under `canaries/`: a board marks its
arm's remote results stale when it shows drift, and the results of each model type it reports
another model of. Nothing here signs: results
are stored unsigned, and the owner signs them offline (`mendari scoreboard sign`). Messages name
files, hashes, packs, sets and configurations, never a case.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from mendari.core import clock
from mendari.core.decision import Decision, DecisionResult
from mendari.core.origin import Origin
from mendari.core.pack import Pack
from mendari.core.reference import bundled_dir
from mendari.core.registry import SearchPath, discover
from mendari.core.spec import is_pack_id, version_key
from mendari.errors import MendariError, PackNotFoundError, ProviderError
from mendari.evals import arms, hidden, method, runs, scoreboard, signing
from mendari.evals import ledger as ledgers
from mendari.evals.harness import Dataset, record_problems, resolve_dataset
from mendari.evals.ledger import Ledger, canonical, case_hash
from mendari.providers import MODEL_TYPES, build_provider

ROOT = "scoreboard"  # from the repository's root
RESULTS, LEDGERS, RUNS, PRICES = "results", "ledgers", "runs", "prices"
CANARIES, DECLARED = "canaries", "declared"
KEYS, DISCLOSURES = "keys.json", "disclosures.json"
# The formats the store holds: a scoreboard entry, and a hidden set's answer (LP-17k), held to
# its closed schema.
FORMATS = (scoreboard.FORMAT, hidden.HIDDEN_SCOREBOARD_FORMAT)
# A pack's sets that fit its models (naive Bayes is trained on one, calibrated on the other):
# never scored. Every other set it declares is a public scoring set.
FITTING_SETS = ("train", "calibration")
MAINTAINED: Origin = "maintained"  # the `Pack.origin` of the packs Mendari maintains
KEPT, STORED = "kept", "stored"
_BODY = re.compile(r"sha256:[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]{64}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# Each directory's file names: a hash, or a price table's date.
NAMES = {
    RESULTS: re.compile(r"[0-9a-f]{64}\.json"),
    LEDGERS: re.compile(r"[0-9a-f]{64}\.jsonl"),
    RUNS: re.compile(r"[0-9a-f]{64}\.json"),
    PRICES: re.compile(r"\d{4}-\d{2}-\d{2}\.json"),
    CANARIES: re.compile(r"[0-9a-f]{64}\.json"),
    DECLARED: re.compile(r"[a-z0-9][a-z0-9-]{0,99}\.json"),
}
PackSource = Callable[[str], Pack]


def maintained_pack(ref: str, root: Path | None = None) -> Pack:
    """The pack Mendari maintains that `ref` names (`namespace/name`, its newest version, or
    `namespace/name@version`), read from the bundled packs alone: `root`, or `bundled_dir()`
    (the repository's `packs/` in a checkout). Never from `MENDARI_PACKS_PATH`, the registry
    cache or the community packs, and never a directory `ref` might also name, so the store is
    run, checked and shown against the packs the repository holds, whatever the search path."""
    where = root if root is not None else bundled_dir()
    pack_id, _, version = ref.partition("@")
    found = (
        []
        if where is None
        else [
            pack
            for pack in discover([SearchPath(where, MAINTAINED)]).packs
            if pack.id == pack_id and (not version or pack.version == version)
        ]
    )
    if not found:
        raise PackNotFoundError(
            f"{ref} is not a pack Mendari maintains: the store holds the reference packs' "
            "results (give one by its id)"
        )
    return max(found, key=lambda pack: version_key(pack.version))


def scoring_sets(pack: Pack) -> list[str]:
    """The pack's public scoring sets, in its manifest's order: every set it declares but its
    fitting sets (its `test` set, its enriched set and that set's holdout, for the five
    reference packs)."""
    return [name for name in pack.manifest.evaluation.datasets if name not in FITTING_SETS]


def name_of(result: Mapping[str, Any]) -> str:
    """The name a result is stored under: its body's SHA-256 (`scoreboard.body_sha256`; a hidden
    set's, `hidden.scoreboard_body_sha256`, which leaves its run block out), in hex."""
    if result.get("format") == hidden.HIDDEN_SCOREBOARD_FORMAT:
        return hidden.scoreboard_body_sha256(result).removeprefix("sha256:")
    return scoreboard.body_sha256(result).removeprefix("sha256:")


def written(providers: Mapping[str, str]) -> str:
    """A configuration as the board writes it: `slot=variant`, its slots sorted."""
    return ",".join(f"{slot}={providers[slot]}" for slot in sorted(providers))


def configuration_of(pack: Pack, pairs: Sequence[str] = ()) -> arms.Configuration:
    """The configuration of `pack` that `pairs` choose (`slot=variant`, or several joined by
    commas, as `mendari leaderboard --run` takes them), every other slot on the pack's default
    variant. Never read from the environment: what is run is what is named here."""
    slots = pack.manifest.providers
    chosen: dict[str, str] = {}
    for item in (part.strip() for pair in pairs for part in pair.split(",")):
        slot, sep, variant = (text.strip() for text in item.partition("="))
        if not sep or not slot or not variant:
            raise MendariError(f"--config {item!r} must look like slot=variant")
        if slot not in slots:
            raise MendariError(f"{pack.id} has no slot {slot!r} (its slots: {', '.join(slots)})")
        if variant not in slots[slot].variants:
            raise MendariError(
                f"{pack.id}'s slot {slot} has no variant {variant!r} "
                f"(its variants: {', '.join(slots[slot].variants)})"
            )
        if chosen.get(slot, variant) != variant:
            raise MendariError(f"--config names two variants for the slot {slot}")
        chosen[slot] = variant
    return _configuration(pack, {slot: chosen.get(slot, spec.use) for slot, spec in slots.items()})


def local_configurations(pack: Pack) -> list[arms.Configuration]:
    """Every local configuration of `pack` (`arms.Configuration.local`), in the manifest's
    order: what the early scoreboard runs."""
    return [found for found in arms.configurations(pack) if found.local]


def _configuration(pack: Pack, providers: Mapping[str, str]) -> arms.Configuration:
    for found in arms.configurations(pack):
        if found.providers == dict(providers):
            return found
    raise MendariError(f"{pack.id} has no configuration {written(providers)}")


# --------------------------------------------------------------------------------------------
# What a stored result holds
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Held:
    """A stored result, and the fields of it the store reads: what it is of (its pack, set, arm
    and configuration), when it was measured, the result it supersedes (a name, or None) and its
    inputs."""

    name: str
    result: dict[str, Any]
    pack_id: str
    pack_digest: str
    set_kind: str
    set_name: str
    arm: str
    providers: dict[str, str]
    local: bool
    measured_on: str
    supersedes: str | None
    ledger: str
    run: str
    prices: str | None
    dataset: str
    method: str
    # Each replicate's ledger id and run record SHA-256, replicate 1 first (LP-17h): one for a
    # result of one replicate. And the SHA-256 of the declaration it cites, or None.
    replicates: tuple[tuple[str, str], ...] = ()
    declared: str | None = None

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        """One pack, set, arm and configuration: what a later result of it supersedes."""
        return (self.pack_id, self.set_kind, self.set_name, self.arm, written(self.providers))

    @property
    def signed(self) -> bool:
        return "signature" in self.result

    @property
    def shown(self) -> str:
        """The pack, set and configuration (none for a hidden set's result), for a message."""
        return " ".join(
            part for part in (self.pack_id, self.set_name, written(self.providers)) if part
        )

    @property
    def is_hidden(self) -> bool:
        """Whether it is a hidden set's result: by its format (`mendari.hidden_scoreboard/1`),
        never by what its scope says, so an entry that calls its set hidden is still checked as
        a public one, and refused."""
        return bool(self.result["format"] == hidden.HIDDEN_SCOREBOARD_FORMAT)


def held(result: Any, *, name: str | None = None) -> Held:
    """`result` as the store reads it, or a `MendariError` naming what it lacks: a format the
    store holds, a body that is canonical JSON, a `body_sha256` that is its body's, and the
    fields of its scope, configuration and inputs the store reads. `name`, when given, must be
    the name it is stored under."""
    if not isinstance(result, Mapping) or result.get("format") not in FORMATS:
        raise MendariError(f"not a result the store holds ({' or '.join(FORMATS)})")
    result = dict(result)
    signing.body_bytes(result)  # canonical JSON, or a MendariError
    if result["format"] == hidden.HIDDEN_SCOREBOARD_FORMAT:
        return _held_hidden(result, name)
    found = name_of(result)
    if result.get("body_sha256") != f"sha256:{found}":
        raise MendariError("its body_sha256 is not its body's SHA-256")
    if name is not None and name != found:
        raise MendariError(f"its name is not its body's SHA-256 ({found})")
    try:
        scope, inputs, configuration = result["scope"], result["inputs"], result["configuration"]
        supersedes, prices = result["supersedes"], inputs["prices"]
        values = Held(
            name=found,
            result=result,
            pack_id=scope["pack"]["id"],
            pack_digest=scope["pack"]["digest"],
            set_kind=scope["set"]["kind"],
            set_name=scope["set"]["name"],
            arm=result["arm"],
            providers=dict(configuration["providers"]),
            local=configuration["local"],
            measured_on=scope["measured_on"],
            supersedes=supersedes,
            ledger=inputs["ledger"],
            run=inputs["run"]["sha256"],
            prices=None if prices is None else prices["sha256"],
            dataset=inputs["dataset"],
            method=inputs["method"],
            replicates=_replicate_inputs(inputs),
            declared=inputs.get("declared"),
        )
    except (KeyError, TypeError, AttributeError, ValueError):
        raise MendariError("it lacks a field the store reads (its scope or inputs)") from None
    texts = [
        *(values.pack_id, values.pack_digest, values.set_kind, values.set_name, values.arm),
        *(values.ledger, values.run, values.dataset, values.method, values.measured_on),
        *values.providers,
        *values.providers.values(),
    ]
    if not all(isinstance(text, str) for text in texts) or not isinstance(values.local, bool):
        raise MendariError("a field the store reads is not of its type")
    if _day(values.measured_on) is None:
        raise MendariError("its scope names no day it was measured (YYYY-MM-DD)")
    if not is_pack_id(values.pack_id):
        raise MendariError("its scope names no pack id (namespace/name)")
    # What names a file: a hash, never a path.
    if not (_BODY.fullmatch(values.ledger) and _BODY.fullmatch(values.run)):
        raise MendariError("its ledger and run record are named by sha256:<64 hex>")
    hashes = (values.dataset, values.method, values.prices or "0" * 64, values.declared or "0" * 64)
    if not all(isinstance(text, str) and _HEX.fullmatch(text) for text in hashes):
        raise MendariError(
            "its dataset, method, price table and declaration are named by 64 hex digits"
        )
    named = [text for pair in values.replicates for text in pair]
    if not all(isinstance(text, str) and _BODY.fullmatch(text) for text in named):
        raise MendariError("its replicates' ledgers and run records are named by sha256:<64 hex>")
    if values.replicates and values.replicates[0] != (values.ledger, values.run):
        raise MendariError("its replicate 1 is not its own ledger and run record (the headline)")
    if supersedes is not None and not (isinstance(supersedes, str) and _BODY.fullmatch(supersedes)):
        raise MendariError("supersedes names a result by its body's SHA-256 (sha256:<64 hex>)")
    return replace(values, supersedes=None if supersedes is None else supersedes[len("sha256:") :])


def _replicate_inputs(inputs: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """A result's replicates, as its inputs list them (`inputs.replicates`, LP-17h): each one's
    ledger id and run record SHA-256, numbered 1 up, or none for a result of one replicate."""
    listed = inputs.get("replicates")
    if listed is None:
        return ()
    if not isinstance(listed, list) or not listed:
        raise ValueError("replicates")
    found = []
    for number, item in enumerate(listed, start=1):
        if not isinstance(item, Mapping) or item.get("replicate") != number:
            raise ValueError("replicates")
        found.append((item["ledger"], item["run"]))
    return tuple(found)


def _held_hidden(result: dict[str, Any], name: str | None) -> Held:
    """A hidden set's result as the store reads it: it fits its closed schema, its body's
    SHA-256 is its `body_sha256` (and `name`, when given), and it names a day of the calendar.
    It has no ledger, run record, price table or dataset hash: those fields are empty, and its
    arm and configuration too (its arms are in it), so one pack and set has one current
    result."""
    problem = hidden.scoreboard_problem(result)
    if problem is not None:
        raise MendariError(problem)
    found = name_of(result)
    if name is not None and name != found:
        raise MendariError(f"its name is not its body's SHA-256 ({found})")
    scope, supersedes = result["scope"], result["supersedes"]
    if _day(scope["measured_on"]) is None:
        raise MendariError("its scope names no day it was measured (YYYY-MM-DD)")
    return Held(
        name=found,
        result=result,
        pack_id=scope["pack"]["id"],
        pack_digest=scope["pack"]["digest"],
        set_kind=scope["set"]["kind"],
        set_name=scope["set"]["id"],
        arm="",
        providers={},
        local=True,
        measured_on=scope["measured_on"],
        supersedes=None if supersedes is None else supersedes.removeprefix("sha256:"),
        ledger="",
        run="",
        prices=None,
        dataset="",
        method=scope["method"]["sha256"],
    )


def _day(text: str) -> datetime.date | None:
    """The day `text` names (YYYY-MM-DD, a day of the calendar), or None."""
    if not _DAY.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:  # the right shape, but no such day (2026-02-30)
        return None


# --------------------------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Store:
    """The results store at `root` (`scoreboard/`, from the repository's root)."""

    root: Path

    @classmethod
    def at(cls, root: str | Path) -> Store:
        """The store at `root`, which must be a directory: a command run elsewhere than the
        repository's root is refused, never given a new store."""
        where = Path(root)
        if not where.is_dir():
            raise MendariError(
                f"no results store at {where}: run this from the repository's root, or give --store"
            )
        return cls(where)

    def files(self, kind: str) -> dict[str, Path]:
        """The files in one of the store's directories, by name. Hidden files (`.DS_Store`, a
        signature being written) are no part of it."""
        folder = self.root / kind
        if not folder.is_dir():
            return {}
        return {
            path.name: path for path in sorted(folder.iterdir()) if not path.name.startswith(".")
        }

    def results(self) -> dict[str, Held]:
        """Every stored result, by name. A `MendariError` naming the first file that is not a
        stored result (`check` names each)."""
        found = {}
        for file_name, path in self.files(RESULTS).items():
            name = file_name.removesuffix(".json")
            try:
                found[name] = _read_result(path.read_bytes(), name)
            except MendariError as error:
                raise MendariError(f"{RESULTS}/{file_name}: {error}") from None
        return found

    def ledger(self, ledger_id: str) -> Ledger:
        """A stored ledger, by its id."""
        path = self.root / LEDGERS / (ledger_id.removeprefix("sha256:") + ".jsonl")
        return _ledger(_read(path, f"the ledger {ledger_id}"), path.name)

    def run(self, run_sha256: str) -> dict[str, Any]:
        """A stored run record, by the SHA-256 of its bytes."""
        path = self.root / RUNS / (run_sha256.removeprefix("sha256:") + ".json")
        return _run(_read(path, f"the run record {run_sha256}"), path.name)

    def prices(self, sha256: str) -> runs.Prices:
        """The stored price table whose bytes have this SHA-256."""
        for file_name, path in self.files(PRICES).items():
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() == sha256.removeprefix("sha256:"):
                return _prices(raw, file_name)
        raise MendariError(f"no price table in {PRICES}/ has the SHA-256 {sha256}")

    def put_ledger(self, ledger: Ledger) -> Path:
        """Store a ledger under its id. One stored already is left as it is: its name is its
        content."""
        return _put(self.root / LEDGERS / ledger.file_name, ledger.dumps())

    def put_run(self, record: Mapping[str, Any]) -> Path:
        """Store a run record under the SHA-256 of its bytes."""
        data = runs.dumps_run(record)
        return _put(self.root / RUNS / (hashlib.sha256(data).hexdigest() + ".json"), data)

    def keys(self) -> dict[str, signing.GradingKey]:
        """The public grading keys (`keys.json`), read as strictly as a result."""
        try:
            text = (self.root / KEYS).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise MendariError(f"{KEYS} cannot be read") from None
        return signing.load_keys(signing.read(text))

    def disclosures(self) -> runs.Disclosures | None:
        """The disclosures (`disclosures.json`, `mendari.disclosures/1`), or None while the store
        has none: every remote type's statement is then pending."""
        path = self.root / DISCLOSURES
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise MendariError(f"{DISCLOSURES}: {error.strerror}") from None
        return runs.disclosures(raw, where=DISCLOSURES)

    def declaration_path(self, pack_id: str) -> Path:
        """Where a pack's declaration is kept: `declared/<pack name>.json`."""
        return self.root / DECLARED / f"{pack_id.partition('/')[2]}.json"

    def declaration(self, pack_id: str) -> runs.Declaration | None:
        """The pack's declared configurations (`mendari.declared/1`), or None while it has none.
        A declaration of another pack under its name is refused."""
        path = self.declaration_path(pack_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise MendariError(f"{DECLARED}/{path.name}: {error.strerror}") from None
        found = runs.declaration(raw, where=f"{DECLARED}/{path.name}")
        if found.pack != pack_id:
            raise MendariError(f"{DECLARED}/{path.name} declares {found.pack}, not {pack_id}")
        return found

    def storable_prices(self, raw: bytes, *, where: str = "the price table") -> runs.Prices:
        """A price table from its bytes, refused when the store holds another table of its date
        (`prices/<date>.json`): a table is cited by its SHA-256, and never changes. Asked before
        a run (`refuse_unrunnable`), so that no call is made for a result whose table would then
        be refused."""
        table = runs.prices(raw, where=where)
        path = self.root / PRICES / f"{table.date}.json"
        if path.exists() and path.read_bytes() != raw:
            raise MendariError(
                f"{PRICES}/{path.name} holds another table of that date: a price table never "
                "changes (date the new one)"
            )
        return table

    def put_prices(self, raw: bytes) -> runs.Prices:
        """Store a price table under its date (`prices/<date>.json`), and give it. One stored
        already with the same bytes is left as it is; another table of the same date is
        refused (`storable_prices`)."""
        table = self.storable_prices(raw)
        _put(self.root / PRICES / f"{table.date}.json", raw)
        return table

    def canaries(self) -> dict[str, dict[str, Any]]:
        """Every stored canary (`mendari.canary/1`), by name: its body's SHA-256."""
        found = {}
        for file_name, path in self.files(CANARIES).items():
            name = file_name.removesuffix(".json")
            try:
                found[name] = _read_canary(path.read_bytes(), name)
            except MendariError as error:
                raise MendariError(f"{CANARIES}/{file_name}: {error}") from None
        return found

    def put_canary(self, found: Mapping[str, Any]) -> str:
        """Store a canary under its body's SHA-256, never over another file, and give its name."""
        name = scoreboard.body_sha256(found).removeprefix("sha256:")
        path = self.root / CANARIES / f"{name}.json"
        if path.exists():
            raise MendariError(f"{CANARIES}/{path.name} is stored already")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as target:
            target.write(signing.dump(dict(found)))
        return name

    def add(
        self,
        result: Mapping[str, Any],
        *,
        publisher_ids: Iterable[str] = (),
        packs: PackSource = maintained_pack,
        today: datetime.date | None = None,
    ) -> str:
        """Store `result` under its name, and give that name. Refused, writing nothing: a result
        the store does not hold (`held`); one stored already (the store never overwrites); a
        `supersedes` that names no stored result of the same pack, set, arm and configuration
        measured no later, or one another result supersedes already; a second current result of
        one pack, set, arm and configuration (a new one supersedes the old); a signature that
        does not verify against `keys.json`; and a result that breaks `check`'s rules for a new
        one (`_verdict`): a public result not on its pack's current digest, set and method,
        measured after `today` (the clock's, by default), or not what its stored inputs give,
        with a local ledger that re-runs to the same bytes or a remote configuration's 3 to 5
        replicates and its pack's declaration of it; a hidden set's result that `_hidden`
        refuses."""
        found = held(result)
        path = self.root / RESULTS / f"{found.name}.json"
        if path.exists():
            raise MendariError(
                f"{RESULTS}/{path.name} is stored already: the store never overwrites"
            )
        stored = self.results()
        problem = _link_problem(found, stored) or _claimed_problem(found, stored)
        if problem is None and found.supersedes is None:
            current = _current(found.identity, stored)
            if current is not None:
                problem = (
                    f"{current} is the current result of {found.shown}: a new one supersedes it"
                )
        if problem is None and found.signed:
            signature = signing.problem(found.result, self.keys(), publisher_ids=publisher_ids)
            if signature is not None:
                problem = f"its signature does not verify: {signature}"
        if problem is None:
            day = clock.today() if today is None else today
            problem = _verdict(found, self, packs, new=True, reruns={}, today=day).problem
        if problem is not None:
            raise MendariError(problem)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as target:  # never over another file
            target.write(signing.dump(found.result))
        return found.name


def _read(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise MendariError(f"{what} is not in the store ({path.parent.name}/{path.name})") from None
    except OSError as error:
        raise MendariError(f"{path.parent.name}/{path.name}: {error.strerror}") from None


def _put(path: Path, data: bytes) -> Path:
    if path.exists():
        if path.read_bytes() != data:  # its name is its hash: only a broken store gets here
            raise MendariError(f"{path.parent.name}/{path.name} holds other bytes than its name's")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as target:
        target.write(data)
    return path


def _read_result(raw: bytes, name: str) -> Held:
    """A stored result from its file's bytes: read strictly (`signing.read`), in the form
    `signing.dump` writes, and named by its body's SHA-256."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MendariError("not UTF-8 text") from None
    found = held(signing.read(text), name=name)
    if signing.dump(found.result) != text:
        raise MendariError("not in the form the store writes (sorted, indented, one newline)")
    return found


def _ledger(raw: bytes, file_name: str) -> Ledger:
    ledger = Ledger.loads(raw, where=f"{LEDGERS}/{file_name}")
    if ledger.dumps() != raw:
        raise MendariError(f"{LEDGERS}/{file_name}: not in a ledger's canonical form")
    if ledger.file_name != file_name:
        raise MendariError(f"{LEDGERS}/{file_name}: its name is not its id ({ledger.id})")
    return ledger


def _run(raw: bytes, file_name: str) -> dict[str, Any]:
    record = runs.loads_run(raw, where=f"{RUNS}/{file_name}")
    if hashlib.sha256(raw).hexdigest() + ".json" != file_name:
        raise MendariError(f"{RUNS}/{file_name}: its name is not the SHA-256 of its bytes")
    return record


def _read_canary(raw: bytes, name: str) -> dict[str, Any]:
    """A stored canary from its file's bytes: read strictly, in the form the store writes, of
    its format, and named by its body's SHA-256."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MendariError("not UTF-8 text") from None
    found = signing.read(text)
    if not isinstance(found, dict) or found.get("format") != scoreboard.CANARY_FORMAT:
        raise MendariError(f"not a {scoreboard.CANARY_FORMAT} canary")
    if signing.dump(found) != text:
        raise MendariError("not in the form the store writes (sorted, indented, one newline)")
    body = scoreboard.body_sha256(found)
    if found.get("body_sha256") != body or body != f"sha256:{name}":
        raise MendariError("its name and its body_sha256 are not its body's SHA-256")
    return found


def _declared(raw: bytes, file_name: str) -> runs.Declaration:
    found = runs.declaration(raw, where=f"{DECLARED}/{file_name}")
    if f"{found.pack.partition('/')[2]}.json" != file_name:
        raise MendariError(f"{DECLARED}/{file_name}: its name is not its pack's ({found.pack})")
    return found


def _prices(raw: bytes, file_name: str) -> runs.Prices:
    table = runs.prices(raw, where=f"{PRICES}/{file_name}")
    if f"{table.date}.json" != file_name:
        raise MendariError(f"{PRICES}/{file_name}: its name is not its date ({table.date})")
    return table


# --------------------------------------------------------------------------------------------
# Supersedes, and the current results
# --------------------------------------------------------------------------------------------


def superseded(results: Mapping[str, Held]) -> dict[str, str]:
    """Each superseded result's name, with the name of the result that supersedes it (the
    first by name, should two name it: `check` refuses that)."""
    found: dict[str, str] = {}
    for name in sorted(results):
        target = results[name].supersedes
        if target is not None and target not in found:
            found[target] = name
    return found


def heads(results: Mapping[str, Held]) -> dict[str, Held]:
    """The current results: those no stored result supersedes."""
    later = superseded(results)
    return {name: found for name, found in sorted(results.items()) if name not in later}


def _current(identity: tuple[str, ...], results: Mapping[str, Held]) -> str | None:
    """The current result of one pack, set, arm and configuration, by name, or None (the
    latest measured, should there be more than one: `check` refuses that)."""
    shown = [found for found in heads(results).values() if found.identity == identity]
    if not shown:
        return None
    return max(shown, key=lambda found: (found.measured_on, found.name)).name


def _link_problem(found: Held, results: Mapping[str, Held]) -> str | None:
    """Why `found`'s `supersedes` does not name an earlier stored result of the same pack, set,
    arm and configuration, or None."""
    if found.supersedes is None:
        return None
    target = results.get(found.supersedes)
    if target is None:
        return f"it supersedes {found.supersedes}, which is not stored"
    if target.identity != found.identity:
        return f"it supersedes {target.name}, a result of another pack, set, arm or configuration"
    if target.measured_on > found.measured_on:
        return f"it supersedes {target.name}, measured after it"
    return None


def _claimed_problem(found: Held, results: Mapping[str, Held]) -> str | None:
    """Why `found` may not supersede what it names: another stored result supersedes it."""
    if found.supersedes is None:
        return None
    for name, other in sorted(results.items()):
        if name != found.name and other.supersedes == found.supersedes:
            return f"it supersedes {found.supersedes}, which {name} supersedes already"
    return None


# --------------------------------------------------------------------------------------------
# Running a configuration
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Measured:
    """One run of one configuration over one set: its ledger, its run record and its entry;
    and, for a remote configuration's replicates, every replicate's ledger and run record,
    replicate 1 (the headline, `ledger` and `run`) first."""

    ledger: Ledger
    run: dict[str, Any]
    entry: dict[str, Any]
    replicates: tuple[tuple[Ledger, dict[str, Any]], ...] = ()

    @property
    def name(self) -> str:
        return name_of(self.entry)

    @property
    def inputs(self) -> list[tuple[Ledger, dict[str, Any]]]:
        """Every ledger and run record the entry is made from."""
        return list(self.replicates) or [(self.ledger, self.run)]


def refuse_in_ci(what: str) -> None:
    """A remote configuration's replicates and a canary call off the machine, and are run by
    hand: never in CI (`CI` or `GITHUB_ACTIONS` set), whatever a job's settings hold."""
    for name in signing.CI_SETTINGS:
        if os.environ.get(name, "").strip():
            raise MendariError(
                f"{name} is set: {what} is never run in CI; it is run by hand, outside CI "
                "(docs/operations/scoreboard.md)"
            )


def committed(path: Path) -> bool:
    """Whether `path` is committed as it is: the file git's HEAD holds at that path has its
    bytes. False outside a git checkout, for a file git does not track, and for one changed
    since its last commit."""
    try:
        raw = path.read_bytes()
        shown = subprocess.run(
            ["git", "-C", str(path.parent), "show", f"HEAD:./{path.name}"],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return shown.returncode == 0 and shown.stdout == raw


def declared_for(store: Store, pack: Pack, configuration: arms.Configuration) -> str:
    """The SHA-256 of the declaration that declares `configuration` as its arm's remote
    configuration (`declared/<pack name>.json`, `mendari.declared/1`), committed before the run.
    Refused, before anything runs: no declaration, one that does not declare this configuration
    for its arm, one that names a configuration the pack does not have, and one not committed
    as it is (a run whose configuration was not declared before it is refused)."""
    path = store.declaration_path(pack.id)
    shown = f"{DECLARED}/{path.name}"
    found = store.declaration(pack.id)
    if found is None:
        raise MendariError(
            f"{written(configuration.providers)} is not declared: a remote configuration is run "
            f"only as its arm's declared one ({shown}, committed before the run)"
        )
    problem = runs.declaration_problem(found, arms.configurations(pack))
    if problem is not None:
        raise MendariError(f"{shown}: {problem}")
    declared = found.declared(configuration.arm, runs.REMOTE)
    if declared != configuration.providers:
        named = "none" if declared is None else written(declared)
        raise MendariError(
            f"{written(configuration.providers)} is not the declared remote configuration of "
            f"{configuration.arm} in {shown} ({named}): one configuration per arm is declared "
            "before any run"
        )
    if not committed(path):
        raise MendariError(
            f"{shown} is not committed as it is: commit the declaration before the run, so the "
            "run cannot choose it"
        )
    return found.sha256


def refuse_unconfigured(pack: Pack, configuration: arms.Configuration) -> None:
    """A remote configuration whose providers this machine has not set up is refused before
    anything runs: each provider it calls is preflighted (`Provider.preflight`, as a decision
    preflights it: the CQL engine's URL, Jev's model and endpoint, an LLM's model). Run without
    them, every case would fail and go to a person, and the result stored, of those failures,
    would supersede a good one."""
    for slot, variant in configuration.providers.items():
        for leaf in build_provider(pack, slot, variant).leaves():
            try:
                leaf.preflight()
            except ProviderError as error:
                raise MendariError(
                    f"{written(configuration.providers)}: {slot}={variant} cannot run on this "
                    f"machine, so every case would fail: {error}; set its provider up before "
                    "the run (docs/operations/scoreboard.md, step 8)"
                ) from None


def _metered(configuration: arms.Configuration) -> bool:
    """Whether the configuration calls a model type, whose tokens a price table prices."""
    return bool(MODEL_TYPES.intersection(configuration.remote_types))


def refuse_unmeasurable(
    pack: Pack, set_name: str, configuration: arms.Configuration, *, replicates: int = 1
) -> None:
    """A `MendariError` unless `pack` is a pack Mendari maintains and `set_name` one of its
    public scoring sets: the store's results are the reference packs', on sets in the
    repository. A local configuration is deterministic, so it runs one replicate. A remote one
    runs `method.MIN_REPLICATES` to `method.MAX_REPLICATES`, never in CI."""
    if pack.origin != MAINTAINED:
        raise MendariError(
            f"{pack.ref} is not a pack Mendari maintains: the store holds the reference packs' "
            "results (give the pack by its id)"
        )
    sets = ", ".join(scoring_sets(pack))
    if set_name in FITTING_SETS and set_name in pack.manifest.evaluation.datasets:
        raise MendariError(
            f"{set_name} fits {pack.id}'s models, so it is never scored (its public scoring "
            f"sets: {sets})"
        )
    if set_name not in scoring_sets(pack):
        raise MendariError(
            f"{set_name!r} is not one of {pack.id}'s public scoring sets ({sets}): a set is "
            "named, never a file, and no hidden set is run here"
        )
    shown = written(configuration.providers)
    if configuration.local:
        if replicates != 1:
            raise MendariError(
                f"{shown} is local, so deterministic: one replicate, no flips (not {replicates})"
            )
        return
    refuse_in_ci(f"a remote configuration ({shown} calls {', '.join(configuration.remote_types)})")
    if not method.MIN_REPLICATES <= replicates <= method.MAX_REPLICATES:
        raise MendariError(
            f"{shown} calls {', '.join(configuration.remote_types)} off this machine: it runs "
            f"{method.MIN_REPLICATES} to {method.MAX_REPLICATES} replicates (--replicates N), "
            f"not {replicates or 'none'}"
        )


def _decided(
    pack: Pack, dataset: Dataset, configuration: arms.Configuration
) -> tuple[Decision, list[DecisionResult], Ledger]:
    """Each case of `dataset` decided by `pack` under `configuration` and its own policy (as
    `mendari eval` checks it), and the ledger of those decisions."""
    problems = record_problems(pack.manifest, dataset)
    if problems:
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        raise MendariError(f"{problems[0]}{more}")
    decision = Decision(pack, providers=configuration.providers, origin_cap=False)
    results = [decision.decide(record["input"]) for record in dataset.records]
    return decision, results, ledgers.build(decision, dataset, results)


def measure(
    pack: Pack,
    set_name: str,
    configuration: arms.Configuration,
    *,
    date: datetime.date,
    supersedes: str | None = None,
    replicates: int = 1,
    prices: runs.Prices | None = None,
    disclosures: runs.Disclosures | None = None,
    declared: str | None = None,
) -> Measured:
    """Run `configuration` of `pack` over its public scoring set `set_name` (refused unless
    `refuse_unmeasurable` allows it), and give its ledger, its run record (dated `date`) and its
    entry, unsigned, which names `supersedes` (a stored result's name) when given. A remote
    configuration runs `replicates` times, one after another, each with its own providers: the
    entry's figures are replicate 1's, with the replicates' spread and flip rate, priced from
    `prices`, with the statements of `disclosures`, citing the declaration `declared`."""
    refuse_unmeasurable(pack, set_name, configuration, replicates=replicates)
    dataset = resolve_dataset(pack, set_name)
    made = []
    for number in range(1, replicates + 1):
        decision, results, ledger = _decided(pack, dataset, configuration)
        made.append((ledger, runs.record(decision, ledger, results, date=date, replicate=number)))
    (ledger, record), others = made[0], made[1:]
    found = scoreboard.entry(
        ledger,
        dataset,
        record,
        pack=scoreboard.PackScope.of(pack),
        arm=configuration.arm,
        configuration=configuration,
        prices=prices,
        disclosures=disclosures,
        supersedes=None if supersedes is None else f"sha256:{supersedes}",
        replicates=others,
        declared=declared,
    )
    return Measured(ledger, record, found, tuple(made) if others else ())


@dataclass(frozen=True)
class Outcome:
    """What `record_run` did for one configuration on one set: `kept` its current result, or
    `stored` a new one (`name`), which supersedes `supersedes` when that is not None."""

    action: str
    name: str
    shown: str
    measured_on: str
    supersedes: str | None = None

    @property
    def line(self) -> str:
        if self.action == KEPT:
            return (
                f"kept: {self.shown} is current ({RESULTS}/{self.name}.json, measured on "
                f"{self.measured_on})"
            )
        later = f", superseding {self.supersedes}" if self.supersedes else ""
        return f"stored: {self.shown} as {RESULTS}/{self.name}.json{later}"


def is_current(found: Held, pack: Pack, set_sha256: str, today: datetime.date) -> bool:
    """Whether `found` is current on `today`: on the pack's digest, the set's bytes and the
    method, and not past the day it goes stale (`STALE_AFTER_DAYS` after it was measured). The
    marks only a board works out, from the other results and the canaries ("stale: canary
    drift", "stale: another model reported"), are not read here: a result they mark is run
    again with `again` (`--again`, docs/operations/scoreboard.md)."""
    stale = datetime.date.fromisoformat(scoreboard.stale_after(found.result))
    return (
        found.pack_digest == pack.digest
        and found.dataset == set_sha256
        and found.method == method.METHOD_SHA256
        and today <= stale
    )


def refuse_unrunnable(
    store: Store,
    pack: Pack,
    set_name: str,
    configuration: arms.Configuration,
    *,
    replicates: int = 1,
    prices: bytes | None = None,
) -> str | None:
    """Everything `record_run` refuses before anything runs, and the SHA-256 of the declaration
    a remote configuration cites (None for a local one): `refuse_unmeasurable`, then, for a
    remote configuration, its declaration (`declared_for`) and, when it calls a model type, a
    price table (`--prices`), so that its cost is priced from a dated table, never unknown for
    want of one. A configuration that calls no model (CQL) needs none: nothing is priced. A
    table the store could not keep (another table of its date is stored) is refused here, before
    any call, not after every replicate has run (`Store.storable_prices`); and so is a provider
    this machine has not set up (`refuse_unconfigured`)."""
    refuse_unmeasurable(pack, set_name, configuration, replicates=replicates)
    if configuration.local:
        if prices is not None:
            raise MendariError(
                f"{written(configuration.providers)} is local: it calls no model, so it is never "
                "priced (no --prices)"
            )
        return None
    declared = declared_for(store, pack, configuration)
    if prices is None and _metered(configuration):
        raise MendariError(
            f"{written(configuration.providers)} calls a model: give its dated price table "
            "(--prices scoreboard/prices/<date>.json)"
        )
    if prices is not None:
        store.storable_prices(prices, where="--prices")
    refuse_unconfigured(pack, configuration)
    return declared


def record_run(
    store: Store,
    pack: Pack,
    set_name: str,
    configuration: arms.Configuration,
    *,
    date: datetime.date,
    again: bool = False,
    replicates: int = 1,
    prices: bytes | None = None,
) -> Outcome:
    """Run `configuration` of `pack` over `set_name`, and store its ledger, its run record and
    its entry; unless its current result is current on `date` (`is_current`) and not `again`,
    which is kept. A new result supersedes the current one, if any. A remote configuration runs
    `replicates` times, by hand and never in CI, as its arm's declared one (`refuse_unrunnable`):
    every replicate's ledger and run record is stored, and the price table (`prices`, its bytes)
    when the entry is priced from it."""
    declared = refuse_unrunnable(
        store, pack, set_name, configuration, replicates=replicates, prices=prices
    )
    stored = store.results()
    providers = written(configuration.providers)
    identity = (pack.id, scoreboard.PUBLIC, set_name, configuration.arm, providers)
    shown = f"{pack.id} {set_name} {providers}"
    current = _current(identity, stored)
    if current is not None and not again:
        dataset = resolve_dataset(pack, set_name)
        if is_current(stored[current], pack, dataset.sha256, date):
            return Outcome(KEPT, current, shown, stored[current].measured_on)
    measured = measure(
        pack,
        set_name,
        configuration,
        date=date,
        supersedes=current,
        replicates=replicates,
        prices=None if prices is None else runs.prices(prices, where="--prices"),
        disclosures=None if configuration.local else store.disclosures(),
        declared=declared,
    )
    cited = measured.entry["inputs"]["prices"]
    inputs = [
        path
        for ledger, record in measured.inputs
        for path in (
            store.root / LEDGERS / ledger.file_name,
            store.root / RUNS / (runs.run_sha256(record).removeprefix("sha256:") + ".json"),
        )
    ]
    if cited is not None:
        inputs.append(store.root / PRICES / f"{cited['date']}.json")
    made = list(dict.fromkeys(path for path in inputs if not path.exists()))
    try:
        for ledger, record in measured.inputs:
            store.put_ledger(ledger)
            store.put_run(record)
        if cited is not None and prices is not None:
            store.put_prices(prices)
        name = store.add(measured.entry, packs=lambda _: pack)
    except MendariError:
        for path in made:  # no input is left behind without its result
            path.unlink(missing_ok=True)
        raise
    return Outcome(STORED, name, shown, date.isoformat(), supersedes=current)


def run_canary(
    store: Store,
    pack: Pack,
    configuration: arms.Configuration,
    *,
    size: int,
    seed: int,
    date: datetime.date,
) -> tuple[str, dict[str, Any]]:
    """A canary (the method's section 12): a seeded draw of `size` cases from `pack`'s `test`
    set, decided once by `configuration`, by hand and never in CI, and compared with replicate 1
    of its current result there, which must hold replicates and be on the pack's current digest
    and set, with its providers set up on this machine (`refuse_unconfigured`: a canary of
    failures would show drift, and canaries are never removed). Its ledger, run record and canary
    (`mendari.canary/1`) are stored; gives the canary's name and the canary. No schedule runs
    it."""
    refuse_in_ci("a canary")
    shown = written(configuration.providers)
    if pack.origin != MAINTAINED:
        raise MendariError(f"{pack.ref} is not a pack Mendari maintains")
    if configuration.local:
        raise MendariError(f"{shown} is local, so deterministic: a canary is of a remote one")
    if size != method.CANARY_SIZE:
        raise MendariError(f"a canary under {method.METHOD} is {method.CANARY_SIZE} cases")
    if scoreboard.CANARY_SET not in scoring_sets(pack):
        raise MendariError(f"{pack.id} has no `{scoreboard.CANARY_SET}` set to draw a canary from")
    identity = (pack.id, scoreboard.PUBLIC, scoreboard.CANARY_SET, configuration.arm, shown)
    stored = store.results()
    current = _current(identity, stored)
    if current is None or not stored[current].replicates:
        raise MendariError(
            f"{pack.id} {scoreboard.CANARY_SET} {shown} has no current result with replicates: "
            "run them first (mendari scoreboard run --replicates N)"
        )
    against = stored[current]
    dataset = resolve_dataset(pack, scoreboard.CANARY_SET)
    if against.pack_digest != pack.digest or against.dataset != dataset.sha256:
        raise MendariError(
            f"{RESULTS}/{current}.json is of another digest or set: run its replicates again first"
        )
    refuse_unconfigured(pack, configuration)
    hashes = [case_hash(dataset.case_key, record["id"]) for record in dataset.records]
    drawn = replace(dataset, records=[dataset.records[at] for at in runs.draw(hashes, size, seed)])
    decision, results, ledger = _decided(pack, drawn, configuration)
    record = runs.record(decision, ledger, results, date=date)
    found = scoreboard.canary(
        ledger,
        record,
        against=against.result,
        headline=store.ledger(against.ledger),
        dataset=dataset,
        size=size,
        seed=seed,
    )
    store.put_ledger(ledger)
    store.put_run(record)
    return store.put_canary(found), found


# --------------------------------------------------------------------------------------------
# Recomputing a public result
# --------------------------------------------------------------------------------------------


def recompute(found: Held, store: Store, pack: Pack, *, new: bool = True) -> dict[str, Any]:
    """The entry `found`'s stored inputs give: its ledger, its run record and its price table
    from the store, its dataset from `pack` (the pack it is of, as it is now), the method, the
    disclosures and its `supersedes` link. A result of an older digest is recomputed with its
    own pack scope and configuration, as it holds them; one on the current digest with the
    pack's own. A remote configuration's disclosure statements are, for a `new` result, the
    store's (`disclosures.json`; none while there is no file, so every remote type's is
    pending), never the ones the result brings; one stored before keeps those it holds, which
    were the store's when it was added. A result with replicates is recomputed from every
    replicate's stored ledger and run record, and keeps the declaration it cites (LP-17h)."""
    scope = found.result["scope"]
    if found.pack_digest == pack.digest:
        pack_scope = scoreboard.PackScope.of(pack)
        configuration = _configuration(pack, found.providers)
    else:
        own = scope["pack"]
        pack_scope = scoreboard.PackScope(own["id"], own["version"], own["digest"], scope["status"])
        stored = found.result["configuration"]
        configuration = arms.Configuration(
            providers=dict(stored["providers"]),
            arm=found.arm,
            local=bool(stored["local"]),
            remote_types=tuple(stored["remote_types"]),
        )
    disclosures: runs.Disclosures | None = None  # a local configuration: "no model provider"
    if configuration.remote_types and new:
        disclosures = store.disclosures()
    elif configuration.remote_types:
        statements = _held_statements(scope)
        disclosures = runs.Disclosures(date="", statements=statements) if statements else None
    return scoreboard.entry(
        store.ledger(found.ledger),
        resolve_dataset(pack, found.set_name),
        store.run(found.run),
        pack=pack_scope,
        arm=found.arm,
        configuration=configuration,
        prices=None if found.prices is None else store.prices(found.prices),
        disclosures=disclosures,
        supersedes=found.result["supersedes"],
        replicates=[(store.ledger(ledger), store.run(run)) for ledger, run in found.replicates[1:]],
        declared=found.declared,
    )


def _held_statements(scope: Mapping[str, Any]) -> dict[str, str]:
    """The disclosure statements a stored result's scope holds, by remote type (a pending
    type's is null, and left out)."""
    disclosure = scope.get("disclosure")
    statements = disclosure.get("statements") if isinstance(disclosure, Mapping) else None
    if not isinstance(statements, Mapping):
        return {}
    return {kind: text for kind, text in statements.items() if isinstance(text, str)}


def rerun(found: Held, pack: Pack) -> Ledger:
    """The ledger a new run of `found`'s configuration over its set gives: every case decided
    again, on this machine. Only a local result on its pack's current digest is re-run."""
    if found.pack_digest != pack.digest or not found.local:
        raise MendariError(f"{found.name}: only a local result on its pack's digest is re-run")
    configuration = _configuration(pack, found.providers)
    return _decided(pack, resolve_dataset(pack, found.set_name), configuration)[2]


@dataclass(frozen=True)
class Verdict:
    """What checking one public result found: a `problem`, or None; whether it was recomputed;
    and why it was `kept` without all of it being checked again, or None."""

    problem: str | None = None
    recomputed: bool = False
    kept: str | None = None


def _public(
    found: Held,
    store: Store,
    packs: PackSource,
    *,
    new: bool,
    reruns: dict[str, bool],
    today: datetime.date,
) -> Verdict:
    """Whether a public result is what its stored inputs give. A `new` one must be on its pack's
    current digest, set and method, and measured no later than `today`, so that it is
    recomputed, and a later run can supersede it. A new local one's ledger is re-run. A new
    remote one (LP-17h) must hold 3 to 5 replicates and cite its pack's declaration as the store
    holds it, which declares its configuration as its arm's remote one (`_remote_problem`); it
    is recomputed from every replicate's ledger and run record, and never re-run, which would
    call off the machine. One stored before is recomputed while its set and method are current,
    and its ledger re-run while its digest is too and it is local; otherwise it is kept. Its
    inputs must be in the store either way. `reruns` holds each ledger re-run already, by id:
    two results that share a ledger run it once."""
    where = f"{RESULTS}/{found.name}.json ({found.shown})"
    if found.set_kind != scoreboard.PUBLIC:
        return Verdict(f"{where}: not a public set's result")
    if new and found.measured_on > today.isoformat():
        return Verdict(
            f"{where}: it is measured on {found.measured_on}, after today ({today.isoformat()}): "
            "a result is dated the day it is run"
        )
    problem = _remote_problem(found, store) if not found.local else None
    if new and found.local and found.declared is not None:
        problem = "a local configuration is never declared for a public set: every one is run"
    if new and problem is not None:
        return Verdict(f"{where}: {problem}")
    try:
        for ledger, run in found.replicates or ((found.ledger, found.run),):
            store.ledger(ledger)
            store.run(run)
        if found.prices is not None:
            store.prices(found.prices)
        pack = packs(found.pack_id)
    except MendariError as error:
        return Verdict(f"{where}: {error}")
    stale = _stale_inputs(found, pack)
    if stale is not None and new:
        return Verdict(
            f"{where}: a new result is measured on its pack's current digest, set and method "
            f"({stale})"
        )
    if stale is not None and stale != scoreboard.STALE_PACK:
        return Verdict(kept=f"{found.name}: kept as it is, not recomputed ({stale})")
    try:
        again = recompute(found, store, pack, new=new)
    except MendariError as error:
        return Verdict(f"{where}: it cannot be recomputed from its inputs: {error}")
    if canonical(again) != canonical(signing.body(found.result)):
        return Verdict(f"{where}: it is not what its inputs give (recomputed, a byte differs)")
    if stale is not None:
        return Verdict(recomputed=True, kept=f"{found.name}: its ledger not re-run ({stale})")
    if not found.local:  # stored before: a remote configuration is never re-run, no call is made
        return Verdict(
            recomputed=True, kept=f"{found.name}: its ledger not re-run (a remote configuration)"
        )
    if found.ledger not in reruns:
        try:
            reruns[found.ledger] = rerun(found, pack).dumps() == store.ledger(found.ledger).dumps()
        except MendariError as error:
            return Verdict(f"{where}: its ledger cannot be re-run: {error}", recomputed=True)
    if not reruns[found.ledger]:
        return Verdict(
            f"{where}: its ledger is not what a new run gives (re-run, a byte differs)",
            recomputed=True,
        )
    return Verdict(recomputed=True)


def _remote_problem(found: Held, store: Store) -> str | None:
    """Why a new remote configuration's result may not be stored (LP-17h), or None: it holds
    `method.MIN_REPLICATES` to `method.MAX_REPLICATES` replicates, and cites the SHA-256 of its
    pack's declaration as the store holds it now, which declares its configuration as its arm's
    remote one."""
    count = len(found.replicates)
    if not method.MIN_REPLICATES <= count <= method.MAX_REPLICATES:
        return (
            f"a remote configuration's result holds {method.MIN_REPLICATES} to "
            f"{method.MAX_REPLICATES} replicates, not {count or 'one'}"
        )
    if found.declared is None:
        return "a remote configuration's result cites its pack's declaration (it cites none)"
    shown = f"{DECLARED}/{store.declaration_path(found.pack_id).name}"
    try:
        declared = store.declaration(found.pack_id)
    except MendariError as error:
        return str(error)
    if declared is None:
        return f"it cites a declaration, and the store has none ({shown})"
    if declared.sha256 != found.declared:
        return (
            f"it cites the declaration {found.declared}, not {shown} as it is ({declared.sha256})"
        )
    if declared.declared(found.arm, runs.REMOTE) != found.providers:
        return f"its configuration is not the remote configuration {shown} declares for {found.arm}"
    return None


def _verdict(
    found: Held,
    store: Store,
    packs: PackSource,
    *,
    new: bool,
    reruns: dict[str, bool],
    today: datetime.date,
) -> Verdict:
    """`_hidden` for a hidden set's result, `_public` for any other."""
    if found.is_hidden:
        return _hidden(found, store, packs, new=new, today=today)
    return _public(found, store, packs, new=new, reruns=reruns, today=today)


HIDDEN_KEPT = "kept as it is: a hidden set's result is never recomputed (no ledger of it is kept)"


def _hidden(
    found: Held, store: Store, packs: PackSource, *, new: bool, today: datetime.date
) -> Verdict:
    """Whether a hidden set's result may stay in the store, or be added (`new`): of a pack
    Mendari maintains (any of its versions: the deployment graded the one it serves); when new,
    under the current method, measured no later than `today`, in effect or not as the method is
    on the day measured, and grading what its pack's declaration declares
    (`_hidden_declaration_problem`); and, under the current method, agreeing with its own
    figures (`hidden.scoreboard_disagreement`: its intervals, evidence, grades, banner and
    ranking, each draft while its scope's method is not in effect). Its closed schema was
    checked when it was read (`held`). It is kept as it is: with no ledger, its k and n are
    never recomputed. One under an earlier method, or stored before its declaration changed, is
    kept as it was added."""
    where = f"{RESULTS}/{found.name}.json ({found.shown})"
    if new and found.measured_on > today.isoformat():
        return Verdict(
            f"{where}: it is measured on {found.measured_on}, after today ({today.isoformat()}): "
            "a result is dated the day it is run"
        )
    if new and found.method != method.METHOD_SHA256:
        return Verdict(
            f"{where}: a new result is made under the current method ({scoreboard.STALE_METHOD})"
        )
    measured = _day(found.measured_on)
    assert measured is not None  # `held` refused any other
    in_effect = found.result["scope"]["method"]["in_effect"]
    if new and in_effect != method.in_effect(measured):
        return Verdict(
            f"{where}: its scope says the method is {'' if in_effect else 'not '}in effect on "
            f"{found.measured_on}, which the method's effective date does not give"
        )
    problem = _hidden_declaration_problem(found, store) if new else None
    if problem is not None:
        return Verdict(f"{where}: {problem}")
    if found.method == method.METHOD_SHA256:
        problem = hidden.scoreboard_disagreement(found.result)
        if problem is not None:
            return Verdict(f"{where}: {problem}")
    try:
        packs(found.pack_id)
    except MendariError as error:
        return Verdict(f"{where}: {error}")
    return Verdict(kept=f"{found.name}: {HIDDEN_KEPT}")


def _hidden_declaration_problem(found: Held, store: Store) -> str | None:
    """Why a new hidden set's result does not grade what its pack's declaration declares
    (`mendari.declared/1`, LP-17h), or None: the store holds the pack's declaration, the answer
    cites its SHA-256 as the store holds it now (`scope.declared`), and the arms it graded are
    the arms the declaration names a `local` configuration for (the one a hidden set grades),
    each with that configuration, every one of them in the one read. The grading route never
    reads the file, so this is where an answer meets it."""
    shown = f"{DECLARED}/{store.declaration_path(found.pack_id).name}"
    try:
        declared = store.declaration(found.pack_id)
    except MendariError as error:
        return str(error)
    if declared is None:
        return f"it cites a declaration, and the store has none ({shown})"
    cited = found.result["scope"]["declared"]
    if declared.sha256 != cited:
        return f"it cites the declaration {cited}, not {shown} as it is ({declared.sha256})"
    graded = {
        item["arm"]: item["configuration"]["providers"]
        for item in found.result["arms"]
        if item["status"] == hidden.GRADED
    }
    for arm in arms.ARMS:
        local = declared.declared(arm, runs.LOCAL)
        if arm in graded and local is None:
            return f"it grades {arm}, for which {shown} declares no local configuration"
        if arm in graded and graded[arm] != local:
            return (
                f"its {arm} ran {written(graded[arm])}, not the local configuration {shown} "
                f"declares for it"
            )
        if arm not in graded and local is not None:
            return (
                f"it leaves out {arm}, whose local configuration {shown} declares: every "
                "declared arm is graded in the one read"
            )
    return None


def _stale_inputs(found: Held, pack: Pack) -> str | None:
    """Which of `found`'s set, method and pack digest is not the current one (the first, in
    that order), or None."""
    datasets = pack.manifest.evaluation.datasets
    if found.set_name not in datasets:
        return scoreboard.STALE_SET
    if hashlib.sha256(pack.read_bytes(datasets[found.set_name])).hexdigest() != found.dataset:
        return scoreboard.STALE_SET
    if found.method != method.METHOD_SHA256:
        return scoreboard.STALE_METHOD
    if found.pack_digest != pack.digest:
        return scoreboard.STALE_PACK
    return None


# --------------------------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------------------------


@dataclass
class Check:
    """What `check` found: its `problems` (none: the store is sound), the results it kept
    without checking all of them again and why, and counts of what it read, recomputed and
    re-ran."""

    problems: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    results: int = 0
    current: int = 0
    signed: int = 0
    recomputed: int = 0
    rerun: int = 0

    @property
    def summary(self) -> str:
        return (
            f"{self.results} results ({self.current} current, {self.signed} signed), "
            f"{self.recomputed} recomputed from their inputs, {self.rerun} ledgers re-run"
        )


def check(
    store: Store,
    *,
    base: Mapping[str, bytes],
    publisher_ids: Iterable[str] = (),
    packs: PackSource = maintained_pack,
    today: datetime.date | None = None,
    base_canaries: Mapping[str, bytes] | None = None,
) -> Check:
    """Check the store against `base`: the base commit's result files, by name (their file
    name without `.json`), with their bytes, on `today` (the clock's, by default); and, when
    `base_canaries` is given, its canary files the same way. It needs no secret, and names a
    problem for each of these:

    - a file whose name is not its content's (a result's body SHA-256, a ledger's id, the
      SHA-256 of a run record's bytes, a price table's date), or whose bytes are not in that
      kind's form (the keys file's and the disclosures file's too);
    - a result of `base` removed, or changed in any way but a valid signature added to an
      unsigned result whose body stays the same;
    - a `supersedes` link to no stored result, to one of another pack, set, arm or
      configuration, to one measured later, to one another result supersedes too, or in a
      cycle; more than one current result of one pack, set, arm and configuration;
    - a signature that does not verify against `keys.json`, or that a publisher's or a
      registry's key made (`publisher_ids`: the ones this machine pinned, none in CI), on a
      current result, a result new against `base`, or a result whose signature is new against
      it (a superseded result keeps the signature it had, so a key taken out of the keys file
      leaves it as history);
    - a public result that is not what its stored inputs give, or whose ledger does not re-run
      to the same bytes; or a new one not on its pack's current digest, set and method, measured
      after `today`, or of a remote configuration without 3 to 5 replicates and its pack's
      declaration of it (`_public`);
    - a declaration (`declared/<pack name>.json`) that is not one, not of the pack its name
      names, or that names a configuration the pack does not have, in another arm, or not
      remote (or local) where it says so;
    - a canary of `base_canaries` removed or changed, or a canary that is not what its stored
      ledger, run record and the result it was compared with give, or that is new and measured
      after `today`;
    - a hidden set's result that does not fit its closed schema (`held`), is not of a pack
      Mendari maintains, does not agree with its own figures under the current method or,
      when new, is under another method, measured after `today`, says the method is in effect
      when it is not (or the reverse), or does not grade the `local` configurations of the
      declaration it cites as the store holds it (`_hidden`)."""
    found = Check()
    problems = found.problems
    listed: dict[str, dict[str, bytes]] = {}
    for kind, pattern in NAMES.items():
        listed[kind] = {}
        if (store.root / kind).is_symlink():
            problems.append(f"{kind}/: a symbolic link, never part of the store")
            continue
        for file_name, path in store.files(kind).items():
            if path.is_symlink() or not path.is_file():
                problems.append(f"{kind}/{file_name}: not a file (a link or a folder)")
            elif not pattern.fullmatch(file_name):
                problems.append(f"{kind}/{file_name}: not a name the store gives a file")
            else:
                listed[kind][file_name] = path.read_bytes()
    readers: dict[str, Callable[[bytes, str], object]] = {
        LEDGERS: _ledger,
        RUNS: _run,
        PRICES: _prices,
        DECLARED: _declared,
    }
    for kind, reader in readers.items():
        for file_name, raw in listed[kind].items():
            try:
                reader(raw, file_name)
            except MendariError as error:
                problems.append(str(error))
    results: dict[str, Held] = {}
    now: dict[str, bytes] = {}
    for file_name, raw in listed[RESULTS].items():
        name = file_name.removesuffix(".json")
        now[name] = raw
        try:
            results[name] = _read_result(raw, name)
        except MendariError as error:
            problems.append(f"{RESULTS}/{file_name}: {error}")
    try:
        keys = store.keys()
    except MendariError as error:
        problems.append(f"{KEYS}: {error}")
        keys = {}
    try:
        store.disclosures()
    except MendariError as error:
        problems.append(str(error))
    publishers = frozenset(publisher_ids)
    for name, old in sorted(base.items()):
        problem = _base_problem(old, now.get(name), keys, publishers)
        if problem is not None:
            problems.append(f"{RESULTS}/{name}.json: {problem}")
    problems += _links_problems(results)
    later = superseded(results)
    for name, held_result in sorted(results.items()):
        if not held_result.signed or (name in later and base.get(name) == now[name]):
            continue  # unsigned; or history: a superseded result keeps the signature it had
        signature = signing.problem(held_result.result, keys, publisher_ids=publishers)
        if signature is not None:
            problems.append(f"{RESULTS}/{name}.json: its signature does not verify: {signature}")
    reruns: dict[str, bool] = {}
    day = clock.today() if today is None else today
    for name, held_result in sorted(results.items()):
        new = name not in base
        verdict = _verdict(held_result, store, packs, new=new, reruns=reruns, today=day)
        if verdict.problem is not None:
            problems.append(verdict.problem)
        if verdict.kept is not None:
            found.kept.append(verdict.kept)
        found.recomputed += verdict.recomputed
    for file_name, raw in listed[DECLARED].items():
        problem = _declared_pack_problem(raw, file_name, packs)
        if problem is not None:
            problems.append(f"{DECLARED}/{file_name}: {problem}")
    for name, old in sorted((base_canaries or {}).items()):
        if listed[CANARIES].get(f"{name}.json") != old:
            problems.append(
                f"{CANARIES}/{name}.json: removed or changed: a canary is kept as it was run"
            )
    for file_name, raw in listed[CANARIES].items():
        name = file_name.removesuffix(".json")
        new = base_canaries is not None and name not in base_canaries
        problem = _canary_problem(raw, name, store, results, packs, new=new, today=day)
        if problem is not None:
            problems.append(f"{CANARIES}/{file_name}: {problem}")
    found.results = len(results)
    found.current = len(heads(results))
    found.signed = sum(held_result.signed for held_result in results.values())
    found.rerun = len(reruns)
    return found


def _declared_pack_problem(raw: bytes, file_name: str, packs: PackSource) -> str | None:
    """Why a stored declaration is not one of the pack it names (its configurations, arms and
    where each runs), or None. One that cannot be read is named by the readers above."""
    try:
        found = _declared(raw, file_name)
    except MendariError:
        return None
    try:
        pack = packs(found.pack)
    except MendariError as error:
        return str(error)
    return runs.declaration_problem(found, arms.configurations(pack))


def _canary_problem(
    raw: bytes,
    name: str,
    store: Store,
    results: Mapping[str, Held],
    packs: PackSource,
    *,
    new: bool,
    today: datetime.date,
) -> str | None:
    """Why a stored canary is not what its inputs give (its ledger, its run record, the result
    it was compared with and that result's replicate 1 ledger; its draw, while the pack's `test`
    set is the one it was drawn from), or None."""
    try:
        found = _read_canary(raw, name)
        if new and found["measured_on"] > today.isoformat():
            return f"it is measured on {found['measured_on']}, after today ({today.isoformat()})"
        against = results.get(str(found["against"]["result"]).removeprefix("sha256:"))
        if against is None:
            return "the result it was compared with is not stored"
        try:
            dataset = resolve_dataset(packs(found["pack"]["id"]), scoreboard.CANARY_SET)
        except MendariError:
            dataset = None
        again = scoreboard.canary(
            store.ledger(found["inputs"]["ledger"]),
            store.run(found["inputs"]["run"]),
            against=against.result,
            headline=store.ledger(against.ledger),
            dataset=dataset,
            size=found["draw"]["size"],
            seed=found["draw"]["seed"],
        )
    except MendariError as error:
        return str(error)
    except (KeyError, TypeError, AttributeError, ValueError):
        return "it lacks a field a canary holds"
    if canonical(again) != canonical(found):
        return "it is not what its inputs give (recomputed, a byte differs)"
    return None


def _base_problem(
    old: bytes,
    now: bytes | None,
    keys: Mapping[str, signing.GradingKey],
    publishers: frozenset[str],
) -> str | None:
    """Why a result of the base is not as the store may leave it, or None: there, byte for
    byte, or an unsigned result that gained a valid signature and nothing else."""
    if now is None:
        return "removed: the store is append-only, and a result is never taken out"
    if now == old:
        return None
    changed = "changed: only a valid signature may be added to an unsigned result"
    try:
        before, after = signing.read(old.decode("utf-8")), signing.read(now.decode("utf-8"))
    except (UnicodeDecodeError, MendariError):
        return changed
    if isinstance(before, Mapping) and "signature" in before:
        return "changed after it was signed: a result is signed once, and never changes after"
    if not isinstance(after, Mapping) or "signature" not in after:
        return changed
    if signing.dump(signing.body(after)).encode("utf-8") != old:
        return changed
    signature = signing.problem(after, keys, publisher_ids=publishers)
    if signature is not None:
        return f"a signature was added that does not verify: {signature}"
    return None


def _links_problems(results: Mapping[str, Held]) -> list[str]:
    """What is wrong with the store's `supersedes` links (`check`)."""
    problems = []
    claimed: dict[str, str] = {}
    for name, found in sorted(results.items()):
        problem = _link_problem(found, results)
        if problem is not None:
            problems.append(f"{RESULTS}/{name}.json: {problem}")
        if found.supersedes is None:
            continue
        if found.supersedes in claimed:
            problems.append(
                f"{RESULTS}/{name}.json: it supersedes {found.supersedes}, which "
                f"{claimed[found.supersedes]} supersedes already"
            )
        else:
            claimed[found.supersedes] = name
    for start in sorted(results):
        seen = {start}
        step = results[start].supersedes
        while step is not None and step in results:
            if step in seen:
                problems.append(f"{RESULTS}/{start}.json: its supersedes links form a cycle")
                break
            seen.add(step)
            step = results[step].supersedes
    counted: dict[tuple[str, ...], list[str]] = {}
    for name, found in heads(results).items():
        counted.setdefault(found.identity, []).append(name)
    for names in counted.values():
        if len(names) > 1:
            problems.append(
                f"{len(names)} current results of {results[names[0]].shown} "
                f"({', '.join(names)}): a new result supersedes the current one"
            )
    return problems
