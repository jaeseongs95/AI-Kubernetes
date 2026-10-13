"""AIK-016 auth state machine over connection bindings, runs and lock signals.

State per connection: valid / expired / locked / revoked / rotating / unknown.
A registered or newly re-authenticated connection starts as unknown and only a
revalidation (registry probe or an observed AuthObservation) can make it valid.
Every mutating operation takes an expected revision; concurrent rotation,
re-authentication and deletion therefore serialize as RevisionConflict.

All state lives in process memory under one lock. A registry probe runs while
that lock is held, so a slow adapter delays other callers of this lifecycle.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timezone
from threading import RLock

from aik_connection.errors import AuthenticationRequired, CredentialRefUnavailable, UnknownConnection
from aik_pool.registry import ResourcePoolRegistry
from aik_provider.contracts import (
    TERMINAL, AuthKind, AuthObservation, AuthState as ProviderAuthState, ConnectionBinding, JobReceipt,
)
from aik_runtime.errors import InvalidRecord, RevisionConflict
from aik_runtime.models import MAX_INTEGER, identifier
from aik_vault import is_credential_ref

from .models import (
    LOCK_SESSION, LOCK_VAULT, AuthEvent, AuthEventKind, AuthLifecycleError, AuthSnapshot,
    AuthState, LockPolicy, RunRecord, RunStatus, RunTicket,
)


_PROVIDER_STATES = {
    ProviderAuthState.AUTHENTICATED: AuthState.VALID,
    ProviderAuthState.EXPIRED: AuthState.EXPIRED,
    ProviderAuthState.UNAUTHENTICATED: AuthState.EXPIRED,
    ProviderAuthState.LOCKED: AuthState.LOCKED,
    ProviderAuthState.PENDING: AuthState.UNKNOWN,
    ProviderAuthState.UNKNOWN: AuthState.UNKNOWN,
}
_PROBE_STATES = {
    "bound": AuthState.VALID,
    "expired": AuthState.EXPIRED,
    "unauthenticated": AuthState.EXPIRED,
    "locked": AuthState.LOCKED,
}
_UNREADABLE = object()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise AuthLifecycleError("INVALID_" + field.upper()) from None


def _check_revision(value: object) -> None:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise AuthLifecycleError("INVALID_REVISION")


def _expiry(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise AuthLifecycleError("INVALID_EXPIRY")
    return value


def _rebuild_binding(value: object) -> ConnectionBinding:
    # Frozen dataclasses can be forged with object.__setattr__; revalidate them.
    if type(value) is not ConnectionBinding:
        raise AuthLifecycleError("INVALID_BINDING")
    try:
        return ConnectionBinding(**{f.name: getattr(value, f.name) for f in fields(ConnectionBinding)})
    except (TypeError, ValueError):
        raise AuthLifecycleError("INVALID_BINDING") from None


def _rebuild_observation(value: object) -> AuthObservation:
    # Like bindings, observations are reconstructed so a forged field fails with a fixed code.
    if type(value) is not AuthObservation:
        raise AuthLifecycleError("INVALID_OBSERVATION")
    try:
        rebuilt = AuthObservation(**{f.name: getattr(value, f.name) for f in fields(AuthObservation)})
    except (TypeError, ValueError, AttributeError):
        raise AuthLifecycleError("INVALID_OBSERVATION") from None
    if type(rebuilt.state) is not ProviderAuthState:
        raise AuthLifecycleError("INVALID_OBSERVATION")
    return rebuilt


class _Record:
    __slots__ = ("connection_id", "binding", "base", "revision", "credential_version",
                 "expires_at", "locks", "deleted")

    def __init__(self, binding: ConnectionBinding, expires_at: datetime | None):
        self.connection_id = binding.connection_id
        self.binding = binding
        self.base = AuthState.UNKNOWN
        self.revision = 1
        self.credential_version = 1
        self.expires_at = expires_at
        self.locks: frozenset[str] = frozenset()
        self.deleted = False

    def effective(self) -> AuthState:
        if self.deleted:
            return AuthState.REVOKED
        if self.locks:
            return AuthState.LOCKED
        return self.base


class _Run:
    __slots__ = ("run_id", "binding", "credential_version", "status", "outcome", "reason", "job_id")

    def __init__(self, run_id: str, binding: ConnectionBinding, credential_version: int):
        self.run_id = run_id
        self.binding = binding
        self.credential_version = credential_version
        self.status = RunStatus.ACTIVE
        self.outcome: str | None = None
        self.reason: str | None = None
        self.job_id: str | None = None  # Bound by the first accepted receipt.

    def record(self) -> RunRecord:
        return RunRecord(self.run_id, self.binding.connection_id, self.binding.revision,
                         self.credential_version, self.binding.credential_ref, self.status,
                         self.outcome, self.reason)


class AuthLifecycle:
    """Auth state machine with injectable clock, vault checker, registry and lock sources."""

    def __init__(self, *, clock=None, credential_checker=None, registry=None,
                 vault_lock_source=None, session_lock_source=None, policy=None,
                 listener=None, max_events: int = 4096):
        if clock is not None and not callable(clock):
            raise AuthLifecycleError("INVALID_CLOCK")
        if credential_checker is not None and not callable(getattr(credential_checker, "check", None)):
            raise AuthLifecycleError("INVALID_CREDENTIAL_CHECKER")
        if registry is not None and not isinstance(registry, ResourcePoolRegistry):
            raise AuthLifecycleError("INVALID_REGISTRY")
        for callback in (vault_lock_source, session_lock_source, listener):
            if callback is not None and not callable(callback):
                raise AuthLifecycleError("INVALID_CALLBACK")
        policy = LockPolicy() if policy is None else policy
        if type(policy) is not LockPolicy:
            raise AuthLifecycleError("INVALID_LOCK_POLICY")
        if (policy.vault_lock_blocks and vault_lock_source is None) or \
                (policy.session_lock_blocks and session_lock_source is None):
            raise AuthLifecycleError("LOCK_SOURCE_REQUIRED")
        if type(max_events) is not int or not 1 <= max_events <= 65536:
            raise AuthLifecycleError("INVALID_EVENT_LIMIT")
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._checker = credential_checker
        self._registry = registry
        self._vault_source = vault_lock_source
        self._session_source = session_lock_source
        self._policy = policy
        self._listener = listener
        self._records: dict[str, _Record] = {}
        self._runs: dict[str, _Run] = {}
        self._events: deque = deque(maxlen=max_events)
        self._listener_failures = 0
        self._generations: dict[str, int | None] = {}
        for kind, source in (("vault", vault_lock_source), ("session", session_lock_source)):
            generation = self._read_generation(source)
            self._generations[kind] = generation if type(generation) is int else None
        self._lock = RLock()

    # ------------------------------------------------------------ plumbing
    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise AuthLifecycleError("CLOCK_UNAVAILABLE") from None
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise AuthLifecycleError("INVALID_CLOCK")
        return value

    @contextmanager
    def _operation(self):
        pending: list[AuthEvent] = []
        try:
            with self._lock:
                yield pending
        finally:
            self._deliver(pending)

    def _deliver(self, pending: list) -> None:
        if self._listener is None:
            return
        for event in pending:
            try:
                self._listener(event)
            except Exception:
                # The state change already happened; the failure is counted, not hidden.
                with self._lock:
                    self._listener_failures += 1

    def _emit(self, pending: list, kind: AuthEventKind, rec: _Record, now: datetime,
              reason: str | None = None, run_ids: tuple = ()) -> None:
        event = AuthEvent(kind, rec.connection_id, rec.revision, rec.effective(), _iso(now), reason, tuple(run_ids))
        self._events.append(event)
        pending.append(event)

    @staticmethod
    def _bump(rec: _Record) -> None:
        if rec.revision >= MAX_INTEGER:
            raise RevisionConflict("The auth lifecycle revision cannot be incremented.")
        rec.revision += 1

    @staticmethod
    def _signal(source) -> bool:
        if source is None:
            return False
        try:
            value = source()
        except Exception:
            return True  # An unreadable signal is treated as locked (fail-closed).
        return value if type(value) is bool else True

    @staticmethod
    def _read_generation(source):
        """None when the source declares no generation; _UNREADABLE when it is broken."""
        if source is None:
            return None
        try:
            value = getattr(source, "generation")
        except AttributeError:
            return None
        except Exception:
            return _UNREADABLE
        return value if type(value) is int else _UNREADABLE

    def _observe(self, kind: str, source) -> tuple:
        """Return (locked now, a lock happened since the previous poll)."""
        locked = self._signal(source)
        generation = self._read_generation(source)
        if generation is _UNREADABLE:
            return True, False  # A broken lock history is treated as locked (fail-closed).
        if generation is None:
            return locked, False
        previous = self._generations.get(kind)
        self._generations[kind] = generation
        return locked, previous is not None and generation != previous

    def _poll(self, pending: list) -> datetime:
        now = self._now()
        vault, vault_pulse = (self._observe("vault", self._vault_source)
                              if self._policy.vault_lock_blocks else (False, False))
        session, session_pulse = (self._observe("session", self._session_source)
                                  if self._policy.session_lock_blocks else (False, False))
        for cid in sorted(self._records):
            rec = self._records[cid]
            if rec.deleted:
                continue
            wanted = set()
            pulsed = set()
            if rec.binding.auth_kind is AuthKind.API_KEY:
                if vault:
                    wanted.add(LOCK_VAULT)
                if vault_pulse:
                    pulsed.add(LOCK_VAULT)
            if session:
                wanted.add(LOCK_SESSION)
            if session_pulse:
                pulsed.add(LOCK_SESSION)
            wanted = frozenset(wanted)
            # A lock set and released between two polls is not visible in the level.
            missed = frozenset(pulsed) - wanted - rec.locks
            if wanted != rec.locks:
                newly = wanted - rec.locks
                rec.locks = wanted
                if newly and rec.base is AuthState.VALID:
                    rec.base = AuthState.UNKNOWN  # A lock invalidates the earlier validation.
                self._bump(rec)
                if wanted:
                    self._emit(pending, AuthEventKind.LOCKED, rec, now, "lock:" + ",".join(sorted(wanted)))
                else:
                    self._emit(pending, AuthEventKind.UNLOCKED, rec, now, "unlocked")
                if newly and self._policy.reclaim_running_on_lock:
                    self._reclaim(pending, rec, now, "lock:" + ",".join(sorted(newly)))
            if missed:
                label = ",".join(sorted(missed))
                if rec.base is AuthState.VALID:
                    rec.base = AuthState.UNKNOWN
                self._bump(rec)
                self._emit(pending, AuthEventKind.LOCKED, rec, now, "lock_missed:" + label)
                if self._policy.reclaim_running_on_lock:
                    self._reclaim(pending, rec, now, "lock:" + label)
            if rec.expires_at is not None and now >= rec.expires_at and \
                    rec.base in (AuthState.VALID, AuthState.UNKNOWN):
                rec.base = AuthState.EXPIRED
                self._bump(rec)
                self._emit(pending, AuthEventKind.EXPIRED, rec, now, "expires_at")
        return now

    def _reclaim(self, pending: list, rec: _Record, now: datetime, reason: str, predicate=None) -> tuple:
        changed = []
        for run_id in sorted(self._runs):
            run = self._runs[run_id]
            if run.binding.connection_id != rec.connection_id or run.status is not RunStatus.ACTIVE:
                continue
            if predicate is not None and not predicate(run):
                continue
            run.status = RunStatus.RECLAIM_REQUIRED
            run.reason = reason
            changed.append(run_id)
        if changed:
            self._emit(pending, AuthEventKind.RECLAIM_REQUIRED, rec, now, reason, tuple(changed))
        return tuple(changed)

    def _writable(self, connection_id: object, expected_revision: object) -> _Record:
        _check_id(connection_id, "connection_id")
        _check_revision(expected_revision)
        rec = self._records.get(connection_id)
        if rec is None:
            raise UnknownConnection("The connection is not registered.")
        if rec.revision != expected_revision:
            raise RevisionConflict("The auth lifecycle revision has changed.")
        if rec.deleted:
            raise UnknownConnection("The connection has been deleted.")
        return rec

    def _check_credential(self, binding: ConnectionBinding) -> None:
        if binding.auth_kind is not AuthKind.API_KEY:
            return  # Host sessions are owned by the official host.
        if not is_credential_ref(binding.credential_ref):
            raise CredentialRefUnavailable("An API connection requires an aik_vault reference.")
        if self._checker is None:
            raise CredentialRefUnavailable("No vault adapter is available to confirm the credential reference.")
        try:
            self._checker.check(binding.credential_ref)
        except CredentialRefUnavailable:
            raise
        except Exception:
            raise CredentialRefUnavailable("The vault credential could not be confirmed.") from None

    def _routes(self, connection_id: str) -> tuple:
        if self._registry is None:
            return ()
        return self._registry.routes(connection_id=connection_id)

    @staticmethod
    def _check_successor(old: ConnectionBinding, new: ConnectionBinding, *, same_account: bool) -> None:
        if (new.provider, new.runtime, new.connection_id, new.auth_kind) != (
                old.provider, old.runtime, old.connection_id, old.auth_kind):
            raise AuthLifecycleError("BINDING_IDENTITY_CHANGED")
        if new.revision <= old.revision:
            raise AuthLifecycleError("STALE_BINDING_REVISION")
        if same_account and new.account_fingerprint != old.account_fingerprint:
            raise AuthLifecycleError("ACCOUNT_CHANGED")

    def _sync_registry(self, binding: ConnectionBinding) -> None:
        # update_binding resets every probe, verification and qualification.
        if self._routes(binding.connection_id):
            self._registry.update_binding(binding)

    @staticmethod
    def _snapshot(rec: _Record) -> AuthSnapshot:
        return AuthSnapshot(rec.connection_id, rec.effective(), rec.revision, rec.binding.revision,
                            rec.credential_version, rec.binding.credential_ref, tuple(sorted(rec.locks)),
                            _iso(rec.expires_at), rec.deleted)

    # ------------------------------------------------------------ connections
    def register(self, binding: ConnectionBinding, *, expires_at: datetime | None = None) -> AuthSnapshot:
        """Track a connection. It starts as unknown and is not placeable until revalidated."""
        binding = _rebuild_binding(binding)
        expires = _expiry(expires_at)
        with self._operation() as pending:
            now = self._poll(pending)
            if binding.connection_id in self._records:
                raise AuthLifecycleError("DUPLICATE_CONNECTION")
            self._check_credential(binding)
            for status in self._routes(binding.connection_id):
                if status.connection_revision != binding.revision:
                    raise AuthLifecycleError("REGISTRY_BINDING_MISMATCH")
            rec = _Record(binding, expires)
            self._records[binding.connection_id] = rec
            self._emit(pending, AuthEventKind.REGISTERED, rec, now)
            return self._snapshot(rec)

    def revalidate(self, connection_id: str, expected_revision: int, *,
                   observation: AuthObservation | None = None) -> AuthSnapshot:
        """Confirm authentication with an observation or a registry probe of every route."""
        with self._operation() as pending:
            now = self._poll(pending)
            rec = self._writable(connection_id, expected_revision)
            if rec.locks:
                raise AuthLifecycleError("LOCKED")
            if rec.base is AuthState.ROTATING:
                raise AuthLifecycleError("ROTATION_IN_PROGRESS")
            if rec.base is AuthState.REVOKED:
                raise AuthLifecycleError("REAUTH_REQUIRED")
            routes = ()
            if observation is not None:
                observation = _rebuild_observation(observation)
            else:
                routes = self._routes(rec.connection_id)
                if not routes:
                    raise AuthLifecycleError("NO_REVALIDATION_SOURCE")
            binding = rec.binding
            if rec.expires_at is not None and now >= rec.expires_at:
                result, reason = AuthState.EXPIRED, "expires_at"
            else:
                try:
                    self._check_credential(binding)
                except CredentialRefUnavailable:
                    result, reason = AuthState.UNKNOWN, "credential_unavailable"
                else:
                    if observation is not None:
                        result, reason = self._from_observation(binding, observation)
                    else:
                        result, reason = self._from_probe(binding, routes)
            rec.base = result
            self._bump(rec)
            kind = AuthEventKind.REVALIDATED if result is AuthState.VALID else AuthEventKind.REVALIDATION_FAILED
            self._emit(pending, kind, rec, now, reason)
            return self._snapshot(rec)

    @staticmethod
    def _from_observation(binding: ConnectionBinding, observation: AuthObservation) -> tuple:
        if observation.connection_id != binding.connection_id or observation.revision != binding.revision:
            return AuthState.UNKNOWN, "stale_observation"
        if observation.state is ProviderAuthState.AUTHENTICATED and \
                observation.account_fingerprint != binding.account_fingerprint:
            return AuthState.UNKNOWN, "account_changed"
        return _PROVIDER_STATES.get(observation.state, AuthState.UNKNOWN), "observation:" + observation.state.value

    def _from_probe(self, binding: ConnectionBinding, routes: tuple) -> tuple:
        results = []
        for status in routes:
            try:
                probed = self._registry.probe(status.pool_id, status.model)
            except Exception:
                # A failed revalidation withdraws the earlier validation; no provider text is kept.
                results.append((AuthState.UNKNOWN, "probe_failed"))
                continue
            if probed.connection_revision != binding.revision:
                results.append((AuthState.UNKNOWN, "stale_binding"))
            else:
                state = _PROBE_STATES.get(probed.account_binding, AuthState.UNKNOWN)
                results.append((state, "probe:" + probed.account_binding))
        for result in results:
            if result[0] is not AuthState.VALID:
                return result
        return AuthState.VALID, "probe:bound"

    def mark_expired(self, connection_id: str, expected_revision: int) -> AuthSnapshot:
        """Record an expiry reported by the host or provider."""
        with self._operation() as pending:
            now = self._poll(pending)
            rec = self._writable(connection_id, expected_revision)
            if rec.base is AuthState.REVOKED:
                raise AuthLifecycleError("REAUTH_REQUIRED")
            rec.base = AuthState.EXPIRED
            self._bump(rec)
            self._emit(pending, AuthEventKind.EXPIRED, rec, now, "reported")
            return self._snapshot(rec)

    def begin_rotation(self, connection_id: str, expected_revision: int) -> AuthSnapshot:
        with self._operation() as pending:
            now = self._poll(pending)
            rec = self._writable(connection_id, expected_revision)
            if rec.locks:
                raise AuthLifecycleError("LOCKED")
            if rec.base is AuthState.ROTATING:
                raise AuthLifecycleError("ROTATION_IN_PROGRESS")
            if rec.base is AuthState.REVOKED:
                raise AuthLifecycleError("REAUTH_REQUIRED")
            rec.base = AuthState.ROTATING
            self._bump(rec)
            self._emit(pending, AuthEventKind.ROTATION_STARTED, rec, now)
            return self._snapshot(rec)

    def cancel_rotation(self, connection_id: str, expected_revision: int) -> AuthSnapshot:
        with self._operation() as pending:
            now = self._poll(pending)
            rec = self._writable(connection_id, expected_revision)
            if rec.base is not AuthState.ROTATING:
                raise AuthLifecycleError("NOT_ROTATING")
            rec.base = AuthState.UNKNOWN
            self._bump(rec)
            self._emit(pending, AuthEventKind.ROTATION_CANCELLED, rec, now)
            return self._snapshot(rec)

    def complete_rotation(self, connection_id: str, new_binding: ConnectionBinding, expected_revision: int, *,
                          expires_at: datetime | None = None) -> AuthSnapshot:
        """Install the rotated reference: new version, revalidation required, old runs reclaimed."""
        new = _rebuild_binding(new_binding)
        expires = _expiry(expires_at)
        with self._operation() as pending:
            now = self._poll(pending)
            rec = self._writable(connection_id, expected_revision)
            if rec.locks:
                raise AuthLifecycleError("LOCKED")
            if rec.base is not AuthState.ROTATING:
                raise AuthLifecycleError("NOT_ROTATING")
            self._check_successor(rec.binding, new, same_account=True)
            self._check_credential(new)
            self._sync_registry(new)
            rec.binding = new
            rec.credential_version += 1
            rec.base = AuthState.UNKNOWN
            rec.expires_at = expires
            self._bump(rec)
            self._emit(pending, AuthEventKind.ROTATED, rec, now)
            self._reclaim(pending, rec, now, "rotated", lambda run: run.binding.revision < new.revision)
            return self._snapshot(rec)

    def reauthenticate(self, connection_id: str, new_binding: ConnectionBinding, expected_revision: int, *,
                       expires_at: datetime | None = None) -> AuthSnapshot:
        """Install a re-authenticated binding; the connection stays unknown until revalidated."""
        new = _rebuild_binding(new_binding)
        expires = _expiry(expires_at)
        with self._operation() as pending:
            now = self._poll(pending)
            rec = self._writable(connection_id, expected_revision)
            if rec.locks:
                raise AuthLifecycleError("LOCKED")
            if rec.base is AuthState.ROTATING:
                raise AuthLifecycleError("ROTATION_IN_PROGRESS")
            self._check_successor(rec.binding, new, same_account=False)
            self._check_credential(new)
            self._sync_registry(new)
            if new.credential_ref != rec.binding.credential_ref:
                rec.credential_version += 1
            rec.binding = new
            rec.base = AuthState.UNKNOWN
            rec.expires_at = expires
            self._bump(rec)
            self._emit(pending, AuthEventKind.REAUTHENTICATED, rec, now)
            self._reclaim(pending, rec, now, "reauthenticated", lambda run: run.binding.revision < new.revision)
            return self._snapshot(rec)

    def force_logout(self, connection_id: str, expected_revision: int) -> AuthSnapshot:
        """Revoke the connection. Active runs need reclaim; nothing is marked done or refunded."""
        with self._operation() as pending:
            now = self._poll(pending)
            rec = self._writable(connection_id, expected_revision)
            rec.base = AuthState.REVOKED
            self._bump(rec)
            self._emit(pending, AuthEventKind.LOGOUT_FORCED, rec, now)
            self._reclaim(pending, rec, now, "forced_logout")
            return self._snapshot(rec)

    def delete_connection(self, connection_id: str, expected_revision: int) -> AuthSnapshot:
        """Tombstone the connection; its runs stay tracked until their actual state is reclaimed."""
        with self._operation() as pending:
            now = self._poll(pending)
            rec = self._writable(connection_id, expected_revision)
            rec.deleted = True
            rec.base = AuthState.REVOKED
            rec.locks = frozenset()
            self._bump(rec)
            self._emit(pending, AuthEventKind.DELETED, rec, now)
            self._reclaim(pending, rec, now, "connection_deleted")
            return self._snapshot(rec)

    # ------------------------------------------------------------ runs
    def begin_run(self, connection_id: str, run_id: str) -> RunTicket:
        """Admit a new run only on a valid, unlocked connection; returns the current reference version."""
        _check_id(connection_id, "connection_id")
        _check_id(run_id, "run_id")
        with self._operation() as pending:
            now = self._poll(pending)
            rec = self._records.get(connection_id)
            if rec is None or rec.deleted:
                raise UnknownConnection("The connection is not registered.")
            if run_id in self._runs:
                raise AuthLifecycleError("DUPLICATE_RUN")
            if rec.effective() is not AuthState.VALID:
                raise AuthenticationRequired("The connection authentication is not valid for new runs.")
            self._runs[run_id] = _Run(run_id, rec.binding, rec.credential_version)
            self._emit(pending, AuthEventKind.RUN_STARTED, rec, now, None, (run_id,))
            return RunTicket(run_id, connection_id, rec.binding, rec.credential_version)

    def record_observation(self, receipt: JobReceipt) -> RunRecord:
        """Apply an actual job observation made with the run's original binding and job.

        The first accepted receipt binds the run to its job_id; a receipt for a
        different job is rejected as RECEIPT_MISMATCH.
        """
        if type(receipt) is not JobReceipt:
            raise AuthLifecycleError("INVALID_RECEIPT")
        with self._operation() as pending:
            run = self._runs.get(receipt.request_id)
            if run is None:
                raise AuthLifecycleError("UNKNOWN_RUN")
            binding = run.binding
            if (receipt.connection_id, receipt.revision, receipt.account_fingerprint) != (
                    binding.connection_id, binding.revision, binding.account_fingerprint):
                raise AuthLifecycleError("RECEIPT_MISMATCH")
            if run.job_id is not None and receipt.provider_job_id != run.job_id:
                raise AuthLifecycleError("RECEIPT_MISMATCH")
            if run.status is RunStatus.CLOSED:
                if run.outcome != receipt.state.value:
                    raise AuthLifecycleError("RUN_ALREADY_CLOSED")
                return run.record()
            terminal = receipt.state in TERMINAL
            # Read the clock before any mutation so a clock failure leaves the run unchanged.
            now = self._now() if terminal else None
            if run.job_id is None:
                run.job_id = receipt.provider_job_id
            if terminal:
                run.status = RunStatus.CLOSED
                run.outcome = receipt.state.value
                rec = self._records[binding.connection_id]
                self._emit(pending, AuthEventKind.RUN_CLOSED, rec, now, run.outcome, (run.run_id,))
            return run.record()

    def run(self, run_id: str) -> RunRecord:
        with self._lock:
            run = self._runs.get(run_id) if type(run_id) is str else None
            if run is None:
                raise AuthLifecycleError("UNKNOWN_RUN")
            return run.record()

    def runs(self, *, connection_id: str | None = None, status: RunStatus | None = None) -> tuple:
        with self._lock:
            return tuple(
                run.record() for key, run in sorted(self._runs.items())
                if (connection_id is None or run.binding.connection_id == connection_id)
                and (status is None or run.status is status)
            )

    def refs_in_use(self) -> tuple:
        """Credential reference locators that must not be deleted yet.

        Includes the current binding of every non-deleted connection and the
        original binding of every run that is not closed (active or awaiting
        reclaim). This is advice for the caller; it does not block vault deletion.
        """
        with self._lock:
            refs = {rec.binding.credential_ref for rec in self._records.values() if not rec.deleted}
            refs.update(run.binding.credential_ref for run in self._runs.values()
                        if run.status is not RunStatus.CLOSED)
            return tuple(sorted(refs))

    # ------------------------------------------------------------ queries
    def poll(self) -> tuple:
        """Apply lock signals and clock expiry now; returns every snapshot."""
        with self._operation() as pending:
            self._poll(pending)
            return tuple(self._snapshot(self._records[cid]) for cid in sorted(self._records))

    def snapshot(self, connection_id: str) -> AuthSnapshot:
        _check_id(connection_id, "connection_id")
        with self._operation() as pending:
            self._poll(pending)
            rec = self._records.get(connection_id)
            if rec is None:
                raise UnknownConnection("The connection is not registered.")
            return self._snapshot(rec)

    def binding(self, connection_id: str) -> ConnectionBinding:
        return self._binding(connection_id)

    def _binding(self, connection_id: str) -> ConnectionBinding:
        _check_id(connection_id, "connection_id")
        with self._lock:
            rec = self._records.get(connection_id)
            if rec is None or rec.deleted:
                raise UnknownConnection("The connection is not registered.")
            return rec.binding

    def placement_candidates(self, model: str | None = None, *, require_qualified: bool = True) -> tuple:
        """Registry routes that are placeable now and whose connection auth is valid.

        require_qualified=True uses operational_inventory (qualified routes only);
        False uses executable_now for diagnostics and development checks.
        The auth exclusion holds only for callers of this method (or begin_run);
        reading the registry directly bypasses it.
        """
        if self._registry is None:
            raise AuthLifecycleError("NO_REGISTRY")
        if type(require_qualified) is not bool:
            raise AuthLifecycleError("INVALID_PLACEMENT_MODE")
        with self._operation() as pending:
            self._poll(pending)
            if require_qualified:
                statuses = self._registry.operational_inventory(model=model)
            else:
                statuses = tuple(s for s in self._registry.routes(model=model) if s.executable_now)
            selected = []
            for status in statuses:
                rec = self._records.get(status.connection_id)
                if rec is None or rec.deleted or rec.effective() is not AuthState.VALID:
                    continue
                if status.connection_revision != rec.binding.revision:
                    continue
                selected.append(status)
            return tuple(selected)

    def events(self) -> tuple:
        with self._lock:
            return tuple(self._events)

    @property
    def listener_failures(self) -> int:
        with self._lock:
            return self._listener_failures


__all__ = ["AuthLifecycle"]
