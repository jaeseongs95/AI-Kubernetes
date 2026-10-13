"""Typed task execution binding (task-execution-binding.schema.json v1).

A binding is a cross-reference contract. It grants no authorization; VM still
owns Task/Attempt/authorization and AIK owns pool/placement/lease metadata.

``validate_binding_field`` and ``check_assignment`` are the shared rules for
binding fields and for AIK placement/lease/pool pre-checks. The hosted runtime
reuses them; ``TaskExecutionBinding`` and ``verify_assignment`` keep their
original behavior and error subjects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Callable, Mapping

from aik_provider.contracts import AuthKind
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, Lease, Placement, Pool, identifier

from .errors import BridgeCode, EffectOutcome, VmBridgeError


DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WORKSPACE_REF = re.compile(r"[a-z][a-z0-9+.-]{0,31}://[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
BINDING_FIELDS = (
    "vm_instance_id", "vm_project_id", "vm_goal_revision_id", "vm_plan_revision_id", "vm_task_id",
    "vm_attempt_id", "vm_execution_spec_digest", "vm_runtime_intent_id", "vm_provider_call_id",
    "cluster_id", "route_id", "route_revision", "connection_id", "connection_revision", "provider",
    "auth_kind", "provider_account_fingerprint", "pool_id", "placement_id", "lease_id",
    "lease_fencing_token", "support_matrix_digest", "input_manifest_digest", "workspace_root_ref",
)
# The AIK-owned part of the binding (integration-contract.json aik_fields).
AIK_BINDING_FIELDS = BINDING_FIELDS[BINDING_FIELDS.index("cluster_id"):]
_DIGEST_FIELDS = frozenset({"vm_execution_spec_digest", "support_matrix_digest", "input_manifest_digest"})
_AUTH_KINDS = frozenset(kind.value for kind in AuthKind)


def _invalid(field: str) -> VmBridgeError:
    return VmBridgeError(BridgeCode.INVALID_REQUEST, subject=field)


def validate_binding_field(name: str, value: object) -> None:
    """Validate one binding field value with the schema v1 rule for that field."""
    if name not in BINDING_FIELDS:
        raise _invalid("binding")
    if name in _DIGEST_FIELDS:
        if type(value) is not str or DIGEST_PATTERN.fullmatch(value) is None:
            raise _invalid(name)
    elif name == "lease_fencing_token":
        if type(value) is not int or not 1 <= value <= MAX_INTEGER:
            raise _invalid(name)
    elif name == "workspace_root_ref":
        if type(value) is not str or _WORKSPACE_REF.fullmatch(value) is None or any(
            part in ("", ".", "..") for part in value.split("://", 1)[1].split("/")
        ):
            raise _invalid(name)
    elif name == "auth_kind":
        if type(value) is not str or value not in _AUTH_KINDS:
            raise _invalid(name)
    else:
        try:
            identifier(value, name)
        except InvalidRecord:
            raise _invalid(name) from None


@dataclass(frozen=True, slots=True)
class TaskExecutionBinding:
    vm_instance_id: str
    vm_project_id: str
    vm_goal_revision_id: str
    vm_plan_revision_id: str
    vm_task_id: str
    vm_attempt_id: str
    vm_execution_spec_digest: str
    vm_runtime_intent_id: str
    vm_provider_call_id: str
    cluster_id: str
    route_id: str
    route_revision: str
    connection_id: str
    connection_revision: str
    provider: str
    auth_kind: str
    provider_account_fingerprint: str
    pool_id: str
    placement_id: str
    lease_id: str
    lease_fencing_token: int
    support_matrix_digest: str
    input_manifest_digest: str
    workspace_root_ref: str

    def __post_init__(self) -> None:
        for name in BINDING_FIELDS:
            validate_binding_field(name, getattr(self, name))

    def to_mapping(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in BINDING_FIELDS}

    def digest(self) -> str:
        text = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> "TaskExecutionBinding":
        # The schema has additionalProperties=false and every field required.
        if not isinstance(value, Mapping) or set(value) != set(BINDING_FIELDS):
            raise _invalid("binding")
        return cls(**{name: value[name] for name in BINDING_FIELDS})


def check_assignment(*, placement: Placement, lease: Lease, pool: Pool, now: Callable[[], datetime],
                     placement_id: str, pool_id: str, lease_id: str, lease_fencing_token: int,
                     vm_instance_id: str | None = None, vm_project_id: str | None = None,
                     vm_task_id: str | None = None, vm_attempt_id: str | None = None,
                     connection_id: str | None = None, provider: str | None = None,
                     require_usable: bool = True) -> None:
    """Compare AIK placement/lease/pool metadata with expected IDs, then check usability.

    An expected value of ``None`` is not compared. ``require_usable=False``
    only performs the identity and fencing comparison. This is a local
    pre-check over AIK-owned metadata; it is not a durable fencing transition
    and does not authorize execution.
    """
    if type(placement) is not Placement or type(lease) is not Lease or type(pool) is not Pool:
        raise _invalid("assignment")
    pairs = (
        ("placement_id", placement.id, placement_id),
        ("pool_id", placement.pool_id, pool_id),
        ("vm_instance_id", placement.vm_instance_id, vm_instance_id),
        ("vm_project_id", placement.vm_project_id, vm_project_id),
        ("vm_task_id", placement.vm_task_id, vm_task_id),
        ("vm_attempt_id", placement.vm_attempt_id, vm_attempt_id),
        ("lease_id", lease.id, lease_id),
        ("lease_placement_id", lease.placement_id, placement_id),
        ("lease_pool_id", lease.pool_id, pool_id),
        ("lease_fencing_token", lease.fencing_token, lease_fencing_token),
        ("pool_id", pool.id, pool_id),
        ("connection_id", pool.connection_id, connection_id),
        ("provider", pool.provider, provider),
    )
    for subject, stored, bound in pairs:
        if bound is not None and stored != bound:
            raise VmBridgeError(BridgeCode.BINDING_MISMATCH, EffectOutcome.KNOWN_FAILURE, subject=subject)
    if not require_usable:
        return
    if placement.state not in ("pending", "assigned"):
        raise VmBridgeError(BridgeCode.LEASE_NOT_USABLE, subject="placement_state")
    if not pool.enabled:
        raise VmBridgeError(BridgeCode.LEASE_NOT_USABLE, subject="pool_enabled")
    if lease.state not in ("reserved", "active"):
        raise VmBridgeError(BridgeCode.LEASE_NOT_USABLE, subject="lease_state")
    current = now()
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise _invalid("now")
    expiry = datetime.fromisoformat(lease.expires_at.replace("Z", "+00:00"))
    if expiry <= current:
        raise VmBridgeError(BridgeCode.LEASE_NOT_USABLE, subject="lease_expired")


def verify_assignment(binding: TaskExecutionBinding, *, placement: Placement, lease: Lease, pool: Pool,
                      now: Callable[[], datetime]) -> None:
    """Check AIK runtime metadata against the binding before any VM effect.

    This is a local pre-check over AIK-owned metadata; it is not a durable
    fencing transition and does not authorize execution.
    """
    if (type(binding) is not TaskExecutionBinding or type(placement) is not Placement
            or type(lease) is not Lease or type(pool) is not Pool):
        raise _invalid("assignment")
    check_assignment(
        placement=placement, lease=lease, pool=pool, now=now,
        placement_id=binding.placement_id, pool_id=binding.pool_id, lease_id=binding.lease_id,
        lease_fencing_token=binding.lease_fencing_token, vm_instance_id=binding.vm_instance_id,
        vm_project_id=binding.vm_project_id, vm_task_id=binding.vm_task_id,
        vm_attempt_id=binding.vm_attempt_id, connection_id=binding.connection_id, provider=binding.provider,
    )
