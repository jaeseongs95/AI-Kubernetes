"""Adapter from controller commands to aik_authority.AuthorityGateway tool calls.

Each effectful VM command maps to a declared director tool. The gateway decides
inside its session scope (policy, profile, VM authorization, lease). This
adapter never grants anything and never maps AUTHORIZE.

The gateway is expected to raise on denial. Its return value is still checked
fail-closed: None, False, a denial text or a decision object that exposes a
non-True grant flag or a denial state is treated as a denial. The exact real
gateway decision contract is not verified here (NOT_RUN); callers can pin it
with ``decision_check``.
"""

from __future__ import annotations

from typing import Mapping

from aik_authority import AuthorityCode, AuthorityDenied, EffectKind, ToolCall
from aik_runtime.models import MAX_INTEGER
from aik_vm_bridge import Command

from .errors import InvalidControllerConfig
from .models import is_identifier
from .ports import EFFECTFUL_COMMANDS


_DENIAL_WORDS = frozenset({
    "denied", "deny", "rejected", "refused", "forbidden", "unknown", "escalation_required",
    "not_authorized", "unauthorized",
})
_GRANT_FLAGS = ("allowed", "authorized", "granted", "permitted", "approved")
_STATE_FIELDS = ("state", "status", "outcome", "decision", "verdict", "result")


def _denial_text(value: object) -> bool:
    value = getattr(value, "value", value)
    return type(value) is str and value.strip().lower() in _DENIAL_WORDS


def _granted(result: object) -> bool:
    """Fail-closed reading of a gateway return value; never grants by itself."""
    if result is None or result is False:
        return False
    if result is True:
        return True
    try:
        if _denial_text(result):
            return False
        for name in _GRANT_FLAGS:
            if hasattr(result, name) and getattr(result, name) is not True:
                return False
        if getattr(result, "denied", False):
            return False
        for name in _STATE_FIELDS:
            if _denial_text(getattr(result, name, None)):
                return False
    except Exception:
        return False
    return True


class GatewayCommandAuthority:
    def __init__(self, gateway, session, *, workspace_id: str, input_revision: str, authorization_revision: int,
                 tool_ids: Mapping, target_scheme: str = "vm", decision_check=None):
        if not callable(getattr(gateway, "request", None)) or not callable(getattr(gateway, "policy", None)):
            raise InvalidControllerConfig("A gateway with request and policy is required.")
        for value in (workspace_id, input_revision):
            if not is_identifier(value):
                raise InvalidControllerConfig("Invalid authority identifier.")
        if type(authorization_revision) is not int or not 1 <= authorization_revision <= MAX_INTEGER:
            raise InvalidControllerConfig("Invalid authorization revision.")
        if type(target_scheme) is not str or not target_scheme.isalpha() or not target_scheme.islower():
            raise InvalidControllerConfig("Invalid target scheme.")
        if not isinstance(tool_ids, Mapping):
            raise InvalidControllerConfig("tool_ids must be a mapping.")
        if decision_check is not None and not callable(decision_check):
            raise InvalidControllerConfig("decision_check must be callable.")
        tools = {}
        for command, tool_id in tool_ids.items():
            try:
                command = Command(command)
            except ValueError:
                raise InvalidControllerConfig("Unknown VM command in tool_ids.") from None
            if command not in EFFECTFUL_COMMANDS:
                raise InvalidControllerConfig("Only effectful director commands can be mapped to tools.")
            if not is_identifier(tool_id):
                raise InvalidControllerConfig("Invalid tool identifier.")
            tools[command] = tool_id
        self._gateway = gateway
        self._session = session
        self._workspace = workspace_id
        self._input_revision = input_revision
        self._authorization_revision = authorization_revision
        self._tools = tools
        self._scheme = target_scheme
        self._decision_check = decision_check

    def _accepted(self, result: object) -> bool:
        if not _granted(result):
            return False
        if self._decision_check is None:
            return True
        try:
            return self._decision_check(result) is True
        except Exception:
            return False

    def check(self, command, *, project_id: str, request_id: str, task_id: str | None = None) -> None:
        try:
            command = Command(command)
        except ValueError:
            raise AuthorityDenied(AuthorityCode.INVALID_REQUEST, subject="command") from None
        tool_id = self._tools.get(command)
        if tool_id is None:
            raise AuthorityDenied(AuthorityCode.TOOL_NOT_DECLARED, subject="command")
        current = self._gateway.policy(self._workspace)
        if current is None:
            raise AuthorityDenied(AuthorityCode.POLICY_UNKNOWN)
        declaration = current[0].declaration(tool_id)
        if declaration is None:
            raise AuthorityDenied(AuthorityCode.TOOL_NOT_DECLARED, subject="tool_id")
        effect_id = None if declaration.effect_kind is EffectKind.READ_ONLY else request_id
        target = f"{self._scheme}://{project_id}" + ("" if task_id is None else f"/{task_id}")
        call = ToolCall(request_id, tool_id, self._workspace, target, self._input_revision,
                        self._authorization_revision,
                        {"command": command.value, "project_id": project_id, "task_id": task_id},
                        effect_id)
        result = self._gateway.request(self._session, call)
        if not self._accepted(result):
            # A return value that is not recognizably an authorization is a denial.
            raise AuthorityDenied(AuthorityCode.INVALID_REQUEST, subject="command")
