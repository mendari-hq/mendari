# SPDX-License-Identifier: Apache-2.0
"""The per-case ledger of an evaluation run (`mendari.ledger/1`, LP-10): what every figure in its
report is computed from, so the figures can be recomputed byte for byte, compared case by case
between two runs (`mendari compare`), and re-graded. No input text and no case id is in it.

JSON Lines, canonical (sorted keys, no spaces, ASCII, LF), its id the sha256 of its bytes. Line 1
is the header: the pack, the dataset, the case-hash scheme, the questions with their labels in
order, the critical labels, the subgroup attributes, the providers and the policy (a governance
override as whether it allows AUTO and a hash of its reference, never who approved it). Then one
line per case, in the dataset's order:

    {"c": <case hash>, "o": <outcome>, "r": <recommendation or null>,
     "q": {<question>: [truth, predicted, confidence, [probabilities], model, error], ...},
     "k": [<indices of the critical labels this case is a positive of>],
     "u": [cost_usd, input_tokens, output_tokens], "a": {<attribute>: <value>}}

`q` holds a question the case is labelled for or the run answered (or failed): truth or null,
the predicted label or null, the confidence and the probabilities in the header's label order
(rounded to 6 places, as an answer's record is, so the figures computed from them agree on every
platform), the index of the answering model in the header's `models`, and an error code or null.
`a` holds only the pack's declared subgroup attributes, each cut to 64 characters.

The case hash is HMAC-SHA256 of the case's id (as canonical JSON), keyed by a key derived from
the dataset file's own bytes, cut to 128 bits: whoever holds the dataset can match a hash to its
case, and no one else can, as the ids of a small dataset could be guessed from an unkeyed hash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from mendari.core.decision import Decision, DecisionResult
from mendari.errors import MendariError

FORMAT = "mendari.ledger/1"
CASE_HASH = "hmac-sha256-128"
_KEY_DERIVATION = b"mendari-ledger-key-v1"
_CASE_PREFIX = b"mendari-case-v1\x00"
ATTRIBUTE_CHARS = 64
DIGITS = 6


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def case_key(raw: bytes) -> bytes:
    """The key a dataset's case hashes are made with, from the dataset file's bytes."""
    return hmac.new(_KEY_DERIVATION, raw, hashlib.sha256).digest()


def case_hash(key: bytes, case_id: Any) -> str:
    message = _CASE_PREFIX + canonical(case_id).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class Ledger:
    header: dict[str, Any]
    cases: list[dict[str, Any]]

    def dumps(self) -> bytes:
        return "".join(canonical(line) + "\n" for line in [self.header, *self.cases]).encode()

    @property
    def id(self) -> str:
        return "sha256:" + hashlib.sha256(self.dumps()).hexdigest()

    @property
    def file_name(self) -> str:
        return self.id.removeprefix("sha256:") + ".jsonl"

    @classmethod
    def loads(cls, data: bytes, *, where: str = "ledger") -> Ledger:
        """A ledger from its bytes, refused in one line when it is not one."""
        try:
            lines = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise MendariError(f"{where}: not a ledger (JSON Lines)") from None
        if not lines or not isinstance(lines[0], dict) or lines[0].get("format") != FORMAT:
            raise MendariError(f"{where}: not a {FORMAT} ledger")
        header, cases = lines[0], lines[1:]
        if not all(isinstance(case, dict) and isinstance(case.get("c"), str) for case in cases):
            raise MendariError(f"{where}: a case line is not a ledger case")
        if header.get("cases", {}).get("n") != len(cases):
            raise MendariError(f"{where}: its header and its cases disagree on how many there are")
        return cls(header, cases)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, DIGITS)


def build(
    decision: Decision,
    dataset: Any,
    results: Sequence[DecisionResult],
) -> Ledger:
    """The ledger of one run: `results` in the order of `dataset.records`."""
    pack = decision.pack
    manifest = pack.manifest
    questions = {question: list(manifest.labels(question)) for question in manifest.questions}
    critical = [(item.question, item.label) for item in manifest.evaluation.critical]
    attributes = list(manifest.evaluation.subgroups)
    models: list[str] = []
    seen: set[str] = set()
    cases = []
    for record, result in zip(dataset.records, results, strict=True):
        hashed = case_hash(dataset.case_key, record["id"])
        if hashed in seen:
            raise MendariError(f"{dataset.name}: two cases have the same id")
        seen.add(hashed)
        labels = record.get("labels", {})
        answered: dict[str, list[Any]] = {}
        for question, order in questions.items():
            answer = result.answers.get(question)
            error = result.errors.get(question)
            truth = labels.get(question)
            if answer is None and error is None and truth is None:
                continue
            model = None
            if answer is not None and answer.provider.model:
                if answer.provider.model not in models:
                    models.append(answer.provider.model)
                model = models.index(answer.provider.model)
            answered[question] = [
                truth,
                answer.label if answer else None,
                _rounded(answer.confidence) if answer else None,
                [_rounded(answer.probabilities.get(label, 0.0)) for label in order]
                if answer
                else None,
                model,
                error,
            ]
        values = record.get("attributes", {})
        cases.append(
            {
                "c": hashed,
                "o": result.outcome.value,
                "r": result.recommendation,
                "q": answered,
                "k": [i for i, (q, label) in enumerate(critical) if labels.get(q) == label],
                "u": [result.cost_usd, result.input_tokens, result.output_tokens],
                "a": {
                    name: str(values.get(name, "unknown"))[:ATTRIBUTE_CHARS] for name in attributes
                },
            }
        )
    governance = decision.governance
    header = {
        "format": FORMAT,
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "dataset": {
            "name": dataset.name,
            "sha256": dataset.sha256,
            "n": len(dataset.records),
            "limit": dataset.limit,
        },
        "cases": {"hash": CASE_HASH, "key": "dataset/v1", "n": len(cases)},
        "primary": manifest.policy.primary,
        "questions": questions,
        "critical": [list(item) for item in critical],
        "subgroups": attributes,
        "providers": {
            slot: {"variant": info.variant, "type": info.type, "model": info.model}
            for slot, info in decision.provider_info.items()
        },
        "policy": {
            "risk_level": int(manifest.risk.level),
            "thresholds": manifest.policy.thresholds.model_dump(),
            "governance": None
            if governance is None
            else {
                "allow_auto": governance.allow_auto,
                "reference_sha256": hashlib.sha256(governance.reference.encode()).hexdigest(),
            },
        },
        "models": models,
    }
    return Ledger(header, cases)
