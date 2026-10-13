"""Switch policy, requests, events and outcomes. IDs, codes and model names only."""

from __future__ import annotations

from dataclasses import dataclass, fields
import re

from aik_continuity import HandoffPacket
from aik_director.models import DirectorBinding, RunnerSession
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier

from .errors import InvalidSwitch


SWITCH_REASONS = ("user_request", "auth_expired", "auth_locked", "provider_unavailable", "capability_lost")
AUTH_REASONS = ("auth_expired", "auth_locked")
CALL_TERMINAL_KINDS = ("completed", "failed", "cancelled")
SWITCH_STATES = ("active", "switch_pending", "reauth_required", "decision_required")
BLOCKED_STATES = ("reauth_required", "decision_required")
MAX_QUALITY = 100
MAX_SCOPES = 64
MAX_TOOLS = 64
MAX_RULES = 9
MAX_CONTEXT = 2**31 - 1
_POSITION = re.compile(r"(?:primary|fallback:[0-7])\Z")


def check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise InvalidSwitch(f"Invalid identifier field: {field}.") from None


def check_position(value: object) -> None:
    if type(value) is not str or _POSITION.fullmatch(value) is None:
        raise InvalidSwitch("A profile position must be 'primary' or 'fallback:<0-7>'.")


def _bounded(value: object, field: str, minimum: int, maximum: int = MAX_INTEGER) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidSwitch(f"Invalid integer field: {field}.")


def _id_tuple(value: object, field: str, maximum: int, *, casefold: bool = False) -> None:
    if type(value) is not tuple or len(value) > maximum:
        raise InvalidSwitch(f"The {field} must be a bounded tuple.")
    for item in value:
        check_id(item, field)
    keys = [item.casefold() for item in value] if casefold else list(value)
    if len(set(keys)) != len(keys):
        raise InvalidSwitch(f"The {field} must not repeat values.")


def rebuild(value):
    """Reconstruct a frozen record: object.__setattr__ can bypass __post_init__."""
    return type(value)(**{field.name: getattr(value, field.name) for field in fields(value)})


@dataclass(frozen=True, slots=True)
class FallbackRule:
    """User-assigned limits for one allowed profile position.

    quality is the user's rank (1..100) of that resource; estimated_cost is the
    per-call estimate in the profile budget unit; data_scopes lists the data
    scope labels (for example workspace or connection scopes) it may receive.
    """

    position: str
    quality: int
    estimated_cost: int = 0
    data_scopes: tuple = ()

    def __post_init__(self) -> None:
        check_position(self.position)
        _bounded(self.quality, "quality", 1, MAX_QUALITY)
        _bounded(self.estimated_cost, "estimated_cost", 0)
        _id_tuple(self.data_scopes, "data_scopes", MAX_SCOPES)


@dataclass(frozen=True, slots=True)
class SwitchPolicy:
    """The user's fallback permission for one director profile revision."""

    profile_id: str
    profile_revision: int
    approval_ref: str
    data_scope: str
    rules: tuple = ()
    min_quality: int = 1
    required_tools: tuple = ()
    min_context: int | None = None
    max_cost_per_call: int | None = None
    fallback_on_auth: bool = False
    require_checkpoint: bool = True

    def __post_init__(self) -> None:
        check_id(self.profile_id, "profile_id")
        _bounded(self.profile_revision, "profile_revision", 1)
        check_id(self.approval_ref, "approval_ref")
        check_id(self.data_scope, "data_scope")
        if type(self.rules) is not tuple or len(self.rules) > MAX_RULES:
            raise InvalidSwitch("Policy rules must be a bounded tuple.")
        rules = []
        for rule in self.rules:
            if type(rule) is not FallbackRule:
                raise InvalidSwitch("Policy rules must be FallbackRule records.")
            rules.append(rebuild(rule))
        if len({rule.position for rule in rules}) != len(rules):
            raise InvalidSwitch("A policy names each position at most once.")
        object.__setattr__(self, "rules", tuple(rules))
        _bounded(self.min_quality, "min_quality", 1, MAX_QUALITY)
        _id_tuple(self.required_tools, "required_tools", MAX_TOOLS, casefold=True)
        if self.min_context is not None:
            _bounded(self.min_context, "min_context", 1, MAX_CONTEXT)
        if self.max_cost_per_call is not None:
            _bounded(self.max_cost_per_call, "max_cost_per_call", 0)
        if type(self.fallback_on_auth) is not bool or type(self.require_checkpoint) is not bool:
            raise InvalidSwitch("Policy flags must be booleans.")

    def rule(self, position: str) -> FallbackRule | None:
        for rule in self.rules:
            if rule.position == position:
                return rule
        return None


@dataclass(frozen=True, slots=True)
class SwitchRequest:
    """user_request names its target position; failure reasons select automatically."""

    event_id: str
    reason: str
    target: str | None = None
    budget_remaining: int | None = None

    def __post_init__(self) -> None:
        check_id(self.event_id, "event_id")
        if type(self.reason) is not str or self.reason not in SWITCH_REASONS:
            raise InvalidSwitch("Unknown switch reason.")
        if self.reason == "user_request":
            check_position(self.target)
        elif self.target is not None:
            raise InvalidSwitch("Only a user request names a target position.")
        if self.budget_remaining is not None:
            _bounded(self.budget_remaining, "budget_remaining", 0)


@dataclass(frozen=True, slots=True)
class CallEvent:
    """A terminal observation of one in-flight call (the switch application point)."""

    event_id: str
    call_id: str
    kind: str

    def __post_init__(self) -> None:
        check_id(self.event_id, "event_id")
        check_id(self.call_id, "call_id")
        if type(self.kind) is not str or self.kind not in CALL_TERMINAL_KINDS:
            raise InvalidSwitch("A call event must be completed, failed or cancelled.")


@dataclass(frozen=True, slots=True)
class CandidateCheck:
    position: str
    accepted: bool
    reasons: tuple
    supported_by: str | None = None
    evidence_digest: str | None = None

    def to_dict(self) -> dict:
        return {"position": self.position, "accepted": self.accepted, "reasons": list(self.reasons),
                "supported_by": self.supported_by, "evidence_digest": self.evidence_digest}


def binding_to_dict(binding: DirectorBinding) -> dict:
    return {
        "profile_id": binding.profile_id, "profile_revision": binding.profile_revision,
        "connection_id": binding.connection_id, "model": binding.model, "effort": binding.effort,
        "tools": list(binding.tools), "position": binding.position,
    }


@dataclass(frozen=True, slots=True)
class SwitchOutcome:
    """status: applied, pending, decision_required, reauth_required, rejected,
    recheck_failed, activation_failed, resumed or duplicate.

    binding/state are the controller values after this operation. record is the
    stored request document; apply_record the stored application document.
    """

    status: str
    event_id: str
    state: str
    binding: DirectorBinding
    record: dict
    apply_record: dict | None = None
    session: RunnerSession | None = None
    handoff: HandoffPacket | None = None


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """status: recorded, duplicate, already_terminal or unknown_call."""

    status: str
    event_id: str
    call_id: str
    kind: str
    switch: SwitchOutcome | None = None
