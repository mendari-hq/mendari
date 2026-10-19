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
method changed is kept as it is. A new result must be of a local configuration (a remote one's
ledger could not be re-run without a call off the machine), on its pack's current digest, set
and method, and measured no later than today, so that all of it is checked. The packs are the
ones Mendari maintains, read from the bundled packs alone (`maintained_pack`: the repository's
`packs/` in a checkout), never from `MENDARI_PACKS_PATH` or the registry cache.

`measure` runs one local configuration of a maintained pack over one of its public scoring sets
(every set it declares but its fitting sets, `train` and `calibration`), and gives its ledger,
its run record (each case's latency and tokens, from its `DecisionResult`) and its entry. It
refuses a remote configuration, a fitting set and anything but a set the pack names, so no file
and no hidden set: nothing here calls off the machine. Nothing here signs: results are stored
unsigned, and the owner signs them offline (`mendari scoreboard sign`). Messages name files,
hashes, packs, sets and configurations, never a case.
"""

from __future__ import annotations

import datetime
import hashlib
import re
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
from mendari.errors import MendariError, PackNotFoundError
from mendari.evals import arms, method, runs, scoreboard, signing
from mendari.evals import ledger as ledgers
from mendari.evals.harness import Dataset, record_problems, resolve_dataset
from mendari.evals.ledger import Ledger, canonical

ROOT = "scoreboard"  # from the repository's root
RESULTS, LEDGERS, RUNS, PRICES = "results", "ledgers", "runs", "prices"
KEYS, DISCLOSURES = "keys.json", "disclosures.json"
# The formats the store holds. A hidden set's answer (`mendari.hidden_scoreboard/1`) joins with
# LP-17k, with its closed schema.
FORMATS = (scoreboard.FORMAT,)
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
    """The name a result is stored under: its body's SHA-256 (`scoreboard.body_sha256`), in hex."""
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

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        """One pack, set, arm and configuration: what a later result of it supersedes."""
        return (self.pack_id, self.set_kind, self.set_name, self.arm, written(self.providers))

    @property
    def signed(self) -> bool:
        return "signature" in self.result

    @property
    def shown(self) -> str:
        """The pack, set and configuration, for a message."""
        return f"{self.pack_id} {self.set_name} {written(self.providers)}"


def held(result: Any, *, name: str | None = None) -> Held:
    """`result` as the store reads it, or a `MendariError` naming what it lacks: a format the
    store holds, a body that is canonical JSON, a `body_sha256` that is its body's, and the
    fields of its scope, configuration and inputs the store reads. `name`, when given, must be
    the name it is stored under."""
    if not isinstance(result, Mapping) or result.get("format") not in FORMATS:
        raise MendariError(f"not a result the store holds ({' or '.join(FORMATS)})")
    result = dict(result)
    signing.body_bytes(result)  # canonical JSON, or a MendariError
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
    hashes = (values.dataset, values.method, values.prices or "0" * 64)
    if not all(isinstance(text, str) and _HEX.fullmatch(text) for text in hashes):
        raise MendariError("its dataset, method and price table are named by 64 hex digits")
    if supersedes is not None and not (isinstance(supersedes, str) and _BODY.fullmatch(supersedes)):
        raise MendariError("supersedes names a result by its body's SHA-256 (sha256:<64 hex>)")
    return replace(values, supersedes=None if supersedes is None else supersedes[len("sha256:") :])


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
        does not verify against `keys.json`; and a public result that is not of a local
        configuration, on its pack's current digest, set and method, measured no later than
        `today` (the clock's, by default), and what its stored inputs give, with a ledger that
        re-runs to the same bytes (`check`'s rules for a new result)."""
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
            problem = _public(found, self, packs, new=True, reruns={}, today=day).problem
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
    """One run of one configuration over one set: its ledger, its run record and its entry."""

    ledger: Ledger
    run: dict[str, Any]
    entry: dict[str, Any]

    @property
    def name(self) -> str:
        return name_of(self.entry)


def refuse_unmeasurable(pack: Pack, set_name: str, configuration: arms.Configuration) -> None:
    """A `MendariError` unless `pack` is a pack Mendari maintains, `set_name` one of its public
    scoring sets and `configuration` local: the store's results are the reference packs', on
    sets in the repository, decided on this machine."""
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
    if not configuration.local:
        raise MendariError(
            f"{written(configuration.providers)} calls {', '.join(configuration.remote_types)} "
            "off this machine: only local configurations are run here"
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
) -> Measured:
    """Run `configuration` of `pack` over its public scoring set `set_name` (refused unless
    `refuse_unmeasurable` allows it), and give its ledger, its run record (dated `date`) and its
    entry, unsigned, which names `supersedes` (a stored result's name) when given."""
    refuse_unmeasurable(pack, set_name, configuration)
    dataset = resolve_dataset(pack, set_name)
    decision, results, ledger = _decided(pack, dataset, configuration)
    record = runs.record(decision, ledger, results, date=date)
    found = scoreboard.entry(
        ledger,
        dataset,
        record,
        pack=scoreboard.PackScope.of(pack),
        arm=configuration.arm,
        configuration=configuration,
        prices=None,
        disclosures=None,
        supersedes=None if supersedes is None else f"sha256:{supersedes}",
    )
    return Measured(ledger, record, found)


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
    method, and not past the day it goes stale (`STALE_AFTER_DAYS` after it was measured)."""
    stale = datetime.date.fromisoformat(scoreboard.stale_after(found.result))
    return (
        found.pack_digest == pack.digest
        and found.dataset == set_sha256
        and found.method == method.METHOD_SHA256
        and today <= stale
    )


def record_run(
    store: Store,
    pack: Pack,
    set_name: str,
    configuration: arms.Configuration,
    *,
    date: datetime.date,
    again: bool = False,
) -> Outcome:
    """Run `configuration` of `pack` over `set_name`, and store its ledger, its run record and
    its entry; unless its current result is current on `date` (`is_current`) and not `again`,
    which is kept. A new result supersedes the current one, if any."""
    refuse_unmeasurable(pack, set_name, configuration)
    stored = store.results()
    providers = written(configuration.providers)
    identity = (pack.id, scoreboard.PUBLIC, set_name, configuration.arm, providers)
    shown = f"{pack.id} {set_name} {providers}"
    current = _current(identity, stored)
    if current is not None and not again:
        dataset = resolve_dataset(pack, set_name)
        if is_current(stored[current], pack, dataset.sha256, date):
            return Outcome(KEPT, current, shown, stored[current].measured_on)
    measured = measure(pack, set_name, configuration, date=date, supersedes=current)
    inputs = [
        store.root / LEDGERS / measured.ledger.file_name,
        store.root / RUNS / (runs.run_sha256(measured.run).removeprefix("sha256:") + ".json"),
    ]
    made = [path for path in inputs if not path.exists()]
    store.put_ledger(measured.ledger)
    store.put_run(measured.run)
    try:
        name = store.add(measured.entry, packs=lambda _: pack)
    except MendariError:
        for path in made:  # no input is left behind without its result
            path.unlink(missing_ok=True)
        raise
    return Outcome(STORED, name, shown, date.isoformat(), supersedes=current)


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
    were the store's when it was added."""
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
    """Whether a public result is what its stored inputs give. A `new` one must be of a local
    configuration, on its pack's current digest, set and method, and measured no later than
    `today`, so that it is recomputed and its ledger re-run, and a later run can supersede it.
    One stored before is recomputed while its set and method are current, and its ledger re-run
    while its digest is too and it is local; otherwise it is kept. Its inputs must be in the
    store either way. `reruns` holds each ledger re-run already, by id: two results that share a
    ledger run it once."""
    where = f"{RESULTS}/{found.name}.json ({found.shown})"
    if found.set_kind != scoreboard.PUBLIC:
        return Verdict(f"{where}: not a public set's result")
    if new and found.measured_on > today.isoformat():
        return Verdict(
            f"{where}: it is measured on {found.measured_on}, after today ({today.isoformat()}): "
            "a result is dated the day it is run"
        )
    if new and not found.local:
        return Verdict(
            f"{where}: a remote configuration's result: its ledger could not be re-run without "
            "a call off this machine, so only a local configuration's result is stored"
        )
    try:
        store.ledger(found.ledger)
        store.run(found.run)
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
) -> Check:
    """Check the store against `base`: the base commit's result files, by name (their file
    name without `.json`), with their bytes, on `today` (the clock's, by default). It needs no
    secret, and names a problem for each of these:

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
      to the same bytes; or a new one of a remote configuration, not on its pack's current
      digest, set and method, or measured after `today` (`_public`)."""
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
        verdict = _public(held_result, store, packs, new=new, reruns=reruns, today=day)
        if verdict.problem is not None:
            problems.append(verdict.problem)
        if verdict.kept is not None:
            found.kept.append(verdict.kept)
        found.recomputed += verdict.recomputed
    found.results = len(results)
    found.current = len(heads(results))
    found.signed = sum(held_result.signed for held_result in results.values())
    found.rerun = len(reruns)
    return found


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
