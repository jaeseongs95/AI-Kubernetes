"""AIK-046 Anthropic API-key connector for ``aik_connect.ConnectionService``.

Implements ``aik_connect.KeyVerifier`` over ``AnthropicAdapter``. The service
stores the key in the vault and keeps the opaque ``credential_ref``; this
verifier never sees or keeps the key. ``verify`` asks the adapter to bind the
stored reference at the connection's binding revision (``attach_key``) and
then runs the adapter's authentication check.

Rotation: the service replaces the vault entry in place and moves the binding
revision on. ``attach_key`` with the newer revision makes the adapter refuse
bindings of older revisions (ACCOUNT_CHANGED), and an older revision is never
bound again (UNKNOWN/``STALE_BINDING``). A key replaced between binding and
checking reports UNKNOWN/``KEY_CHANGED``; checking again settles it.

Every vendor fault becomes a ``CheckResult`` with a fixed code; no key, vault
reference or vendor text leaves this module.
"""

from __future__ import annotations

from threading import RLock

from aik_connect import CheckResult, CheckState, ConnectorInfo
from aik_connection import Connection
from aik_provider.contracts import AdapterFailure, AuthState, FaultKind, ProviderFault
from aik_vault import VaultBackend

from .adapter import PROVIDER, AnthropicAdapter, _valid_key
from .transport import UrllibTransport

OFFICIAL_HOST = "anthropic-api"
HOST_PROFILE = "default"
INFO = ConnectorInfo(PROVIDER, "api", OFFICIAL_HOST, HOST_PROFILE, "Anthropic API")
_TRANSPORT_NATIVE = frozenset({"timeout", "connection_error", "transport_error", "malformed_response"})


def _unknown(code: str) -> CheckResult:
    return CheckResult(CheckState.UNKNOWN, code=code)


def _fault_result(fault: ProviderFault) -> CheckResult:
    kind, native = fault.kind, fault.native_code
    if kind is FaultKind.AUTH_REQUIRED:
        if native in ("credential_missing", "invalid_credential_format"):
            return CheckResult(CheckState.UNAUTHENTICATED, code="CREDENTIAL_UNAVAILABLE")
        return CheckResult(CheckState.UNAUTHENTICATED, code="KEY_REJECTED")
    if kind is FaultKind.ACCESS_DENIED:
        return CheckResult(CheckState.LOCKED, code="VAULT_LOCKED" if native == "vault_error" else "ACCESS_DENIED")
    if kind is FaultKind.RATE_LIMITED:
        return _unknown("RATE_LIMITED")
    if kind is FaultKind.ACCOUNT_CHANGED:
        return _unknown("KEY_CHANGED")
    if kind is FaultKind.STALE_CONNECTION:
        return _unknown("STALE_BINDING")
    if kind is FaultKind.PROVIDER_FAILURE:
        if native == "vault_error":
            return _unknown("VAULT_UNAVAILABLE")
        return _unknown("TRANSPORT_FAILED" if native in _TRANSPORT_NATIVE else "VENDOR_UNAVAILABLE")
    if kind is FaultKind.UNSUPPORTED:
        return _unknown("VENDOR_UNAVAILABLE")
    if kind is FaultKind.INVALID_CONTRACT:
        return _unknown("INVALID_BINDING")
    return _unknown("VERIFY_FAILED")


class AnthropicKeyVerifier:
    """``KeyVerifier`` for Anthropic API keys stored by ``ConnectionService``."""

    def __init__(self, adapter: AnthropicAdapter):
        if not isinstance(adapter, AnthropicAdapter):
            raise TypeError("An AnthropicAdapter is required.")
        self.info = INFO
        self._adapter = adapter
        self._lock = RLock()

    def __repr__(self) -> str:
        return "AnthropicKeyVerifier()"

    def accepts(self, secret: str) -> bool:
        """The adapter's local key format check; nothing is sent or kept."""
        try:
            return _valid_key(secret) is True
        except Exception:  # noqa: BLE001 - any refusal is a plain False
            return False

    def verify(self, connection: Connection) -> CheckResult:
        try:
            return self._verify(connection)
        except Exception:  # noqa: BLE001 - vendor faults never escape; the text is dropped
            return _unknown("VERIFY_FAILED")

    def _verify(self, connection: Connection) -> CheckResult:
        if (type(connection) is not Connection or connection.provider != PROVIDER
                or connection.execution_kind != "api" or connection.credential_ref is None):
            return _unknown("CONNECTION_MISMATCH")
        fault = None
        with self._lock:
            try:
                binding = self._adapter.attach_key(connection.connection_id, connection.credential_ref,
                                                   connection.binding_revision)
                observation = self._adapter.auth_status(binding)
            except AdapterFailure as error:
                fault = error.fault
        if fault is not None:
            return _fault_result(fault)
        if observation.state is AuthState.AUTHENTICATED:
            return CheckResult(CheckState.VERIFIED)
        if observation.state is AuthState.UNAUTHENTICATED:
            return CheckResult(CheckState.UNAUTHENTICATED, code="KEY_REJECTED")
        return _unknown("VENDOR_UNAVAILABLE")


def create_connector(*, vault: VaultBackend, transport=None) -> AnthropicKeyVerifier:
    """Production wiring: the per-user vault and, by default, the shared standard-library HTTPS transport."""
    return AnthropicKeyVerifier(AnthropicAdapter(vault, UrllibTransport() if transport is None else transport))


__all__ = ["AnthropicKeyVerifier", "create_connector", "INFO", "OFFICIAL_HOST", "HOST_PROFILE"]
