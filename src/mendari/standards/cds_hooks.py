# SPDX-License-Identifier: Apache-2.0
"""CDS Hooks 2.0: expose each pack that declares `cds_hooks` as a CDS service.

The EHR sends the data via `prefetch` (resources or search Bundles), and for order hooks the
orders in `context.draftOrders`. A prefetch key the EHR left out is fetched from the request's
`fhirServer` with its `fhirAuthorization` token, using the pack's own prefetch template, but only
when that server is on the deployment's allow-list (`MENDARI_FHIR_ALLOWED_SERVERS`, empty by
default; see `fhir_client`). A key sent empty (`{}`, `[]`, `""`) counts as left out, as before. A
key left out with no `fhirServer` is a `412` (`PrefetchRequired`), as CDS Hooks 2.0 says; a key
whose value is null is "no data" and is not fetched, and one whose value is neither null nor a
resource cannot be read. What the client sent goes through the structural R4 check, every
value of it (a `400`), before any of it is set aside.

The hook always answers (LP-23d). A key that could not be read (each read on its own: a failed
fetch, an OperationOutcome, a resource or search page Mendari fetched that is not R4, a page
cap) makes the decision ABSTAIN with its reason, recorded like any other, and the card says so
whatever the pack's `show` says. Card wording comes from the pack's `cds_hooks.card`; without it,
the card describes a routing decision. Spec 0.3 adds two keys to it (§5): `refused`, for a decision
that refused its input, whose card lists why whatever the links (a template that does not name it
words such a decision as `none`, as before), and `also_show`, the policy rules whose match shows a
card for a recommendation `show` leaves out, in the rule's own words. A pack that declares
`coverage_information` also returns, for each decided order, a Da Vinci CRD system action
(`standards/davinci.py`): `indeterminate` for a technical reason when the order abstained over
unreadable data and the Coverage was read.

**Linked data** (LP-23h): for a spec 0.3 pack, what its mapping reaches by a link the record does
not hold (a note's Binary, the payer behind a Coverage, the drug behind a MedicationRequest) is
read from the same `fhirServer`, under its base, with the same token, at most 20 links a
decision (`standards/linked.py`); a link that is not read is its field's reason, and the
decision abstains. Without a `fhirServer`, nothing is read and such a link is `not_fetched`; a
`fhirServer` off the allow-list is refused before anything is decided, as for the prefetch.

**Cards** (LP-23f) are CDS Hooks 2.0's: the card's `uuid` is its decision's id and its one
suggestion's is `suggestion_id` of it, so feedback names them without anything stored; a card
with suggestions says `selectionBehavior` (`at-most-one`) and offers the override reasons of
`terminology.OVERRIDE_REASONS`. Its summary is the pack's words with display names
(`templates.fill_display`), and its detail the pack's status, the notice and one plain sentence,
never a percentage. With `MENDARI_PUBLIC_URL` set (`CardLinks`) the reasons are behind a link to
the decision in the audit view, `source.url` is the pack's card, and when the EHR may launch the
pack (SMART on, the request's `fhirServer` registered for launches in the caller's organization)
a `smart` link opens it; without the public URL the reasons stay in the detail, but for the
confidence thresholds' (`policy.threshold_reason`), whose figures carry no interval. A Da Vinci
CRD service (a pack with `coverage_information`) declares the CRD version and its one
configuration option, `coverage-info`, in discovery, and sets every card's `source.topic`; a
request that sets `coverage-info` to false gets neither its cards nor its system actions, and is
still decided.

**Feedback** (`CardFeedback`): what the clinician did with each card, accepted with the
suggestion taken or overridden with one of the reasons offered, checked against what the card
offered before anything is looked up, with nothing CDS Hooks 2.0 does not define but
`extension`; neither the comment nor an extension is kept.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from mendari import notices
from mendari.core import clock
from mendari.core.decision import Decision, DecisionResult
from mendari.core.origin import COMMUNITY_LABEL
from mendari.core.pack import Pack
from mendari.core.policy import threshold_reason
from mendari.core.spec import REFUSED, SUMMARY_CHARS, CdsCard, Outcome, Priority, RuleCard
from mendari.core.templates import NO_RECOMMENDATION, fill_display, shown
from mendari.errors import (
    CdsHooksError,
    FhirError,
    FhirStructureError,
    PrefetchRequired,
    ReadReason,
)
from mendari.standards import davinci, fhir_structure
from mendari.standards.data_coverage import source_type
from mendari.standards.fhir import FhirAdapter
from mendari.standards.fhir_client import FhirClient, check_allowed
from mendari.standards.linked import Linker, reads_links
from mendari.standards.terminology import (
    OVERRIDE_REASONS,
    OVERRIDE_REASONS_SYSTEM,
    override_reasons,
)

# Hooks whose context carries the orders being written (CDS Hooks `draftOrders`).
ORDER_HOOKS = ("order-select", "order-sign")
# The most draft orders one order hook request may decide: each is a decision (provider calls,
# a chained record), so an unbounded list would multiply one request's cost.
MAX_ORDERS = 50
MAX_FEEDBACK = 50  # the most cards one feedback request may name
_TOKEN = re.compile(r"\{\{\s*context\.(\w+)\s*\}\}")
_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")  # a FHIR id
# What an ABSTAIN over a record that could not be read says on its card (owner decision 6): fixed
# words, then each key and its reason; never a URL, a query or what a server answered.
UNREADABLE_SUMMARY = "No recommendation: part of the record could not be read"
UNREADABLE_DETAIL = (
    "Mendari could not read part of this patient's record, so it made no recommendation and a "
    "person decides. Not read:"
)
# What a card keyed `refused` says (spec 0.3 §5): fixed words, then each reason the input was
# refused, as the audit record keeps them (where in the input and which rule of the schema, a
# resource by its type and position: never a value or an id).
REFUSED_DETAIL = (
    "Refused: Mendari could not accept the data sent as this pack's input, so it checked "
    "nothing and made no recommendation. Why:"
)
# A card's detail: the pack's status, the notice, and one plain sentence for its outcome.
OUTCOME_SENTENCES = {
    Outcome.AUTO: "Automated: the recommendation may be acted on without a person reviewing it.",
    Outcome.HUMAN_REVIEW: (
        "For review: a person confirms or corrects the recommendation before anyone acts on it."
    ),
    Outcome.ABSTAIN: "No recommendation: the existing workflow handles this case.",
    Outcome.BLOCK: (
        "Blocked: the action must not happen, because the input was invalid or a blocking rule "
        "matched."
    ),
}
# A card with suggestions lets the clinician take at most one (CDS Hooks 2.0).
SELECTION_BEHAVIOR = "at-most-one"
AUDIT_LINK = "Reasons and engine detail (Mendari audit trail)"
LAUNCH_LINK = "Open in Mendari"
# Da Vinci CRD (2.2.1): the versions a CRD service declares in discovery (its first two
# numbers), its one configuration option, which the request's `davinci-crd.configuration` may
# turn off, and the type every card of a CRD service says it is (`source.topic`).
CRD_VERSIONS = ("2.2",)
CRD_VERSION = "davinci-crd.version"
CRD_OPTIONS = "davinci-crd.configuration-options"
CRD_CONFIGURATION = "davinci-crd.configuration"
COVERAGE_INFO = "coverage-info"
CARD_TYPE = {
    "system": "http://terminology.hl7.org/CodeSystem/cdshooks-card-type",
    "code": COVERAGE_INFO,
    "display": "Coverage Information",
}
COVERAGE_INFO_OPTION = {
    "code": COVERAGE_INFO,
    "type": "boolean",
    "name": "Coverage Information",
    "description": "Whether each order comes back with its coverage information and its card. "
    "Off, the service sends neither, and still decides and records each order.",
    "default": True,
}


@dataclass(frozen=True)
class CardLinks:
    """Where a card may link: Mendari's public address (`MENDARI_PUBLIC_URL`, None: no link and
    the reasons in the detail, the confidence thresholds' left out), and whether the EHR may
    launch the pack's SMART view from the card (SMART on, and the request's `fhirServer`
    registered for launches in the caller's organization, `MENDARI_SMART_EHRS`)."""

    public_url: str | None = None
    launch: bool = False


NO_LINKS = CardLinks()


def suggestion_id(decision_id: str) -> str:
    """The uuid of a card's one suggestion: a UUID 5 of its decision's id, so feedback that names
    it is checked without anything stored."""
    return str(uuid.uuid5(uuid.UUID(decision_id), "suggestion"))


def service_id(pack: Pack) -> str:
    return pack.id.replace("/", ".")


def discovery(packs: Iterable[Pack]) -> dict[str, Any]:
    services = []
    for pack in packs:
        hooks = pack.manifest.cds_hooks
        if hooks is None:
            continue
        usage = (
            f"{pack.status_label} {notices.NOTICE} "
            f"Risk level {int(pack.manifest.risk.level)} ({pack.manifest.risk.category}). "
            f"Validate locally first: `mendari eval {pack.id} --dataset <local data>`."
        )
        description = hooks.description
        if pack.community:  # the EHR's integrator reads this before turning the service on
            description = f"{description} ({COMMUNITY_LABEL})"
            usage = (
                f"{COMMUNITY_LABEL}: it recommends, and a person confirms every decision. {usage}"
            )
        service: dict[str, Any] = {
            "hook": hooks.hook,
            "id": service_id(pack),
            "title": hooks.title,
            "description": description,
            "prefetch": hooks.prefetch,
            "usageRequirements": usage,
        }
        if hooks.coverage_information:  # a Da Vinci CRD service
            service["extension"] = {
                CRD_VERSION: list(CRD_VERSIONS),
                CRD_OPTIONS: [dict(COVERAGE_INFO_OPTION)],
            }
        services.append(service)
    return {"services": services}


def invoke(
    decision: Decision,
    request: Mapping[str, Any],
    *,
    most: int = MAX_ORDERS,
    charge: Callable[[int], None] | None = None,
    links: CardLinks = NO_LINKS,
) -> dict[str, Any]:
    """The cards (and Da Vinci system actions) for one hook request. An order hook decides each
    draft order the pack is about, at most `most` of them; `charge` is given the number of
    decisions about to be made, before anything is fetched or decided (the rate limit), and
    refuses by raising. `links` says where the cards may link."""
    hooks = decision.pack.manifest.cds_hooks
    if hooks is None:
        raise FhirError(f"pack {decision.pack.id} is not a CDS service")
    if request.get("hook") != hooks.hook:
        raise FhirError(f"service expects hook {hooks.hook!r}, got {request.get('hook')!r}")
    orders: list[dict[str, Any]] = []
    if hooks.hook in ORDER_HOOKS:
        # One decision per draft order the pack is about (e.g. each referral, not the lab
        # orders signed with it), each with the prefetched record: as the decision reads its
        # record, with its organization's site code map (LP-23k).
        context = _object(request, "context")
        site = decision.site_map()
        orders = FhirAdapter.for_pack(decision.pack).focus_resources(
            context.get("draftOrders"), site_codes=None if site is None else site.sets
        )
        if len(orders) > most:
            raise CdsHooksError(
                f"{len(orders)} draft orders to decide; at most {most} in one request"
            )
    if charge is not None:
        charge(len(orders) if hooks.hook in ORDER_HOOKS else 1)
    sent = _object(request, "prefetch")
    # A key left out, or sent empty, is fetched, as before; a null value is "no data" (CDS
    # Hooks 2.0), never fetched.
    missing = [key for key in hooks.prefetch if _left_out(sent, key)]
    found: dict[str, Any] = {}
    unreadable: dict[str, ReadReason] = {}
    if missing:
        found, unreadable = fetch(hooks.prefetch, missing, request)
    # Every value the client sent goes through the structural R4 check before any is set aside,
    # as before: a structure error is the request's fault (a `400`), whatever the value is.
    _require_r4(sent)
    prefetch: dict[str, Any] = {}
    for key, value in sent.items():
        if value is None or key in missing:
            continue
        if key in hooks.prefetch and _outcome(value):
            unreadable[key] = "operation_outcome"  # the client's own read failed
            continue
        if key in hooks.prefetch and not _resource(value):
            # Neither data nor "no data" (a string, a list, an object with no resourceType):
            # nothing is decided on it, but it stays as sent, so a required resource it does not
            # hold is still refused (`decide_fhir`), as before.
            unreadable[key] = "unsupported_type"
        prefetch[key] = value
    prefetch.update(found)
    templates = hooks.prefetch
    linking = linker(decision, request)
    with linking if linking is not None else nullcontext():
        if hooks.hook in ORDER_HOOKS:
            decided = [
                decision.decide_fhir(
                    prefetch, focus=order, unreadable=unreadable, sources=templates, links=linking
                )
                for order in orders
            ]
        else:
            decided = [
                decision.decide_fhir(
                    prefetch, unreadable=unreadable, sources=templates, links=linking
                )
            ]
    template = hooks.card
    # A CRD service the request turned `coverage-info` off for: decided and recorded, and
    # nothing of that type sent back, neither cards nor system actions (CRD 2.2.1).
    silenced = bool(hooks.coverage_information) and not coverage_info_wanted(request)
    cards = []
    for result, task in decided:
        if silenced or not shows_card(template, result):
            continue  # decided and audited, but nothing worth interrupting the user for
        cards.append(card(decision.pack, result, task, links))
    response: dict[str, Any] = {"cards": cards}
    crd = bool(hooks.coverage_information) and not silenced
    coverage = davinci.coverage_reference(prefetch) if crd else None
    if coverage is not None:
        actions = []
        for order, (result, _) in zip(orders, decided, strict=True):
            if result.abstained_unreadable:  # the requirements were not checked: CRD's way
                extension = davinci.indeterminate(result, coverage, clock.today())
                actions.append(davinci.system_action(order, extension))
                continue
            info = hooks.coverage_information.get(result.recommendation or "")
            if info is not None:
                form = None
                dtr = hooks.questionnaire
                if dtr is not None and result.recommendation == dtr.missing:
                    labels = {key: answer.label for key, answer in result.answers.items()}
                    context = davinci.context_of(decision.pack, labels)
                    url = davinci.questionnaire_url(decision.pack, context)
                    form = f"{url}|{decision.pack.version}"
                extension = davinci.coverage_information(
                    info, result, coverage, clock.today(), questionnaire=form
                )
                actions.append(davinci.system_action(order, extension))
        if actions:
            response["systemActions"] = actions
    return response


def coverage_info_wanted(request: Mapping[str, Any]) -> bool:
    """Whether a CRD request wants coverage information: unless its
    `extension.davinci-crd.configuration` sets `coverage-info` to false. Configuration Mendari
    does not support, or of another shape, is ignored, as CRD says."""
    extension = request.get("extension")
    configuration = extension.get(CRD_CONFIGURATION) if isinstance(extension, Mapping) else None
    return not (isinstance(configuration, Mapping) and configuration.get(COVERAGE_INFO) is False)


def fetch(
    templates: Mapping[str, str], keys: list[str], request: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, ReadReason]]:
    """The prefetch the EHR left out, read from its `fhirServer` (allow-listed servers only):
    what was read, and each key that could not be read with its reason (`read_each`).

    A request fault raises, before anything is read: no `fhirServer` (`PrefetchRequired`, a
    `412`), a `fhirAuthorization` that is not a Bearer token, a context that cannot fill a
    template. So does a server off the allow-list: that is configuration, not the patient's
    data, so nothing is fetched or recorded."""
    server = request.get("fhirServer")
    if not server:
        raise PrefetchRequired(
            f"prefetch required for: {', '.join(keys)} (and no fhirServer to fetch from)"
        )
    token = _token(request)
    context = _object(request, "context")
    queries = {key: fill_template(templates[key], context) for key in keys}
    with FhirClient(str(server), token=token) as client:
        return read_each(client, templates, queries)


def linker(decision: Decision, request: Mapping[str, Any]) -> Linker | None:
    """The linked reads of one hook request (LP-23h): from its `fhirServer`, with its token, for
    a pack whose mapping reaches anything by a link (spec 0.3). None without a `fhirServer` or
    for any other pack: nothing is read. The client opens when a link first needs it, so a
    request whose record holds every link opens none. A request fault is refused before anything
    is decided, as for the prefetch (`fetch`): a `fhirAuthorization` that is not a Bearer token,
    and a `fhirServer` off the allow-list, which is configuration, not the patient's data, so
    nothing is fetched or recorded (conflict 14), whether or not the record holds every link."""
    server = request.get("fhirServer")
    if not server or not reads_links(FhirAdapter.for_pack(decision.pack).mapping):
        return None
    token = _token(request)
    check_allowed(str(server))
    return Linker(lambda: FhirClient(str(server), token=token))


def _token(request: Mapping[str, Any]) -> str | None:
    """The request's `fhirAuthorization` access token, a Bearer token or none; anything else is
    refused (`FhirError`), as a client error."""
    authorization = _object(request, "fhirAuthorization")
    token = authorization.get("access_token")
    if token is not None and not isinstance(token, str):
        raise FhirError("fhirAuthorization.access_token must be a string")
    if token and str(authorization.get("token_type", "Bearer")).lower() != "bearer":
        raise FhirError("fhirAuthorization must be a Bearer token")
    return token


def read_each(
    client: FhirClient,
    templates: Mapping[str, str],
    queries: Mapping[str, str],
    *,
    fatal: Callable[[FhirError], bool] | None = None,
) -> tuple[dict[str, Any], dict[str, ReadReason]]:
    """Read each prefetch key's query on its own (`queries`, filled from `templates`): the keys
    read, and the keys that could not be, each with its reason. A search is read whole (a page
    cap is `cap`). What comes back must be what was asked for: an OperationOutcome is
    `operation_outcome`, another resource type (or a resource for a search) `unsupported_type`,
    and a resource that fails the structural R4 check (`fhir_structure`), or a search page whose
    `entry` or `link` is not R4's shape (`FhirClient.read`), `not_r4`, so nothing is decided on
    it. An error `fatal` says so (SMART's `401`: the launch has ended) is raised."""
    found: dict[str, Any] = {}
    unreadable: dict[str, ReadReason] = {}
    for key, query in queries.items():
        try:
            value = client.read(query, complete=True)
        except FhirError as error:
            if fatal is not None and fatal(error):
                raise
            unreadable[key] = error.reason or "network"
            continue
        reason = _unread(templates[key], value)
        if reason is None:
            found[key] = value
        else:
            unreadable[key] = reason
    return found, unreadable


def _unread(template: str, value: Mapping[str, Any]) -> ReadReason | None:
    """Why a value Mendari read for `template` cannot be used, or None."""
    if _outcome(value):
        return "operation_outcome"
    search = "?" in template or "/" not in template
    wanted = "Bundle" if search else source_type(template)
    if value.get("resourceType") != wanted:
        return "unsupported_type"
    if fhir_structure.blocking(fhir_structure.check(value)):
        return "not_r4"
    return None


def _left_out(sent: Mapping[str, Any], key: str) -> bool:
    """A prefetch key the client did not send, or sent empty (`{}`, `[]`, `""`): it is fetched,
    or with no `fhirServer` a `412`, as an empty value was before. A null value is not left out:
    it says there is no data (CDS Hooks 2.0)."""
    if key not in sent:
        return True
    value = sent[key]
    return value is not None and not value


def _require_r4(sent: Mapping[str, Any]) -> None:
    """The structural R4 check on the prefetch the client sent (`fhir_structure`): a blocking
    issue is the request's fault, a `FhirStructureError` (`400`), paths only."""
    malformed = fhir_structure.blocking(fhir_structure.check(sent))
    if malformed:
        raise FhirStructureError(
            [
                {"code": i.code, "expression": i.expression, "diagnostics": i.diagnostics}
                for i in malformed
            ]
        )


def _outcome(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("resourceType") == "OperationOutcome"


def _resource(value: Any) -> bool:
    """A resource or a Bundle: what a prefetch key's value is when it is not null."""
    return isinstance(value, Mapping) and isinstance(value.get("resourceType"), str)


def _object(request: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """The request's `key`, an object (absent or null: empty); anything else is refused, as a
    client error, never a crash."""
    value = request.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FhirError(f"{key} must be an object")
    return value


def fill_template(template: str, context: Mapping[str, Any]) -> str:
    """A prefetch template with its `{{context.x}}` tokens filled; each value must be a FHIR id,
    so nothing but an id can reach the query."""

    def value(match: re.Match[str]) -> str:
        filled = context.get(match.group(1))
        if not isinstance(filled, str) or not _ID.match(filled):
            raise FhirError(f"context.{match.group(1)} is needed to fetch {template.split('?')[0]}")
        return quote(filled, safe="")

    query = _TOKEN.sub(value, template)
    if "{{" in query:
        raise FhirError(f"cannot fill the prefetch template {template!r}")
    return query


def source_label(pack: Pack) -> str:
    """Who a card comes from, as the clinician sees it under the card: a community pack says it
    is not clinically reviewed."""
    label = f"Mendari · {pack.manifest.name} {pack.version}"
    return f"{label} · {COMMUNITY_LABEL}" if pack.community else label


def card_key(template: CdsCard | None, result: DecisionResult) -> str:
    """Which of a card template's keys words a decision's card: its recommendation, `none`
    without one, or `refused` for a decision that refused its input (a BLOCK before any
    provider ran) when the template names `refused` (spec 0.3 §5); one that does not reads it
    as `none`, as before."""
    if result.recommendation is not None:
        return result.recommendation
    refused = result.outcome is Outcome.BLOCK and result.rejected
    return REFUSED if refused and template is not None and template.names_refused() else "none"


def rule_card(template: CdsCard | None, result: DecisionResult) -> RuleCard | None:
    """The wording of the policy rule that decided, for a recommendation `show` leaves out
    (`also_show`, spec 0.3 §5); None for one `show` names, or any other decision."""
    if template is None or template.show is None or result.rule is None:
        return None
    if card_key(template, result) in template.show:
        return None
    return template.also_show.get(result.rule)


def shows_card(template: CdsCard | None, result: DecisionResult) -> bool:
    """Whether a decision shows a card: every decision without a `show` list; with one, a key
    it names, a decision a rule in `also_show` matched, and an ABSTAIN because part of the
    record could not be read, whatever `show` says."""
    if template is None or template.show is None or result.abstained_unreadable:
        return True
    return card_key(template, result) in template.show or rule_card(template, result) is not None


def card(
    pack: Pack,
    result: DecisionResult,
    task: Mapping[str, Any],
    links: CardLinks = NO_LINKS,
) -> dict[str, Any]:
    template = pack.manifest.cds_hooks.card if pack.manifest.cds_hooks else None
    key = card_key(template, result)
    if result.abstained_unreadable:
        detail = _unreadable_detail(pack, result)
        return _card(pack, result, task, "info", UNREADABLE_SUMMARY, detail, links)
    by_rule = rule_card(template, result)
    if result.priority is Priority.STAT:
        indicator = "critical"
    elif by_rule is not None:
        indicator = by_rule.indicator
    elif template is not None and key in template.indicators:
        indicator = template.indicators[key]
    elif result.outcome is Outcome.AUTO:
        indicator = "info"
    else:
        indicator = "warning"
    if by_rule is not None:
        summary = fill_display(by_rule.summary, result, pack.manifest)
    elif template is not None and key in template.summaries:
        summary = fill_display(template.summaries[key], result, pack.manifest)
    else:
        verb = {
            Outcome.AUTO: "Routed",
            Outcome.HUMAN_REVIEW: "Review: route",
            Outcome.ABSTAIN: "No recommendation;",
            Outcome.BLOCK: "Blocked;",
        }[result.outcome]
        summary = f"{verb} to {_target(pack, result)}" + (
            f" ({result.priority.value})" if result.priority.value != "routine" else ""
        )
    if key == REFUSED:
        return _card(pack, result, task, indicator, summary, _refused_detail(pack, result), links)
    # The engine's reasons are behind the audit link when there is one, else in the detail, but
    # for the confidence thresholds': its figures carry no interval, and the outcome's sentence
    # says what it decided.
    reasons = (
        []
        if links.public_url
        else [f"- {reason}" for reason in result.reasons if not threshold_reason(reason)]
    )
    detail = "\n".join(
        [f"*{pack.status_label}* *{notices.NOTICE}*", "", OUTCOME_SENTENCES[result.outcome]]
        + (["", *reasons] if reasons else [])
        + ([f"\nFallback: {result.fallback}"] if result.fallback else [])
    )
    return _card(pack, result, task, indicator, summary, detail, links)


def _target(pack: Pack, result: DecisionResult) -> str:
    """Who a decision routes to, as a person reads it: the recommendation's display name."""
    if result.recommendation is None:
        return NO_RECOMMENDATION
    return shown(pack.manifest, pack.manifest.policy.primary, result.recommendation)


def _unreadable_detail(pack: Pack, result: DecisionResult) -> str:
    """An unreadable ABSTAIN's detail: the pack's status and the notice, the fixed words, and
    each key with its reason code (the pack's own names and a closed set: nothing from the
    record, the request or a server)."""
    return "\n".join(
        [f"*{pack.status_label}* *{notices.NOTICE}*", "", UNREADABLE_DETAIL, ""]
        + [f"- {key}: {reason}" for key, reason in result.unreadable]
        + ([f"\nFallback: {result.fallback}"] if result.fallback else [])
    )


def _refused_detail(pack: Pack, result: DecisionResult) -> str:
    """A refused input's detail (the `refused` key): the pack's status and the notice, the fixed
    words, and each reason as the audit record keeps it (`DecisionResult.to_record`), whatever
    the links: why it was refused is what the card is for."""
    reasons = [reason.removeprefix("input rejected: ") for reason in result.to_record()["reasons"]]
    return "\n".join(
        [f"*{pack.status_label}* *{notices.NOTICE}*", "", REFUSED_DETAIL, ""]
        + [f"- {reason}" for reason in reasons]
        + ([f"\nFallback: {result.fallback}"] if result.fallback else [])
    )


def _card(
    pack: Pack,
    result: DecisionResult,
    task: Mapping[str, Any],
    indicator: str,
    summary: str,
    detail: str,
    links: CardLinks,
) -> dict[str, Any]:
    """A CDS Hooks 2.0 card for one decision: its uuid is the decision's id, its one
    suggestion's `suggestion_id` of it."""
    template = pack.manifest.cds_hooks.card if pack.manifest.cds_hooks else None
    label = (
        template.suggestion
        if template is not None
        else f"Create routing task for {_target(pack, result)}"
    )
    source: dict[str, Any] = {"label": source_label(pack)}
    shown_pack = quote(pack.id, safe="/")
    public = links.public_url.rstrip("/") if links.public_url else None
    if public is not None:
        source["url"] = f"{public}/?pack={shown_pack}#card"
    if pack.manifest.cds_hooks is not None and pack.manifest.cds_hooks.coverage_information:
        source["topic"] = dict(CARD_TYPE)  # CRD 2.2.1: every card of a CRD service says its type
    found: dict[str, Any] = {
        "uuid": result.id,
        "summary": summary[:SUMMARY_CHARS],
        "indicator": indicator,
        "detail": detail,
        "source": source,
        "selectionBehavior": SELECTION_BEHAVIOR,
        "suggestions": [
            {
                "label": label,
                "uuid": suggestion_id(result.id),
                "isRecommended": result.outcome is not Outcome.BLOCK,
                "actions": [
                    {"type": "create", "description": task["description"], "resource": task}
                ],
            }
        ],
        "overrideReasons": override_reasons(),
        "extension": {"mendari.decision_id": result.id},
    }
    if public is not None:
        found["links"] = [
            {
                "label": AUDIT_LINK,
                "url": f"{public}/?pack={shown_pack}#audit/{quote(result.id, safe='')}",
                "type": "absolute",
            }
        ]
        if links.launch:
            found["links"].append(
                {
                    "label": LAUNCH_LINK,
                    "url": f"{public}/smart/launch?pack={shown_pack}",
                    "type": "smart",
                }
            )
    return found


# -- feedback (CDS Hooks 2.0 `POST {service}/feedback`) ---------------------------------------


class _Closed(BaseModel):
    """CDS Hooks 2.0's shape and nothing else: a field it does not define is refused, but for
    `extension`, which it reserves for implementations (a JSON object), accepted on any object
    and never kept."""

    model_config = ConfigDict(extra="forbid")

    extension: dict[str, Any] | None = None


class FeedbackCoding(_Closed):
    """A Coding as a CDS client sends it back, CDS Hooks 2.0's (`system`, `code`, `display`):
    the reason it chose, of those a card offered."""

    system: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=64)
    display: str | None = Field(default=None, max_length=250)


class OverrideReason(_Closed):
    reason: FeedbackCoding | None = None
    # Free text (owner decision 7): accepted, and never kept.
    userComment: str | None = None


class AcceptedSuggestion(_Closed):
    id: str = Field(min_length=1, max_length=64)


class FeedbackItem(_Closed):
    """What a clinician did with one card: accepted, with the suggestion taken (a card's
    selection is at most one), or overridden, with one of the reasons the card offered or none.
    Checked against what the card offered (its suggestion's `suggestion_id`, the codes of
    `OVERRIDE_REASONS`) without anything looked up."""

    card: str = Field(min_length=1, max_length=64)
    outcome: Literal["accepted", "overridden"]
    acceptedSuggestions: list[AcceptedSuggestion] | None = Field(
        default=None, min_length=1, max_length=1
    )
    overrideReason: OverrideReason | None = None
    outcomeTimestamp: AwareDatetime

    @field_validator("outcomeTimestamp")
    @classmethod
    def _in_utc(cls, value: datetime) -> datetime:
        """The time in UTC, as CDS Hooks says and the chain keeps it: one with no UTC time
        (before year 1 or after year 9999 once its offset is taken off) is refused."""
        try:
            return value.astimezone(UTC)
        except OverflowError:
            raise ValueError("outcomeTimestamp has no time in UTC") from None

    @model_validator(mode="after")
    def _offered(self) -> FeedbackItem:
        if self.outcome == "accepted":
            if not self.acceptedSuggestions:
                raise ValueError("an accepted card names the suggestion taken")
            if self.overrideReason is not None:
                raise ValueError("an accepted card has no override reason")
            # A card id that is no decision's (not a UUID) is the store's to refuse, as unknown.
            offered = _suggestion_of(self.card)
            if offered is not None and any(t.id != offered for t in self.acceptedSuggestions):
                raise ValueError("acceptedSuggestions names a suggestion the card did not offer")
        elif self.acceptedSuggestions is not None:
            raise ValueError("an overridden card names no accepted suggestion")
        reason = self.overrideReason.reason if self.overrideReason is not None else None
        if reason is not None and (
            reason.system != OVERRIDE_REASONS_SYSTEM or reason.code not in OVERRIDE_REASONS
        ):
            raise ValueError("overrideReason.reason is not one of the reasons the card offered")
        return self

    def chained(self) -> dict[str, Any]:
        """What the chain keeps of it: codes, ids and the time (`AuditStore.record_card_feedback`),
        never the comment, a display or an extension."""
        reason = self.overrideReason.reason if self.overrideReason is not None else None
        return {
            "card": self.card,
            "outcome": self.outcome,
            "accepted_suggestions": [taken.id for taken in self.acceptedSuggestions or []],
            "override_reason": None
            if reason is None
            else {"system": reason.system, "code": reason.code},
            "outcome_timestamp": self.outcomeTimestamp.isoformat(),  # in UTC (`_in_utc`)
        }


class CardFeedback(_Closed):
    """CDS Hooks 2.0 feedback: 1 to `MAX_FEEDBACK` cards, each once."""

    feedback: list[FeedbackItem] = Field(min_length=1, max_length=MAX_FEEDBACK)

    @model_validator(mode="after")
    def _each_card_once(self) -> CardFeedback:
        cards = [item.card for item in self.feedback]
        if len(set(cards)) != len(cards):
            raise ValueError("each card is named once")
        return self


def _suggestion_of(card: str) -> str | None:
    """The suggestion a card offered, or None for a card id that is not a UUID, so no
    decision's."""
    try:
        return suggestion_id(card)
    except ValueError:
        return None
