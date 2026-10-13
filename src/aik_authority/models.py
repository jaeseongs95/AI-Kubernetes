"""Authority metadata: tool declarations, role allowlists, VM authorization scope.

Nothing here grants execution by itself. An AuthorizationScope only references
a VM authorization result (approval evidence) that VM produced; AIK never
creates that evidence. No model has a field for secret material.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import StrEnum
import json
import math
import re
from typing import Any, Mapping

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier
from aik_vm_bridge import BridgeReceipt, Command

from .errors import AuthorityCode as C, AuthorityDenied, InvalidPolicy


ROLES = ("director", "worker", "reviewer")
MAX_TOOLS = 256
MAX_TARGETS = 64
MAX_ARGUMENT_CHARS = 65536
MAX_ARGUMENT_DEPTH = 16
STATUS_AUTHORIZED = "authorized"  # gateway permission only, never effect completion
_TARGET = re.compile(r"[a-z][a-z0-9+.-]{0,31}://[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
# Tools that would let an agent grant authority or write VM verdicts are never
# declarable: completion and authorization remain VM/user owned. This is a
# best-effort name filter; VM authority is what actually prevents overwrites.
_RESERVED_PREFIXES = ("authority.", "approval.", "verdict.", "vm.authorize", "vm.verdict",
                      "vm.final_report", "vm.goal_verdict")
_RESERVED_NAMES = frozenset({"authorize", "approve", "grant", "grant_permission", "set_verdict",
                             "verdict", "complete_task", "mark_done"})
# Any name token starting with one of these is reserved (e.g. vm.set_verdict,
# goal.verdict, task.approve_plan, plan:approval).
_RESERVED_TOKENS = ("verdict", "authoriz", "authority", "approv")
_SEGMENT_SPLIT = re.compile(r"[.:/]")
_TOKEN_SPLIT = re.compile(r"[._:/-]")


class EffectKind(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


def is_reserved_tool(tool_id: str) -> bool:
    folded = tool_id.casefold()
    if folded in _RESERVED_NAMES or folded.startswith(_RESERVED_PREFIXES):
        return True
    if any(segment in _RESERVED_NAMES for segment in _SEGMENT_SPLIT.split(folded)):
        return True
    return any(token.startswith(_RESERVED_TOKENS) for token in _TOKEN_SPLIT.split(folded))


def _policy_id(value: object, field_name: str) -> None:
    try:
        identifier(value, field_name)
    except InvalidRecord:
        raise InvalidPolicy(subject=field_name) from None


def request_id(value: object, field_name: str) -> None:
    try:
        identifier(value, field_name)
    except InvalidRecord:
        raise AuthorityDenied(C.INVALID_REQUEST, subject=field_name) from None


def check_target(value: object, field_name: str = "target") -> None:
    if type(value) is not str or _TARGET.fullmatch(value) is None or any(
        part in ("", ".", "..") for part in value.split("://", 1)[1].split("/")
    ):
        raise AuthorityDenied(C.INVALID_REQUEST, subject=field_name)


def target_covers(scope_target: str, target: str) -> bool:
    return target == scope_target or target.startswith(scope_target + "/")


def _timestamp(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise AuthorityDenied(C.INVALID_REQUEST, subject=field_name)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise AuthorityDenied(C.INVALID_REQUEST, subject=field_name) from None


def canonical_arguments(value: object) -> str:
    """Canonical JSON for digesting; only bounded JSON-compatible values."""
    def walk(item, depth):
        if depth > MAX_ARGUMENT_DEPTH:
            raise ValueError
        kind = type(item)
        if item is None or kind in (bool, int, str):
            return
        if kind is float:
            if not math.isfinite(item):
                raise ValueError
            return
        if kind in (list, tuple):
            for element in item:
                walk(element, depth + 1)
            return
        if kind is dict:
            for key, element in item.items():
                if type(key) is not str:
                    raise ValueError
                walk(element, depth + 1)
            return
        raise ValueError

    if type(value) is not dict:
        raise AuthorityDenied(C.INVALID_REQUEST, subject="arguments")
    try:
        walk(value, 0)
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise AuthorityDenied(C.INVALID_REQUEST, subject="arguments") from None
    if len(text) > MAX_ARGUMENT_CHARS:
        raise AuthorityDenied(C.INVALID_REQUEST, subject="arguments")
    return text


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    tool_id: str
    effect_kind: EffectKind
    requires_credential: bool = False

    def __post_init__(self) -> None:
        _policy_id(self.tool_id, "tool_id")
        if is_reserved_tool(self.tool_id):
            raise InvalidPolicy(C.RESERVED_TOOL, subject="tool_id")
        if type(self.effect_kind) is not EffectKind:
            raise InvalidPolicy(subject="effect_kind")
        if type(self.requires_credential) is not bool:
            raise InvalidPolicy(subject="requires_credential")


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Declared tools and per-role allowlists of one workspace (default deny)."""

    workspace_id: str
    tools: tuple[ToolDeclaration, ...]
    director_tools: tuple[str, ...] = ()
    worker_tools: tuple[str, ...] = ()
    reviewer_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _policy_id(self.workspace_id, "workspace_id")
        if type(self.tools) is not tuple or len(self.tools) > MAX_TOOLS:
            raise InvalidPolicy(subject="tools")
        seen = set()
        for declaration in self.tools:
            if type(declaration) is not ToolDeclaration:
                raise InvalidPolicy(subject="tools")
            folded = declaration.tool_id.casefold()
            if folded in seen:
                raise InvalidPolicy(subject="tool_id")
            seen.add(folded)
        for role in ROLES:
            name = f"{role}_tools"
            allowed = getattr(self, name)
            if type(allowed) is not tuple or len(allowed) > MAX_TOOLS or len(set(allowed)) != len(allowed):
                raise InvalidPolicy(subject=name)
            for tool in allowed:
                _policy_id(tool, name)
                declaration = self.declaration(tool)
                if declaration is None:
                    raise InvalidPolicy(subject=name)
                if role == "reviewer" and declaration.effect_kind is not EffectKind.READ_ONLY:
                    raise InvalidPolicy(subject=name)

    def declaration(self, tool_id: str) -> ToolDeclaration | None:
        for declaration in self.tools:
            if declaration.tool_id == tool_id:
                return declaration
        return None

    def allowed(self, role: str) -> frozenset:
        return frozenset(getattr(self, f"{role}_tools")) if role in ROLES else frozenset()


def rebuild_policy(policy: object) -> ToolPolicy:
    """Reconstruct, so object.__setattr__ forgeries are validated again."""
    if type(policy) is not ToolPolicy or type(policy.tools) is not tuple:
        raise InvalidPolicy(subject="policy")
    try:
        declarations = []
        for item in policy.tools:
            if type(item) is not ToolDeclaration:
                raise InvalidPolicy(subject="tools")
            declarations.append(ToolDeclaration(item.tool_id, item.effect_kind, item.requires_credential))
        return ToolPolicy(policy.workspace_id, tuple(declarations), policy.director_tools,
                          policy.worker_tools, policy.reviewer_tools)
    except InvalidPolicy:
        raise
    except (AttributeError, TypeError):
        raise InvalidPolicy(subject="policy") from None


@dataclass(frozen=True, slots=True)
class AuthorizationScope:
    """Scope of one VM authorization result; ``vm_authorization_id`` is the evidence reference."""

    vm_authorization_id: str
    vm_project_id: str
    workspace_id: str
    input_revision: str
    tools: tuple[str, ...]
    targets: tuple[str, ...]
    effect_kinds: tuple[EffectKind, ...]
    expires_at: str

    def __post_init__(self) -> None:
        for name in ("vm_authorization_id", "vm_project_id", "workspace_id", "input_revision"):
            request_id(getattr(self, name), name)
        if type(self.tools) is not tuple or not 1 <= len(self.tools) <= MAX_TOOLS \
                or len(set(self.tools)) != len(self.tools):
            raise AuthorityDenied(C.INVALID_REQUEST, subject="tools")
        for tool in self.tools:
            request_id(tool, "tools")
        if type(self.targets) is not tuple or not 1 <= len(self.targets) <= MAX_TARGETS \
                or len(set(self.targets)) != len(self.targets):
            raise AuthorityDenied(C.INVALID_REQUEST, subject="targets")
        for target in self.targets:
            check_target(target, "targets")
        if type(self.effect_kinds) is not tuple or not 1 <= len(self.effect_kinds) <= len(EffectKind) \
                or len(set(self.effect_kinds)) != len(self.effect_kinds) \
                or any(type(kind) is not EffectKind for kind in self.effect_kinds):
            raise AuthorityDenied(C.INVALID_REQUEST, subject="effect_kinds")
        object.__setattr__(self, "expires_at", _timestamp(self.expires_at, "expires_at"))

    def expiry(self) -> datetime:
        return datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))

    def covers(self, target: str) -> bool:
        return any(target_covers(scope_target, target) for scope_target in self.targets)


def rebuild_scope(scope: object) -> AuthorizationScope:
    if type(scope) is not AuthorizationScope:
        raise AuthorityDenied(C.AUTHORIZATION_EVIDENCE_MISSING, subject="scope")
    return AuthorizationScope(**{item.name: getattr(scope, item.name) for item in fields(AuthorizationScope)})


def map_vm_authorization(receipt: object, *, workspace_id: str, tools, targets, effect_kinds,
                         expires_at: str) -> AuthorizationScope:
    """Authorization mapper: reference an existing VM AUTHORIZE result.

    The receipt is the approval evidence; this function never creates one. The
    scope fields are supplied by the trusted backend from that VM result.
    """
    if type(receipt) is not BridgeReceipt or receipt.command is not Command.AUTHORIZE:
        raise AuthorityDenied(C.AUTHORIZATION_EVIDENCE_MISSING, subject="receipt")
    refs = receipt.refs
    if refs.authorization_id is None or receipt.project_id is None or refs.plan_revision_id is None:
        raise AuthorityDenied(C.AUTHORIZATION_EVIDENCE_MISSING, subject="receipt")
    if refs.project_id is not None and refs.project_id != receipt.project_id:
        raise AuthorityDenied(C.AUTHORIZATION_EVIDENCE_MISSING, subject="project_id")
    try:
        tools, targets, effect_kinds = tuple(tools), tuple(targets), tuple(effect_kinds)
    except TypeError:
        raise AuthorityDenied(C.INVALID_REQUEST, subject="scope") from None
    return AuthorizationScope(refs.authorization_id, receipt.project_id, workspace_id, refs.plan_revision_id,
                              tools, targets, effect_kinds, expires_at)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One requested tool call. Arguments are digested, never stored."""

    tool_call_id: str
    tool_id: str
    workspace_id: str
    target: str
    input_revision: str
    authorization_revision: int
    arguments: Mapping[str, Any] = field(default_factory=dict)
    effect_id: str | None = None
    connection_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("tool_call_id", "tool_id", "workspace_id", "input_revision"):
            request_id(getattr(self, name), name)
        check_target(self.target)
        if type(self.authorization_revision) is not int or not 1 <= self.authorization_revision <= MAX_INTEGER:
            raise AuthorityDenied(C.INVALID_REQUEST, subject="authorization_revision")
        canonical_arguments(self.arguments)
        for name in ("effect_id", "connection_id"):
            if getattr(self, name) is not None:
                request_id(getattr(self, name), name)


def rebuild_call(call: object) -> ToolCall:
    if type(call) is not ToolCall:
        raise AuthorityDenied(C.INVALID_REQUEST, subject="call")
    return ToolCall(**{item.name: getattr(call, item.name) for item in fields(ToolCall)})


@dataclass(frozen=True, slots=True)
class ToolCallAuthorization:
    """Gateway permission bound to Attempt, authorization revision and lease.

    ``status`` is always ``authorized``: the gateway allowed the call to be
    issued. It is not an effect receipt and not a completion verdict.
    """

    status: str
    tool_call_id: str
    tool_id: str
    effect_kind: EffectKind
    effect_id: str | None
    request_digest: str
    session_id: str
    role: str
    workspace_id: str
    vm_project_id: str
    vm_task_id: str
    vm_attempt_id: str
    vm_authorization_id: str
    authorization_revision: int
    lease_id: str
    lease_fencing_token: int
    policy_revision: int
    binding_digest: str
    decided_at: str


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    """Forwarding result. ``vm_state`` is VM's verbatim state, never interpreted here."""

    effect_id: str
    tool_call_id: str
    reported_status: str
    vm_state: str | None
    reconciliation_required: bool
