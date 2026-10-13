"""AIK-034 lease manager: reservation transaction, fence counter, TTL.

Storage:
- Leases are aik_runtime.Lease records written through RuntimeStore.transaction().
  That transaction holds the runtime process lock and runs BEGIN IMMEDIATE.
  The capacity check and the lease insert are therefore one atomic step.
- The budget reservation is created by aik_budget.BudgetStore.admit. Its
  reservation ID is the lease ID.

Reservation order:
1. The runtime transaction checks the placement, the route pool and holders on
   the same placement. It checks pool capacity and computes the next fencing
   token. It stores the lease as "reserved".
2. The budget admission runs. If it is not admitted, the lease becomes
   "failed". If it raises, the lease becomes "failed" and the exception
   propagates.
3. The lease is read again. If it is no longer "reserved", the budget
   reservation is released and the request is rejected. This happens when an
   expiry or a reconcile ran in between.

Invariant: provider_job_id is set exactly when a dispatch has been recorded
(activate). A lease without provider_job_id has no recorded provider call.
Expiry or release of such a lease may therefore free its budget hold.
A lease with a provider job never frees the hold here. Settlement belongs to
the observation module (BudgetStore.settle). Usage is never settled as zero
or as an estimate here.

Error messages are fixed codes and never contain supplied values.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import math

from aik_budget import AdmissionRequest, BudgetError, BudgetStore
from aik_config.models import Revisioned
from aik_pool import RouteStatus
from aik_profiles import rebuild_profile
from aik_profiles.errors import InvalidProfile
from aik_runtime import InvalidRecord, Lease, Placement, Pool, RuntimeStore, Versioned
from aik_runtime.models import MAX_INTEGER, identifier


HOLDING_STATES = ("reserved", "active", "draining", "unknown")
TERMINAL_STATES = ("released", "failed", "expired")
_LIVE_STATES = ("reserved", "active", "draining")
_PAGE = 1000
_MAX_TTL = 86400.0


class LeaseError(ValueError):
    """Fixed code-only failure; never contains provider text or secrets."""


class UnknownLease(LeaseError):
    """The lease is not stored."""


class StaleLease(LeaseError):
    """The fencing token does not identify the current owner of the placement."""


class LeaseExpired(LeaseError):
    """The lease expiry time has passed; it cannot be renewed or activated."""


class InvalidTransition(LeaseError):
    """The lease state does not permit the requested change."""


@dataclass(frozen=True, slots=True)
class LeaseResult:
    """granted (lease is set), wait (retry later) or rejected.

    lease_id names the stored lease record, including a lease that was marked
    failed after a budget wait or rejection. It is None when no record was
    written.
    """

    status: str
    lease: Versioned | None
    reasons: tuple[str, ...]
    retry_at: str | None = None
    lease_id: str | None = None

    @property
    def granted(self) -> bool:
        return self.status == "granted"


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    failed_leases: tuple[str, ...]
    released_reservations: tuple[str, ...]


def _check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise LeaseError("INVALID_" + field.upper()) from None


def _check_token(value: object) -> None:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise LeaseError("INVALID_FENCING_TOKEN")


def _check_flag(value: object, field: str) -> None:
    if type(value) is not bool:
        raise LeaseError("INVALID_" + field.upper())


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _expiry(lease: Lease) -> datetime:
    return datetime.fromisoformat(lease.expires_at.replace("Z", "+00:00"))


def lease_id_for(placement_id: str, fencing_token: int) -> str:
    """Deterministic lease ID for (placement, token), within identifier rules.

    The digits of the token contain no '-', so "lease-<placement>-<token>" is
    unambiguous. A placement ID too long for that form uses a ':'-separated
    SHA-256 form instead, which cannot collide with the '-' form.
    """
    _check_id(placement_id, "placement_id")
    _check_token(fencing_token)
    candidate = f"lease-{placement_id}-{fencing_token}"
    try:
        identifier(candidate)
        return candidate
    except InvalidRecord:
        digest = hashlib.sha256(placement_id.encode("utf-8")).hexdigest()[:40]
        return f"lease:{digest}:{fencing_token}"


def _all_leases(tx) -> list:
    leases = []
    after = None
    while True:
        page = tx.list(Lease, _PAGE, after)
        leases.extend(page)
        if len(page) < _PAGE:
            return leases
        after = page[-1].value.id


def _check_profile(profile) -> None:
    value = profile.value if type(profile) is Revisioned else profile
    try:
        rebuild_profile(value)
    except InvalidProfile:
        raise LeaseError("INVALID_PROFILE") from None


class LeaseManager:
    """Atomic capacity + budget reservation with fencing and TTL expiry.

    The manager keeps no state of its own. Every operation uses the
    RuntimeStore/BudgetStore file locks, so several managers and processes on
    the same files cooperate. Expiry runs only when expire_due() is called.
    """

    def __init__(self, store: RuntimeStore, budget: BudgetStore, *, ttl_seconds: float, clock=None):
        if not isinstance(store, RuntimeStore):
            raise LeaseError("INVALID_RUNTIME_STORE")
        if not isinstance(budget, BudgetStore):
            raise LeaseError("INVALID_BUDGET_STORE")
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= _MAX_TTL):
            raise LeaseError("INVALID_TTL")
        if clock is not None and not callable(clock):
            raise LeaseError("INVALID_CLOCK")
        self._store = store
        self._budget = budget
        self._ttl = timedelta(seconds=float(ttl_seconds))
        self._clock = clock

    # ---------------------------------------------------------------- helpers
    def _now(self) -> datetime:
        if self._clock is None:
            value = datetime.now(timezone.utc)
        else:
            try:
                value = self._clock()
            except Exception:
                raise LeaseError("CLOCK_UNAVAILABLE") from None
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise LeaseError("INVALID_CLOCK")
        return value.astimezone(timezone.utc)

    def _deadline(self, now: datetime) -> str:
        try:
            return _stamp(now + self._ttl)
        except OverflowError:
            raise LeaseError("INVALID_CLOCK") from None

    @staticmethod
    def _current(tx, lease_id: object, fencing_token: object) -> Versioned:
        """Return the stored lease only to the owner of its current fencing token."""
        _check_id(lease_id, "lease_id")
        _check_token(fencing_token)
        current = tx.get(Lease, lease_id)
        if current is None:
            raise UnknownLease("UNKNOWN_LEASE")
        if current.value.fencing_token != fencing_token:
            raise StaleLease("STALE_FENCING_TOKEN")
        for other in _all_leases(tx):
            if (other.value.placement_id == current.value.placement_id
                    and other.value.fencing_token > fencing_token):
                raise StaleLease("SUPERSEDED_LEASE")
        return current

    def _fail_reserved(self, lease_id: str) -> bool:
        """Mark a reserved lease without a recorded dispatch as failed."""
        with self._store.transaction() as tx:
            current = tx.get(Lease, lease_id)
            if current is None or current.value.state != "reserved" or current.value.provider_job_id is not None:
                return False
            tx.save(replace(current.value, state="failed"), expected_revision=current.revision)
            return True

    def _release_budget(self, lease_id: str) -> bool:
        """Release the linked budget reservation only while it is still active."""
        reservation = self._budget.reservation(lease_id)
        if reservation is None or reservation.state != "reserved":
            return False
        try:
            self._budget.release(lease_id)
        except BudgetError:
            again = self._budget.reservation(lease_id)
            if again is not None and again.state == "reserved":
                raise
            return False
        return True

    # ---------------------------------------------------------------- reserve
    def _reserve_record(self, tx, placement_id: str, worker_id: str, route: RouteStatus):
        now = self._now()
        placement = tx.get(Placement, placement_id)
        if placement is None:
            return LeaseResult("rejected", None, ("PLACEMENT_UNKNOWN",)), None
        if placement.value.state != "assigned":
            return LeaseResult("rejected", None, ("PLACEMENT_NOT_ASSIGNED",)), None
        if route.pool_id != placement.value.pool_id:
            return LeaseResult("rejected", None, ("ROUTE_POOL_MISMATCH",)), None
        pool = tx.get(Pool, placement.value.pool_id)
        if pool is None:
            return LeaseResult("rejected", None, ("POOL_UNKNOWN",)), None
        if pool.value.enabled is not True:
            return LeaseResult("rejected", None, ("POOL_DISABLED",)), None
        leases = [entry.value for entry in _all_leases(tx)]
        same = [lease for lease in leases if lease.placement_id == placement_id]
        if any(lease.state == "unknown" for lease in same):
            # A live job may still run; it is never relaunched immediately.
            return LeaseResult("rejected", None, ("PREVIOUS_LEASE_UNKNOWN",)), None
        if any(lease.state in _LIVE_STATES for lease in same):
            return LeaseResult("rejected", None, ("LEASE_ALREADY_HELD",)), None
        held = sum(1 for lease in leases if lease.pool_id == pool.value.id and lease.state in HOLDING_STATES)
        if held >= pool.value.max_parallel:
            return LeaseResult("wait", None, ("POOL_CAPACITY",)), None
        token = max((lease.fencing_token for lease in same), default=0) + 1
        if token > MAX_INTEGER:
            return LeaseResult("rejected", None, ("FENCE_EXHAUSTED",)), None
        lease = Lease(lease_id_for(placement_id, token), placement_id, pool.value.id, worker_id, token,
                      self._deadline(now))
        return None, tx.save(lease, expected_revision=0)

    def reserve(self, placement_id: str, *, worker_id: str, route: RouteStatus, budget_profile,
                max_seconds: int, scopes: tuple[str, ...] = (),
                api_cost_estimate_cents: int | None = None) -> LeaseResult:
        """Reserve pool capacity and budget together, or leave neither active."""
        _check_id(placement_id, "placement_id")
        _check_id(worker_id, "worker_id")
        if type(route) is not RouteStatus:
            raise LeaseError("INVALID_ROUTE")
        _check_profile(budget_profile)
        # Validates pool/model/max_seconds/scopes/estimate before any record is written.
        template = AdmissionRequest("lease-validation", route.pool_id, route.model, max_seconds,
                                    scopes, api_cost_estimate_cents)
        with self._store.transaction() as tx:
            refusal, lease = self._reserve_record(tx, placement_id, worker_id, route)
        if refusal is not None:
            return refusal
        lease_id = lease.value.id
        request = replace(template, reservation_id=lease_id)
        try:
            decision = self._budget.admit(request, budget_profile, route)
        except BaseException:
            try:
                self._fail_reserved(lease_id)
            except Exception:
                pass  # reconcile_reservations() repairs a lease left reserved.
            raise
        if decision.status != "admitted":
            self._fail_reserved(lease_id)
            return LeaseResult(decision.status, None, decision.reasons, decision.retry_at, lease_id)
        with self._store.transaction() as tx:
            current = tx.get(Lease, lease_id)
        if current is None or current.value.state != "reserved":
            # Expired or reconciled while the budget admission ran.
            self._release_budget(lease_id)
            return LeaseResult("rejected", None, ("LEASE_LOST_BEFORE_GRANT",), None, lease_id)
        return LeaseResult("granted", current, (), None, lease_id)

    # ------------------------------------------------------------ transitions
    def activate(self, lease_id: str, fencing_token: int, provider_job_id: str | None = None) -> Versioned:
        """reserved -> active, written before the provider call (dispatch intent).

        Call it without a job ID before starting the provider job, then again
        with the provider job ID once the receipt arrives. An active lease is
        never expired as "no dispatch": a crash between the provider call and
        the receipt leaves it active, so TTL expiry turns it unknown instead of
        releasing its budget hold.
        """
        if provider_job_id is not None:
            _check_id(provider_job_id, "provider_job_id")
        with self._store.transaction() as tx:
            now = self._now()
            current = self._current(tx, lease_id, fencing_token)
            lease = current.value
            if lease.state == "active" and lease.provider_job_id is None and provider_job_id is not None:
                return tx.save(replace(lease, provider_job_id=provider_job_id), expected_revision=current.revision)
            if lease.state != "reserved":
                raise InvalidTransition("LEASE_NOT_RESERVED")
            if now >= _expiry(lease):
                raise LeaseExpired("LEASE_EXPIRED")
            return tx.save(replace(lease, state="active", provider_job_id=provider_job_id),
                           expected_revision=current.revision)

    def renew(self, lease_id: str, fencing_token: int) -> Versioned:
        """Extend the expiry to now + TTL, only before the current expiry."""
        with self._store.transaction() as tx:
            now = self._now()
            current = self._current(tx, lease_id, fencing_token)
            if current.value.state not in _LIVE_STATES:
                raise InvalidTransition("LEASE_NOT_RENEWABLE")
            if now >= _expiry(current.value):
                raise LeaseExpired("LEASE_EXPIRED")
            return tx.save(replace(current.value, expires_at=self._deadline(now)),
                           expected_revision=current.revision)

    def release(self, lease_id: str, fencing_token: int, *, dispatched: bool,
                provider_terminal: bool) -> Versioned:
        """Return the lease.

        - dispatched=False: no provider call happened; lease released, budget hold released.
        - dispatched=True, provider_terminal=True: lease released; settlement is separate.
        - dispatched=True, provider_terminal=False: lease draining until termination is observed.
        """
        _check_flag(dispatched, "dispatched")
        _check_flag(provider_terminal, "provider_terminal")
        with self._store.transaction() as tx:
            current = self._current(tx, lease_id, fencing_token)
            lease = current.value
            if lease.state not in _LIVE_STATES:
                raise InvalidTransition("LEASE_NOT_RELEASABLE")
            if not dispatched:
                if lease.provider_job_id is not None:
                    raise InvalidTransition("DISPATCH_RECORDED")
                result = tx.save(replace(lease, state="released"), expected_revision=current.revision)
            else:
                if lease.provider_job_id is None and lease.state == "reserved":
                    raise InvalidTransition("DISPATCH_NOT_RECORDED")
                if provider_terminal:
                    result = tx.save(replace(lease, state="released"), expected_revision=current.revision)
                elif lease.state == "draining":
                    result = current
                else:
                    result = tx.save(replace(lease, state="draining"), expected_revision=current.revision)
        if not dispatched:
            self._release_budget(result.value.id)
        return result

    def expire_due(self) -> tuple[Versioned, ...]:
        """Apply TTL expiry. Expiry is not provider termination.

        reserved without a dispatch -> expired (budget hold released).
        any lease with a recorded dispatch -> unknown (capacity stays held).
        """
        changed = []
        expired = []
        with self._store.transaction() as tx:
            now = self._now()
            for entry in _all_leases(tx):
                lease = entry.value
                if lease.state not in _LIVE_STATES or now < _expiry(lease):
                    continue
                if lease.state == "reserved" and lease.provider_job_id is None:
                    changed.append(tx.save(replace(lease, state="expired"), expected_revision=entry.revision))
                    expired.append(lease.id)
                else:
                    changed.append(tx.save(replace(lease, state="unknown"), expected_revision=entry.revision))
        for lease_id in expired:
            self._release_budget(lease_id)
        return tuple(changed)

    def resolve_unknown(self, lease_id: str, fencing_token: int, *, provider_terminal: bool) -> Versioned:
        """Observed termination releases an unknown lease; otherwise it stays unknown."""
        _check_flag(provider_terminal, "provider_terminal")
        with self._store.transaction() as tx:
            current = self._current(tx, lease_id, fencing_token)
            if current.value.state != "unknown":
                raise InvalidTransition("LEASE_NOT_UNKNOWN")
            if not provider_terminal:
                return current
            return tx.save(replace(current.value, state="released"), expected_revision=current.revision)

    # -------------------------------------------------------------- recovery
    def reconcile_reservations(self) -> ReconcileReport:
        """Repair state left by a crash between the lease write and budget steps.

        1. A reserved lease without a dispatch and without a budget reservation
           becomes failed. No effect has happened yet.
        2. A released/failed/expired lease without a dispatch whose budget
           reservation is still reserved has its budget hold released.
        """
        with self._store.transaction() as tx:
            leases = _all_leases(tx)
        candidates = [entry.value.id for entry in leases
                      if entry.value.state == "reserved" and entry.value.provider_job_id is None]
        orphans = [lease_id for lease_id in candidates if self._budget.reservation(lease_id) is None]
        failed = [lease_id for lease_id in orphans if self._fail_reserved(lease_id)]
        with self._store.transaction() as tx:
            leases = _all_leases(tx)
        released = [entry.value.id for entry in leases
                    if entry.value.state in TERMINAL_STATES and entry.value.provider_job_id is None
                    and self._release_budget(entry.value.id)]
        return ReconcileReport(tuple(failed), tuple(released))


__all__ = [
    "LeaseManager", "LeaseResult", "ReconcileReport", "lease_id_for",
    "HOLDING_STATES", "TERMINAL_STATES",
    "LeaseError", "UnknownLease", "StaleLease", "LeaseExpired", "InvalidTransition",
]
