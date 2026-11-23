# SPDX-License-Identifier: Apache-2.0
"""Publisher evaluations, signed and checked (ADR-0020).

A publisher publishes an evaluation of one listed version, one per configuration and public
dataset, as a `mendari.publisher-evaluation/1` record it signs with one of its namespace's keys
(the keys of `mendari keys register`) and sends with the namespace's publisher token
(`mendari eval AGENT --dataset NAME --publish --key publisher.key`):

    {"format": "mendari.publisher-evaluation/1",
     "pack": {"id": "mendari/portal-message-agent", "version": "0.1.2", "digest": "sha256:…"},
     "configuration": {"reader": "rules"},          # a variant for every slot
     "dataset": {"name": "test", "sha256": "<64 hex>", "n": 600},
     "report": {...mendari.site-report/2 counts...},
     "claimed": {"latency_ms_p50": 1.2, "latency_ms_p95": 3.4, "cost_usd_per_decision": null},
     "models": ["mock-portal-agent"],
     "created_at": "2026-11-22T12:00:00Z", "key_id": "ed25519:<16 hex>",
     "signature": "<base64>"}

Counts in, rates out: `report` is the run's counts (`evals/share.py` `counted_report`), whole,
never suppressed (a public dataset holds no patient, so nothing in it is a small cell to hide)
and never a rate: the registry computes every figure it shows (`figures`). `remote_consent`
says whether the configuration calls a provider off the machine; a public set may go to one.
`claimed` (latency, cost) and `models` are the publisher's own word, shown labelled so.

The signature is Ed25519 over `PREFIX`, the format and a zero byte, then the canonical JSON of
every field but the signature (sorted keys, no spaces, ASCII). No other Mendari signature is over
bytes that start so: an archive's manifest, a yank and the registry's records sign bare JSON, a
site and the grading key their own prefixes (`hub/site_signing.py`, `evals/signing.py`).

The registry (`EvaluationStore.publish_evaluation`) checks, in order, and refuses naming the
field, never echoing a value: the record's shape; its time (within `MAX_SKEW_S`); that it is this
route's agent and version; the key (one of the namespace's, not revoked) and the signature;
that the version is listed (not recalled or deleted); the digest; the configuration (every slot,
a variant the version declares, and the report's providers); the dataset (one the manifest
declares, in the signed archive, with that SHA-256 and n); the counts (whole, adding up, the
agent's own policy, consent as the configuration calls). A configuration whose every provider is
local is then run again in the regression check's isolated child (`hub/regression.py` `rerun`),
and its counts must be the publisher's, exactly; with re-runs off (`MENDARI_REGISTRY_REGRESSION
=off`) such a configuration is refused, never taken unchecked. One that calls a model (an agent,
Jev, an LLM; an agent answered offline from its `mock_file` too: the registry goes by the
provider's type) is not re-run, and says so.

One live evaluation per version, configuration and dataset: a later one supersedes it (both stay
stored and chained, `evaluation.published`, `evaluation.superseded`). The same signed record sent
again is idempotent while it is live (nothing written, nothing chained) and refused once
superseded; a record signed before the live one is refused. Its id is the SHA-256 of the
record's canonical JSON.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mendari.core.origin import is_local
from mendari.errors import HubError, MendariError
from mendari.evals import figures as served_figures
from mendari.evals.result import MIN_POSITIVES
from mendari.evals.share import (
    MIN_DATASET,
    Name,
    SharedPack,
    SiteReportV2,
    adds_up,
    counted_report,
)
from mendari.evals.share import where as share_where
from mendari.hub import archive

if TYPE_CHECKING:
    from mendari.core.db import Backend
    from mendari.core.pack import Pack
    from mendari.evals.harness import Evaluation

FORMAT = "mendari.publisher-evaluation/1"
LISTING_FORMAT = "mendari.publisher-evaluations/1"
PREFIX = b"mendari-publisher-evaluation-v1\x00"
MAX_SKEW_S = 600  # as a recall's (`yank.MAX_SKEW_S`): sign it again
MAX_BYTES = 256_000  # a record of the demo agent is about 4 KB
MAX_MODELS = 16
MATCHED = "matched"
CALLS_A_MODEL = "calls_a_model"
RERUN_WORDS = {
    MATCHED: "re-run by the registry: matched",
    CALLS_A_MODEL: "signed by the publisher; not re-run: calls a model",
}
REVOKED = "key revoked"
NOTE = (
    "The publisher's own evaluations, on its own public dataset: a configuration can be tuned "
    "to it. Site evidence and hidden sets are stronger evidence. Not a ranking."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_evaluations (
    id            TEXT PRIMARY KEY,
    namespace     TEXT NOT NULL,
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,
    configuration TEXT NOT NULL,
    dataset       TEXT NOT NULL,
    status        TEXT NOT NULL,
    rerun         TEXT NOT NULL,
    key_id        TEXT NOT NULL,
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_key_revocations (
    key_id      TEXT PRIMARY KEY,
    namespace   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    revoked_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

_MODEL_NAME = r"^[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,127}$"
_KEY_ID = r"^ed25519:[0-9a-f]{16}$"
_TIME = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
Number = Annotated[float, Field(ge=0.0, le=1e9, allow_inf_nan=False)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EvaluatedDataset(_Closed):
    name: Name
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    n: Annotated[int, Field(ge=MIN_DATASET, le=10**9)]


class Claimed(_Closed):
    """What the publisher says of its run that is not a count: shown as its own figures."""

    latency_ms_p50: Number | None = None
    latency_ms_p95: Number | None = None
    cost_usd_per_decision: Number | None = None


class PublisherEvaluation(_Closed):
    format: Literal["mendari.publisher-evaluation/1"]
    pack: SharedPack
    configuration: Annotated[dict[Name, Name], Field(min_length=1, max_length=64)]
    dataset: EvaluatedDataset
    report: SiteReportV2
    claimed: Claimed
    models: Annotated[
        list[Annotated[str, Field(pattern=_MODEL_NAME)]], Field(max_length=MAX_MODELS)
    ]
    created_at: Annotated[str, Field(pattern=_TIME)]
    key_id: Annotated[str, Field(pattern=_KEY_ID)]
    signature: Annotated[str, Field(min_length=1, max_length=200)]


_FIELDS = frozenset(PublisherEvaluation.model_fields) | {
    name for model in (EvaluatedDataset, Claimed) for name in model.model_fields
}


def _where(loc: tuple[Any, ...] | list[Any]) -> str:
    """A validation error's place, without the data in it (`share.where` for the report's)."""
    if not loc:
        return "body"
    head = str(loc[0]) if loc[0] in _FIELDS else "<key>"
    if loc[0] == "report":
        rest = share_where(loc[1:])
        return f"report.{rest}" if rest else "report"
    if loc[0] in ("dataset", "claimed") and len(loc) > 1 and loc[1] in _FIELDS:
        return f"{head}.{loc[1]}"
    return head


# -- signing -------------------------------------------------------------------------------------


def canonical(fields: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def signed_bytes(record: Mapping[str, Any]) -> bytes:
    """What a publisher's signature is over: the prefix, the format, a zero byte, and the
    canonical JSON of every field but the signature."""
    body = {key: value for key, value in record.items() if key != "signature"}
    return PREFIX + str(body.get("format", "")).encode() + b"\x00" + canonical(body)


def evaluation_id(record: Mapping[str, Any]) -> str:
    """The id of a signed record: the SHA-256 of its canonical JSON, signature included."""
    return hashlib.sha256(canonical(record)).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sign(fields: Mapping[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    """`fields` (every field but `key_id` and `signature`) signed with a namespace key."""
    record = {**fields, "key_id": archive.key_id(key.public_key())}
    signature = base64.b64encode(key.sign(signed_bytes(record))).decode("ascii")
    return {**record, "signature": signature}


def signature_holds(record: Mapping[str, Any], public: Ed25519PublicKey) -> bool:
    try:
        signature = base64.b64decode(str(record.get("signature", "")), validate=True)
        public.verify(signature, signed_bytes(record))
    except (binascii.Error, ValueError, InvalidSignature):
        return False
    return archive.key_id(public) == record.get("key_id")


def calls_a_model(pack: Pack, configuration: Mapping[str, str]) -> bool:
    """Whether a configuration of the pack runs any provider that is not local (a local type,
    or a composite of those): the registry's rule, by the providers' types alone."""
    for slot, variant in configuration.items():
        spec = pack.manifest.providers[slot].variants[variant]
        if not is_local({"type": spec.type, **(spec.model_extra or {})}):
            return True
    return False


def build(
    pack: Pack, run: Evaluation, *, key: Ed25519PrivateKey, at: str | None = None
) -> dict[str, Any]:
    """The signed record of one run of `pack` (`mendari eval --publish`): its counts from the
    run's ledger, its configuration, its dataset by name, SHA-256 and size, the models that
    answered, and the run's latency and cost as the publisher's own figures."""
    header = run.ledger.header
    configuration = {slot: str(p["variant"]) for slot, p in header["providers"].items()}
    dataset = header["dataset"]
    if dataset.get("limit") is not None:
        raise MendariError("a published evaluation covers the whole dataset: drop --limit")
    report = run.report
    latency = (report.get("run") or {}).get("latency_ms") or {}
    cost = (report.get("cost") or {}).get("per_decision_usd")
    fields = {
        "format": FORMAT,
        "pack": dict(header["pack"]),
        "configuration": configuration,
        "dataset": {"name": dataset["name"], "sha256": dataset["sha256"], "n": dataset["n"]},
        "report": counted_report(run.ledger, remote_consent=calls_a_model(pack, configuration)),
        "claimed": {
            "latency_ms_p50": _number(latency.get("p50")),
            "latency_ms_p95": _number(latency.get("p95")),
            "cost_usd_per_decision": _number(cost),
        },
        "models": [str(m) for m in header.get("models") or []][:MAX_MODELS],
        "created_at": at or now(),
    }
    record = sign(fields, key)
    parse(record)  # what the registry would refuse, refused here first
    return record


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(float(value), 3)


def parse(record: Any) -> PublisherEvaluation:
    """The record as the closed model, or a `HubError` naming its fields (never their values)."""
    if not isinstance(record, Mapping) or record.get("format") != FORMAT:
        raise HubError(f"not a {FORMAT} record: format")
    try:
        return PublisherEvaluation.model_validate(dict(record))
    except ValidationError as error:
        fields = sorted({_where(e["loc"]) for e in error.errors()})
        shown = ", ".join(fields[:5]) + (f" and {len(fields) - 5} more" if len(fields) > 5 else "")
        raise HubError(f"not a {FORMAT} record: {shown}") from None


def verify(record: Mapping[str, Any], public: Ed25519PublicKey) -> PublisherEvaluation:
    """The record, parsed, once its signature holds with `public` (a `HubError` otherwise)."""
    parsed = parse(record)
    if not signature_holds(record, public):
        raise HubError("signature: it does not hold with the key named", 403)
    return parsed


def skewed(created_at: str, *, now_: datetime | None = None) -> bool:
    when = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    current = now_ or datetime.now(UTC)
    return abs((current - when).total_seconds()) > MAX_SKEW_S


# -- figures -------------------------------------------------------------------------------------


def figures(report: Mapping[str, Any]) -> dict[str, Any]:
    """What the Hub shows of one evaluation, computed here from its counts (k of n, each with its
    exact 95% interval): each critical label's detection and its automated misses, then the
    primary question's accuracy and false automation, then the outcomes."""
    of = served_figures.of
    n = report["dataset"]["n"]
    primary = report["questions"].get(report["primary"])
    decisions = report["decisions"]
    return {
        "format": served_figures.FORMAT,
        "n": n,
        "primary": report["primary"],
        "critical": [
            {
                "question": item["question"],
                "label": item["label"],
                "detected": of(item["detected"], item["n"]),
                "automated_misses": item["auto_missed"],
                "automated": of(item["auto_missed"], item["n"]),
                "insufficient_evidence": item["n"] < MIN_POSITIVES,
            }
            for item in decisions["critical"]
        ],
        "accuracy": None if primary is None else of(primary["right"], primary["n"]),
        "false_automation": of(decisions["false_automation"], primary["n"] if primary else 0),
        "outcomes": {
            key: {"count": count, "share": served_figures.maybe(count, n)}
            for key, count in decisions["outcomes"].items()
        },
    }


# -- the registry's store ------------------------------------------------------------------------


def _canonical_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class EvaluationStore:
    """The publisher-evaluation methods of `RegistryStore` (a mixin: the store's `_db`)."""

    _db: Backend

    def revoked_keys(self, namespace: str | None = None) -> set[str]:
        """The namespace keys revoked (`RegistryStore.revoke_key`), of one namespace or all."""
        with self._db.session(exclusive=False) as db:
            if not db.columns("hub_key_revocations"):
                return set()
            if namespace is None:
                rows = db.all("SELECT key_id FROM hub_key_revocations")
            else:
                rows = db.all(
                    "SELECT key_id FROM hub_key_revocations WHERE namespace = ?", (namespace,)
                )
        return {str(row["key_id"]) for row in rows}

    def publish_evaluation(
        self, namespace: str, name: str, version: str, record: Any, *, by: str
    ) -> dict[str, Any]:
        """Check a signed evaluation and store it: see the module's docstring for the order.
        Chained `evaluation.published` (and `evaluation.superseded` for the one it replaces)."""
        from mendari.hub import regression
        from mendari.hub.registry import REVIEWER, _unknown_names, record_event
        from mendari.hub.sites import _recall_free, configuration_problem, wholly_local

        evaluation = parse(record)
        if skewed(evaluation.created_at):
            raise HubError("created_at: it is not now: sign it again")
        if (evaluation.pack.id, evaluation.pack.version) != (f"{namespace}/{name}", version):
            raise HubError("pack: it is not this agent and version")
        keys = {
            str(k["key_id"]): str(k["public_pem"])
            for k in self.namespace(namespace, REVIEWER)["keys"]  # type: ignore[attr-defined]
        }
        if evaluation.key_id not in keys:
            raise HubError("key_id: it is not a key of this namespace", 403)
        if evaluation.key_id in self.revoked_keys(namespace):
            raise HubError("key_id: that key is revoked", 403)
        verify(record, archive.load_public_key(keys[evaluation.key_id].encode()))
        found = self.version(namespace, name, version, REVIEWER)  # type: ignore[attr-defined]
        if found["yank"] is not None or found["deleted"] is not None or not found["listed"]:
            raise HubError("version: it is recalled, deleted or not listed", 409)
        if evaluation.pack.digest != found["digest"]:
            raise HubError("pack.digest: it is not this version's digest", 409)
        evaluation_key = evaluation_id(record)
        known = self._evaluation_row(evaluation_key)
        if known is not None:  # the same signed record again: nothing written, nothing chained
            if known["status"] == "live":
                return self._answer(known)
            raise HubError("that evaluation was superseded: sign a new one", 409)
        names = found.get("names")
        if not isinstance(names, dict):
            raise HubError(
                "version: it was published before evaluations were checked against their "
                "agent; publish a new version",
                409,
            )
        configuration = dict(evaluation.configuration)
        problem = configuration_problem(configuration, names)
        if problem:
            raise HubError(f"configuration: it {problem}")
        report = evaluation.report
        if {slot: p.variant for slot, p in report.providers.items()} != configuration:
            raise HubError("report.providers: they are not the configuration")
        if report.pack != evaluation.pack:
            raise HubError("report.pack: it is not the evaluation's agent")
        unknown = _unknown_names(report, names, found.get("primary"))
        if unknown:
            raise HubError(f"report: it names {unknown} the agent does not have")
        if report.dataset.n != evaluation.dataset.n:
            raise HubError("report.dataset.n: it is not the dataset's n")
        shared = report.model_dump(mode="json")
        if _has_null(shared):
            raise HubError("report: a public dataset's counts are whole (no null cell)")
        if not adds_up(report):
            raise HubError("report: its counts do not add up to the totals they state")
        if report.policy.governance_override:
            raise HubError("report.policy: an evaluation measures the agent's own policy")
        local = wholly_local(configuration, names)
        if report.remote_consent == local:
            raise HubError(
                "report.remote_consent: it is not whether the configuration calls a model"
            )
        if local and not regression.enabled():
            raise HubError(
                "configuration: this registry does not re-run evaluations, so it takes no "
                "evaluation of a configuration it could check (only one that calls a model)",
                409,
            )

        with tempfile.TemporaryDirectory() as scratch:
            pack_dir = Path(scratch) / "pack"
            self._archived(namespace, found, pack_dir)
            from mendari.core.pack import load_pack
            from mendari.evals.harness import resolve_dataset

            try:
                pack = load_pack(pack_dir, origin="hub")
                if evaluation.dataset.name not in pack.manifest.evaluation.datasets:
                    raise HubError(
                        "dataset.name: it is not a dataset the agent's manifest declares"
                    )
                dataset = resolve_dataset(pack, evaluation.dataset.name)
            except HubError:
                raise
            except Exception as error:  # the publisher's content: named, never a 500
                raise HubError(f"dataset: it does not load ({type(error).__name__})") from None
            if dataset.sha256 != evaluation.dataset.sha256:
                raise HubError("dataset.sha256: it is not the archived dataset's")
            if len(dataset.records) != evaluation.dataset.n:
                raise HubError("dataset.n: it is not the archived dataset's")
            rerun = CALLS_A_MODEL
            if local:
                with regression.slot():  # at most a few at once; else 503, try again
                    measured = regression.rerun(pack_dir, configuration, evaluation.dataset.name)
                if measured.get("status") != "measured":
                    raise HubError(
                        "the registry's re-run did not finish: the evaluation is not taken", 409
                    )
                if measured.get("counts") != shared:
                    raise HubError("report: the registry's re-run counted otherwise: refused", 409)
                rerun = MATCHED

        body = json.dumps(dict(record), sort_keys=True, separators=(",", ":"))
        configuration_text = _canonical_text(configuration)
        subject = f"{namespace}/{name}@{version}"
        with self._db.session(exclusive=True) as db:
            if not _recall_free(db, namespace, name, version):
                raise HubError("version: it is recalled, deleted or not listed", 409)
            if db.one("SELECT id FROM hub_evaluations WHERE id = ?", (evaluation_key,)):
                raise HubError("that evaluation was sent at the same time: send it again", 409)
            live = db.one(
                "SELECT * FROM hub_evaluations WHERE namespace = ? AND name = ? AND version = ? "
                "AND configuration = ? AND dataset = ? AND status = 'live'",
                (namespace, name, version, configuration_text, evaluation.dataset.name),
            )
            if live is not None and str(live["created_at"]) >= evaluation.created_at:
                raise HubError(
                    "created_at: an evaluation of this configuration and dataset signed later "
                    "is live",
                    409,
                )
            db.all(
                "INSERT INTO hub_evaluations (id, namespace, name, version, configuration, "
                "dataset, status, rerun, key_id, body, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'live', ?, ?, ?, ?)",
                (
                    evaluation_key,
                    namespace,
                    name,
                    version,
                    configuration_text,
                    evaluation.dataset.name,
                    rerun,
                    evaluation.key_id,
                    body,
                    evaluation.created_at,
                ),
            )
            record_event(
                db,
                "evaluation.published",
                subject,
                {
                    "evaluation_id": evaluation_key,
                    "pack": f"{namespace}/{name}",
                    "version": version,
                    "digest": evaluation.pack.digest,
                    "configuration": configuration,
                    "dataset": evaluation.dataset.name,
                    "dataset_sha256": evaluation.dataset.sha256,
                    "key_id": evaluation.key_id,
                    "rerun": rerun,
                    "supersedes": None if live is None else live["id"],
                    "by": by,
                },
                row=("evaluations", "id = ?", (evaluation_key,)),
            )
            if live is not None:
                db.all(
                    "UPDATE hub_evaluations SET status = 'superseded' WHERE id = ?",
                    (live["id"],),
                )
                record_event(
                    db,
                    "evaluation.superseded",
                    subject,
                    {
                        "evaluation_id": live["id"],
                        "superseded_by": evaluation_key,
                        "by": by,
                    },
                    row=("evaluations", "id = ?", (live["id"],)),
                )
            written = db.one("SELECT * FROM hub_evaluations WHERE id = ?", (evaluation_key,))
        assert written is not None
        return self._answer(written)

    def _evaluation_row(self, evaluation_key: str) -> dict[str, Any] | None:
        with self._db.session(exclusive=False) as db:
            if not db.columns("hub_evaluations"):
                return None
            return db.one("SELECT * FROM hub_evaluations WHERE id = ?", (evaluation_key,))

    def _archived(self, namespace: str, found: Mapping[str, Any], dest: Path) -> None:
        """The version's signed archive, verified with its namespace's keys (revoked ones too:
        it was accepted with one) and extracted to `dest`."""
        with self._db.session(exclusive=False) as db:
            held = db.one(
                "SELECT data FROM hub_archives WHERE archive_sha256 = ?",
                (found["archive_sha256"],),
            )
            listed = db.all(
                "SELECT key_id, public_pem FROM hub_keys WHERE namespace = ?", (namespace,)
            )
        if held is None:
            raise HubError("version: its archive is missing", 409)
        keys = {
            str(k["key_id"]): archive.load_public_key(str(k["public_pem"]).encode()) for k in listed
        }
        verified = archive.verify(bytes(held["data"]), keys)
        try:
            archive.extract(verified, dest, origin="hub")
        except HubError:
            raise
        except MendariError as error:
            raise HubError(f"version: its archive does not load ({error})") from None

    @staticmethod
    def _answer(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "pack": f"{row['namespace']}/{row['name']}",
            "version": row["version"],
            "configuration": json.loads(row["configuration"]),
            "dataset": row["dataset"],
            "status": row["status"],
            "rerun": row["rerun"],
            "rerun_words": RERUN_WORDS.get(str(row["rerun"]), str(row["rerun"])),
            "key_id": row["key_id"],
        }

    def evaluations(self, namespace: str, name: str, version: str) -> dict[str, Any]:
        """A version's live evaluations, with the figures worked out here from their counts.
        One signed with a key revoked since is listed and left out of the comparison
        (`excluded`). Ordered by configuration and dataset: never by a figure."""
        with self._db.session(exclusive=False) as db:
            rows = []
            if db.columns("hub_evaluations"):
                rows = db.all(
                    "SELECT * FROM hub_evaluations WHERE namespace = ? AND name = ? AND "
                    "version = ? AND status = 'live' ORDER BY configuration, dataset, id",
                    (namespace, name, version),
                )
                superseded = db.one(
                    "SELECT COUNT(*) AS n FROM hub_evaluations WHERE namespace = ? AND name = ? "
                    "AND version = ? AND status = 'superseded'",
                    (namespace, name, version),
                )
            else:
                superseded = None
        revoked = self.revoked_keys(namespace)
        listed = []
        for row in rows:
            record = json.loads(row["body"])
            listed.append(
                {
                    **self._answer(row),
                    "dataset": {
                        "name": record["dataset"]["name"],
                        "n": record["dataset"]["n"],
                        "sha256": record["dataset"]["sha256"],
                    },
                    "models": list(record.get("models") or []),
                    "claimed": dict(record.get("claimed") or {}),
                    "created_at": row["created_at"],
                    "excluded": REVOKED if str(row["key_id"]) in revoked else None,
                    "figures": _figures_of(record),
                }
            )
        return {
            "format": LISTING_FORMAT,
            "pack": f"{namespace}/{name}",
            "version": version,
            "note": NOTE,
            "evaluations": listed,
            "superseded": int(superseded["n"]) if superseded else 0,
        }


def _figures_of(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """An evaluation's figures, or null when what it stores is malformed (never a failed page)."""
    shown: dict[str, Any] | None = served_figures.guarded(lambda: figures(record["report"]))
    return shown


def _has_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, Mapping):
        return any(_has_null(v) for k, v in value.items() if k != "model")
    if isinstance(value, list):
        return any(_has_null(v) for v in value)
    return False
