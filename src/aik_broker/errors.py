"""Broker errors carry only fixed codes and messages, never secret values."""

from __future__ import annotations


MESSAGES = {
    "INVALID_REQUEST": "The broker request was rejected.",
    "ADAPTER_NOT_ALLOWED": "The caller is not the adapter authorized by this grant.",
    "ADAPTER_EXISTS": "The adapter is already registered.",
    "GRANT_UNKNOWN": "The grant is unknown.",
    "GRANT_EXPIRED": "The grant has expired.",
    "GRANT_EXHAUSTED": "The grant has no remaining uses.",
    "GRANT_REVOKED": "The grant was revoked.",
    "GRANT_CAPACITY": "The broker reached its bounded grant capacity.",
    "WORKSPACE_MISMATCH": "The request does not match the grant workspace.",
    "TASK_MISMATCH": "The request does not match the grant task.",
    "CONNECTION_OUT_OF_SCOPE": "The connection is outside the grant scope.",
    "CONNECTION_UNAVAILABLE": "The connection is not registered or is invalid.",
    "PROVIDER_MISMATCH": "The connection provider does not match the adapter.",
    "NO_BROKER_SECRET": "The connection has no broker-managed credential.",
    "AUTH_REQUIRED": "The connection binding requires verification.",
    "BINDING_CHANGED": "The connection binding changed after the grant was issued.",
    "LEASE_INACTIVE": "The execution lease is not active.",
    "CLOCK_UNAVAILABLE": "The broker clock is unavailable.",
    "AUDIT_UNAVAILABLE": "The audit record could not be written.",
    "CREDENTIAL_UNAVAILABLE": "The credential could not be resolved.",
    "ADAPTER_FAILED": "The adapter failed while using the credential.",
    "SECRET_IN_OUTPUT": "The adapter output contained the credential and was withheld.",
    "ARGV_CONTAINS_SECRET": "A command-line argument contained the credential.",
    "ENV_CONTAINS_SECRET": "The base environment already contained the credential.",
    "CHILD_TIMEOUT": "The child process exceeded its deadline and was terminated.",
    "CHILD_START_FAILED": "The child process could not be started.",
    "INTERNAL_ERROR": "The broker request failed.",
}


class BrokerError(Exception):
    """A broker operation failed. ``code`` is always one of MESSAGES."""

    def __init__(self, code: str = "INTERNAL_ERROR"):
        selected = code if type(code) is str and code in MESSAGES else "INTERNAL_ERROR"
        super().__init__(f"{selected}: {MESSAGES[selected]}")
        self.code = selected


class BrokerDenied(BrokerError):
    """Authorization failed; no plaintext was resolved."""


class CredentialUnavailable(BrokerError):
    """The authorized vault lookup failed; no plaintext was delivered."""


class AdapterFailed(BrokerError):
    """The adapter callback failed; its original exception was discarded."""


class SecretLeakBlocked(BrokerError):
    """The adapter tried to return the plaintext across the boundary."""


class InjectionError(BrokerError):
    """A child-process injection could not be performed safely."""
