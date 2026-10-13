"""Pause/drain gate for new reservations, and a lease manager wrapper that honours it.

A closed scope (paused or draining) blocks new reservations only. It does not
touch running provider jobs; ending them is the controller's job.
"""

from __future__ import annotations

from dataclasses import dataclass

from aik_lease import LeaseError, LeaseManager, LeaseResult
from aik_runtime import Placement, RuntimeStore
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier

from .database import LifecycleDatabase, timestamp
from .errors import LifecycleError


GATE_STATES = ("open", "paused", "draining")
SCOPE_GLOBAL = "global"
_SCOPE_KINDS = ("pool", "project")
_DELEGATED = frozenset({"activate", "renew", "release", "expire_due", "resolve_unknown", "reconcile_reservations"})


def check_scope(scope: object) -> str:
    """'global', 'pool:<pool_id>' or 'project:<vm_project_id>'."""
    if scope == SCOPE_GLOBAL and type(scope) is str:
        return scope
    if type(scope) is str:
        kind, separator, rest = scope.partition(":")
        if separator and kind in _SCOPE_KINDS:
            try:
                identifier(rest, "scope")
                return scope
            except InvalidRecord:
                pass
    raise LifecycleError("INVALID_SCOPE")


def _check_reason(reason: object) -> None:
    try:
        identifier(reason, "reason")
    except InvalidRecord:
        raise LifecycleError("INVALID_REASON") from None


@dataclass(frozen=True, slots=True)
class GateState:
    scope: str
    state: str
    reason: str | None
    event_seq: int | None

    @property
    def open(self) -> bool:
        return self.state == "open"


@dataclass(frozen=True, slots=True)
class GateEvent:
    seq: int
    scope: str
    action: str
    reason: str | None
    recorded_at: str


class LifecycleGate:
    """Durable scope gate with an append-only event history."""

    def __init__(self, path, *, clock=None, timeout: float = 5.0):
        if clock is not None and not callable(clock):
            raise LifecycleError("INVALID_CLOCK")
        self._db = LifecycleDatabase(path, timeout=timeout)
        self._clock = clock

    @property
    def path(self):
        return self._db.path

    def pause(self, scope: str, reason: str) -> GateState:
        _check_reason(reason)
        return self._set(scope, "pause", "paused", reason)

    def drain(self, scope: str, reason: str) -> GateState:
        _check_reason(reason)
        return self._set(scope, "drain", "draining", reason)

    def resume(self, scope: str) -> GateState:
        return self._set(scope, "resume", "open", None)

    def _set(self, scope, action: str, state: str, reason) -> GateState:
        scope = check_scope(scope)
        recorded_at = timestamp(self._clock)
        with self._db.transaction() as connection:
            seq = connection.execute(
                'INSERT INTO "gate_events" ("scope", "action", "reason", "recorded_at") VALUES (?, ?, ?, ?)',
                (scope, action, reason, recorded_at),
            ).lastrowid
            connection.execute(
                'INSERT INTO "gate_state" ("scope", "state", "reason", "event_seq") VALUES (?, ?, ?, ?) '
                'ON CONFLICT ("scope") DO UPDATE SET "state" = excluded."state", '
                '"reason" = excluded."reason", "event_seq" = excluded."event_seq"',
                (scope, state, reason, seq),
            )
        return GateState(scope, state, reason, seq)

    def state(self, scope: str) -> GateState:
        scope = check_scope(scope)
        with self._db.transaction() as connection:
            row = connection.execute(
                'SELECT "state", "reason", "event_seq" FROM "gate_state" WHERE "scope" = ?', (scope,),
            ).fetchone()
        return GateState(scope, "open", None, None) if row is None else GateState(scope, *row)

    def closed_scopes(self) -> tuple[GateState, ...]:
        with self._db.transaction() as connection:
            rows = connection.execute(
                'SELECT "scope", "state", "reason", "event_seq" FROM "gate_state" '
                'WHERE "state" != \'open\' ORDER BY "scope"'
            ).fetchall()
        return tuple(GateState(*row) for row in rows)

    def is_open(self, pool_id: str | None = None, vm_project_id: str | None = None) -> tuple[bool, tuple[str, ...]]:
        """(True, ()) when global, the pool and the project are all open; otherwise reasons."""
        scopes = [SCOPE_GLOBAL]
        if pool_id is not None:
            scopes.append(check_scope("pool:" + pool_id) if type(pool_id) is str else check_scope(pool_id))
        if vm_project_id is not None:
            scopes.append(check_scope("project:" + vm_project_id) if type(vm_project_id) is str
                          else check_scope(vm_project_id))
        with self._db.transaction() as connection:
            states = dict(connection.execute(
                'SELECT "scope", "state" FROM "gate_state" WHERE "scope" IN (%s)' % ", ".join("?" for _ in scopes),
                scopes,
            ).fetchall())
        reasons = []
        for scope in scopes:
            state = states.get(scope, "open")
            if state == "paused":
                reasons.append("SCOPE_PAUSED@" + scope)
            elif state == "draining":
                reasons.append("SCOPE_DRAINING@" + scope)
        return not reasons, tuple(reasons)

    def events(self, limit: int = 100, after_seq: int | None = None) -> tuple[GateEvent, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise LifecycleError("INVALID_LIMIT")
        if after_seq is not None and (type(after_seq) is not int or after_seq < 0):
            raise LifecycleError("INVALID_AFTER_SEQ")
        with self._db.transaction() as connection:
            rows = connection.execute(
                'SELECT "seq", "scope", "action", "reason", "recorded_at" FROM "gate_events" '
                'WHERE "seq" > ? ORDER BY "seq" LIMIT ?', (after_seq or 0, limit),
            ).fetchall()
        return tuple(GateEvent(*row) for row in rows)


def runtime_placement_lookup(store: RuntimeStore):
    """placement_id -> (pool_id, vm_project_id) from the AIK runtime store, or None."""
    if not isinstance(store, RuntimeStore):
        raise LifecycleError("INVALID_RUNTIME_STORE")

    def lookup(placement_id: str):
        entry = store.get(Placement, placement_id)
        return None if entry is None else (entry.value.pool_id, entry.value.vm_project_id)

    return lookup


class GatedLeaseManager:
    """aik_lease.LeaseManager with the lifecycle gate checked around reserve().

    The gate is checked before reserving and again after a grant. A grant that
    raced with a pause/drain is released at once (no dispatch happened) and
    reported as "wait". Every other method is delegated unchanged.
    """

    def __init__(self, lease_manager: LeaseManager, gate: LifecycleGate, placement_lookup):
        if not isinstance(lease_manager, LeaseManager):
            raise LifecycleError("INVALID_LEASE_MANAGER")
        if not isinstance(gate, LifecycleGate):
            raise LifecycleError("INVALID_GATE")
        if not callable(placement_lookup):
            raise LifecycleError("INVALID_PLACEMENT_LOOKUP")
        self._leases = lease_manager
        self._gate = gate
        self._lookup = placement_lookup

    @property
    def lease_manager(self) -> LeaseManager:
        return self._leases

    def _check(self, placement_id: str) -> tuple[bool, tuple[str, ...]]:
        try:
            located = self._lookup(placement_id)
            pool_id = project_id = None
            if located is not None:
                pool_id, project_id = located
            return self._gate.is_open(pool_id, project_id)
        except Exception:
            # Fail closed: an unreadable gate never permits a new reservation.
            return False, ("GATE_UNAVAILABLE",)

    def reserve(self, placement_id: str, **request) -> LeaseResult:
        try:
            identifier(placement_id, "placement_id")
        except InvalidRecord:
            raise LeaseError("INVALID_PLACEMENT_ID") from None
        is_open, reasons = self._check(placement_id)
        if not is_open:
            return LeaseResult("wait", None, reasons)
        result = self._leases.reserve(placement_id, **request)
        if not result.granted:
            return result
        is_open, reasons = self._check(placement_id)
        if is_open:
            return result
        lease = result.lease.value
        try:
            self._leases.release(lease.id, lease.fencing_token, dispatched=False, provider_terminal=False)
        except Exception:
            # Not handed out as granted; expiry or reconcile frees it later.
            reasons = reasons + ("LEASE_RELEASE_PENDING",)
        return LeaseResult("wait", None, reasons, None, lease.id)

    def __getattr__(self, name: str):
        if name in _DELEGATED:
            return getattr(self._leases, name)
        raise AttributeError(name)
