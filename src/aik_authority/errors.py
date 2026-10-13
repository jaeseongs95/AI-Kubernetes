"""Authority errors carry fixed codes and field names, never supplied values."""

from __future__ import annotations

from enum import StrEnum


class AuthorityCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_POLICY = "INVALID_POLICY"
    UNKNOWN_SESSION = "UNKNOWN_SESSION"
    SESSION_CLOSED = "SESSION_CLOSED"
    POLICY_UNKNOWN = "POLICY_UNKNOWN"
    TOOL_NOT_DECLARED = "TOOL_NOT_DECLARED"
    RESERVED_TOOL = "RESERVED_TOOL"
    TOOL_NOT_ALLOWED_FOR_ROLE = "TOOL_NOT_ALLOWED_FOR_ROLE"
    TOOL_NOT_IN_PROFILE = "TOOL_NOT_IN_PROFILE"
    TOOL_NOT_IN_AUTHORIZATION = "TOOL_NOT_IN_AUTHORIZATION"
    SCOPE_ESCALATION = "SCOPE_ESCALATION"
    ROLE_MISMATCH = "ROLE_MISMATCH"
    ROLE_CANNOT_DELEGATE = "ROLE_CANNOT_DELEGATE"
    WORKSPACE_MISMATCH = "WORKSPACE_MISMATCH"
    PROFILE_NOT_BOUND = "PROFILE_NOT_BOUND"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    BROKER_SCOPE_MISMATCH = "BROKER_SCOPE_MISMATCH"
    CREDENTIAL_SCOPE_MISSING = "CREDENTIAL_SCOPE_MISSING"
    CONNECTION_OUT_OF_SCOPE = "CONNECTION_OUT_OF_SCOPE"
    AUTHORIZATION_UNKNOWN = "AUTHORIZATION_UNKNOWN"
    AUTHORIZATION_EVIDENCE_MISSING = "AUTHORIZATION_EVIDENCE_MISSING"
    AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
    AUTHORIZATION_WITHDRAWN = "AUTHORIZATION_WITHDRAWN"
    STALE_AUTHORIZATION_REVISION = "STALE_AUTHORIZATION_REVISION"
    STALE_INPUT_REVISION = "STALE_INPUT_REVISION"
    TARGET_OUT_OF_SCOPE = "TARGET_OUT_OF_SCOPE"
    EFFECT_OUT_OF_SCOPE = "EFFECT_OUT_OF_SCOPE"
    LEASE_INACTIVE = "LEASE_INACTIVE"
    CLOCK_UNAVAILABLE = "CLOCK_UNAVAILABLE"
    TOOL_CALL_UNKNOWN = "TOOL_CALL_UNKNOWN"
    TOOL_CALL_CONFLICT = "TOOL_CALL_CONFLICT"
    EFFECT_ID_CONFLICT = "EFFECT_ID_CONFLICT"
    EFFECT_ALREADY_AUTHORIZED = "EFFECT_ALREADY_AUTHORIZED"
    EFFECT_UNKNOWN = "EFFECT_UNKNOWN"
    SEAM_UNAVAILABLE = "SEAM_UNAVAILABLE"
    VM_REJECTED = "VM_REJECTED"
    INTENT_UNKNOWN = "INTENT_UNKNOWN"
    RECEIPT_UNKNOWN = "RECEIPT_UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


# A request outside the authorized scope is an escalation: it needs a new VM
# authorization (user approval through VM). The gateway never self-grants it.
ESCALATION_CODES = frozenset({
    AuthorityCode.TOOL_NOT_IN_AUTHORIZATION, AuthorityCode.TARGET_OUT_OF_SCOPE,
    AuthorityCode.EFFECT_OUT_OF_SCOPE, AuthorityCode.SCOPE_ESCALATION,
})

# Assumed correspondence to VM public-contract codes. Not verified against an
# installed VM (NOT_RUN); callers must treat it as advisory classification.
VM_CONTRACT = {
    AuthorityCode.STALE_INPUT_REVISION: "STALE_EXECUTION_INPUT",
    AuthorityCode.STALE_AUTHORIZATION_REVISION: "STALE_EXECUTION_INPUT",
    AuthorityCode.TOOL_NOT_IN_AUTHORIZATION: "CORE_CAPABILITY_DENIED",
    AuthorityCode.TARGET_OUT_OF_SCOPE: "CORE_CAPABILITY_DENIED",
    AuthorityCode.EFFECT_OUT_OF_SCOPE: "CORE_CAPABILITY_DENIED",
    AuthorityCode.SCOPE_ESCALATION: "CORE_CAPABILITY_DENIED",
}


class AuthorityError(Exception):
    """A machine-readable authority failure.

    ``subject`` and ``details`` contain only field names or validated tool
    identifiers. ``vm_code`` keeps a VM-supplied safe code unchanged.
    """

    def __init__(self, code, *, subject=None, vm_code=None, details=()):
        self.code = AuthorityCode(code)
        self.subject = subject
        self.vm_code = vm_code
        self.details = tuple(details)
        self.escalation = self.code in ESCALATION_CODES
        self.vm_contract = VM_CONTRACT.get(self.code)
        text = self.code.value
        if vm_code:
            text += f" vm_code={vm_code}"
        if subject:
            text += f" subject={subject}"
        super().__init__(text)


class AuthorityDenied(AuthorityError):
    """The request was refused; no permission was issued."""


class EffectOutcomeUnknown(AuthorityError):
    """A VM effect-authority call has an unclear result; reconciliation is required."""


class InvalidPolicy(AuthorityError, ValueError):
    """A tool declaration or policy violates its contract."""

    def __init__(self, code=AuthorityCode.INVALID_POLICY, **kwargs):
        super().__init__(code, **kwargs)
