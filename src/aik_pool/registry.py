"""AIK-019 resource inventory: health probe, TTL refresh, qualification levels.

A route is one (aik_runtime.Pool, model) pair. Each Pool keeps its own pool_id
and connection_id. This registry does not modify aik_provider or aik_runtime.
It only calls the read-only describe/auth_status operations through
GuardedAdapter, and never starts, interrupts, signs in or signs out of a job.

Principles:
- An adapter implementation alone is not an advertisement that a route is
  executable now. executable_now is a probe-based technical availability
  flag, not an execution authorization; a rejected governance verdict always
  clears it. Automatic placement uses placement_eligible only.
- Values that are missing, cannot be verified or have passed the TTL are
  shown as unknown or stale. They are never turned into 0 or unlimited capacity.
- The qualified level requires live evidence approved by an injected external
  verifier. With no verifier, or with any non-live record, it is rejected.
- A governance approval belongs to the binding and matrix it was given under.
  A new binding revision or matrix revision revokes it; a rejection is kept.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import math
import re
from threading import RLock

from aik_provider import (
    AccountIsolation, AdapterFailure, AuthKind, AuthState, ConnectionBinding,
    FaultKind, GuardedAdapter, Support,
)
from aik_runtime import InvalidRecord, Pool
from aik_runtime.models import identifier


_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REVISION = 2**63 - 1
_MAX_TOOLS = 64


class PoolRegistryError(ValueError):
    """Fixed code-only failure; never contains provider text or secrets."""


class UnknownRoute(PoolRegistryError):
    """The requested pool/model route is not registered."""


class QualificationRejected(PoolRegistryError):
    """Qualification evidence was not accepted; the route stays unqualified."""


class QualificationLevel(StrEnum):
    UNKNOWN = "unknown"
    NOT_IMPLEMENTED = "not_implemented"
    IMPLEMENTED = "implemented"
    PROBE_VERIFIED = "probe_verified"
    CANDIDATE_FOR_QUALIFICATION = "candidate_for_qualification"
    QUALIFIED = "qualified"


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class Governance(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


_RUNNABLE_LEVELS = frozenset({
    QualificationLevel.PROBE_VERIFIED,
    QualificationLevel.CANDIDATE_FOR_QUALIFICATION,
    QualificationLevel.QUALIFIED,
})

_ACCOUNT_FAULTS = {
    FaultKind.ACCOUNT_CHANGED: "account_changed",
    FaultKind.STALE_CONNECTION: "stale_binding",
    FaultKind.AUTH_REQUIRED: "unauthenticated",
}


def _check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise PoolRegistryError("INVALID_" + field.upper()) from None


def _check_revision(value: object, field: str) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise PoolRegistryError("INVALID_" + field.upper())


def _check_model(value: object) -> None:
    if type(value) is not str or _MODEL.fullmatch(value) is None:
        raise PoolRegistryError("INVALID_MODEL")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class QualificationEvidence:
    """A reference to an external qualification result, without its body.

    No field has a default value: a caller must label the kind and verdict
    explicitly. The registry accepts only evidence_kind="live" and
    verdict="PASS", and only when the injected verifier returns True.
    """

    pool_id: str
    model: str
    connection_id: str
    connection_revision: int
    account_fingerprint: str
    runtime: str
    matrix_revision: int
    evidence_ref: str
    evidence_kind: str
    verdict: str

    def __post_init__(self) -> None:
        _check_id(self.pool_id, "pool_id")
        _check_model(self.model)
        _check_id(self.connection_id, "connection_id")
        _check_revision(self.connection_revision, "connection_revision")
        if type(self.account_fingerprint) is not str or _FINGERPRINT.fullmatch(self.account_fingerprint) is None:
            raise PoolRegistryError("INVALID_ACCOUNT_FINGERPRINT")
        _check_id(self.runtime, "runtime")
        _check_revision(self.matrix_revision, "matrix_revision")
        _check_id(self.evidence_ref, "evidence_ref")
        _check_id(self.evidence_kind, "evidence_kind")
        _check_id(self.verdict, "verdict")


@dataclass(frozen=True, slots=True)
class RouteStatus:
    """A snapshot that keeps each observation and judgment in its own field."""

    pool_id: str
    connection_id: str
    connection_revision: int
    provider: str
    host: str
    auth_kind: str
    model: str
    tools: tuple[str, ...]
    pool_enabled: bool
    configured_max_parallel: int
    implementation_state: str
    account_binding: str
    runtime_support: str
    account_isolation: str
    governance: str
    last_probe_at: str | None
    last_verified_at: str | None
    freshness: str
    matrix_revision: int
    qualification_matrix_revision: int | None
    capacity_state: str
    available_capacity: int | None
    level: QualificationLevel
    executable_now: bool
    placement_eligible: bool


@dataclass(frozen=True, slots=True)
class _Observation:
    observed_at: datetime
    implementation: str
    runtime: str
    account: str
    isolation: str
    capacity: int | None


class _Route:
    __slots__ = ("pool", "binding", "adapter", "guard", "model", "tools",
                 "governance", "observation", "last_verified", "qualification")

    def __init__(self, pool: Pool, binding: ConnectionBinding, adapter, guard: GuardedAdapter,
                 model: str, tools: tuple[str, ...]):
        self.pool = pool
        self.binding = binding
        self.adapter = adapter
        self.guard = guard
        self.model = model
        self.tools = tools
        self.governance = Governance.UNKNOWN
        self.observation: _Observation | None = None
        self.last_verified: datetime | None = None
        self.qualification: QualificationEvidence | None = None

    def revoke_approval(self) -> None:
        """An approval is tied to its binding and matrix; a rejection is kept (fail-closed)."""
        if self.governance is Governance.APPROVED:
            self.governance = Governance.UNKNOWN

    def reset(self) -> None:
        self.observation = None
        self.last_verified = None
        self.qualification = None
        self.revoke_approval()


def _runtime_support(caps) -> str:
    features = (caps.auth_status, caps.job_start, caps.job_observe, caps.job_interrupt)
    if any(feature is Support.UNSUPPORTED for feature in features):
        return "unsupported"
    if not all(feature is Support.SUPPORTED for feature in features):
        return "unknown"
    if caps.auth_kind is AuthKind.NATIVE_SUBSCRIPTION and caps.isolation is AccountIsolation.UNKNOWN:
        # GuardedAdapter refuses to start a native job in an unknown context.
        return "unknown"
    return "supported"


def _guard(adapter) -> GuardedAdapter:
    try:
        return GuardedAdapter(adapter)
    except TypeError:
        raise PoolRegistryError("INVALID_ADAPTER") from None


class ResourcePoolRegistry:
    """Inventory of pool/model routes, with an injectable clock and TTL.

    All operations run under one lock. A probe calls the read-only adapter
    operations while it holds that lock, so a slow provider delays other
    callers of this registry. Mutating operations read the clock before they
    change any state, so a clock failure leaves the registry unchanged.
    """

    def __init__(self, *, matrix_revision: int, ttl_seconds: float = 300.0, clock=None,
                 capacity_probe=None, qualification_verifier=None):
        _check_revision(matrix_revision, "matrix_revision")
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 86400):
            raise PoolRegistryError("INVALID_TTL")
        for callback in (clock, capacity_probe, qualification_verifier):
            if callback is not None and not callable(callback):
                raise PoolRegistryError("INVALID_CALLBACK")
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._ttl = timedelta(seconds=float(ttl_seconds))
        self._capacity_probe = capacity_probe
        self._verifier = qualification_verifier
        self._matrix_revision = matrix_revision
        self._routes: dict[tuple[str, str], _Route] = {}
        self._lock = RLock()

    # ----- configuration -------------------------------------------------
    @property
    def matrix_revision(self) -> int:
        with self._lock:
            return self._matrix_revision

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise PoolRegistryError("CLOCK_UNAVAILABLE") from None
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise PoolRegistryError("INVALID_CLOCK")
        return value

    def register(self, pool: Pool, binding: ConnectionBinding, adapter, model: str, *,
                 tools: tuple[str, ...] = ()) -> RouteStatus:
        if type(pool) is not Pool:
            raise PoolRegistryError("INVALID_POOL")
        if type(binding) is not ConnectionBinding:
            raise PoolRegistryError("INVALID_CONNECTION_BINDING")
        if pool.connection_id != binding.connection_id:
            raise PoolRegistryError("POOL_CONNECTION_MISMATCH")
        if pool.provider != binding.provider:
            raise PoolRegistryError("POOL_PROVIDER_MISMATCH")
        _check_model(model)
        if type(tools) is not tuple or len(tools) > _MAX_TOOLS:
            raise PoolRegistryError("INVALID_TOOLS")
        for tool in tools:
            _check_id(tool, "tool")
        if len(set(tools)) != len(tools):
            raise PoolRegistryError("DUPLICATE_TOOL")
        guard = _guard(adapter)
        with self._lock:
            key = (pool.id, model)
            if key in self._routes:
                raise PoolRegistryError("DUPLICATE_ROUTE")
            for route in self._routes.values():
                if route.pool.id == pool.id and (route.pool != pool or route.binding != binding):
                    raise PoolRegistryError("POOL_CONFLICT")
                if route.binding.connection_id == binding.connection_id and route.binding != binding:
                    raise PoolRegistryError("CONNECTION_REVISION_CONFLICT")
            now = self._now()
            route = _Route(pool, binding, adapter, guard, model, tools)
            self._routes[key] = route
            return self._status(route, now)

    def update_binding(self, binding: ConnectionBinding) -> tuple[RouteStatus, ...]:
        """A new connection revision (e.g. after re-login) discards every observation.

        It also revokes a governance approval; a rejection is kept.
        """
        if type(binding) is not ConnectionBinding:
            raise PoolRegistryError("INVALID_CONNECTION_BINDING")
        with self._lock:
            affected = [route for route in self._routes.values()
                        if route.binding.connection_id == binding.connection_id]
            if not affected:
                raise UnknownRoute("UNKNOWN_CONNECTION")
            old = affected[0].binding
            if (old.provider, old.runtime, old.auth_kind) != (binding.provider, binding.runtime, binding.auth_kind):
                raise PoolRegistryError("BINDING_IDENTITY_CHANGED")
            if binding.revision <= old.revision:
                raise PoolRegistryError("STALE_BINDING_REVISION")
            guards = [_guard(route.adapter) for route in affected]
            now = self._now()
            for route, guard in zip(affected, guards):
                route.binding = binding
                route.guard = guard
                route.reset()
            return tuple(self._status(route, now) for route in affected)

    def update_pool(self, pool: Pool) -> tuple[RouteStatus, ...]:
        """Replace AIK-owned settings (enabled, max_parallel); identity is unchanged."""
        if type(pool) is not Pool:
            raise PoolRegistryError("INVALID_POOL")
        with self._lock:
            affected = [route for route in self._routes.values() if route.pool.id == pool.id]
            if not affected:
                raise UnknownRoute("UNKNOWN_POOL")
            old = affected[0].pool
            if (old.provider, old.connection_id) != (pool.provider, pool.connection_id):
                raise PoolRegistryError("POOL_IDENTITY_CHANGED")
            now = self._now()
            for route in affected:
                route.pool = pool
            return tuple(self._status(route, now) for route in affected)

    def set_governance(self, pool_id: str, model: str, verdict: Governance) -> RouteStatus:
        if type(verdict) is not Governance:
            raise PoolRegistryError("INVALID_GOVERNANCE")
        with self._lock:
            route = self._route(pool_id, model)
            now = self._now()
            route.governance = verdict
            return self._status(route, now)

    def set_matrix_revision(self, revision: int) -> None:
        _check_revision(revision, "matrix_revision")
        with self._lock:
            if revision < self._matrix_revision:
                raise PoolRegistryError("MATRIX_REVISION_REGRESSION")
            if revision > self._matrix_revision:
                self._matrix_revision = revision
                for route in self._routes.values():
                    route.qualification = None
                    route.revoke_approval()

    # ----- probes ------------------------------------------------------
    def _route(self, pool_id: str, model: str) -> _Route:
        try:
            return self._routes[(pool_id, model)]
        except (KeyError, TypeError):
            raise UnknownRoute("UNKNOWN_ROUTE") from None

    def _probe_capacity(self, route: _Route) -> int | None:
        if self._capacity_probe is None:
            return None
        try:
            value = self._capacity_probe(route.pool.id, route.binding.connection_id, route.model)
        except Exception:
            return None
        if type(value) is not int or not 0 <= value <= route.pool.max_parallel:
            return None
        return value

    def _probe_route(self, route: _Route, now: datetime) -> None:
        implementation = runtime = account = isolation = "unknown"
        caps_ok = auth_ok = False
        binding = route.binding
        try:
            caps = route.guard.capabilities(binding)
            caps_ok = True
            implementation = caps.implementation_state
            isolation = caps.isolation.value
            runtime = _runtime_support(caps)
            if caps.auth_status is Support.SUPPORTED:
                try:
                    observed = route.guard.auth_status(binding)
                except AdapterFailure as error:
                    account = _ACCOUNT_FAULTS.get(error.fault.kind, "unknown")
                else:
                    auth_ok = True
                    account = "bound" if observed.state is AuthState.AUTHENTICATED else observed.state.value
        except Exception:
            # The GuardedAdapter has already reduced provider errors to a
            # fixed code. The original text is never stored or exposed.
            pass
        capacity = self._probe_capacity(route)
        route.observation = _Observation(now, implementation, runtime, account, isolation, capacity)
        if caps_ok and auth_ok and account == "bound" and runtime == "supported":
            # Only a confirmed bound account on a supported runtime counts as
            # verification; a successful call that reports logout does not.
            route.last_verified = now
        if account != "bound" or runtime != "supported" or implementation != "implemented":
            # Logout, account change, unverifiable account, lost runtime
            # support or a changed implementation claim: qualification for
            # this route must be performed again.
            route.qualification = None

    def probe(self, pool_id: str, model: str) -> RouteStatus:
        with self._lock:
            route = self._route(pool_id, model)
            now = self._now()
            self._probe_route(route, now)
            return self._status(route, now)

    def refresh(self, *, stale_only: bool = True) -> tuple[RouteStatus, ...]:
        """TTL refresh: re-probe stale or never-probed routes (or all of them)."""
        if type(stale_only) is not bool:
            raise PoolRegistryError("INVALID_REFRESH_MODE")
        with self._lock:
            now = self._now()
            refreshed = []
            for key in sorted(self._routes):
                route = self._routes[key]
                if stale_only and self._freshness(route, now) is Freshness.FRESH:
                    continue
                self._probe_route(route, now)
                refreshed.append(self._status(route, now))
            return tuple(refreshed)

    # ----- qualification ------------------------------------------------
    def record_qualification(self, evidence: QualificationEvidence) -> RouteStatus:
        if type(evidence) is not QualificationEvidence:
            raise QualificationRejected("INVALID_EVIDENCE")
        with self._lock:
            route = self._route(evidence.pool_id, evidence.model)
            binding = route.binding
            if evidence.evidence_kind != "live":
                raise QualificationRejected("NON_LIVE_EVIDENCE")
            if evidence.verdict != "PASS":
                raise QualificationRejected("EVIDENCE_NOT_PASS")
            if (evidence.connection_id, evidence.connection_revision, evidence.account_fingerprint,
                    evidence.runtime) != (binding.connection_id, binding.revision,
                                          binding.account_fingerprint, binding.runtime):
                raise QualificationRejected("EVIDENCE_BINDING_MISMATCH")
            if evidence.matrix_revision != self._matrix_revision:
                raise QualificationRejected("EVIDENCE_MATRIX_MISMATCH")
            now = self._now()
            if self._status(route, now).level is not QualificationLevel.CANDIDATE_FOR_QUALIFICATION:
                raise QualificationRejected("ROUTE_NOT_CANDIDATE")
            if self._verifier is None:
                raise QualificationRejected("NO_QUALIFICATION_VERIFIER")
            try:
                accepted = self._verifier(evidence)
            except Exception:
                raise QualificationRejected("VERIFIER_FAILED") from None
            if accepted is not True:
                raise QualificationRejected("EVIDENCE_NOT_VERIFIED")
            route.qualification = evidence
            return self._status(route, now)

    def _qualification_valid(self, route: _Route) -> bool:
        record = route.qualification
        binding = route.binding
        return record is not None and (
            record.pool_id, record.model, record.connection_id, record.connection_revision,
            record.account_fingerprint, record.runtime, record.matrix_revision,
        ) == (
            route.pool.id, route.model, binding.connection_id, binding.revision,
            binding.account_fingerprint, binding.runtime, self._matrix_revision,
        )

    # ----- snapshots ------------------------------------------------------
    def _freshness(self, route: _Route, now: datetime) -> Freshness:
        observation = route.observation
        if observation is None:
            return Freshness.UNKNOWN
        age = now - observation.observed_at
        # A clock that moves backwards is not proof of freshness.
        if age < timedelta(0) or age > self._ttl:
            return Freshness.STALE
        return Freshness.FRESH

    def _status(self, route: _Route, now: datetime) -> RouteStatus:
        observation = route.observation
        freshness = self._freshness(route, now)
        if observation is None:
            implementation = account = runtime = isolation = capacity_state = "unknown"
            capacity = None
        elif freshness is Freshness.STALE:
            # Every observed value, including the implementation claim, is
            # past its TTL; the level therefore becomes unknown.
            implementation = account = runtime = isolation = capacity_state = "stale"
            capacity = None
        else:
            implementation = observation.implementation
            account, runtime, isolation = observation.account, observation.runtime, observation.isolation
            capacity = observation.capacity
            if capacity is not None and capacity > route.pool.max_parallel:
                capacity = None
            capacity_state = "known" if capacity is not None else "unknown"

        if implementation == "implemented":
            level = QualificationLevel.IMPLEMENTED
            if freshness is Freshness.FRESH and account == "bound" and runtime == "supported":
                level = QualificationLevel.PROBE_VERIFIED
                if route.governance is Governance.APPROVED:
                    level = QualificationLevel.CANDIDATE_FOR_QUALIFICATION
                    if self._qualification_valid(route):
                        level = QualificationLevel.QUALIFIED
        elif implementation == "not_implemented":
            level = QualificationLevel.NOT_IMPLEMENTED
        else:
            level = QualificationLevel.UNKNOWN

        executable = (level in _RUNNABLE_LEVELS and route.governance is not Governance.REJECTED
                      and route.pool.enabled
                      and capacity_state == "known" and capacity is not None and capacity > 0)
        record = route.qualification if self._qualification_valid(route) else None
        binding = route.binding
        return RouteStatus(
            pool_id=route.pool.id,
            connection_id=binding.connection_id,
            connection_revision=binding.revision,
            provider=binding.provider,
            host=binding.runtime,
            auth_kind=binding.auth_kind.value,
            model=route.model,
            tools=route.tools,
            pool_enabled=route.pool.enabled,
            configured_max_parallel=route.pool.max_parallel,
            implementation_state=implementation,
            account_binding=account,
            runtime_support=runtime,
            account_isolation=isolation,
            governance=route.governance.value,
            last_probe_at=_iso(observation.observed_at if observation else None),
            last_verified_at=_iso(route.last_verified),
            freshness=freshness.value,
            matrix_revision=self._matrix_revision,
            qualification_matrix_revision=record.matrix_revision if record else None,
            capacity_state=capacity_state,
            available_capacity=capacity,
            level=level,
            executable_now=executable,
            placement_eligible=executable and level is QualificationLevel.QUALIFIED,
        )

    def status(self, pool_id: str, model: str) -> RouteStatus:
        with self._lock:
            return self._status(self._route(pool_id, model), self._now())

    def routes(self, *, model: str | None = None, pool_id: str | None = None,
               connection_id: str | None = None) -> tuple[RouteStatus, ...]:
        """Diagnostic view: every route and its actual level, including the unqualified ones."""
        with self._lock:
            now = self._now()
            selected = []
            for key in sorted(self._routes):
                route = self._routes[key]
                if model is not None and route.model != model:
                    continue
                if pool_id is not None and route.pool.id != pool_id:
                    continue
                if connection_id is not None and route.binding.connection_id != connection_id:
                    continue
                selected.append(self._status(route, now))
            return tuple(selected)

    def operational_inventory(self, *, model: str | None = None) -> tuple[RouteStatus, ...]:
        """General operations and automatic placement see only qualified, fresh, executable routes."""
        return tuple(status for status in self.routes(model=model) if status.placement_eligible)


__all__ = [
    "Freshness", "Governance", "PoolRegistryError", "QualificationEvidence",
    "QualificationLevel", "QualificationRejected", "ResourcePoolRegistry",
    "RouteStatus", "UnknownRoute",
]
