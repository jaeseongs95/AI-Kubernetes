"""Pre-execution capability verification with requested and bound provenance.

The primary choice is validated against an aik_capability inventory. When it
is rejected, a fallback is tried only if a FallbackApproval names that exact
profile revision and fallback position. Fallbacks are never iterated
automatically. Inventory errors and malformed results reject (fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from aik_capability import CapabilityError, Provenance, ValidationResult
from aik_config.models import Revisioned
from aik_runtime.models import MAX_INTEGER

from .errors import CapabilityMismatch, InvalidProfile, ProfileError
from .models import (
    MAX_FALLBACKS, ExecutionChoice, check_profile_identifier, choice_to_dict, profile_to_dict, rebuild_profile,
)


EXECUTION_FORMAT = "aik.execution-binding"
EXECUTION_FORMAT_VERSION = 1
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class FallbackApproval:
    """Explicit approval of one fallback position of one profile revision."""

    profile_id: str
    profile_revision: int
    fallback_index: int
    approval_ref: str

    def __post_init__(self) -> None:
        check_profile_identifier(self.profile_id, "profile_id")
        check_profile_identifier(self.approval_ref, "approval_ref")
        if type(self.profile_revision) is not int or not 1 <= self.profile_revision <= MAX_INTEGER:
            raise InvalidProfile("Invalid positive integer field: profile_revision.")
        if type(self.fallback_index) is not int or not 0 <= self.fallback_index < MAX_FALLBACKS:
            raise InvalidProfile("Invalid fallback index.")


@dataclass(frozen=True, slots=True)
class CapabilityAttempt:
    position: str
    choice: ExecutionChoice
    accepted: bool
    reasons: tuple[str, ...]
    validation: ValidationResult | None


def _validation_dict(result: ValidationResult | None) -> dict | None:
    if result is None:
        return None
    return {
        "connection_id": result.connection_id,
        "connection_revision": result.connection_revision,
        "resource_revision": result.resource_revision,
        "model": result.model,
        "effort": result.effort,
        "tools": list(result.tools),
        "request_provenance": result.request_provenance.value,
        "accepted": result.accepted,
        "supported_by": None if result.supported_by is None else result.supported_by.value,
        "evidence_digest": result.evidence_digest,
        "reasons": list(result.reasons),
        "validated_at": result.validated_at,
    }


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    """Requested profile, actual binding (or none) and every capability attempt."""

    status: str
    requested_profile: object
    profile_revision: int
    bound: ExecutionChoice | None
    bound_position: str | None
    approval: FallbackApproval | None
    attempts: tuple[CapabilityAttempt, ...]
    reasons: tuple[str, ...]
    decided_at: str

    @property
    def profile_id(self) -> str:
        return self.requested_profile.id

    @property
    def role(self) -> str:
        return self.requested_profile.role

    @property
    def workspace_id(self) -> str:
        return self.requested_profile.workspace_id

    @property
    def requested(self) -> ExecutionChoice:
        return self.requested_profile.primary

    @property
    def bound_validation(self) -> ValidationResult | None:
        for attempt in self.attempts:
            if attempt.accepted and attempt.position == self.bound_position:
                return attempt.validation
        return None

    def require(self) -> "ExecutionDecision":
        if self.status != "bound" or self.bound is None:
            raise CapabilityMismatch("The requested execution profile has no supported binding.", decision=self)
        return self

    def to_document(self) -> dict:
        bound = self.bound_validation
        return {
            "format": EXECUTION_FORMAT,
            "format_version": EXECUTION_FORMAT_VERSION,
            "status": self.status,
            "requested_profile": profile_to_dict(self.requested_profile),
            "profile_revision": self.profile_revision,
            "requested": choice_to_dict(self.requested),
            "bound": None if self.bound is None else choice_to_dict(self.bound),
            "bound_position": self.bound_position,
            "binding_provenance": None if bound is None else {
                "supported_by": bound.supported_by.value,
                "evidence_digest": bound.evidence_digest,
                "connection_revision": bound.connection_revision,
                "resource_revision": bound.resource_revision,
                "validated_at": bound.validated_at,
            },
            "approval": None if self.approval is None else {
                "profile_id": self.approval.profile_id,
                "profile_revision": self.approval.profile_revision,
                "fallback_index": self.approval.fallback_index,
                "approval_ref": self.approval.approval_ref,
            },
            "attempts": [
                {"position": attempt.position, "choice": choice_to_dict(attempt.choice),
                 "accepted": attempt.accepted, "reasons": list(attempt.reasons),
                 "validation": _validation_dict(attempt.validation)}
                for attempt in self.attempts
            ],
            "reasons": list(self.reasons),
            "decided_at": self.decided_at,
        }


def _now(clock) -> str:
    if clock is None:
        value = datetime.now(timezone.utc)
    else:
        if not callable(clock):
            raise ProfileError("The execution clock must be callable.")
        try:
            value = clock()
        except Exception:
            raise ProfileError("The execution clock is unavailable.") from None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProfileError("The execution clock must return a timezone-aware datetime.")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _entry(entry) -> tuple:
    if type(entry) is not Revisioned:
        raise InvalidProfile("Execution requires a stored, revisioned profile.")
    profile = rebuild_profile(entry.value)
    if type(entry.revision) is not int or not 1 <= entry.revision <= MAX_INTEGER:
        raise InvalidProfile("Execution requires a positive profile revision.")
    return profile, entry.revision


def validate_approval(approval) -> FallbackApproval | None:
    if approval is None:
        return None
    if type(approval) is not FallbackApproval:
        raise InvalidProfile("A fallback approval must be a FallbackApproval.")
    return FallbackApproval(approval.profile_id, approval.profile_revision, approval.fallback_index,
                            approval.approval_ref)


def _attempt(position: str, choice: ExecutionChoice, inventory) -> CapabilityAttempt:
    try:
        result = inventory.validate(choice.connection_id, choice.model, choice.effort, choice.tools)
    except CapabilityError as error:
        code = error.args[0] if error.args and type(error.args[0]) is str else ""
        code = code if _CODE.fullmatch(code) else "UNCLASSIFIED"
        return CapabilityAttempt(position, choice, False, ("CAPABILITY_ERROR:" + code,), None)
    except Exception:
        return CapabilityAttempt(position, choice, False, ("INVENTORY_UNAVAILABLE",), None)
    if type(result) is not ValidationResult or (
        result.connection_id, result.model, result.effort, tuple(result.tools),
    ) != (choice.connection_id, choice.model, choice.effort, choice.tools):
        return CapabilityAttempt(position, choice, False, ("VALIDATION_RESULT_MISMATCH",), None)
    reasons = tuple(str(reason) for reason in result.reasons)
    evidence = result.supported_by is Provenance.PROVIDER_OBSERVED or \
        result.supported_by is Provenance.CONFIGURED_CATALOG
    accepted = result.accepted is True and evidence and result.request_provenance is Provenance.REQUESTED
    if result.accepted is True and not accepted:
        reasons += ("NO_SUPPORT_EVIDENCE",)
    return CapabilityAttempt(position, choice, accepted, reasons, result)


def rejected_decision(entry, reasons, *, clock=None) -> ExecutionDecision:
    """A rejection decided before any capability check (e.g. dangling references)."""
    profile, revision = _entry(entry)
    return ExecutionDecision("rejected", profile, revision, None, None, None, (), tuple(reasons), _now(clock))


def verify_execution(entry, inventory, *, approval=None, clock=None) -> ExecutionDecision:
    """Validate the primary choice, or one explicitly approved fallback."""
    profile, revision = _entry(entry)
    approval = validate_approval(approval)
    decided_at = _now(clock)
    primary = _attempt("primary", profile.primary, inventory)
    attempts = [primary]
    reasons: list[str] = []
    if primary.accepted:
        if approval is not None:
            reasons.append("FALLBACK_APPROVAL_UNUSED")
        return ExecutionDecision("bound", profile, revision, profile.primary, "primary", None,
                                 tuple(attempts), tuple(reasons), decided_at)
    reasons.append("PRIMARY_REJECTED")
    if approval is None:
        if profile.fallbacks:
            reasons.append("FALLBACK_NOT_APPROVED")
    elif approval.profile_id != profile.id:
        reasons.append("FALLBACK_APPROVAL_PROFILE_MISMATCH")
    elif approval.profile_revision != revision:
        reasons.append("FALLBACK_APPROVAL_STALE_REVISION")
    elif approval.fallback_index >= len(profile.fallbacks):
        reasons.append("FALLBACK_INDEX_OUT_OF_RANGE")
    else:
        position = f"fallback:{approval.fallback_index}"
        choice = profile.fallbacks[approval.fallback_index]
        attempt = _attempt(position, choice, inventory)
        attempts.append(attempt)
        if attempt.accepted:
            return ExecutionDecision("bound", profile, revision, choice, position, approval,
                                     tuple(attempts), tuple(reasons), decided_at)
        reasons.append("APPROVED_FALLBACK_REJECTED")
    return ExecutionDecision("rejected", profile, revision, None, None, None, tuple(attempts), tuple(reasons),
                             decided_at)
