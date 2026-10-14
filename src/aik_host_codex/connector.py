"""AIK-047 ChatGPT subscription (Codex CLI) connector for ``aik_connect.ConnectionService``.

Implements ``aik_connect.HostLogin`` over ``CodexHostBridge``. The official
Codex CLI owns the login and its tokens; this connector starts, observes and
cancels that login and binds the account the host reports. The account
binding is the bridge's account fingerprint (64 hex digits, a digest of the
host-provided account identifier), never a token.

Login states follow the bridge's capability report: PENDING while the started
login runs, SIGNED_IN once the host reports a ChatGPT login with an
identifiable account, CANCELLED after ``cancel_login``, FAILED with a fixed
code otherwise (host absent/unsupported, signed out, expired, API-key login,
account not identifiable, host state unknown).

Bridge bindings live in this process only. For a stored connection without
one (e.g. after a restart) ``check``/``logout`` present the stored account
binding to the bridge, which adopts it as stale; the bridge's ``reconnect``
then re-validates the same account under a newer revision. ``logout`` never
signs out a different host account (the bridge refuses it).

Every bridge fault becomes a ``CheckResult``/``LoginStatus`` with a fixed code
(``logout`` raises ``aik_connect.ConnectError`` with a fixed code); no token,
path, command output or vendor text leaves this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from aik_connect import CheckResult, CheckState, ConnectError, ConnectorInfo, LoginState, LoginStatus
from aik_connection import Connection
from aik_provider.contracts import (
    AdapterFailure, AuthKind, AuthObservation, AuthState, ConnectionBinding, FaultKind, ProviderFault,
)

from .bridge import DEFAULT_PROFILE, PROVIDER, CodexHostBridge, HostCapability, HostPresence, ProtocolSupport
from .protocol import LoginMethod
from .protocol import LoginState as HostLoginState
from .runner import SubprocessRunner

OFFICIAL_HOST = "codex-cli"
DISPLAY_NAME = "ChatGPT 구독(Codex)"
_NATIVE_CODES = {
    "host_absent": "HOST_ABSENT",
    "host_unsupported": "HOST_UNSUPPORTED",
    "host_timeout": "HOST_STATE_UNKNOWN",
    "host_output_limit": "HOST_STATE_UNKNOWN",
    "host_runner_error": "HOST_STATE_UNKNOWN",
    "host_state_unknown": "HOST_STATE_UNKNOWN",
    "auth_required": "NOT_SIGNED_IN",
    "auth_expired": "AUTH_EXPIRED",
    "auth_pending": "LOGIN_PENDING",
    "account_unknown": "ACCOUNT_UNKNOWN",
    "non_subscription_login": "NON_SUBSCRIPTION_LOGIN",
    "account_changed": "HOST_ACCOUNT_CHANGED",
    "reconnect_required": "RECONNECT_REQUIRED",
    "logout_failed": "LOGOUT_FAILED",
}
_UNAUTHENTICATED = frozenset({"NOT_SIGNED_IN", "AUTH_EXPIRED", "NON_SUBSCRIPTION_LOGIN"})


def _code(fault: ProviderFault, default: str) -> str:
    code = _NATIVE_CODES.get(fault.native_code)
    return default if code is None else code


def _check_failure(fault: ProviderFault) -> CheckResult:
    code = _code(fault, "CHECK_FAILED")
    if code in _UNAUTHENTICATED:
        return CheckResult(CheckState.UNAUTHENTICATED, code=code)
    return CheckResult(CheckState.UNKNOWN, code=code)


def _capability_login(capability: HostCapability) -> tuple[LoginState, str | None]:
    """Map the bridge's capability report to a login state and fixed code."""
    if capability.presence is HostPresence.ABSENT:
        return LoginState.FAILED, "HOST_ABSENT"
    if capability.protocol is ProtocolSupport.UNSUPPORTED:
        return LoginState.FAILED, "HOST_UNSUPPORTED"
    login = capability.login
    if login is HostLoginState.PENDING:
        return LoginState.PENDING, None
    if login is HostLoginState.LOGGED_IN:
        if capability.method is not LoginMethod.SUBSCRIPTION:
            return LoginState.FAILED, "NON_SUBSCRIPTION_LOGIN"
        if "account_unknown" in capability.reasons:
            return LoginState.FAILED, "ACCOUNT_UNKNOWN"
        return LoginState.SIGNED_IN, None
    if login is HostLoginState.LOGGED_OUT:
        return LoginState.FAILED, "NOT_SIGNED_IN"
    if login is HostLoginState.EXPIRED:
        return LoginState.FAILED, "AUTH_EXPIRED"
    return LoginState.FAILED, "HOST_STATE_UNKNOWN"


@dataclass(slots=True)
class _Attempt:
    connection_id: str
    code: str | None = None
    cancelled: bool = False


class CodexHostLogin:
    """``HostLogin`` for a ChatGPT subscription through the official Codex CLI."""

    def __init__(self, bridge: CodexHostBridge):
        if not isinstance(bridge, CodexHostBridge):
            raise TypeError("A CodexHostBridge is required.")
        profile = bridge.credential_ref.rsplit("/", 1)[1]
        self.info = ConnectorInfo(PROVIDER, "subscription", OFFICIAL_HOST, profile, DISPLAY_NAME)
        self._bridge = bridge
        self._lock = RLock()
        self._attempt: _Attempt | None = None
        self._bindings: dict[str, ConnectionBinding] = {}

    def __repr__(self) -> str:
        return "CodexHostLogin()"

    # -- login ------------------------------------------------------------------
    @staticmethod
    def _status(state: LoginState, code: str | None = None) -> LoginStatus:
        return LoginStatus(state, True, code)

    def start_login(self, connection_id: str) -> LoginStatus:
        with self._lock:
            code = None
            capability = None
            try:
                capability = self._bridge.start_login(connection_id)
            except AdapterFailure as error:
                fault = error.fault
                code = ("LOGIN_IN_PROGRESS" if fault.kind is FaultKind.ACCOUNT_CONFLICT
                        else _code(fault, "LOGIN_START_FAILED"))
            except Exception:  # noqa: BLE001 - the text is dropped
                code = "LOGIN_START_FAILED"
            self._attempt = _Attempt(connection_id if type(connection_id) is str else "", code)
            if code is not None:
                return self._status(LoginState.FAILED, code)
            return self._status(*_capability_login(capability))

    def login_status(self) -> LoginStatus:
        with self._lock:
            attempt = self._attempt
            if attempt is None:
                return self._status(LoginState.IDLE)
            if attempt.cancelled:
                return self._status(LoginState.CANCELLED)
            if attempt.code is not None:
                return self._status(LoginState.FAILED, attempt.code)
            try:
                capability = self._bridge.probe()
            except Exception:  # noqa: BLE001
                return self._status(LoginState.FAILED, "HOST_STATE_UNKNOWN")
            return self._status(*_capability_login(capability))

    def cancel_login(self) -> bool:
        with self._lock:
            try:
                cancelled = self._bridge.cancel_login() is True
            except Exception:  # noqa: BLE001
                cancelled = False
            if cancelled and self._attempt is not None:
                self._attempt.cancelled = True
            return cancelled

    # -- binding ------------------------------------------------------------------
    def connect(self, connection_id: str) -> CheckResult:
        try:
            with self._lock:
                binding = self._bridge.connect(connection_id)
                self._bindings[connection_id] = binding
                return CheckResult(CheckState.VERIFIED, account_binding=binding.account_fingerprint)
        except AdapterFailure as error:
            return _check_failure(error.fault)
        except Exception:  # noqa: BLE001
            return CheckResult(CheckState.UNKNOWN, code="CHECK_FAILED")

    def _binding_for(self, connection: object) -> ConnectionBinding | None:
        """The binding this connector holds, else the stored account presented for adoption."""
        if (type(connection) is not Connection or connection.provider != PROVIDER
                or connection.execution_kind != "subscription"):
            return None
        held = self._bindings.get(connection.connection_id)
        if held is not None:
            return held
        try:
            return ConnectionBinding(PROVIDER, self._bridge.runtime, connection.connection_id,
                                     connection.binding_revision, AuthKind.NATIVE_SUBSCRIPTION,
                                     self._bridge.credential_ref, connection.account_binding)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _observed(observation: AuthObservation) -> CheckResult:
        state = observation.state
        if state is AuthState.AUTHENTICATED:
            return CheckResult(CheckState.VERIFIED, account_binding=observation.account_fingerprint)
        if state is AuthState.UNAUTHENTICATED:
            return CheckResult(CheckState.UNAUTHENTICATED, code="NOT_SIGNED_IN")
        if state is AuthState.EXPIRED:
            return CheckResult(CheckState.UNAUTHENTICATED, code="AUTH_EXPIRED")
        if state is AuthState.PENDING:
            return CheckResult(CheckState.UNKNOWN, code="LOGIN_PENDING")
        return CheckResult(CheckState.UNKNOWN, code="ACCOUNT_UNKNOWN")

    def check(self, connection: Connection) -> CheckResult:
        try:
            with self._lock:
                return self._check(connection)
        except Exception:  # noqa: BLE001
            return CheckResult(CheckState.UNKNOWN, code="CHECK_FAILED")

    def _check(self, connection: Connection) -> CheckResult:
        binding = self._binding_for(connection)
        if binding is None:
            return CheckResult(CheckState.UNKNOWN, code="CONNECTION_MISMATCH")
        fault = None
        try:
            return self._observed(self._bridge.auth_status(binding))
        except AdapterFailure as error:
            fault = error.fault
        if fault.kind is not FaultKind.STALE_CONNECTION:
            return _check_failure(fault)
        # The bridge needs re-validation (adopted after a restart, or a login change of the
        # same account): reconnect issues a newer revision for the account it observes now.
        try:
            rebound = self._bridge.reconnect(binding)
        except AdapterFailure as error:
            return _check_failure(error.fault)
        self._bindings[connection.connection_id] = rebound
        return CheckResult(CheckState.VERIFIED, account_binding=rebound.account_fingerprint)

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

    def _sign_out(self, binding: ConnectionBinding) -> ProviderFault | str | None:
        try:
            observation = self._bridge.sign_out(binding)
        except AdapterFailure as error:
            return error.fault
        if observation.state is not AuthState.UNAUTHENTICATED:
            return "LOGOUT_UNCONFIRMED"
        return None

    def _logout(self, connection: Connection) -> str | None:
        binding = self._binding_for(connection)
        if binding is None:
            return "CONNECTION_MISMATCH"
        outcome = self._sign_out(binding)
        if type(outcome) is ProviderFault and outcome.kind is FaultKind.STALE_CONNECTION:
            # The bridge knows another revision of this connection: sign out only the same account.
            try:
                current = self._bridge.connect(connection.connection_id)
            except AdapterFailure as error:
                if error.fault.native_code in ("auth_required", "auth_expired"):
                    outcome = None  # already signed out on the host
                else:
                    return _code(error.fault, "LOGOUT_FAILED")
            else:
                self._bindings[connection.connection_id] = current
                if current.account_fingerprint != connection.account_binding:
                    return "HOST_ACCOUNT_CHANGED"
                outcome = self._sign_out(current)
        if type(outcome) is ProviderFault:
            if outcome.kind is FaultKind.UNKNOWN_OUTCOME:
                return "LOGOUT_UNCONFIRMED"
            return _code(outcome, "LOGOUT_FAILED")
        if outcome is not None:
            return outcome
        self._bindings.pop(connection.connection_id, None)
        return None


def create_connector(*, runner=None, executable: str = "codex", profile: str = DEFAULT_PROFILE,
                     environment=None) -> CodexHostLogin:
    """Production wiring: the live subprocess runner and the ``codex`` executable found on PATH."""
    bridge = CodexHostBridge(SubprocessRunner() if runner is None else runner, executable=executable,
                             profile=profile, environment=environment)
    return CodexHostLogin(bridge)


__all__ = ["CodexHostLogin", "create_connector", "OFFICIAL_HOST", "DISPLAY_NAME"]
