# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy. Every error raised on purpose by Mendari derives from MendariError."""


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

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class ReviewError(MendariError):
    """A human-review action is not allowed (unknown item, already resolved, bad label)."""


class NoteError(MendariError):
    """A note cannot be kept here (PHI mode `hash` keeps no free text), or a note or a decision's
    kept input cannot be erased (unknown, already erased, or inside a record kept under PHI mode
    `off`). A `422`: the request is well formed, but not here."""


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
    status the registry answers with (404 unknown, 409 conflict, 422 invalid)."""

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


class ScopeError(MendariError):
    """A scope that Mendari does not grant: unknown, for something not served (a write, a
    resource type, a granular or patient scope), or beyond the credential's role (a `422`)."""


class ClientError(MendariError):
    """A SMART Backend Services client that cannot be registered: a JWK Set holding a private or
    symmetric key, a weak or unsupported key, or one Mendari cannot verify with (a `422`)."""
