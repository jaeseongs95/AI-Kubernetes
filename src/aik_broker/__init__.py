"""AIK-018 scoped credential broker with just-in-time adapter injection.

The orchestrator issues a grant that binds one adapter, workspace, task, lease
and a set of connection IDs. Directors and workers only carry IDs. A registered
adapter later asks the broker to inject a secret. The broker resolves the
plaintext from ``aik_vault`` only for that call and passes it to the adapter
callback. The broker has no API that returns plaintext to a caller.

A ``grant_id`` is a short-lived bearer capability. Audit records therefore
carry ``grant_audit_ref(grant_id)``, a non-reversible reference, instead.
"""

from .broker import (
    AUDIT_CAPACITY, MAX_CONNECTIONS_PER_GRANT, MAX_GRANTS, MAX_TTL_SECONDS, MAX_USES,
    AdapterHandle, AuditRecord, CredentialBroker, Grant, RuntimeLeaseChecker, grant_audit_ref,
)
from .errors import (
    AdapterFailed, BrokerDenied, BrokerError, CredentialUnavailable, InjectionError,
    SecretLeakBlocked, MESSAGES,
)
from .injection import ChildResult, run_injected, secret_present, validate_command

__all__ = [
    "CredentialBroker", "AdapterHandle", "Grant", "AuditRecord", "RuntimeLeaseChecker",
    "grant_audit_ref",
    "ChildResult", "run_injected", "secret_present", "validate_command",
    "BrokerError", "BrokerDenied", "CredentialUnavailable", "AdapterFailed",
    "SecretLeakBlocked", "InjectionError", "MESSAGES",
    "MAX_TTL_SECONDS", "MAX_USES", "MAX_GRANTS", "MAX_CONNECTIONS_PER_GRANT", "AUDIT_CAPACITY",
]
