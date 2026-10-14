"""AIK-046 OpenAI API-key connector for ``aik_connect.ConnectionService``.

Implements ``aik_connect.KeyVerifier`` over ``OpenAIApiAdapter``. The service
stores the key in the vault and keeps the opaque ``credential_ref``; this
verifier never sees or keeps the key. ``verify`` attaches a binding built
from the stored connection (slot fingerprint of connection, reference and
binding revision) and runs the adapter's key check.

Rotation: the service replaces the vault entry in place and moves the binding
revision on, while the reference stays the same. ``attach`` refuses a second
binding for one connection, so the verifier detaches the binding it attached
before and attaches the newer revision. Limits:

- The verifier should own its adapter (``create_connector`` builds one). A
  binding attached to the same adapter by someone else is never detached:
  the check reports UNKNOWN/``BINDING_CONFLICT``.
- A binding cannot be replaced while a job of the connection is not terminal
  (UNKNOWN/``CONNECTION_BUSY``); an older revision is never re-attached
  (UNKNOWN/``STALE_BINDING``).
- Another adapter instance still holding the previous revision resolves the
  replaced key under the same reference; fencing that is the holder's job.

Every vendor fault becomes a ``CheckResult`` with a fixed code; no key, vault
reference or vendor text leaves this module.
"""

from __future__ import annotations

from threading import RLock

from aik_connect import CheckResult, CheckState, ConnectorInfo
from aik_connection import Connection
from aik_provider.contracts import AdapterFailure, AuthKind, AuthState, ConnectionBinding, FaultKind
from aik_vault import VaultBackend

from .adapter import PROVIDER, RUNTIME, OpenAIApiAdapter, slot_fingerprint
from .transport import UrllibTransport

OFFICIAL_HOST = "openai-api"
HOST_PROFILE = "default"
INFO = ConnectorInfo(PROVIDER, "api", OFFICIAL_HOST, HOST_PROFILE, "OpenAI API")


def _unknown(code: str) -> CheckResult:
    return CheckResult(CheckState.UNKNOWN, code=code)


def _verify_event(adapter: OpenAIApiAdapter, binding: ConnectionBinding) -> dict:
    """The adapter's own record of the last key check of this binding (no secret in it)."""
    for event in reversed(adapter.events()):
        body = event.get("body") if type(event) is dict else None
        if (type(body) is dict and body.get("operation") == "verify_key"
                and body.get("connection_id") == binding.connection_id
                and body.get("revision") == binding.revision):
            return body
    return {}


def _result(state: AuthState, event: dict) -> CheckResult:
    vault = event.get("result") == "credential_unavailable"
    if state is AuthState.AUTHENTICATED:
        return CheckResult(CheckState.VERIFIED)
    if state is AuthState.UNAUTHENTICATED:
        return CheckResult(CheckState.UNAUTHENTICATED, code="CREDENTIAL_UNAVAILABLE" if vault else "KEY_REJECTED")
    if state is AuthState.LOCKED:
        # The adapter reports a denied vault entry and a vendor 403 both as LOCKED.
        return CheckResult(CheckState.LOCKED, code="VAULT_LOCKED" if vault else "ACCESS_DENIED")
    if vault:
        return _unknown("VAULT_UNAVAILABLE")
    if event.get("result") == "transport_failure":
        return _unknown("TRANSPORT_FAILED")
    if event.get("http_status") == 429:
        return _unknown("RATE_LIMITED")
    return _unknown("VENDOR_UNAVAILABLE")


class OpenAIKeyVerifier:
    """``KeyVerifier`` for OpenAI API keys stored by ``ConnectionService``."""

    def __init__(self, adapter: OpenAIApiAdapter):
        if not isinstance(adapter, OpenAIApiAdapter):
            raise TypeError("An OpenAIApiAdapter is required.")
        self.info = INFO
        self._adapter = adapter
        self._lock = RLock()
        self._bindings: dict[str, ConnectionBinding] = {}

    def __repr__(self) -> str:
        return "OpenAIKeyVerifier()"

    def accepts(self, secret: str) -> bool:
        """The adapter's local key format check; nothing is sent or kept."""
        try:
            OpenAIApiAdapter._check_secret(secret)
        except Exception:  # noqa: BLE001 - any refusal is a plain False
            return False
        return True

    def verify(self, connection: Connection) -> CheckResult:
        try:
            return self._verify(connection)
        except Exception:  # noqa: BLE001 - vendor faults never escape; the text is dropped
            return _unknown("VERIFY_FAILED")

    @staticmethod
    def _binding(connection: object) -> ConnectionBinding | None:
        if (type(connection) is not Connection or connection.provider != PROVIDER
                or connection.execution_kind != "api" or connection.credential_ref is None):
            return None
        try:
            return ConnectionBinding(
                PROVIDER, RUNTIME, connection.connection_id, connection.binding_revision, AuthKind.API_KEY,
                connection.credential_ref,
                slot_fingerprint(connection.connection_id, connection.credential_ref, connection.binding_revision),
            )
        except ValueError:
            return None

    def _attach(self, binding: ConnectionBinding) -> str | None:
        """Attach ``binding``, replacing an older revision this verifier attached. Returns a code on refusal."""
        try:
            self._adapter.attach(binding)
        except AdapterFailure as error:
            if error.fault.kind is not FaultKind.ACCOUNT_CONFLICT:
                return "INVALID_BINDING"
            held = self._bindings.get(binding.connection_id)
            if held is None or held == binding:
                return "BINDING_CONFLICT"  # attached elsewhere, or released by another caller
            if binding.revision < held.revision:
                return "STALE_BINDING"
            try:
                self._adapter.detach(held)
            except AdapterFailure as detach_error:
                if detach_error.fault.kind is FaultKind.ACCOUNT_CONFLICT:
                    return "CONNECTION_BUSY"
                return "BINDING_CONFLICT"
            self._bindings.pop(binding.connection_id, None)
            try:
                self._adapter.attach(binding)
            except AdapterFailure:
                return "BINDING_CONFLICT"
        self._bindings[binding.connection_id] = binding
        return None

    def _verify(self, connection: Connection) -> CheckResult:
        binding = self._binding(connection)
        if binding is None:
            return _unknown("CONNECTION_MISMATCH")
        with self._lock:
            refused = self._attach(binding)
            if refused is not None:
                return _unknown(refused)
            observation = self._adapter.verify_key(binding)
            event = _verify_event(self._adapter, binding)
        return _result(observation.state, event)


def create_connector(*, vault: VaultBackend, transport=None) -> OpenAIKeyVerifier:
    """Production wiring: the per-user vault and, by default, the standard-library HTTPS transport."""
    return OpenAIKeyVerifier(OpenAIApiAdapter(transport=UrllibTransport() if transport is None else transport,
                                              vault=vault))


__all__ = ["OpenAIKeyVerifier", "create_connector", "INFO", "OFFICIAL_HOST", "HOST_PROFILE"]
