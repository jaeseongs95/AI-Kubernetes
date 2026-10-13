"""AIK-040 budget, quota and usage records. No field holds secret material.

Every usage amount has an explicit basis: confirmed, estimated or unknown.
An unknown amount has no value. Nothing is filled with zero or an estimate.
API cost (integer minor currency units), tokens and provider-reported
subscription quota are separate units. They are never converted into one
another.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
import re

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier


BASES = ("confirmed", "estimated", "unknown")
ADMISSION_STATUSES = ("admitted", "wait", "rejected")
MAX_SCOPES = 16
MAX_SECONDS = 366 * 86400
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")


class BudgetError(ValueError):
    """Fixed code-only failure; the message never contains supplied values."""


def check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise BudgetError("INVALID_" + field.upper()) from None


def check_count(value: object, field: str, *, minimum: int = 1, maximum: int = MAX_INTEGER,
                optional: bool = True) -> None:
    if value is None and optional:
        return
    if type(value) is not int or not minimum <= value <= maximum:
        raise BudgetError("INVALID_" + field.upper())


def parse_timestamp(value: object, field: str) -> datetime:
    if type(value) is not str:
        raise BudgetError("INVALID_" + field.upper())
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise BudgetError("INVALID_" + field.upper()) from None


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value: object, field: str) -> str:
    return format_timestamp(parse_timestamp(value, field))


def rebuild(value, model):
    """Reconstruct exact model instances so object.__setattr__ forgeries are revalidated."""
    if type(value) is not model:
        raise BudgetError("INVALID_" + model.__name__.upper())
    return model(**{item.name: getattr(value, item.name) for item in fields(model)})


# ------------------------------------------------------------------ amounts

@dataclass(frozen=True, slots=True)
class Amount:
    """A non-negative integer with its basis; unknown means no value at all."""

    value: int | None = None
    basis: str = "unknown"

    def __post_init__(self) -> None:
        if type(self.basis) is not str or self.basis not in BASES:
            raise BudgetError("INVALID_BASIS")
        if self.basis == "unknown":
            if self.value is not None:
                raise BudgetError("UNKNOWN_AMOUNT_HAS_VALUE")
        else:
            check_count(self.value, "amount", minimum=0, optional=False)

    @property
    def known(self) -> bool:
        return self.basis != "unknown"

    def to_dict(self) -> dict:
        return {"value": self.value, "basis": self.basis}


UNKNOWN = Amount()


def confirmed(value: int) -> Amount:
    return Amount(value, "confirmed")


def estimated(value: int) -> Amount:
    return Amount(value, "estimated")


def amount_from_dict(value: object) -> Amount:
    if type(value) is not dict or set(value) != {"value", "basis"}:
        raise BudgetError("INVALID_AMOUNT")
    return Amount(value["value"], value["basis"])


_USAGE_FIELDS = ("api_cost_cents", "input_tokens", "output_tokens", "quota_used")


@dataclass(frozen=True, slots=True)
class Usage:
    """Reported usage of one call. Every field defaults to unknown, never to zero.

    quota_used is the provider-reported subscription quota consumption in
    quota_unit. It is never derived from tokens, prices or account usage rates.
    """

    api_cost_cents: Amount = UNKNOWN
    input_tokens: Amount = UNKNOWN
    output_tokens: Amount = UNKNOWN
    quota_used: Amount = UNKNOWN
    quota_unit: str | None = None

    def __post_init__(self) -> None:
        for name in _USAGE_FIELDS:
            value = getattr(self, name)
            if type(value) is not Amount:
                raise BudgetError("INVALID_" + name.upper())
            object.__setattr__(self, name, Amount(value.value, value.basis))
        if self.quota_unit is not None:
            check_id(self.quota_unit, "quota_unit")
        if self.quota_used.known and self.quota_unit is None:
            raise BudgetError("QUOTA_UNIT_REQUIRED")

    def to_dict(self) -> dict:
        document = {name: getattr(self, name).to_dict() for name in _USAGE_FIELDS}
        document["quota_unit"] = self.quota_unit
        return document


def usage_from_dict(value: object) -> Usage:
    if type(value) is not dict or set(value) != {*_USAGE_FIELDS, "quota_unit"}:
        raise BudgetError("INVALID_USAGE")
    return Usage(**{name: amount_from_dict(value[name]) for name in _USAGE_FIELDS},
                 quota_unit=value["quota_unit"])


# ------------------------------------------------------------------ policies

@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Limits for one resource or goal scope. None means "no limit of this kind".

    max_calls and deadline are deterministic hard stops. token_stop is an
    opt-in, best-effort stop based only on observed (reported) token counts.
    """

    scope_id: str
    max_api_cost_cents: int | None = None
    max_calls: int | None = None
    deadline: str | None = None
    max_concurrency: int | None = None
    token_stop: int | None = None

    def __post_init__(self) -> None:
        check_id(self.scope_id, "scope_id")
        check_count(self.max_api_cost_cents, "max_api_cost_cents", minimum=0)
        check_count(self.max_calls, "max_calls")
        check_count(self.max_concurrency, "max_concurrency")
        check_count(self.token_stop, "token_stop")
        if self.deadline is not None:
            object.__setattr__(self, "deadline", normalize_timestamp(self.deadline, "deadline"))

    def to_dict(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(BudgetPolicy)}


def policy_from_dict(value: object) -> BudgetPolicy:
    if type(value) is not dict or set(value) != {item.name for item in fields(BudgetPolicy)}:
        raise BudgetError("INVALID_POLICY")
    return BudgetPolicy(**value)


@dataclass(frozen=True, slots=True)
class StoredPolicy:
    policy: BudgetPolicy
    revision: int


# ------------------------------------------------------------------ admission

@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """One planned call. max_seconds bounds its run time; scopes name stored policies."""

    reservation_id: str
    pool_id: str
    model: str
    max_seconds: int
    scopes: tuple[str, ...] = ()
    api_cost_estimate_cents: int | None = None

    def __post_init__(self) -> None:
        check_id(self.reservation_id, "reservation_id")
        check_id(self.pool_id, "pool_id")
        if type(self.model) is not str or _MODEL.fullmatch(self.model) is None:
            raise BudgetError("INVALID_MODEL")
        check_count(self.max_seconds, "max_seconds", maximum=MAX_SECONDS, optional=False)
        if type(self.scopes) is not tuple or len(self.scopes) > MAX_SCOPES:
            raise BudgetError("INVALID_SCOPES")
        for scope in self.scopes:
            check_id(scope, "scope_id")
        if len(set(self.scopes)) != len(self.scopes):
            raise BudgetError("DUPLICATE_SCOPE")
        check_count(self.api_cost_estimate_cents, "api_cost_estimate_cents", minimum=0)

    def to_dict(self) -> dict:
        return {"reservation_id": self.reservation_id, "pool_id": self.pool_id, "model": self.model,
                "max_seconds": self.max_seconds, "scopes": list(self.scopes),
                "api_cost_estimate_cents": self.api_cost_estimate_cents}


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """admitted (reservation created), wait (retry later) or rejected (a limit is reached)."""

    status: str
    reservation_id: str | None
    request_id: str
    profile_id: str
    role: str
    pool_id: str
    reasons: tuple[str, ...]
    retry_at: str | None
    decided_at: str

    @property
    def admitted(self) -> bool:
        return self.status == "admitted"

    def to_dict(self) -> dict:
        return {item.name: (list(getattr(self, item.name)) if item.name == "reasons" else getattr(self, item.name))
                for item in fields(AdmissionDecision)}


# ------------------------------------------------------------------ status views

@dataclass(frozen=True, slots=True)
class CostTotals:
    """API cost in integer minor units, separated by basis.

    reserved_cents are holds of active reservations. unknown_held_cents keep
    the reservation estimate of settled calls whose actual cost was not
    reported; the estimate is not presented as the actual cost.
    """

    confirmed_cents: int
    estimated_cents: int
    reserved_cents: int
    unknown_held_cents: int
    unknown_records: int

    @property
    def committed_cents(self) -> int:
        return self.confirmed_cents + self.estimated_cents + self.reserved_cents + self.unknown_held_cents


@dataclass(frozen=True, slots=True)
class TokenTotals:
    """Sum of reported token counts only; unknown_records > 0 makes it a lower bound."""

    observed: int
    unknown_records: int
    policy: str = "best_effort_observed"


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    """Provider-reported subscription quota consumption in its own unit."""

    unit: str
    confirmed: int
    estimated: int


@dataclass(frozen=True, slots=True)
class ScopeStatus:
    scope_id: str
    configured: bool
    calls: int
    active: int
    api_cost: CostTotals
    tokens: TokenTotals
    quota_used: tuple[QuotaUsage, ...]
    quota_unreported_records: int
    state: str  # open, limit_reached, over_limit, unconfigured
    reasons: tuple[str, ...]
    observed_at: str


@dataclass(frozen=True, slots=True)
class QuotaObservation:
    """Provider-reported remaining subscription quota; remaining None means not reported."""

    unit: str
    remaining: int | None
    limit: int | None
    resets_at: str | None
    observed_at: str
    freshness: str


@dataclass(frozen=True, slots=True)
class PoolStatus:
    pool_id: str
    active: int
    rate_limited: bool
    retry_at: str | None
    quota: tuple[QuotaObservation, ...]
    observed_at: str


@dataclass(frozen=True, slots=True)
class SettlementRecord:
    seq: int
    reservation_id: str
    kind: str  # settlement, release, late
    recorded_at: str
    usage: Usage | None


@dataclass(frozen=True, slots=True)
class Reservation:
    id: str
    pool_id: str
    profile_id: str
    role: str
    state: str
    created_at: str
    run_deadline: str
    api_cost_estimate_cents: int | None
    scopes: tuple[str, ...]
    overdue: bool
