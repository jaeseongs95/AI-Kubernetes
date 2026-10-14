"""AIK-047 Claude Code subscription connector for ``aik_connect.ConnectionService``.

Implements ``aik_connect.HostLogin`` over ``ClaudeHostBridge``. The official
Claude Code CLI owns the login and its tokens; this connector only launches
that login, observes the host and binds the account the host reports. The
account binding is the bridge's account fingerprint (64 hex digits, a digest
of the host-provided account identifier), never a token.

Login states:
- IDLE before ``start_login``.
- SIGNED_IN when the host reports a subscription login (also right away when
  it was already signed in; the bridge then launches nothing).
- PENDING while the launched login runs and the host is not signed in yet.
- FAILED with a fixed code when the host is absent/unsupported, the launch
  fails, or the host is still not signed in after ``LOGIN_TIMEOUT``.

Limits:
- The bridge cannot cancel a launched login: ``cancel_login`` returns False
  and ``cancel_supported`` is False. The launched login process is stopped
  by the runner at the bridge's login deadline; until then (or until the host
  reports a login) the attempt stays PENDING.
- Bridge handles live in this process only. After a restart ``check`` binds
  a new handle only to the stored account (``expected_account_fingerprint``);
  another account is reported as ACCOUNT_CHANGED and is not bound.
- ``logout`` never signs out a different host account than the connection's
  (``HOST_ACCOUNT_CHANGED``); the host is single-account per OS user.

Every bridge fault becomes a ``CheckResult``/``LoginStatus`` with a fixed code
(``logout`` raises ``aik_connect.ConnectError`` with a fixed code); no token,
path, command output or vendor text leaves this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from threading import RLock
import time

from aik_connect import CheckResult, CheckState, ConnectError, ConnectorInfo, LoginState, LoginStatus
from aik_connection import Connection
from aik_provider.contracts import (
    AdapterFailure, AuthObservation, AuthState, ConnectionBinding, FaultKind, ProviderFault,
)

from .bridge import LOGIN_TIMEOUT, PROVIDER, ClaudeHostBridge
from .protocol import AuthMethod, HostCommands, HostState, HostStatus
from .runner import SubprocessRunner

OFFICIAL_HOST = "claude-code"
HOST_PROFILE = "default"
INFO = ConnectorInfo(PROVIDER, "subscription", OFFICIAL_HOST, HOST_PROFILE, "Claude 구독(Claude Code)")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_BUSY = frozenset({"job_in_flight", "release_in_progress"})
_NATIVE_CODES = {
    "host_absent": "HOST_ABSENT",
    "host_unsupported": "HOST_UNSUPPORTED",
    "host_unknown": "HOST_STATE_UNKNOWN",
    "auth_expired": "AUTH_EXPIRED",
    "auth_required": "NOT_SIGNED_IN",
    "api_key_auth_not_subscription": "NON_SUBSCRIPTION_LOGIN",
    "account_not_observed": "ACCOUNT_UNKNOWN",
    "login_launch_unknown": "LOGIN_LAUNCH_UNCONFIRMED",
    "logout_unconfirmed": "LOGOUT_UNCONFIRMED",
    "logout_failed": "LOGOUT_FAILED",
    "connection_exists": "BINDING_CONFLICT",
    "reconnect_required": "RECONNECT_REQUIRED",
    "selected_account_mismatch": "ACCOUNT_CHANGED",
    "account_switched": "ACCOUNT_CHANGED",
    "job_in_flight": "CONNECTION_BUSY",
    "release_in_progress": "CONNECTION_BUSY",
}


def _code(fault: ProviderFault, default: str) -> str:
    code = _NATIVE_CODES.get(fault.native_code)
    if code is not None:
        return code
    if fault.kind is FaultKind.ACCOUNT_CHANGED:
        return "ACCOUNT_CHANGED"
    return default


def _check_failure(fault: ProviderFault) -> CheckResult:
    code = _code(fault, "CHECK_FAILED")
    if code == "ACCOUNT_CHANGED":
        return CheckResult(CheckState.ACCOUNT_CHANGED, code=code)
    if code in ("NOT_SIGNED_IN", "AUTH_EXPIRED", "NON_SUBSCRIPTION_LOGIN"):
        return CheckResult(CheckState.UNAUTHENTICATED, code=code)
    return CheckResult(CheckState.UNKNOWN, code=code)


def _status_code(status: HostStatus | None) -> str | None:
    """A FAILED/UNKNOWN code for a host that cannot be used at all, else None."""
    if status is None:
        return "HOST_STATE_UNKNOWN"
    if status.host_state is HostState.ABSENT:
        return "HOST_ABSENT"
    if status.host_state is HostState.UNSUPPORTED:
        return "HOST_UNSUPPORTED"
    if status.host_state is not HostState.PRESENT:
        return "HOST_STATE_UNKNOWN"
    return None


def _not_signed_in(status: HostStatus | None) -> CheckResult:
    if status is not None and status.auth_method is AuthMethod.API_KEY:
        return CheckResult(CheckState.UNAUTHENTICATED, code="NON_SUBSCRIPTION_LOGIN")
    return CheckResult(CheckState.UNAUTHENTICATED, code="NOT_SIGNED_IN")


@dataclass(slots=True)
class _Attempt:
    connection_id: str
    deadline: float
    code: str | None = None


class ClaudeHostLogin:
    """``HostLogin`` for a Claude subscription through the official Claude Code host."""

    def __init__(self, bridge: ClaudeHostBridge, *, clock=time.monotonic, login_timeout: float = LOGIN_TIMEOUT):
        if not isinstance(bridge, ClaudeHostBridge):
            raise TypeError("A ClaudeHostBridge is required.")
        if not callable(clock):
            raise TypeError("A monotonic clock callable is required.")
        if isinstance(login_timeout, bool) or not isinstance(login_timeout, (int, float)) or not login_timeout > 0:
            raise ValueError("INVALID_LOGIN_TIMEOUT")
        self.info = INFO
        self._bridge = bridge
        self._clock = clock
        self._login_timeout = float(login_timeout)
        self._lock = RLock()
        self._attempt: _Attempt | None = None
        self._bindings: dict[str, ConnectionBinding] = {}

    def __repr__(self) -> str:
        return "ClaudeHostLogin()"

    # -- login ------------------------------------------------------------------
    def _status(self, state: LoginState, code: str | None = None) -> LoginStatus:
        return LoginStatus(state, False, code)

    def start_login(self, connection_id: str) -> LoginStatus:
        with self._lock:
            code = None
            status = None
            try:
                status = self._bridge.start_login()
            except AdapterFailure as error:
                code = _code(error.fault, "LOGIN_START_FAILED")
            except Exception:  # noqa: BLE001 - the text is dropped
                code = "LOGIN_START_FAILED"
            self._attempt = _Attempt(connection_id, self._clock() + self._login_timeout, code)
            if code is not None:
                return self._status(LoginState.FAILED, code)
            if status.auth_state is AuthState.AUTHENTICATED:
                return self._status(LoginState.SIGNED_IN)
            return self._status(LoginState.PENDING)

    def login_status(self) -> LoginStatus:
        with self._lock:
            attempt = self._attempt
            if attempt is None:
                return self._status(LoginState.IDLE)
            if attempt.code is not None:
                return self._status(LoginState.FAILED, attempt.code)
            try:
                status = self._bridge.probe()
            except Exception:  # noqa: BLE001
                status = None
            unusable = _status_code(status)
            if unusable is not None:
                return self._status(LoginState.FAILED, unusable)
            if status.auth_state is AuthState.AUTHENTICATED:
                return self._status(LoginState.SIGNED_IN)
            if self._clock() >= attempt.deadline:
                attempt.code = "LOGIN_TIMEOUT"
                return self._status(LoginState.FAILED, attempt.code)
            return self._status(LoginState.PENDING)

    def cancel_login(self) -> bool:
        return False  # the bridge cannot stop a launched official login

    # -- binding ------------------------------------------------------------------
    def _forget(self, connection_id: str) -> str | None:
        """Release a handle this connector holds (no host logout). Returns a code when refused."""
        held = self._bindings.get(connection_id)
        if held is None:
            return None
        try:
            self._bridge.release(held, host_logout=False)
        except AdapterFailure as error:
            if error.fault.kind is FaultKind.STALE_CONNECTION and error.fault.native_code in _BUSY:
                return "CONNECTION_BUSY"
            # Otherwise the handle is already gone from the bridge.
        except Exception:  # noqa: BLE001
            return "CHECK_FAILED"
        self._bindings.pop(connection_id, None)
        return None

    def _bind(self, connection_id: str, expected: str | None) -> CheckResult:
        try:
            binding = self._bridge.connect(connection_id, expected_account_fingerprint=expected)
        except AdapterFailure as error:
            return _check_failure(error.fault)
        except Exception:  # noqa: BLE001
            return CheckResult(CheckState.UNKNOWN, code="CHECK_FAILED")
        self._bindings[connection_id] = binding
        return CheckResult(CheckState.VERIFIED, account_binding=binding.account_fingerprint)

    def connect(self, connection_id: str) -> CheckResult:
        try:
            with self._lock:
                refused = self._forget(connection_id)
                if refused is not None:
                    return CheckResult(CheckState.UNKNOWN, code=refused)
                return self._bind(connection_id, None)
        except Exception:  # noqa: BLE001
            return CheckResult(CheckState.UNKNOWN, code="CHECK_FAILED")

    def _observed(self, observation: AuthObservation) -> CheckResult:
        if observation.state is AuthState.AUTHENTICATED:
            return CheckResult(CheckState.VERIFIED, account_binding=observation.account_fingerprint)
        status = self._bridge.last_status()
        if observation.state is AuthState.UNAUTHENTICATED:
            return _not_signed_in(status)
        if observation.state is AuthState.EXPIRED:
            return CheckResult(CheckState.UNAUTHENTICATED, code="AUTH_EXPIRED")
        return CheckResult(CheckState.UNKNOWN, code=_status_code(status) or "ACCOUNT_UNKNOWN")

    def check(self, connection: Connection) -> CheckResult:
        try:
            with self._lock:
                return self._check(connection)
        except Exception:  # noqa: BLE001
            return CheckResult(CheckState.UNKNOWN, code="CHECK_FAILED")

    def _check(self, connection: Connection) -> CheckResult:
        if (type(connection) is not Connection or connection.provider != PROVIDER
                or connection.execution_kind != "subscription"):
            return CheckResult(CheckState.UNKNOWN, code="CONNECTION_MISMATCH")
        connection_id = connection.connection_id
        held = self._bindings.get(connection_id)
        if held is not None:
            fault = None
            try:
                return self._observed(self._bridge.auth_status(held))
            except AdapterFailure as error:
                fault = error.fault
            if fault.kind is FaultKind.STALE_CONNECTION and fault.native_code == "reconnect_required":
                # The bound account is signed in again after a change: re-validate it.
                try:
                    rebound = self._bridge.reconnect(held)
                except AdapterFailure as error:
                    return _check_failure(error.fault)
                self._bindings[connection_id] = rebound
                return CheckResult(CheckState.VERIFIED, account_binding=rebound.account_fingerprint)
            if fault.kind is not FaultKind.STALE_CONNECTION or fault.native_code is not None:
                return _check_failure(fault)
            self._bindings.pop(connection_id, None)  # the handle is gone from the bridge
        # No handle in this process (e.g. after a restart): bind only the stored account.
        expected = connection.account_binding
        if type(expected) is not str or _FINGERPRINT.fullmatch(expected) is None:
            return CheckResult(CheckState.UNKNOWN, code="BINDING_UNSUPPORTED")
        return self._bind(connection_id, expected)

    # -- logout ---------------------------------------------------------------------
    def logout(self, connection: Connection) -> None:
        code = None
        try:
            with self._lock:
                code = self._logout(connection)
        except Exception:  # noqa: BLE001
            code = "LOGOUT_FAILED"
        if code is not None:
            raise ConnectError(code)

    def _logout(self, connection: Connection) -> str | None:
        if (type(connection) is not Connection or connection.provider != PROVIDER
                or connection.execution_kind != "subscription"):
            return "CONNECTION_MISMATCH"
        connection_id = connection.connection_id
        status = self._bridge.probe()
        unusable = _status_code(status)
        if unusable is not None:
            return unusable
        if status.auth_state in (AuthState.UNAUTHENTICATED, AuthState.EXPIRED):
            # The subscription is not signed in on the host: only the local handle is left.
            return self._forget(connection_id)
        if status.auth_state is not AuthState.AUTHENTICATED:
            return "HOST_STATE_UNKNOWN"
        if status.account_fingerprint != connection.account_binding:
            return "HOST_ACCOUNT_CHANGED"  # never sign out another account
        for attempt in range(2):
            binding = self._bindings.get(connection_id)
            if binding is None:
                bound = self._bind(connection_id, status.account_fingerprint)
                if bound.state is not CheckState.VERIFIED:
                    return bound.code or "LOGOUT_FAILED"
                binding = self._bindings[connection_id]
            try:
                self._bridge.release(binding, host_logout=True)
            except AdapterFailure as error:
                fault = error.fault
                if attempt == 0 and fault.kind is FaultKind.STALE_CONNECTION and fault.native_code is None:
                    self._bindings.pop(connection_id, None)  # the handle is gone: bind again once
                    continue
                return _code(fault, "LOGOUT_FAILED")
            self._bindings.pop(connection_id, None)
            return None
        return "LOGOUT_FAILED"


def create_connector(*, runner=None, commands: HostCommands | None = None, environ=None) -> ClaudeHostLogin:
    """Production wiring: the live subprocess runner and the default ``claude`` executable found on PATH."""
    bridge = ClaudeHostBridge(SubprocessRunner() if runner is None else runner, commands=commands, environ=environ)
    return ClaudeHostLogin(bridge)


__all__ = ["ClaudeHostLogin", "create_connector", "INFO", "OFFICIAL_HOST", "HOST_PROFILE"]
