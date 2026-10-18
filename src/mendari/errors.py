# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy. Every error raised on purpose by Mendari derives from MendariError."""

from __future__ import annotations

from typing import Literal, get_args

# Every code a provider error may carry: a closed set, held by its type (mypy refuses any other
# code), so what a decision records, and a shared report counts, is one of these.
ProviderErrorCode = Literal[
    "budget_exhausted",
    "credential_unavailable",
    "insufficient_credit",
    "invalid_endpoint",
    "invalid_input",
    "invalid_response",
    "not_configured",
    "provider_unavailable",
    "quota_exhausted",
    "rate_limited",
    "time_budget",
    "timeout",
    "unavailable",
]
# A derived question whose inputs failed (core/derive.py): not a provider error, but recorded
# among them.
DERIVED_INPUT_UNAVAILABLE = "derived_input_unavailable"
# Every code a decision may record a question's failure under.
FAILURE_CODES = frozenset({*get_args(ProviderErrorCode), DERIVED_INPUT_UNAVAILABLE})


class MendariError(Exception):
    """Base class for Mendari errors."""


class PackValidationError(MendariError):
    """A Decision Pack is malformed, inconsistent, or violates its risk-level rules."""

    def __init__(self, pack: str, problems: list[str]):
        self.pack = pack
        self.problems = problems
        detail = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(f"Decision Pack {pack!r} is invalid:\n{detail}")


class PackNotFoundError(MendariError):
    """A pack reference could not be resolved from any search path."""


class ProviderError(MendariError):
    """A provider could not answer. `code` is a stable, audit-safe identifier."""

    def __init__(self, code: ProviderErrorCode, detail: str = ""):
        self.code: ProviderErrorCode = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class ReviewError(MendariError):
    """A human-review action is not allowed (unknown item, already resolved, bad label)."""


class NoteError(MendariError):
    """A note cannot be kept here (PHI mode `hash` keeps no free text), or a note or a decision's
    kept input cannot be erased (unknown, already erased, or inside a record kept under PHI mode
    `off`). A `422`: the request is well formed, but not here."""


class NoAssignment(MendariError):
    """A label for an assignment that is unknown, another labeller's, another organization's or
    closed (answered, withdrawn, in a set no longer a draft, or on a membership no longer a
    labeller's): one refusal for them all (`core/evalsets.py` `NO_ASSIGNMENT`), a `404` on the
    labelling route, as an id that names nothing is (LP-11j)."""


class LabelError(MendariError):
    """A label that does not answer its set's questions, each with one of its labels: names the
    question, never a value given (a `422` on the labelling route, LP-11j)."""


class NoEvalset(MendariError):
    """A hidden evaluation set that is unknown, or another organization's to a route acting for
    one: one refusal for both (`core/evalsets.py` `NO_SET`, naming no id), a `404` on the
    set-management routes (LP-11k), as an id that names nothing is."""


class GradingRefused(MendariError):
    """A grading read of a hidden evaluation set refused (`core/evalsets.py` `grade_read`), or a
    grading run refused before it reads one (LP-11l), with the fixed `reason` the grading route
    answers and logs in place of the message: from the store `no_set`, `not_gradable`,
    `no_runs_left`, `expired`, `set_changed` and `set_unavailable`; the route adds its own
    (`server/app.py` `GRADING_REFUSALS`). The store's message names the set, for the key holder
    and the tests; the route never repeats it. Retiring a set raises it too (`set_changed`,
    `set_unavailable`), as the key holder's command's error."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


class FhirError(MendariError):
    """FHIR input is missing required resources or cannot be mapped to the pack input, or a
    FHIR server could not be read. `status` is the HTTP status the server answered with, when
    it answered with one (a `401`: the token it was sent no longer works)."""

    def __init__(self, *args: object, status: int | None = None):
        self.status = status
        super().__init__(*args)


class FhirStructureError(FhirError):
    """FHIR input is not FHIR R4 JSON: an element has the wrong JSON type (a string where an
    object belongs, say), is not an R4 element, or a primitive does not have its format.
    `issues` are OperationOutcome-shaped ({code, expression, diagnostics}; code `structure` or
    `value`), paths only, never values."""

    def __init__(self, issues: list[dict[str, str]]):
        self.issues = issues
        shown = "; ".join(f"{i['expression']}: {i['diagnostics']}" for i in issues[:5])
        more = f" (and {len(issues) - 5} more)" if len(issues) > 5 else ""
        super().__init__(f"not valid FHIR JSON: {shown}{more}")


class FhirRequestError(FhirStructureError):
    """A FHIR operation's request is malformed: a missing, repeated or unknown parameter, or a
    parameter of the wrong type (a `400`, like the structure errors it extends)."""


class FhirSourceNotFound(FhirError):
    """A FHIR operation's source data names nothing here: Da Vinci DTR's `$questionnaire-package`
    with a `context` id or orders that name no decision of the caller's organization, so no
    package can be built. Unknown and another organization's are one answer, which never repeats
    the id: a `422` OperationOutcome with issue type `not-found` (DTR spec-130 asks for a 4xx; the
    operation exists and the request is well formed, so not a `404` or a `400`)."""


class CdsHooksError(MendariError):
    """A CDS Hooks request Mendari will not answer as sent: more draft orders to decide than
    one request may hold (a `400`; the EHR can send them in several requests)."""


class SmartError(MendariError):
    """A SMART on FHIR launch cannot continue: not configured, a bad or expired state, a token
    the authorization server should not have issued, or no session. `reason` is the fixed code
    the security log gives a refused launch (`smart.launch.refused`)."""

    def __init__(self, message: str, status: int = 400, *, reason: str = "launch"):
        self.status = status
        self.reason = reason
        super().__init__(message)


class HubError(MendariError):
    """A pack archive, signature, registry request or pull failed a check. `status` is the HTTP
    status the registry answers with (404 unknown, 409 conflict, 410 deleted, 422 invalid)."""

    def __init__(self, message: str, status: int = 422):
        self.status = status
        super().__init__(message)


class NoticeNotAcknowledged(MendariError):
    """A signed-in person has not acknowledged the current notice (`mendari.notices`): answered
    with 403 on every route that takes data in until they do (`POST /api/notice/acknowledge`,
    which the dashboard asks for when it opens)."""


class AuthError(MendariError):
    """Signing in or an API key failed a check, or the principal may not do this. `status` is the
    HTTP status (401 not signed in, 403 not allowed, 400 a bad login); `challenge` the
    `WWW-Authenticate` value the answer carries, if any (RFC 6750)."""

    def __init__(self, message: str, status: int = 400, *, challenge: str | None = None):
        self.status = status
        self.challenge = challenge
        super().__init__(message)


class NoOrganization(AuthError):
    """A signed-in principal acting for no organization (memberships mode, LP-12) reached
    organization data through `tenancy.current()`: refused, never widened to all of them."""

    def __init__(self, message: str = "you have no organization here"):
        super().__init__(message, 403)


class OrganizationChanged(AuthError):
    """A request from a window showing another organization than the one its person has chosen
    since (memberships mode: the dashboard sends the one it shows, `X-Mendari-Org`): refused, so
    nothing meant for one organization lands in another (a `409`)."""

    def __init__(
        self,
        message: str = "this window shows another organization than the one you chose since: "
        "reload it",
    ):
        super().__init__(message, 409)


class InvitationRefused(AuthError):
    """An invitation not accepted (memberships mode, LP-12f), with the fixed `reason` the
    security log gives it: `unverified` (403), `invalid` (404: unknown, used, revoked or expired,
    one answer), `other_address` (403, it stays open) or `member` (409, already a member of its
    organization: it stays open)."""

    def __init__(self, message: str, status: int, reason: str):
        self.reason = reason
        super().__init__(message, status)


class ScopeError(MendariError):
    """A scope that Mendari does not grant: unknown, for something not served (a write, a
    resource type, a granular or patient scope), or beyond the credential's role (a `422`)."""


class ClientError(MendariError):
    """A SMART Backend Services client that cannot be registered: a JWK Set holding a private or
    symmetric key, a weak or unsupported key, or one Mendari cannot verify with (a `422`)."""
