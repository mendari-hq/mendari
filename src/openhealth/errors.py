"""Exception hierarchy. Every error raised on purpose by OpenHealth derives from OpenHealthError."""


class OpenHealthError(Exception):
    """Base class for OpenHealth errors."""


class PackValidationError(OpenHealthError):
    """A Decision Pack is malformed, inconsistent, or violates its risk-level rules."""

    def __init__(self, pack: str, problems: list[str]):
        self.pack = pack
        self.problems = problems
        detail = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(f"Decision Pack {pack!r} is invalid:\n{detail}")


class PackNotFoundError(OpenHealthError):
    """A pack reference could not be resolved from any search path."""


class ProviderError(OpenHealthError):
    """A provider could not answer. `code` is a stable, audit-safe identifier."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class ReviewError(OpenHealthError):
    """A human-review action is not allowed (unknown item, already resolved, bad label)."""


class FhirError(OpenHealthError):
    """FHIR input is missing required resources or cannot be mapped to the pack input."""


class SmartError(OpenHealthError):
    """A SMART on FHIR launch cannot continue: not configured, a bad or expired state, a token
    the authorization server should not have issued, or no session."""

    def __init__(self, message: str, status: int = 400):
        self.status = status
        super().__init__(message)


class HubError(OpenHealthError):
    """A pack archive, signature, registry request or pull failed a check."""
