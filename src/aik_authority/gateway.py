"""Scoped tool gateway for director/worker/reviewer sessions.

Fail-closed order for a tool call (first failure wins, fixed code):
session handle -> request shape -> workspace -> authorization revision,
withdrawal, expiry -> lease -> tool declared / reserved / role allowlist /
profile / VM authorization tools / delegated scope -> effect kind -> target ->
input revision -> credential scope -> effect identity -> replay -> VM intent.

Policy changes are serialized by revision and pinned per session (not applied
retroactively). VM authorization revisions are checked on every call; a newer
VM revision makes older sessions stale. Guards live in process memory only.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
import threading
from typing import Any, Mapping, Protocol

from aik_broker import Grant
from aik_profiles import ExecutionChoice, ExecutionDecision, rebuild_profile
from aik_runtime.errors import InvalidRecord, RevisionConflict
from aik_runtime.models import MAX_INTEGER, identifier
from aik_vm_bridge import TaskExecutionBinding, VmBridgeError

from .errors import AuthorityCode as C, AuthorityDenied, AuthorityError, EffectOutcomeUnknown
from .models import (
    ROLES, STATUS_AUTHORIZED, AuthorizationScope, EffectKind, EffectReceipt, ToolCall,
    ToolCallAuthorization, ToolDeclaration, ToolPolicy, canonical_arguments, is_reserved_tool,
    rebuild_call, rebuild_policy, rebuild_scope, request_id,
)


AUDIT_CAPACITY = 4096
RECEIPT_STATUSES = ("succeeded", "failed", "cancelled", "unknown")
_VM_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
_STATE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class VmEffectAuthorityPort(Protocol):
    """Assumed VM public effect-authority extension (not in the pinned VM surface).

    ``record_intent`` must echo ``effect_id`` and ``request_digest``;
    ``record_receipt`` and ``read_effect`` return ``{"effect_id", "state"}``
    (``read_effect`` returns None when VM has no intent for the effect).
    """

    def record_intent(self, intent: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def record_receipt(self, receipt: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def read_effect(self, effect_id: str) -> Mapping[str, Any] | None: ...


class ToolSession:
    """Opaque capability handle compared by identity. Carries no authority object."""

    __slots__ = ("session_id", "role", "workspace_id", "__weakref__")

    def __init__(self, session_id: str, role: str, workspace_id: str):
        self.session_id = session_id
        self.role = role
        self.workspace_id = workspace_id

    def __repr__(self) -> str:
        return f"ToolSession(session_id={self.session_id!r}, role={self.role!r})"

    def __reduce_ex__(self, protocol):
        raise TypeError("A tool session handle cannot be serialized or copied.")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    sequence: int
    event: str
    outcome: str
    code: str | None
    session_id: str | None
    role: str | None
    tool_call_id: str | None
    effect_id: str | None
    request_digest: str | None

    def to_dict(self) -> dict:
        return asdict(self)


class _Session:
    __slots__ = ("handle", "role", "workspace_id", "policy", "policy_revision", "binding", "scope",
                 "authorization_revision", "profile_tools", "parent_tools", "connections", "closed", "parent_id")

    def __init__(self, **values):
        for name in self.__slots__:
            setattr(self, name, values.get(name))


class _Effect:
    __slots__ = ("tool_call_id", "digest", "kind", "state")

    def __init__(self, tool_call_id: str, digest: str, kind: EffectKind, state: str):
        self.tool_call_id = tool_call_id
        self.digest = digest
        self.kind = kind
        self.state = state


def _safe_id(value: object) -> str | None:
    try:
        identifier(value)
    except InvalidRecord:
        return None
    return value


def _vm_code(error: Exception) -> str | None:
    code = getattr(error, "code", None)
    return code if type(code) is str and _VM_CODE.fullmatch(code) else None


def _expected(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise AuthorityDenied(C.INVALID_REQUEST, subject="expected_revision")
    return value


def _profile_view(decision: object) -> tuple[str, str, frozenset]:
    """(role, workspace, bound tools) of a bound aik_profiles decision."""
    if type(decision) is not ExecutionDecision:
        raise AuthorityDenied(C.PROFILE_NOT_BOUND, subject="decision")
    try:
        profile = rebuild_profile(decision.requested_profile)
        bound = decision.bound
        if decision.status != "bound" or type(bound) is not ExecutionChoice:
            raise ValueError
        bound = ExecutionChoice(bound.connection_id, bound.model, bound.effort, bound.tools)
        if bound not in (profile.primary, *profile.fallbacks):
            raise ValueError
    except Exception:
        raise AuthorityDenied(C.PROFILE_NOT_BOUND, subject="decision") from None
    return profile.role, profile.workspace_id, frozenset(bound.tools)


def _binding(value: object) -> TaskExecutionBinding:
    if type(value) is not TaskExecutionBinding:
        raise AuthorityDenied(C.INVALID_REQUEST, subject="binding")
    try:
        return TaskExecutionBinding.from_mapping(value.to_mapping())
    except (VmBridgeError, TypeError, AttributeError):
        raise AuthorityDenied(C.INVALID_REQUEST, subject="binding") from None


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AuthorityGateway:
    """Scoped tool gateway. Trusted backend owns this object; agents get ToolSession handles."""

    def __init__(self, *, lease_checker, clock=None, effect_port=None):
        if not callable(lease_checker) or (clock is not None and not callable(clock)):
            raise TypeError("A lease checker and an optional clock callable are required.")
        self._lease_checker = lease_checker
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._port = effect_port
        self._lock = threading.RLock()
        self._policies: dict[str, tuple[ToolPolicy, int]] = {}
        self._authorizations: dict[str, list] = {}  # id -> [scope, revision, withdrawn]
        self._sessions: dict[str, _Session] = {}
        self._calls: dict[str, ToolCallAuthorization] = {}
        self._effects: dict[str, _Effect] = {}
        self._unresolved: set[str] = set()
        self._audit = deque(maxlen=AUDIT_CAPACITY)
        self._sequence = 0

    def __repr__(self) -> str:
        return "AuthorityGateway()"

    def __reduce_ex__(self, protocol):
        raise TypeError("The authority gateway cannot be serialized.")

    # ------------------------------------------------------------ internals
    def _record(self, event: str, outcome: str, code=None, **ids) -> None:
        with self._lock:
            self._sequence += 1
            self._audit.append(AuditRecord(
                self._sequence, event, outcome, None if code is None else str(code),
                ids.get("session_id"), ids.get("role"), ids.get("tool_call_id"), ids.get("effect_id"),
                ids.get("request_digest"),
            ))

    def _denied(self, event: str, error: AuthorityError, session=None, call=None) -> None:
        session_id = role = tool_call_id = None
        if type(session) is ToolSession and type(session.session_id) is str:
            state = self._sessions.get(session.session_id)
            if state is not None and state.handle is session:
                session_id, role = state.handle.session_id, state.role
        if type(call) is ToolCall:
            tool_call_id = _safe_id(call.tool_call_id)
        self._record(event, "denied", error.code, session_id=session_id, role=role, tool_call_id=tool_call_id)

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise AuthorityDenied(C.CLOCK_UNAVAILABLE) from None
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise AuthorityDenied(C.CLOCK_UNAVAILABLE)
        return value

    def _lease_ok(self, binding: TaskExecutionBinding) -> bool:
        try:
            return self._lease_checker(binding.lease_id, binding.lease_fencing_token) is True
        except Exception:
            return False

    def _state(self, session: object, *, allow_closed: bool = False) -> _Session:
        if type(session) is not ToolSession or type(session.session_id) is not str:
            raise AuthorityDenied(C.UNKNOWN_SESSION)
        state = self._sessions.get(session.session_id)
        if state is None or state.handle is not session:
            raise AuthorityDenied(C.UNKNOWN_SESSION)
        if state.closed and not allow_closed:
            raise AuthorityDenied(C.SESSION_CLOSED)
        return state

    def _check_live(self, state: _Session) -> datetime:
        entry = self._authorizations.get(state.scope.vm_authorization_id)
        if entry is None:
            raise AuthorityDenied(C.AUTHORIZATION_UNKNOWN)
        if entry[2]:
            raise AuthorityDenied(C.AUTHORIZATION_WITHDRAWN)
        if entry[1] != state.authorization_revision:
            raise AuthorityDenied(C.STALE_AUTHORIZATION_REVISION, subject="authorization_revision")
        now = self._now()
        if now >= state.scope.expiry():
            raise AuthorityDenied(C.AUTHORIZATION_EXPIRED)
        if not self._lease_ok(state.binding):
            raise AuthorityDenied(C.LEASE_INACTIVE)
        return now

    @staticmethod
    def _effective(state: _Session) -> frozenset:
        tools = state.policy.allowed(state.role) & state.profile_tools & frozenset(state.scope.tools)
        return tools if state.parent_tools is None else tools & state.parent_tools

    def _tool(self, state: _Session, tool_id: str) -> ToolDeclaration:
        if is_reserved_tool(tool_id):
            raise AuthorityDenied(C.RESERVED_TOOL, subject="tool_id")
        declaration = state.policy.declaration(tool_id)
        if declaration is None:
            raise AuthorityDenied(C.TOOL_NOT_DECLARED, subject="tool_id")
        if tool_id not in state.policy.allowed(state.role):
            raise AuthorityDenied(C.TOOL_NOT_ALLOWED_FOR_ROLE, subject="tool_id")
        if tool_id not in state.profile_tools:
            raise AuthorityDenied(C.TOOL_NOT_IN_PROFILE, subject="tool_id")
        if tool_id not in state.scope.tools:
            raise AuthorityDenied(C.TOOL_NOT_IN_AUTHORIZATION, subject="tool_id", details=(tool_id,))
        if state.parent_tools is not None and tool_id not in state.parent_tools:
            raise AuthorityDenied(C.SCOPE_ESCALATION, subject="tool_id", details=(tool_id,))
        return declaration

    def _effect_port(self):
        port = self._port
        if port is None or not all(callable(getattr(port, name, None))
                                   for name in ("record_intent", "record_receipt", "read_effect")):
            raise AuthorityDenied(C.SEAM_UNAVAILABLE, subject="vm-effect-authority")
        return port

    def _mark_unknown(self, effect_id: str, tool_call_id: str, digest: str, kind: EffectKind, state: str) -> None:
        effect = self._effects.get(effect_id)
        if effect is None:
            self._effects[effect_id] = _Effect(tool_call_id, digest, kind, state)
        else:
            effect.state = state
        self._unresolved.add(effect_id)

    @staticmethod
    def _digest(state: _Session, call: ToolCall, kind: EffectKind, arguments: str) -> str:
        binding, scope = state.binding, state.scope
        payload = {
            "format": "aik.tool-request.v1",
            "tool_id": call.tool_id, "effect_kind": kind.value, "effect_id": call.effect_id,
            "target": call.target, "input_revision": call.input_revision, "workspace_id": call.workspace_id,
            "connection_id": call.connection_id,
            "arguments_sha256": hashlib.sha256(arguments.encode("ascii")).hexdigest(),
            "vm_project_id": binding.vm_project_id, "vm_task_id": binding.vm_task_id,
            "vm_attempt_id": binding.vm_attempt_id, "vm_authorization_id": scope.vm_authorization_id,
            "authorization_revision": state.authorization_revision,
            "lease_id": binding.lease_id, "lease_fencing_token": binding.lease_fencing_token,
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()

    # --------------------------------------------------------------- policy
    def set_policy(self, policy: ToolPolicy, expected_revision: int = 0) -> int:
        """Serialize tool policy changes; running sessions keep their pinned revision."""
        policy = rebuild_policy(policy)
        expected = _expected(expected_revision)
        with self._lock:
            current = self._policies.get(policy.workspace_id)
            revision = 0 if current is None else current[1]
            if expected != revision:
                raise RevisionConflict("The tool policy revision has changed.")
            if revision == MAX_INTEGER:
                raise RevisionConflict("The tool policy revision cannot be incremented.")
            self._policies[policy.workspace_id] = (policy, revision + 1)
        self._record("policy_updated", "info")
        return revision + 1

    def policy(self, workspace_id: str) -> tuple[ToolPolicy, int] | None:
        request_id(workspace_id, "workspace_id")
        with self._lock:
            return self._policies.get(workspace_id)

    # ------------------------------------------------------- authorizations
    def register_authorization(self, scope: AuthorizationScope, expected_revision: int = 0) -> int:
        """Record a VM authorization scope (evidence reference) with revision serialization."""
        scope = rebuild_scope(scope)
        expected = _expected(expected_revision)
        with self._lock:
            entry = self._authorizations.get(scope.vm_authorization_id)
            revision = 0 if entry is None else entry[1]
            if expected != revision:
                raise RevisionConflict("The authorization revision has changed.")
            if entry is not None:
                if entry[2]:
                    raise AuthorityDenied(C.AUTHORIZATION_WITHDRAWN)
                if (entry[0].vm_project_id, entry[0].workspace_id) != (scope.vm_project_id, scope.workspace_id):
                    raise AuthorityDenied(C.INVALID_REQUEST, subject="scope")
            if revision == MAX_INTEGER:
                raise RevisionConflict("The authorization revision cannot be incremented.")
            self._authorizations[scope.vm_authorization_id] = [scope, revision + 1, False]
        self._record("authorization_recorded", "info")
        return revision + 1

    def withdraw_authorization(self, vm_authorization_id: str, expected_revision: int) -> int:
        request_id(vm_authorization_id, "vm_authorization_id")
        expected = _expected(expected_revision)
        with self._lock:
            entry = self._authorizations.get(vm_authorization_id)
            if entry is None or entry[1] != expected or entry[1] == MAX_INTEGER:
                raise RevisionConflict("The authorization revision has changed or it is absent.")
            entry[1] += 1
            entry[2] = True
            revision = entry[1]
        self._record("authorization_withdrawn", "info")
        return revision

    def authorization_revision(self, vm_authorization_id: str) -> int | None:
        request_id(vm_authorization_id, "vm_authorization_id")
        with self._lock:
            entry = self._authorizations.get(vm_authorization_id)
            return None if entry is None else entry[1]

    # ------------------------------------------------------------- sessions
    def open_director_session(self, *, binding: TaskExecutionBinding, vm_authorization_id: str,
                              authorization_revision: int, decision: ExecutionDecision,
                              grant: Grant | None = None) -> ToolSession:
        try:
            with self._lock:
                return self._open(binding, vm_authorization_id, authorization_revision, decision, grant)
        except AuthorityError as error:
            self._denied("session_denied", error)
            raise

    def _open(self, binding, vm_authorization_id, authorization_revision, decision, grant) -> ToolSession:
        binding = _binding(binding)
        request_id(vm_authorization_id, "vm_authorization_id")
        if type(authorization_revision) is not int or not 1 <= authorization_revision <= MAX_INTEGER:
            raise AuthorityDenied(C.INVALID_REQUEST, subject="authorization_revision")
        role, workspace_id, profile_tools = _profile_view(decision)
        if role != "director":
            raise AuthorityDenied(C.ROLE_MISMATCH, subject="decision")
        entry = self._authorizations.get(vm_authorization_id)
        if entry is None:
            raise AuthorityDenied(C.AUTHORIZATION_UNKNOWN)
        scope = entry[0]
        if scope.workspace_id != workspace_id:
            raise AuthorityDenied(C.WORKSPACE_MISMATCH, subject="decision")
        current = self._policies.get(workspace_id)
        if current is None:
            raise AuthorityDenied(C.POLICY_UNKNOWN)
        if scope.vm_project_id != binding.vm_project_id:
            raise AuthorityDenied(C.BINDING_MISMATCH, subject="vm_project_id")
        if scope.input_revision != binding.vm_plan_revision_id:
            raise AuthorityDenied(C.STALE_INPUT_REVISION, subject="vm_plan_revision_id")
        connections = None
        if grant is not None:
            if type(grant) is not Grant:
                raise AuthorityDenied(C.INVALID_REQUEST, subject="grant")
            if grant.workspace_id != workspace_id:
                raise AuthorityDenied(C.WORKSPACE_MISMATCH, subject="grant")
            if (grant.task_id, grant.lease_id, grant.fencing_token) != (
                    binding.vm_task_id, binding.lease_id, binding.lease_fencing_token) \
                    or type(grant.connection_ids) is not tuple or binding.connection_id not in grant.connection_ids:
                raise AuthorityDenied(C.BROKER_SCOPE_MISMATCH, subject="grant")
            # Only the connection scope is kept; the bearer grant_id is not retained.
            connections = frozenset(grant.connection_ids)
        state = _Session(
            handle=None, role="director", workspace_id=workspace_id, policy=current[0], policy_revision=current[1],
            binding=binding, scope=scope, authorization_revision=authorization_revision,
            profile_tools=profile_tools, parent_tools=None, connections=connections, closed=False, parent_id=None,
        )
        self._check_live(state)
        handle = ToolSession("session-" + secrets.token_hex(16), "director", workspace_id)
        state.handle = handle
        self._sessions[handle.session_id] = state
        self._record("session_opened", "allowed", session_id=handle.session_id, role="director")
        return handle

    def delegate(self, parent: ToolSession, *, decision: ExecutionDecision, tools) -> ToolSession:
        """Pass a subset of the director's existing scope to a worker/reviewer.

        No new approval is requested; a request beyond the parent's scope is a
        SCOPE_ESCALATION (escalation=True) and is never granted here.
        """
        try:
            with self._lock:
                return self._delegate(parent, decision, tools)
        except AuthorityError as error:
            self._denied("delegation_denied", error, session=parent)
            raise

    def _delegate(self, parent, decision, tools) -> ToolSession:
        state = self._state(parent)
        if state.role != "director":
            raise AuthorityDenied(C.ROLE_CANNOT_DELEGATE)
        role, workspace_id, profile_tools = _profile_view(decision)
        if role not in ("worker", "reviewer"):
            raise AuthorityDenied(C.ROLE_MISMATCH, subject="decision")
        if workspace_id != state.workspace_id:
            raise AuthorityDenied(C.WORKSPACE_MISMATCH, subject="decision")
        if type(tools) is not tuple or not tools or len(set(tools)) != len(tools):
            raise AuthorityDenied(C.INVALID_REQUEST, subject="tools")
        for tool in tools:
            request_id(tool, "tools")
        self._check_live(state)
        policy = state.policy
        for code, failed in (
            (C.RESERVED_TOOL, [tool for tool in tools if is_reserved_tool(tool)]),
            (C.TOOL_NOT_DECLARED, [tool for tool in tools if policy.declaration(tool) is None]),
            (C.TOOL_NOT_ALLOWED_FOR_ROLE, [tool for tool in tools if tool not in policy.allowed(role)]),
            (C.TOOL_NOT_IN_PROFILE, [tool for tool in tools if tool not in profile_tools]),
            (C.SCOPE_ESCALATION, [tool for tool in tools if tool not in self._effective(state)]),
        ):
            if failed:
                raise AuthorityDenied(code, subject="tools", details=tuple(failed))
        handle = ToolSession("session-" + secrets.token_hex(16), role, workspace_id)
        self._sessions[handle.session_id] = _Session(
            handle=handle, role=role, workspace_id=workspace_id, policy=policy,
            policy_revision=state.policy_revision, binding=state.binding, scope=state.scope,
            authorization_revision=state.authorization_revision, profile_tools=profile_tools,
            parent_tools=frozenset(tools), connections=state.connections, closed=False,
            parent_id=state.handle.session_id,
        )
        self._record("session_delegated", "allowed", session_id=handle.session_id, role=role)
        return handle

    def close_session(self, session: ToolSession) -> bool:
        """Close a session and every session delegated from it."""
        with self._lock:
            try:
                state = self._state(session)
            except AuthorityDenied:
                return False
            pending = [state.handle.session_id]
            while pending:
                current = pending.pop()
                entry = self._sessions[current]
                if not entry.closed:
                    entry.closed = True
                    self._record("session_closed", "info", session_id=current, role=entry.role)
                pending.extend(key for key, child in self._sessions.items()
                               if child.parent_id == current and not child.closed)
            return True

    def allowed_tools(self, session: ToolSession) -> tuple:
        with self._lock:
            return tuple(sorted(self._effective(self._state(session))))

    # ------------------------------------------------------------ tool calls
    def request(self, session: ToolSession, call: ToolCall) -> ToolCallAuthorization:
        try:
            with self._lock:
                return self._request(session, call)
        except AuthorityError as error:
            self._denied("tool_call_denied", error, session=session, call=call)
            raise

    def _request(self, session, call) -> ToolCallAuthorization:
        state = self._state(session)
        call = rebuild_call(call)
        arguments = canonical_arguments(call.arguments)
        if call.workspace_id != state.workspace_id:
            raise AuthorityDenied(C.WORKSPACE_MISMATCH, subject="workspace_id")
        if call.authorization_revision != state.authorization_revision:
            raise AuthorityDenied(C.STALE_AUTHORIZATION_REVISION, subject="authorization_revision")
        now = self._check_live(state)
        declaration = self._tool(state, call.tool_id)
        kind = declaration.effect_kind
        scope = state.scope
        if kind not in scope.effect_kinds:
            raise AuthorityDenied(C.EFFECT_OUT_OF_SCOPE, subject="effect_kind")
        if not scope.covers(call.target):
            raise AuthorityDenied(C.TARGET_OUT_OF_SCOPE, subject="target")
        if call.input_revision != scope.input_revision:
            raise AuthorityDenied(C.STALE_INPUT_REVISION, subject="input_revision")
        if declaration.requires_credential and call.connection_id is None:
            raise AuthorityDenied(C.CREDENTIAL_SCOPE_MISSING, subject="connection_id")
        if call.connection_id is not None:
            if state.connections is None:
                raise AuthorityDenied(C.CREDENTIAL_SCOPE_MISSING, subject="grant")
            if call.connection_id not in state.connections:
                raise AuthorityDenied(C.CONNECTION_OUT_OF_SCOPE, subject="connection_id")
        if (kind is EffectKind.READ_ONLY) != (call.effect_id is None):
            raise AuthorityDenied(C.INVALID_REQUEST, subject="effect_id")
        digest = self._digest(state, call, kind, arguments)

        previous = self._calls.get(call.tool_call_id)
        if previous is not None:
            if previous.session_id != state.handle.session_id or previous.request_digest != digest:
                raise AuthorityDenied(C.TOOL_CALL_CONFLICT, subject="tool_call_id")
            if call.effect_id is not None and call.effect_id in self._unresolved:
                raise AuthorityDenied(C.RECONCILIATION_REQUIRED, subject="effect_id")
            if kind is EffectKind.NON_IDEMPOTENT:
                # Never re-issue a non-idempotent permission; VM receipts decide.
                raise AuthorityDenied(C.EFFECT_ALREADY_AUTHORIZED, subject="effect_id")
            self._record("tool_call_replayed", "allowed", session_id=state.handle.session_id, role=state.role,
                         tool_call_id=call.tool_call_id, effect_id=call.effect_id, request_digest=digest)
            return previous

        binding = state.binding
        decision = ToolCallAuthorization(
            STATUS_AUTHORIZED, call.tool_call_id, call.tool_id, kind, call.effect_id, digest,
            state.handle.session_id, state.role, state.workspace_id, binding.vm_project_id, binding.vm_task_id,
            binding.vm_attempt_id, scope.vm_authorization_id, state.authorization_revision, binding.lease_id,
            binding.lease_fencing_token, state.policy_revision, binding.digest(), _stamp(now),
        )
        if kind is not EffectKind.READ_ONLY:
            effect_id = call.effect_id
            if effect_id in self._unresolved:
                raise AuthorityDenied(C.RECONCILIATION_REQUIRED, subject="effect_id")
            effect = self._effects.get(effect_id)
            if effect is not None:
                if kind is EffectKind.NON_IDEMPOTENT or effect.kind is not kind or effect.digest != digest:
                    raise AuthorityDenied(C.EFFECT_ID_CONFLICT, subject="effect_id")
                # Idempotent duplicate of an intent VM already holds: no new intent.
            else:
                self._record_intent(decision)
        self._calls[call.tool_call_id] = decision
        self._record("tool_call_authorized", "allowed", session_id=state.handle.session_id, role=state.role,
                     tool_call_id=call.tool_call_id, effect_id=call.effect_id, request_digest=digest)
        return decision

    def _record_intent(self, decision: ToolCallAuthorization) -> None:
        port = self._effect_port()
        intent = {
            "effect_id": decision.effect_id, "tool_call_id": decision.tool_call_id,
            "request_digest": decision.request_digest, "effect_kind": decision.effect_kind.value,
            "tool_id": decision.tool_id, "vm_project_id": decision.vm_project_id,
            "vm_task_id": decision.vm_task_id, "vm_attempt_id": decision.vm_attempt_id,
            "vm_authorization_id": decision.vm_authorization_id,
            "authorization_revision": decision.authorization_revision, "lease_id": decision.lease_id,
            "lease_fencing_token": decision.lease_fencing_token, "binding_digest": decision.binding_digest,
        }
        args = (decision.effect_id, decision.tool_call_id, decision.request_digest, decision.effect_kind)
        try:
            response = port.record_intent(dict(intent))
        except Exception as error:
            code = _vm_code(error)
            if code is not None and "UNKNOWN" not in code.split("_"):
                raise AuthorityDenied(C.VM_REJECTED, vm_code=code, subject="record_intent") from None
            self._mark_unknown(*args, "intent_unknown")
            raise EffectOutcomeUnknown(C.INTENT_UNKNOWN, vm_code=code, subject="record_intent") from None
        if not isinstance(response, Mapping) or response.get("effect_id") != decision.effect_id \
                or response.get("request_digest") != decision.request_digest:
            self._mark_unknown(*args, "intent_unknown")
            raise EffectOutcomeUnknown(C.INTENT_UNKNOWN, subject="record_intent")
        self._effects[decision.effect_id] = _Effect(*args[1:], "intent_recorded")

    # ------------------------------------------------- receipts & reconcile
    def forward_receipt(self, session: ToolSession, tool_call_id: str, status: str, *,
                        receipt_ref: str | None = None) -> EffectReceipt:
        """Forward an executor's observation to VM. Never a completion verdict."""
        try:
            with self._lock:
                return self._forward(session, tool_call_id, status, receipt_ref)
        except AuthorityError as error:
            self._denied("receipt_denied", error, session=session)
            raise

    def _forward(self, session, tool_call_id, status, receipt_ref) -> EffectReceipt:
        state = self._state(session, allow_closed=True)
        request_id(tool_call_id, "tool_call_id")
        if type(status) is not str or status not in RECEIPT_STATUSES:
            raise AuthorityDenied(C.INVALID_REQUEST, subject="status")
        if receipt_ref is not None:
            request_id(receipt_ref, "receipt_ref")
        decision = self._calls.get(tool_call_id)
        if decision is None or decision.session_id != state.handle.session_id:
            raise AuthorityDenied(C.TOOL_CALL_UNKNOWN, subject="tool_call_id")
        if decision.effect_kind is EffectKind.READ_ONLY:
            raise AuthorityDenied(C.INVALID_REQUEST, subject="effect_kind")
        effect_id = decision.effect_id
        effect = self._effects.get(effect_id)
        if effect is None:
            raise AuthorityDenied(C.EFFECT_UNKNOWN, subject="effect_id")
        port = self._effect_port()
        receipt = {
            "effect_id": effect_id, "tool_call_id": tool_call_id, "request_digest": decision.request_digest,
            "vm_attempt_id": decision.vm_attempt_id, "vm_authorization_id": decision.vm_authorization_id,
            "authorization_revision": decision.authorization_revision, "lease_id": decision.lease_id,
            "lease_fencing_token": decision.lease_fencing_token, "reported_status": status,
            "receipt_ref": receipt_ref,
        }
        try:
            response = port.record_receipt(dict(receipt))
        except Exception as error:
            code = _vm_code(error)
            if code is not None and "UNKNOWN" not in code.split("_"):
                raise AuthorityDenied(C.VM_REJECTED, vm_code=code, subject="record_receipt") from None
            effect.state = "outcome_unknown"
            self._unresolved.add(effect_id)
            raise EffectOutcomeUnknown(C.RECEIPT_UNKNOWN, vm_code=code, subject="record_receipt") from None
        vm_state = None
        if isinstance(response, Mapping) and response.get("effect_id") == effect_id:
            candidate = response.get("state")
            if type(candidate) is str and _STATE.fullmatch(candidate):
                vm_state = candidate
        unresolved = status == "unknown" or vm_state is None or vm_state == "unknown"
        if unresolved:
            effect.state = "outcome_unknown"
            self._unresolved.add(effect_id)
        else:
            effect.state = "receipt_forwarded"
        self._record("receipt_forwarded", "info", session_id=state.handle.session_id, role=state.role,
                     tool_call_id=tool_call_id, effect_id=effect_id, request_digest=decision.request_digest)
        return EffectReceipt(effect_id, tool_call_id, status, vm_state, unresolved)

    def reconcile(self, effect_id: str) -> str | None:
        """Read VM's effect state. Clears the unresolved mark only on a recognizable answer.

        Returns VM's state string, or None when VM has no intent for the effect
        (the local record is dropped so a fresh request can record intent anew).
        """
        request_id(effect_id, "effect_id")
        with self._lock:
            effect = self._effects.get(effect_id)
            if effect is None and effect_id not in self._unresolved:
                raise AuthorityDenied(C.EFFECT_UNKNOWN, subject="effect_id")
            port = self._effect_port()
            try:
                response = port.read_effect(effect_id)
            except Exception:
                raise AuthorityDenied(C.RECONCILIATION_REQUIRED, subject="read_effect") from None
            if response is None:
                self._effects.pop(effect_id, None)
                self._unresolved.discard(effect_id)
                self._record("effect_reconciled", "info", effect_id=effect_id)
                return None
            state = response.get("state") if isinstance(response, Mapping) else None
            if not isinstance(response, Mapping) or response.get("effect_id") != effect_id \
                    or type(state) is not str or _STATE.fullmatch(state) is None:
                raise AuthorityDenied(C.RECONCILIATION_REQUIRED, subject="read_effect")
            if state == "unknown":
                return state
            if effect is not None:
                effect.state = "vm:" + state
            self._unresolved.discard(effect_id)
            self._record("effect_reconciled", "info", effect_id=effect_id)
            return state

    def effect_state(self, effect_id: str) -> str | None:
        request_id(effect_id, "effect_id")
        with self._lock:
            effect = self._effects.get(effect_id)
            return None if effect is None else effect.state

    @property
    def unresolved_effects(self) -> frozenset:
        with self._lock:
            return frozenset(self._unresolved)

    def audit_records(self) -> tuple:
        with self._lock:
            return tuple(self._audit)


assert set(ROLES) == {"director", "worker", "reviewer"}
