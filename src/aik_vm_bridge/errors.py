"""Bridge errors carry fixed codes and the VM's original code, never raw text."""

from __future__ import annotations

from enum import StrEnum


class BridgeCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_SURFACE = "INVALID_SURFACE"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    SEAM_UNAVAILABLE = "SEAM_UNAVAILABLE"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    LEASE_NOT_USABLE = "LEASE_NOT_USABLE"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    VM_REJECTED = "VM_REJECTED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    QUERY_FAILED = "QUERY_FAILED"


class EffectOutcome(StrEnum):
    """Whether a VM-side effect may have happened despite the failure."""

    KNOWN_FAILURE = "known_failure"
    UNKNOWN = "unknown"


class VmBridgeError(Exception):
    """A machine-readable failure.

    ``vm_code`` keeps the VM's original code (for example
    ``STALE_EXECUTION_INPUT`` or ``EXTERNAL_UNKNOWN``) unchanged. ``subject`` and
    ``details`` only contain names from this package's fixed tables.
    """

    def __init__(self, code, outcome=EffectOutcome.KNOWN_FAILURE, *, vm_code=None,
                 subject=None, details=()):
        self.code = BridgeCode(code)
        self.outcome = EffectOutcome(outcome)
        self.vm_code = vm_code
        self.subject = subject
        self.details = tuple(details)
        text = self.code.value
        if vm_code:
            text += f" vm_code={vm_code}"
        if subject:
            text += f" subject={subject}"
        super().__init__(text)
