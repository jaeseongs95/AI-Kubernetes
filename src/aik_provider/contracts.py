"""AIK-011: vendor-independent auth/execute/observe/interrupt contracts.

Only non-secret account binding metadata crosses this boundary. These types
do not imply provider support, authorization, VM Task authority or product
qualification. Implementations must use supported, official provider paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Protocol, runtime_checkable

from .turns import PromptCacheCapability

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_REF = re.compile(r"(secret|host-session)://[a-z0-9][a-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9._/-]{0,191}\Z")
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")
_TOKEN_LIKE = re.compile(r"(?i)^(?:gh[pousr]_|github_pat_|sk_|AIza)|[A-Za-z0-9]{30,}")
_VERSION = 1


class AuthKind(StrEnum):
    API_KEY = "api_key"
    NATIVE_SUBSCRIPTION = "native_subscription"


class Support(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class AuthState(StrEnum):
    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"
    PENDING = "pending"
    EXPIRED = "expired"
    LOCKED = "locked"
    UNKNOWN = "unknown"


class AccountIsolation(StrEnum):
    ISOLATED = "isolated"
    SINGLE_ACTIVE = "single_active"
    UNKNOWN = "unknown"


class JobState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})


class FaultKind(StrEnum):
    INVALID_CONTRACT = "INVALID_CONTRACT"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ACCESS_DENIED = "ACCESS_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    UNSUPPORTED = "UNSUPPORTED"
    STALE_CONNECTION = "STALE_CONNECTION"
    ACCOUNT_CHANGED = "ACCOUNT_CHANGED"
    ACCOUNT_CONFLICT = "ACCOUNT_CONFLICT"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    INVALID_RECEIPT = "INVALID_RECEIPT"


class Outcome(StrEnum):
    KNOWN_FAILURE = "known_failure"
    UNKNOWN = "unknown"


def _id(value: object, field: str, *, provider: bool = False) -> None:
    pattern = _PROVIDER if provider else _ID
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError("INVALID_" + field.upper())


def _enum(value: object, enum_type: type[StrEnum], field: str) -> None:
    if type(value) is not enum_type:
        raise ValueError("INVALID_" + field.upper())


def _revision(value: object) -> None:
    if type(value) is not int or not 1 <= value < 2**63:
        raise ValueError("INVALID_CONNECTION_REVISION")


@dataclass(frozen=True, slots=True)
class ConnectionBinding:
    """Immutable identity; provider account fingerprint is not an access token."""

    provider: str
    runtime: str
    connection_id: str
    revision: int
    auth_kind: AuthKind
    credential_ref: str
    account_fingerprint: str

    def __post_init__(self) -> None:
        _id(self.provider, "provider", provider=True)
        _id(self.runtime, "runtime")
        _id(self.connection_id, "connection_id")
        _revision(self.revision)
        _enum(self.auth_kind, AuthKind, "auth_kind")
        if type(self.credential_ref) is not str or (m := _REF.fullmatch(self.credential_ref)) is None:
            raise ValueError("INVALID_CREDENTIAL_REFERENCE")
        if any(s in ("", ".", "..") for s in self.credential_ref.split("://", 1)[1].split("/")):
            raise ValueError("INVALID_CREDENTIAL_REFERENCE")
        expected = "secret" if self.auth_kind is AuthKind.API_KEY else "host-session"
        if m.group(1) != expected:
            raise ValueError("AUTH_KINDS_ARE_NOT_INTERCHANGEABLE")
        if type(self.account_fingerprint) is not str or _FINGERPRINT.fullmatch(self.account_fingerprint) is None:
            raise ValueError("INVALID_ACCOUNT_FINGERPRINT")


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """Implementation disclosures, not observed live qualification."""

    provider: str
    runtime: str
    auth_kind: AuthKind
    isolation: AccountIsolation
    auth_status: Support
    sign_in: Support
    sign_out: Support
    job_start: Support
    job_observe: Support
    job_interrupt: Support
    implementation_state: str = "implemented"
    qualification_status: str = "NOT_RUN"
    prompt_cache: PromptCacheCapability | None = None  # None: not disclosed (AIK-093)

    def __post_init__(self) -> None:
        _id(self.provider, "provider", provider=True)
        if self.prompt_cache is not None and not isinstance(self.prompt_cache, PromptCacheCapability):
            raise ValueError("INVALID_PROMPT_CACHE")
        _id(self.runtime, "runtime")
        _enum(self.auth_kind, AuthKind, "auth_kind")
        _enum(self.isolation, AccountIsolation, "isolation")
        for name in ("auth_status", "sign_in", "sign_out", "job_start", "job_observe", "job_interrupt"):
            _enum(getattr(self, name), Support, name)
        if self.implementation_state not in ("implemented", "not_implemented"):
            raise ValueError("INVALID_IMPLEMENTATION_STATE")
        if self.qualification_status != "NOT_RUN":
            # A provider may not self-certify a live route as qualified.
            raise ValueError("QUALIFICATION_REQUIRES_EXTERNAL_EVIDENCE")
        if self.auth_kind is AuthKind.API_KEY and self.sign_in is Support.SUPPORTED:
            raise ValueError("API_KEY_IS_NOT_HOST_SIGN_IN")


@dataclass(frozen=True, slots=True)
class AuthObservation:
    connection_id: str
    revision: int
    account_fingerprint: str | None
    state: AuthState

    def __post_init__(self) -> None:
        _id(self.connection_id, "connection_id")
        _revision(self.revision)
        _enum(self.state, AuthState, "auth_state")
        if self.account_fingerprint is not None and (
            type(self.account_fingerprint) is not str or
            _FINGERPRINT.fullmatch(self.account_fingerprint) is None
        ):
            raise ValueError("INVALID_ACCOUNT_FINGERPRINT")
        if self.state is AuthState.AUTHENTICATED and self.account_fingerprint is None:
            raise ValueError("AUTHENTICATED_ACCOUNT_NOT_OBSERVED")


@dataclass(frozen=True, slots=True)
class JobRequest:
    binding: ConnectionBinding
    request_id: str
    route_id: str
    vm_attempt_id: str
    model: str

    def __post_init__(self) -> None:
        if type(self.binding) is not ConnectionBinding:
            raise ValueError("INVALID_CONNECTION_BINDING")
        for field in ("request_id", "route_id", "vm_attempt_id", "model"):
            _id(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class JobReceipt:
    """Provider state is not an authoritative VM completion verdict."""

    request_id: str
    provider_job_id: str
    connection_id: str
    revision: int
    account_fingerprint: str
    state: JobState

    def __post_init__(self) -> None:
        for field in ("request_id", "provider_job_id", "connection_id"):
            _id(getattr(self, field), field)
        _revision(self.revision)
        if type(self.account_fingerprint) is not str or _FINGERPRINT.fullmatch(self.account_fingerprint) is None:
            raise ValueError("INVALID_ACCOUNT_FINGERPRINT")
        _enum(self.state, JobState, "job_state")


@dataclass(frozen=True, slots=True)
class ProviderFault:
    kind: FaultKind
    outcome: Outcome
    native_code: str | None = None
    http_status: int | None = None
    retry_after_ms: int | None = None

    def __post_init__(self) -> None:
        _enum(self.kind, FaultKind, "fault_kind")
        _enum(self.outcome, Outcome, "outcome")
        if self.kind is FaultKind.UNKNOWN_OUTCOME and self.outcome is not Outcome.UNKNOWN:
            raise ValueError("INCONSISTENT_OUTCOME")
        if self.native_code is not None and (
            type(self.native_code) is not str or _CODE.fullmatch(self.native_code) is None or
            _TOKEN_LIKE.search(self.native_code) is not None
        ):
            raise ValueError("UNSAFE_PROVIDER_CODE")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("INVALID_HTTP_STATUS")
        if self.retry_after_ms is not None and (
            type(self.retry_after_ms) is not int or not 0 <= self.retry_after_ms <= 86400000
        ):
            raise ValueError("INVALID_RETRY_AFTER")


class AdapterFailure(Exception):
    """Safe machine-readable failure; never formats a provider's raw response."""

    def __init__(self, fault: ProviderFault) -> None:
        if type(fault) is not ProviderFault:
            raise TypeError("ProviderFault required")
        self.fault = fault
        super().__init__(fault.kind.value)


def fault_from_provider(
    *,
    http_status: int | None = None,
    native_code: str | None = None,
    retry_after_ms: int | None = None,
    outcome_unknown: bool = False,
) -> ProviderFault:
    """Retain bounded vendor code/rate limit, but never a raw body or token."""
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        raise ValueError("INVALID_HTTP_STATUS")
    if retry_after_ms is not None and (
        type(retry_after_ms) is not int or not 0 <= retry_after_ms <= 86400000
    ):
        raise ValueError("INVALID_RETRY_AFTER")
    if native_code is not None and (
        type(native_code) is not str or _CODE.fullmatch(native_code) is None or
        _TOKEN_LIKE.search(native_code) is not None
    ):
        native_code = None  # raw vendor values must never enter diagnostics
    kind = (
        FaultKind.UNKNOWN_OUTCOME if outcome_unknown else
        FaultKind.AUTH_REQUIRED if http_status == 401 else
        FaultKind.ACCESS_DENIED if http_status == 403 else
        FaultKind.RATE_LIMITED if http_status == 429 else
        FaultKind.UNSUPPORTED if http_status in (404, 405, 501) else
        FaultKind.PROVIDER_FAILURE
    )
    return ProviderFault(
        kind=kind,
        outcome=Outcome.UNKNOWN if outcome_unknown else Outcome.KNOWN_FAILURE,
        native_code=native_code,
        http_status=http_status,
        retry_after_ms=retry_after_ms,
    )


@runtime_checkable
class ProviderAdapter(Protocol):
    def describe(self, binding: ConnectionBinding) -> AdapterCapabilities: ...
    def auth_status(self, binding: ConnectionBinding) -> AuthObservation: ...
    def begin_sign_in(self, binding: ConnectionBinding) -> AuthObservation: ...
    def sign_out(self, binding: ConnectionBinding) -> AuthObservation: ...
    def start_job(self, request: JobRequest) -> JobReceipt: ...
    def observe_job(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt: ...
    def interrupt_job(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt: ...


def require_caps(caps: object, binding: ConnectionBinding) -> AdapterCapabilities:
    if type(caps) is not AdapterCapabilities:
        raise AdapterFailure(ProviderFault(FaultKind.INVALID_CONTRACT, Outcome.KNOWN_FAILURE))
    if (caps.provider, caps.runtime, caps.auth_kind) != (
        binding.provider, binding.runtime, binding.auth_kind
    ):
        raise AdapterFailure(ProviderFault(FaultKind.STALE_CONNECTION, Outcome.KNOWN_FAILURE))
    return caps


def require_auth(observation: object, binding: ConnectionBinding) -> None:
    if type(observation) is not AuthObservation:
        raise AdapterFailure(ProviderFault(FaultKind.INVALID_CONTRACT, Outcome.KNOWN_FAILURE))
    if observation.connection_id != binding.connection_id or observation.revision != binding.revision:
        raise AdapterFailure(ProviderFault(FaultKind.STALE_CONNECTION, Outcome.KNOWN_FAILURE))
    if observation.state is not AuthState.AUTHENTICATED:
        raise AdapterFailure(ProviderFault(FaultKind.AUTH_REQUIRED, Outcome.KNOWN_FAILURE))
    if observation.account_fingerprint != binding.account_fingerprint:
        raise AdapterFailure(ProviderFault(FaultKind.ACCOUNT_CHANGED, Outcome.KNOWN_FAILURE))


def require_job(receipt: object, request: JobRequest) -> JobReceipt:
    if type(receipt) is not JobReceipt:
        raise AdapterFailure(ProviderFault(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN))
    if (receipt.request_id, receipt.connection_id, receipt.revision, receipt.account_fingerprint) != (
        request.request_id, request.binding.connection_id, request.binding.revision,
        request.binding.account_fingerprint,
    ):
        raise AdapterFailure(ProviderFault(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN))
    return receipt


__all__ = [
    "AuthKind", "Support", "AuthState", "AccountIsolation", "JobState", "TERMINAL",
    "FaultKind", "Outcome", "ConnectionBinding", "AdapterCapabilities", "AuthObservation",
    "JobRequest", "JobReceipt", "ProviderFault", "AdapterFailure", "ProviderAdapter",
    "fault_from_provider", "require_caps", "require_auth", "require_job",
]
