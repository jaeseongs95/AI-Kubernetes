"""Grant-scoped credential resolution for registered adapters only.

Authorization order (fail-closed): registered adapter handle (identity, not
name) -> grant exists, not revoked, not expired -> adapter/workspace/task match
-> connection in scope -> remaining use (consumed here) -> active lease ->
connection still API/verified with the binding captured at issuance -> audit
-> vault resolve -> adapter callback. Plaintext never enters audit, logs,
exceptions or the return value. A grant_id is a bearer capability, so audit
records carry only a non-reversible reference to it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import logging
import math
import secrets
import threading
import time

from aik_connection.models import Connection, rebuild
from aik_runtime import Lease, RuntimeStore
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier
from aik_security.redaction import OutputRedactor
from aik_vault import VaultBackend, is_credential_ref

from .errors import (
    MESSAGES, AdapterFailed, BrokerDenied, BrokerError, CredentialUnavailable, SecretLeakBlocked,
)
from .injection import ChildResult, _SecretDetector, run_injected, validate_command


LOGGER = logging.getLogger("aik_broker")
MAX_TTL_SECONDS = 3600
MAX_USES = 16
MAX_GRANTS = 4096
MAX_CONNECTIONS_PER_GRANT = 16
AUDIT_CAPACITY = 4096
_MAX_RESULT_DEPTH = 4


def _valid_id(value: object) -> bool:
    try:
        identifier(value)
    except InvalidRecord:
        return False
    return True


def grant_audit_ref(grant_id: object) -> str | None:
    """Non-reversible audit reference for a grant_id (the grant_id is a bearer capability)."""
    if not _valid_id(grant_id):
        return None
    return "gref-" + hashlib.sha256(grant_id.encode("utf-8")).hexdigest()[:24]


class AdapterHandle:
    """A capability object issued by register_adapter; compared by identity.

    A renderer can only send serializable values, so it cannot present this
    object. A newly constructed handle with the same names is not registered.
    """

    __slots__ = ("adapter_id", "provider", "__weakref__")

    def __init__(self, adapter_id: str, provider: str):
        self.adapter_id = adapter_id
        self.provider = provider

    def __repr__(self) -> str:
        return f"AdapterHandle(adapter_id={self.adapter_id!r}, provider={self.provider!r})"

    def __reduce_ex__(self, protocol):
        raise TypeError("An adapter handle cannot be serialized or copied.")


@dataclass(frozen=True, slots=True)
class Grant:
    """Scope metadata only: IDs, times and limits. No credential_ref or secret."""

    grant_id: str
    workspace_id: str
    task_id: str
    adapter_id: str
    lease_id: str
    fencing_token: int
    connection_ids: tuple
    issued_at: float
    expires_at: float
    max_uses: int


@dataclass(frozen=True, slots=True)
class _Binding:
    connection_id: str
    credential_ref: str
    binding_revision: int
    provenance_id: str


class _GrantState:
    __slots__ = ("grant", "bindings", "uses", "revoked")

    def __init__(self, grant: Grant, bindings: dict):
        self.grant = grant
        self.bindings = bindings
        self.uses = 0
        self.revoked = False


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Audit entry. Fields are IDs and fixed codes; unsafe IDs become None.

    ``grant_id`` holds ``grant_audit_ref(grant_id)``, never the raw grant_id.
    """

    sequence: int
    at: float
    event: str
    outcome: str
    code: str | None
    grant_id: str | None
    workspace_id: str | None
    task_id: str | None
    adapter_id: str | None
    connection_id: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _contains_secret(value, detector: _SecretDetector, secret: str, depth: int = 0) -> bool:
    """Fail closed: unknown types, subclasses and excessive nesting count as leaks.

    Accepted result types: None, bool, int, float, str, bytes, bytearray,
    ChildResult, and list/tuple/dict of these up to four nesting levels.
    """
    if depth > _MAX_RESULT_DEPTH:
        return True
    kind = type(value)
    if value is None or kind in (bool, int, float):
        return False
    if kind is str:
        return detector.present(value)
    if kind in (bytes, bytearray):
        return secret.encode("utf-8") in value or detector.present(bytes(value).decode("utf-8", "replace"))
    if kind is ChildResult:
        if type(value.returncode) is not int or type(value.stdout) is not str or type(value.stderr) is not str:
            return True
        return detector.present(value.stdout) or detector.present(value.stderr)
    if kind in (list, tuple):
        return any(_contains_secret(item, detector, secret, depth + 1) for item in value)
    if kind is dict:
        return any(_contains_secret(key, detector, secret, depth + 1) or
                   _contains_secret(item, detector, secret, depth + 1) for key, item in value.items())
    return True


class RuntimeLeaseChecker:
    """Lease predicate over aik_runtime: state active, same fencing token, not expired."""

    def __init__(self, store: RuntimeStore, now=None):
        if not isinstance(store, RuntimeStore) or (now is not None and not callable(now)):
            raise TypeError("A runtime store and an optional clock callable are required.")
        self._store = store
        self._now = now if now is not None else (lambda: datetime.now().astimezone())

    def __call__(self, lease_id: str, fencing_token: int) -> bool:
        try:
            if type(fencing_token) is not int:
                return False
            entry = self._store.get(Lease, lease_id)
            if entry is None:
                return False
            lease = entry.value
            now = self._now()
            if not isinstance(now, datetime) or now.utcoffset() is None:
                return False
            expires = datetime.fromisoformat(lease.expires_at.replace("Z", "+00:00"))
            return lease.state == "active" and lease.fencing_token == fencing_token and now < expires
        except Exception:
            return False


class CredentialBroker:
    """Resolve vault secrets only for an authorized adapter call.

    ``connections.get(connection_id)`` must return an object whose ``value``
    is an ``aik_connection.Connection`` (``ConnectionStore.get`` does), or None.
    ``lease_checker(lease_id, fencing_token)`` must return True for an active
    lease. ``clock`` returns monotonic seconds and is injectable for tests.
    """

    def __init__(self, vault: VaultBackend, connections, lease_checker, *, clock=time.monotonic,
                 audit_sink=None):
        if not isinstance(vault, VaultBackend):
            raise TypeError("An aik_vault backend is required.")
        if not callable(getattr(connections, "get", None)) or not callable(lease_checker) or not callable(clock):
            raise TypeError("A connection source, lease checker and clock are required.")
        if audit_sink is not None and not callable(audit_sink):
            raise TypeError("The audit sink must be callable.")
        self._vault = vault
        self._connections = connections
        self._lease_checker = lease_checker
        self._clock = clock
        self._sink = audit_sink
        self._lock = threading.RLock()
        self._grants: dict[str, _GrantState] = {}
        self._handles: dict[str, AdapterHandle] = {}
        # grant_id -> cancellation events of children started for that grant.
        self._cancels: dict[str, set] = {}
        self._audit = deque(maxlen=AUDIT_CAPACITY)
        self._sequence = 0
        self._last_now = 0.0
        self._redactor = OutputRedactor()

    def __repr__(self) -> str:
        return "CredentialBroker()"

    def __reduce_ex__(self, protocol):
        raise TypeError("The credential broker cannot be serialized.")

    # ----------------------------------------------------------- internals
    def _now(self) -> float | None:
        try:
            value = self._clock()
        except Exception:
            return None
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return None
        # A clock rollback must not extend a grant's lifetime.
        self._last_now = max(self._last_now, float(value))
        return self._last_now

    def _record(self, event: str, outcome: str, code: str | None = None, *, strict: bool = False,
                grant_id=None, workspace_id=None, task_id=None, adapter_id=None, connection_id=None) -> None:
        safe = self._redactor.safe_id
        if code is not None and (type(code) is not str or code not in MESSAGES):
            # Only fixed codes may be recorded; a forged code could carry a secret.
            code = "INTERNAL_ERROR"
        with self._lock:
            self._sequence += 1
            record = AuditRecord(self._sequence, self._last_now, event, outcome, code, grant_audit_ref(grant_id),
                                 safe(workspace_id), safe(task_id), safe(adapter_id), safe(connection_id))
            self._audit.append(record)
        payload = record.to_dict()
        LOGGER.info("credential broker audit %s", json.dumps(self._redactor.sanitize(payload), sort_keys=True))
        if self._sink is None:
            return
        failed = False
        try:
            self._sink(self._redactor.output("log", payload))
        except Exception:
            failed = True
        if failed and strict:
            raise BrokerError("AUDIT_UNAVAILABLE")

    def _registered(self, handle) -> bool:
        if type(handle) is not AdapterHandle:
            return False
        with self._lock:
            return self._handles.get(handle.adapter_id) is handle

    def _lease_ok(self, lease_id: str, fencing_token: int) -> bool:
        try:
            return self._lease_checker(lease_id, fencing_token) is True
        except Exception:
            return False

    def _check_connection(self, connection_id: str, workspace_id: str, provider: str):
        connection = None
        try:
            entry = self._connections.get(connection_id)
            if entry is not None:
                connection = rebuild(entry.value, Connection)
        except Exception:
            connection = None
        if connection is None or connection.connection_id != connection_id:
            return None, "CONNECTION_UNAVAILABLE"
        if connection.workspace_id != workspace_id:
            return None, "WORKSPACE_MISMATCH"
        if connection.provider != provider:
            return None, "PROVIDER_MISMATCH"
        if connection.auth_kind != "api_key" or connection.execution_kind != "api" \
                or not is_credential_ref(connection.credential_ref):
            return None, "NO_BROKER_SECRET"
        if connection.verified_at is None:
            return None, "AUTH_REQUIRED"
        return connection, None

    def _purge(self) -> None:
        now = self._last_now
        # Keep grants with running children so a later revoke can still stop them.
        for grant_id in [key for key, state in self._grants.items()
                         if key not in self._cancels and (
                             state.revoked or now >= state.grant.expires_at
                             or state.uses >= state.grant.max_uses)]:
            del self._grants[grant_id]

    def _issue(self, workspace_id, task_id, adapter_id, connection_ids, lease_id, fencing_token,
               ttl_seconds, max_uses):
        if not all(_valid_id(value) for value in (workspace_id, task_id, adapter_id, lease_id)):
            return "INVALID_REQUEST", None
        if type(connection_ids) not in (tuple, list) or not 1 <= len(connection_ids) <= MAX_CONNECTIONS_PER_GRANT \
                or not all(_valid_id(value) for value in connection_ids) \
                or len(set(connection_ids)) != len(connection_ids):
            return "INVALID_REQUEST", None
        if type(fencing_token) is not int or not 1 <= fencing_token <= MAX_INTEGER:
            return "INVALID_REQUEST", None
        if type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds) \
                or not 0 < ttl_seconds <= MAX_TTL_SECONDS:
            return "INVALID_REQUEST", None
        if type(max_uses) is not int or not 1 <= max_uses <= MAX_USES:
            return "INVALID_REQUEST", None
        with self._lock:
            handle = self._handles.get(adapter_id)
        if handle is None:
            return "ADAPTER_NOT_ALLOWED", None
        if not self._lease_ok(lease_id, fencing_token):
            return "LEASE_INACTIVE", None
        bindings = {}
        for connection_id in connection_ids:
            connection, code = self._check_connection(connection_id, workspace_id, handle.provider)
            if code is not None:
                return code, None
            bindings[connection_id] = _Binding(connection_id, connection.credential_ref,
                                               connection.binding_revision, connection.provenance_id)
        with self._lock:
            now = self._now()
            if now is None:
                return "CLOCK_UNAVAILABLE", None
            if len(self._grants) >= MAX_GRANTS:
                self._purge()
            if len(self._grants) >= MAX_GRANTS:
                return "GRANT_CAPACITY", None
            grant = Grant("grant-" + secrets.token_hex(16), workspace_id, task_id, adapter_id, lease_id,
                          fencing_token, tuple(connection_ids), now, now + float(ttl_seconds), max_uses)
            self._grants[grant.grant_id] = _GrantState(grant, bindings)
        return None, grant

    def _authorize(self, handle, grant_id, workspace_id, task_id, connection_id):
        if not self._registered(handle):
            return "ADAPTER_NOT_ALLOWED", None, None
        if not all(_valid_id(value) for value in (grant_id, workspace_id, task_id, connection_id)):
            return "INVALID_REQUEST", None, None
        with self._lock:
            now = self._now()
            if now is None:
                return "CLOCK_UNAVAILABLE", None, None
            state = self._grants.get(grant_id)
            if state is None:
                return "GRANT_UNKNOWN", None, None
            grant = state.grant
            if state.revoked:
                return "GRANT_REVOKED", None, None
            if now >= grant.expires_at:
                return "GRANT_EXPIRED", None, None
            if grant.adapter_id != handle.adapter_id:
                return "ADAPTER_NOT_ALLOWED", None, None
            if grant.workspace_id != workspace_id:
                return "WORKSPACE_MISMATCH", None, None
            if grant.task_id != task_id:
                return "TASK_MISMATCH", None, None
            if connection_id not in state.bindings:
                return "CONNECTION_OUT_OF_SCOPE", None, None
            if state.uses >= grant.max_uses:
                return "GRANT_EXHAUSTED", None, None
            # Consume before any external check: a later denial still spends it.
            state.uses += 1
            binding = state.bindings[connection_id]
        if not self._lease_ok(grant.lease_id, grant.fencing_token):
            return "LEASE_INACTIVE", None, None
        connection, code = self._check_connection(connection_id, grant.workspace_id, handle.provider)
        if code is not None:
            return code, None, None
        if (connection.credential_ref, connection.binding_revision, connection.provenance_id) != (
                binding.credential_ref, binding.binding_revision, binding.provenance_id):
            return "BINDING_CHANGED", None, None
        return None, grant, binding

    # ---------------------------------------------------------- public API
    def register_adapter(self, adapter_id: str, provider: str) -> AdapterHandle:
        """Trusted main/backend only. Returns the capability used for injection."""
        if not _valid_id(adapter_id) or not _valid_id(provider):
            self._record("adapter_denied", "denied", "INVALID_REQUEST")
            raise BrokerDenied("INVALID_REQUEST")
        with self._lock:
            exists = adapter_id in self._handles
            if not exists:
                handle = AdapterHandle(adapter_id, provider)
                self._handles[adapter_id] = handle
        if exists:
            self._record("adapter_denied", "denied", "ADAPTER_EXISTS", adapter_id=adapter_id)
            raise BrokerDenied("ADAPTER_EXISTS")
        self._record("adapter_registered", "info", adapter_id=adapter_id)
        return handle

    def unregister_adapter(self, handle: AdapterHandle) -> bool:
        with self._lock:
            removed = self._registered(handle)
            if removed:
                del self._handles[handle.adapter_id]
        if removed:
            self._record("adapter_unregistered", "info", adapter_id=handle.adapter_id)
        return removed

    def issue_grant(self, *, workspace_id: str, task_id: str, adapter_id: str, connection_ids,
                    lease_id: str, fencing_token: int, ttl_seconds: float = 60.0, max_uses: int = 1) -> Grant:
        """Bind adapter, workspace, task, lease and connections for a short TTL.

        The returned grant_id is a bearer capability: treat it as secret.
        """
        code, grant = self._issue(workspace_id, task_id, adapter_id, connection_ids, lease_id, fencing_token,
                                  ttl_seconds, max_uses)
        if code is not None:
            self._record("grant_denied", "denied", code, workspace_id=workspace_id, task_id=task_id,
                         adapter_id=adapter_id)
            raise BrokerDenied(code)
        self._record("grant_issued", "info", grant_id=grant.grant_id, workspace_id=workspace_id,
                     task_id=task_id, adapter_id=adapter_id)
        return grant

    def inject(self, handle: AdapterHandle, grant_id: str, *, workspace_id: str, task_id: str,
               connection_id: str, consumer):
        """Call ``consumer(plaintext)`` once if authorized and return its result.

        The result must be None, bool, int, float, str, bytes, bytearray,
        ChildResult or list/tuple/dict of these (four levels) and must not
        contain the plaintext; anything else is withheld as SECRET_IN_OUTPUT.
        Consumer exceptions are replaced by AdapterFailed without a cause or
        context chain.
        """
        ids = {"grant_id": grant_id, "workspace_id": workspace_id, "task_id": task_id,
               "connection_id": connection_id,
               "adapter_id": handle.adapter_id if type(handle) is AdapterHandle else None}
        if not callable(consumer):
            code, binding = "INVALID_REQUEST", None
        else:
            code, _grant, binding = self._authorize(handle, grant_id, workspace_id, task_id, connection_id)
        if code is not None:
            self._record("resolve_denied", "denied", code, **ids)
            raise BrokerDenied(code)
        self._record("resolve_allowed", "allowed", strict=True, **ids)
        plaintext = None
        try:
            plaintext = self._vault.resolve(binding.credential_ref)
        except Exception:
            plaintext = None
        if type(plaintext) is not str or not plaintext:
            plaintext = None
            self._record("resolve_failed", "denied", "CREDENTIAL_UNAVAILABLE", **ids)
            raise CredentialUnavailable("CREDENTIAL_UNAVAILABLE")
        failure = None
        result = None
        try:
            result = consumer(plaintext)
        except BrokerError as error:
            try:
                candidate = error.code
            except Exception:
                candidate = None
            failure = candidate if type(candidate) is str and candidate in MESSAGES else "ADAPTER_FAILED"
        except Exception:
            failure = "ADAPTER_FAILED"
        if failure is None and _contains_secret(result, _SecretDetector(plaintext), plaintext):
            failure = "SECRET_IN_OUTPUT"
        plaintext = None
        if failure is not None:
            result = None
            self._record("injection_failed", "denied", failure, **ids)
            if failure == "SECRET_IN_OUTPUT":
                raise SecretLeakBlocked(failure)
            raise AdapterFailed(failure)
        self._record("injection_completed", "allowed", **ids)
        return result

    def run_with_credential(self, handle: AdapterHandle, grant_id: str, *, workspace_id: str, task_id: str,
                            connection_id: str, argv, delivery: str = "stdin", env_name: str | None = None,
                            base_env=None, cwd=None, timeout: float = 30.0) -> ChildResult:
        """Adapter injection into a child process via stdin or one env variable.

        Revoking the grant (directly or via revoke_task/revoke_lease) while the
        child runs terminates it and raises AdapterFailed(CHILD_CANCELLED).
        """
        try:
            validate_command(argv, delivery=delivery, env_name=env_name, base_env=base_env, timeout=timeout, cwd=cwd)
            valid = True
        except BrokerError:
            valid = False
        if not valid:
            self._record("resolve_denied", "denied", "INVALID_REQUEST", grant_id=grant_id,
                         workspace_id=workspace_id, task_id=task_id, connection_id=connection_id)
            raise BrokerDenied("INVALID_REQUEST")
        command = tuple(argv)
        cancel = threading.Event()
        key = grant_id if _valid_id(grant_id) else None
        if key is not None:
            # Registered before authorization: a revoke after this point either
            # makes authorization fail or sets this event.
            with self._lock:
                self._cancels.setdefault(key, set()).add(cancel)
        try:
            return self.inject(handle, grant_id, workspace_id=workspace_id, task_id=task_id,
                               connection_id=connection_id,
                               consumer=lambda secret: run_injected(command, secret, delivery=delivery,
                                                                    env_name=env_name, base_env=base_env,
                                                                    cwd=cwd, timeout=timeout, cancel=cancel))
        finally:
            if key is not None:
                with self._lock:
                    events = self._cancels.get(key)
                    if events is not None:
                        events.discard(cancel)
                        if not events:
                            del self._cancels[key]

    def revoke(self, grant_id: str) -> bool:
        """Reject later requests and terminate running children of this grant."""
        with self._lock:
            valid = _valid_id(grant_id)
            state = self._grants.get(grant_id) if valid else None
            changed = state is not None and not state.revoked
            if changed:
                state.revoked = True
            events = tuple(self._cancels.get(grant_id, ())) if valid else ()
        for event in events:
            event.set()
        if changed:
            self._record("grant_revoked", "info", grant_id=grant_id)
        return changed

    def _revoke_where(self, predicate) -> int:
        with self._lock:
            targets = [state.grant.grant_id for state in self._grants.values()
                       if not state.revoked and predicate(state.grant)]
        return sum(1 for grant_id in targets if self.revoke(grant_id))

    def revoke_task(self, task_id: str) -> int:
        return self._revoke_where(lambda grant: grant.task_id == task_id)

    def revoke_lease(self, lease_id: str) -> int:
        return self._revoke_where(lambda grant: grant.lease_id == lease_id)

    def audit_records(self) -> tuple:
        with self._lock:
            return tuple(self._audit)

    def active_grants(self) -> tuple:
        with self._lock:
            now = self._last_now
            return tuple(state.grant for state in self._grants.values()
                         if not state.revoked and now < state.grant.expires_at
                         and state.uses < state.grant.max_uses)
