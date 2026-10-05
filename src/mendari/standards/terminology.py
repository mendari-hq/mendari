"""Mendari's own code systems and identifier systems as FHIR R4 CodeSystem and NamingSystem
resources, generated from the code and the packs a deployment serves, so they cannot drift from
what Mendari emits. The server serves them and CI loads them into the HL7 validator
(`scripts/fhir_terminology.py`), so a code Mendari emits that its CodeSystem lacks is an error.

| CodeSystem | Codes | In |
|---|---|---|
| `…/fhir/CodeSystem/decision-outcome` | `Outcome` | Task.businessStatus |
| `…/fhir/CodeSystem/audit-event` | events chained on a decision | AuditEvent.subtype |
| `…/fhir/CodeSystem/decision-pack` | `id@version` of the packs served | Task.meta.tag |
| a pack's `task.performer_system` | what `FhirAdapter.to_task` routes to | Task.performerType |
| `…/packs/{pack}/CodeSystem/{question}` | labels, defined by their criteria | Da Vinci DTR |

The decision-pack system is a `fragment`: decisions made by packs no longer served keep their
codes. Packs that share a performer system share one CodeSystem, with the union of their codes;
a community pack never shares one with another pack (its systems live under its own url,
`core/origin.py`). A community pack's systems are published by its publisher, experimental, and
say "Community · not clinically reviewed".
The NamingSystems are for decision ids, input hashes, and pack versions acting as the engine.

**Versions.** One `url|version` must never name two contents, in any deployment. The fixed
systems carry the Mendari version (a test pins their content to it). A system whose content
depends on the packs served carries, after the Mendari version, a digest of those packs' ids and
digests; a pack's question systems carry the pack's version and its digest prefix.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from mendari import __version__
from mendari.core.origin import COMMUNITY_LABEL
from mendari.core.pack import Pack
from mendari.core.spec import Outcome
from mendari.errors import MendariError
from mendari.sandbox.fhir_records import CHANNEL_SYSTEM, CHANNELS, SANDBOX_TAG
from mendari.standards import audit_fhir, davinci
from mendari.standards.fhir import (
    DECISION_ID_SYSTEM,
    MANUAL_TRIAGE,
    OUTCOME_SYSTEM,
    PACK_ID_SYSTEM,
    PACK_TAG_SYSTEM,
    FhirAdapter,
    display,
    narrative,
)

PUBLISHER = "Mendari"
NAMING_SYSTEMS_DATE = "2026-10-02"  # when their definitions last changed (pinned by a test)
_OWN = "https://mendari.co/fhir/CodeSystem/"
_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")

OUTCOMES: dict[Outcome, tuple[str, str]] = {
    Outcome.AUTO: (
        "Automated",
        "The recommendation may be acted on without a person in the loop (Task.status ready).",
    ),
    Outcome.HUMAN_REVIEW: (
        "Human review",
        "A person confirms or corrects the recommendation before anyone acts on it "
        "(Task.status requested).",
    ),
    Outcome.ABSTAIN: (
        "Abstain",
        "No recommendation: the existing manual workflow handles the case. Any proposed action "
        "is the pack's fallback, never a recommended action.",
    ),
    Outcome.BLOCK: (
        "Block",
        "The action must not happen: the input was invalid or a blocking policy rule matched. "
        "Any proposed action is the pack's fallback, never the blocked action.",
    ),
}
_ACTIONS = {"C": "create", "R": "read", "U": "update", "D": "delete", "E": "execute"}
NAMING_SYSTEMS = (
    (
        "decision-id",
        DECISION_ID_SYSTEM,
        "Mendari decision id",
        "A decision's id: the Task's identifier, and what its AuditEvents and Provenances target.",
    ),
    (
        "input-hash",
        audit_fhir.INPUT_HASH_SYSTEM,
        "Mendari input hash",
        "A hash of a decision's input, as Provenance.entity identifies it; the input itself is "
        "never exported. Under a deployment's input hash key (required when it keeps PHI) it is "
        "`hmac-sha256:`, the key's id (16 hex digits), `:` and 64 hex digits: an HMAC-SHA256 "
        "only that deployment can recompute. Without a key it is `sha256:` and 64 hex digits, "
        "the plain SHA-256 of the input's canonical JSON.",
    ),
    (
        "decision-pack",
        PACK_ID_SYSTEM,
        "Mendari Decision Pack version",
        "A Decision Pack version (`namespace/name@version`) acting as the engine, in "
        "AuditEvent.agent and Provenance.agent. In Task.meta.tag the same value is a code of "
        f"{PACK_TAG_SYSTEM}.",
    ),
)


@dataclass(frozen=True)
class Terminology:
    """The terminology resources for the packs a deployment serves, found by id or by url."""

    resources: tuple[dict[str, Any], ...]

    @classmethod
    def for_packs(cls, packs: Iterable[Pack]) -> Terminology:
        return cls(tuple(resources(packs)))

    @classmethod
    def serving(cls, packs: Iterable[Pack]) -> tuple[Terminology, dict[str, str]]:
        """The terminology for every pack that can have one, and why the others cannot (a FHIR
        mapping that does not load, an id or url another pack already uses). A server skips
        such a pack's terminology and reports it, rather than not starting."""
        kept: list[Pack] = []
        problems: dict[str, str] = {}
        # Every other pack before any community pack, so a community pack's terminology never
        # costs another pack its own.
        for pack in sorted(packs, key=lambda p: (p.community, p.id)):
            try:
                resources([*kept, pack])
            except MendariError as error:
                problems[pack.id] = f"terminology: {error}"
                continue
            except Exception as error:  # the pack's content, not Mendari's: named, not quoted
                problems[pack.id] = f"terminology: {type(error).__name__}"
                continue
            kept.append(pack)
        return cls.for_packs(kept), problems

    def get(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        return next(
            (
                r
                for r in self.resources
                if r["resourceType"] == resource_type and r["id"] == resource_id
            ),
            None,
        )

    def by_url(self, url: str, version: str | None = None) -> dict[str, Any] | None:
        """A CodeSystem by its canonical url (and version), or a NamingSystem by its uri."""
        for resource in self.resources:
            if resource["resourceType"] == "CodeSystem":
                if resource["url"] == url and version in (None, resource["version"]):
                    return resource
            elif version is None and any(u["value"] == url for u in resource["uniqueId"]):
                return resource
        return None


def resources(packs: Iterable[Pack]) -> list[dict[str, Any]]:
    """Every CodeSystem and NamingSystem for these packs (one version of each, as served)."""
    served = sorted(packs, key=lambda pack: pack.id)
    ids = [pack.id for pack in served]
    repeated = sorted({pack_id for pack_id in ids if ids.count(pack_id) > 1})
    if repeated:
        raise MendariError(f"terminology for one version of each pack, not several of {repeated}")
    found = [
        _outcomes(),
        _audit_events(),
        _decision_packs(served),
        *_performers(served),
        *(system for pack in served for system in _questions(pack)),
        *(_naming_system(*spec) for spec in NAMING_SYSTEMS),
    ]
    _check(found)
    return found


def sandbox_resources() -> list[dict[str, Any]]:
    """The sandbox's own systems, experimental and never served: its synthetic-data tag and its
    Communications' message channel. CI loads them so the sandbox samples validate."""
    return [
        _code_system(
            SANDBOX_TAG["system"],
            "sandbox",
            "Mendari sandbox tag",
            "Marks a resource Mendari's sandbox generated.",
            __version__,
            [_concept("synthetic", "Synthetic", "Generated test data, never a real person's.")],
            experimental=True,
        ),
        _code_system(
            CHANNEL_SYSTEM,
            "message-channel",
            "Mendari sandbox message channel",
            "How a synthetic patient message arrived (Communication.medium).",
            __version__,
            [_concept(code, shown) for code, shown in CHANNELS.items()],
            experimental=True,
        ),
    ]


def resource_id(text: str) -> str:
    """A valid FHIR id from any text: a slug, cut to 64 characters with a hash suffix."""
    slug = re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-.")
    if slug and len(slug) <= 64:
        return slug
    return f"{slug[:55].rstrip('-.')}-{hashlib.sha256(text.encode()).hexdigest()[:8]}".lstrip("-")


def _outcomes() -> dict[str, Any]:
    return _code_system(
        OUTCOME_SYSTEM,
        "decision-outcome",
        "Mendari decision outcome",
        "What the runtime may do with a decision, as Task.businessStatus.",
        __version__,
        [_concept(outcome.value, *OUTCOMES[outcome]) for outcome in Outcome],
    )


def _audit_events() -> dict[str, Any]:
    concepts = [
        _concept(
            event,
            shown,
            f"{shown}: chained in Mendari's audit trail, exported as an AuditEvent with action "
            f"{action} ({_ACTIONS[action]}).",
        )
        for event, (action, shown) in audit_fhir._EVENTS.items()
    ]
    return _code_system(
        audit_fhir.EVENT_SYSTEM,
        "audit-event",
        "Mendari audit event",
        "The events of a decision's hash-chained audit trail, as AuditEvent.subtype.",
        __version__,
        concepts,
    )


def _decision_packs(served: list[Pack]) -> dict[str, Any]:
    return _code_system(
        PACK_TAG_SYSTEM,
        "decision-pack",
        "Mendari Decision Pack",
        "Decision Pack versions (`namespace/name@version`), as Task.meta.tag. Lists the packs "
        "this deployment serves; decisions made by other packs or versions keep their codes, so "
        "this is a fragment.",
        _served_version(served),
        [
            _concept(pack.ref, f"{pack.manifest.name} {pack.version}", pack.manifest.summary)
            for pack in served
        ],
        content="fragment",
    )


def _performers(served: list[Pack]) -> Iterator[dict[str, Any]]:
    systems: dict[str, list[FhirAdapter]] = {}
    for pack in served:
        if pack.manifest.inputs.fhir is not None:
            adapter = FhirAdapter.for_pack(pack)
            systems.setdefault(adapter.mapping.task.performer_system, []).append(adapter)
    for url, adapters in sorted(systems.items()):
        if len({adapter.pack.community for adapter in adapters}) > 1:
            shared = ", ".join(adapter.pack.id for adapter in adapters)
            raise MendariError(
                f"the performer system {url} is used by a community pack and another pack "
                f"({shared}); a community pack's systems are its own"
            )
        community = adapters[0].pack.community
        concepts: dict[str, dict[str, str]] = {}
        for adapter in adapters:
            for code, values in adapter.performer_codes().items():
                concepts.setdefault(code, _performer(adapter.pack, code, values))
        refs = ", ".join(f"`{adapter.pack.ref}`" for adapter in adapters)
        shown = _human(url.rstrip("/").rsplit("/", 1)[-1])
        yield _code_system(
            url,
            resource_id(url.removeprefix(_OWN) if url.startswith(_OWN) else url.split("//")[-1]),
            f"{adapters[0].pack.manifest.name} performer: {shown}"
            if community
            else f"Mendari performer: {shown}",
            f"Who handles a decision, as Task.performerType: the codes {refs} can route to "
            "(fhir/mapping.yaml, `task.performer`).",
            _served_version([adapter.pack for adapter in adapters]),
            list(concepts.values()),
            **(_community(adapters[0].pack) if community else {}),
        )


def _performer(pack: Pack, code: str, values: Mapping[str, str]) -> dict[str, str]:
    """A performer code, defined by the answer that routes to it."""
    manifest = pack.manifest
    if not values:
        return _concept(code, _human(code), f"Every {manifest.name} decision goes here.")
    if len(values) > 1:
        return _concept(code, _human(code), "; ".join(f"{k} {v}" for k, v in values.items()))
    ((name, label),) = values.items()
    if name == "priority":
        return _concept(code, _human(code), f"Priority {label}.")
    if name == "recommendation" and label == MANUAL_TRIAGE:
        return _concept(code, _human(code), "No recommendation: the manual workflow handles it.")
    question = manifest.questions[manifest.policy.primary if name == "recommendation" else name]
    if label not in question.criteria:
        return _concept(code, _human(code), f"`{name}` has no answer (its provider failed).")
    return _concept(code, _human(code), question.criteria[label])


def _questions(pack: Pack) -> Iterator[dict[str, Any]]:
    version = f"{pack.version}+{pack.digest.removeprefix('sha256:')[:8]}"
    for key, question in pack.manifest.questions.items():
        yield _code_system(
            davinci.question_system(pack.id, key),
            resource_id(f"{pack.id}-{key}"),
            f"{pack.manifest.name}: {key}",
            question.instructions,
            version,
            [
                _concept(label, question.criteria[label], question.criteria[label])
                for label in question.labels
            ],
            **(_community(pack) if pack.community else {}),
        )


def _community(pack: Pack) -> dict[str, Any]:
    """How a community pack's systems are published: by its publisher (self-declared), as
    experimental, labelled as not clinically reviewed."""
    return {"publisher": pack.manifest.publisher.name, "experimental": True, "community": True}


def _code_system(
    url: str,
    id_: str,
    title: str,
    description: str,
    version: str,
    concepts: list[dict[str, str]],
    *,
    content: str = "complete",
    experimental: bool = False,
    publisher: str = PUBLISHER,
    community: bool = False,
) -> dict[str, Any]:
    rows = [(concept["code"], concept["display"]) for concept in concepts]
    name = _name(title)
    if community:
        title = f"{title} ({COMMUNITY_LABEL})"
        description = (
            f"{description} {COMMUNITY_LABEL}: an outside developer contributed this pack, and "
            "Mendari has not clinically reviewed it."
        )
    resource: dict[str, Any] = {
        "resourceType": "CodeSystem",
        "id": id_,
        "text": narrative(f"{title} ({url}, version {version}).", rows),
        "url": url,
        "version": version,
        "name": name,
        "title": title,
        "status": "active",
        "experimental": experimental,
        "publisher": publisher,
        "description": " ".join(description.split()),
        "caseSensitive": True,
        "content": content,
    }
    if content == "complete":
        resource["count"] = len(concepts)
    resource["concept"] = concepts
    return resource


def _naming_system(id_: str, uri: str, title: str, description: str) -> dict[str, Any]:
    return {
        "resourceType": "NamingSystem",
        "id": id_,
        "text": narrative(f"{title} ({uri}). {description}"),
        "name": _name(title),
        "status": "active",
        "kind": "identifier",
        "date": NAMING_SYSTEMS_DATE,
        "publisher": PUBLISHER,
        "description": description,
        "uniqueId": [{"type": "uri", "value": uri, "preferred": True}],
    }


def _concept(code: str, shown: str, definition: str | None = None) -> dict[str, str]:
    concept = {"code": code, "display": display(shown)}
    if definition:
        concept["definition"] = " ".join(definition.split())
    return concept


def _served_version(packs: Iterable[Pack]) -> str:
    """The Mendari version and a digest of the packs (ids and digests) a system is made from."""
    listed = "\n".join(sorted(f"{pack.id} {pack.digest}" for pack in packs))
    return f"{__version__}+{hashlib.sha256(listed.encode()).hexdigest()[:8]}"


def _name(title: str) -> str:
    """A computable name (PascalCase, starting with a capital letter, at most 255 characters)."""
    name = "".join(w[:1].upper() + w[1:] for w in re.split(r"[^A-Za-z0-9]+", title) if w)
    return (name if name[:1].isupper() else f"Mendari{name}")[:255]


def _human(code: str) -> str:
    return code.replace("_", " ").replace("-", " ").strip().capitalize() or code


def _check(found: list[dict[str, Any]]) -> None:
    """Ids are valid, and no two resources share an id or a url (e.g. a pack whose performer
    system is one of Mendari's fixed systems)."""
    seen: set[tuple[str, str]] = set()
    for resource in found:
        keys = [("id", f"{resource['resourceType']}/{resource['id']}")]
        if resource["resourceType"] == "CodeSystem":
            keys.append(("url", resource["url"]))
        for key in keys:
            if key in seen:
                raise MendariError(f"two terminology resources share the {key[0]} {key[1]}")
            seen.add(key)
        if not _ID.match(resource["id"]):
            raise MendariError(f"not a FHIR id: {resource['id']!r}")
