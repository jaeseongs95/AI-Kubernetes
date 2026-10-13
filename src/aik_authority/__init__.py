"""AIK-029 scoped tool gateway and authorization mapper.

Tools are granted only through a declared, role-separated allowlist (default
deny). A tool call is permitted only inside the intersection of:

* the workspace ToolPolicy allowlist for the session role (director, worker
  or reviewer), pinned at the policy revision current when the session opened;
* the bound aik_profiles ExecutionDecision tools;
* the VM authorization result (approval evidence reference) mapped from an
  aik_vm_bridge AUTHORIZE receipt, at its current authorization revision;
* the delegated scope of the parent session (worker/reviewer);
* optionally the aik_broker Grant connection scope.

The gateway never creates approval evidence, never approves on a user's behalf,
and never records its own permission as an effect completion. Workers and
reviewers receive only an opaque ToolSession handle, never authority objects.
"""

from .errors import (
    ESCALATION_CODES, VM_CONTRACT, AuthorityCode, AuthorityDenied, AuthorityError,
    EffectOutcomeUnknown, InvalidPolicy,
)
from .gateway import (
    AUDIT_CAPACITY, RECEIPT_STATUSES, AuditRecord, AuthorityGateway, ToolSession, VmEffectAuthorityPort,
)
from .models import (
    ROLES, STATUS_AUTHORIZED, AuthorizationScope, EffectKind, EffectReceipt, ToolCall,
    ToolCallAuthorization, ToolDeclaration, ToolPolicy, canonical_arguments, is_reserved_tool,
    map_vm_authorization,
)

__all__ = [
    "AuthorityGateway", "ToolSession", "VmEffectAuthorityPort", "AuditRecord",
    "AUDIT_CAPACITY", "RECEIPT_STATUSES",
    "ToolDeclaration", "ToolPolicy", "AuthorizationScope", "ToolCall", "ToolCallAuthorization",
    "EffectReceipt", "EffectKind", "ROLES", "STATUS_AUTHORIZED",
    "map_vm_authorization", "canonical_arguments", "is_reserved_tool",
    "AuthorityCode", "AuthorityError", "AuthorityDenied", "EffectOutcomeUnknown", "InvalidPolicy",
    "ESCALATION_CODES", "VM_CONTRACT",
]
