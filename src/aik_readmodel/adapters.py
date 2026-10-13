"""Pure adapters from source records to SourceEvent.

Adapters copy source values into refs/state as given. They never infer a
state, never derive a missing ID and never judge completion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from enum import Enum
import hashlib

from aik_runtime import Lease, Placement, Versioned
from aik_vm_bridge.bridge import BridgeReceipt, VmRefs
from aik_runtime.errors import InvalidRecord

from .errors import InvalidEvent
from .models import PEER_STATES, SourceEvent


_VM_REFS = (
    ("vm_project_id", "project_id"), ("vm_goal_id", "goal_id"),
    ("vm_task_id", "task_id"), ("vm_attempt_id", "attempt_id"),
)
_PEER_REQUIRED = ("event_id", "state", "ags_message_id", "ags_peer_id")
_PEER_OPTIONAL = ("ags_session_id", "ags_workflow_id", "worker_address", "worker_id", "vm_attempt_id")


def _plain(value):
    return value.value if isinstance(value, Enum) else value


def from_bridge_receipt(receipt: object, user_scope: str) -> SourceEvent | None:
    """VM event from an aik_vm_bridge BridgeReceipt; None when it carries no history_cursor.

    IDs are copied from ``receipt.refs`` (VM-owned identifiers) as given. The
    state is the VM RuntimeJob status the bridge observed, when present. The
    bridge request ID is kept in the payload and is not mapped to the dispatch
    ``request_id``.
    """
    if type(receipt) is not BridgeReceipt or type(receipt.refs) is not VmRefs:
        raise InvalidEvent("INVALID_BRIDGE_RECEIPT")
    vm_refs = receipt.refs
    cursor = vm_refs.history_cursor
    if cursor is None:
        return None
    if type(cursor) is not int or cursor < 1:
        raise InvalidEvent("INVALID_HISTORY_CURSOR")
    refs = {}
    for key, short in _VM_REFS:
        value = getattr(vm_refs, short)
        if short == "project_id" and value is None:
            value = receipt.project_id
        if value is not None:
            refs[key] = value
    payload = {"vm_request_id": receipt.request_id}
    job = receipt.job
    if job is not None and job.vm_status is not None:
        payload["state"] = job.vm_status
    return SourceEvent("vm", cursor, f"vm-history-{cursor}", user_scope, _plain(receipt.command), refs, payload)


def _unwrap(value: object, model):
    revision = None
    if type(value) is Versioned:
        value, revision = value.value, value.revision
    if type(value) is not model:
        raise InvalidEvent("INVALID_" + model.__name__.upper())
    try:
        rebuilt = model(**{item.name: getattr(value, item.name) for item in fields(model)})
    except (InvalidRecord, TypeError, ValueError):
        raise InvalidEvent("INVALID_" + model.__name__.upper()) from None
    return rebuilt, revision


def from_lease(lease: object, placement: object, user_scope: str, *, cursor: int) -> SourceEvent:
    """aik_runtime event for a stored lease and its placement.

    The source cursor must be globally monotonic across all runtime events, so
    the caller supplies it; the lease revision is copied into the payload.
    """
    lease_value, revision = _unwrap(lease, Lease)
    if revision is None:
        raise InvalidEvent("LEASE_REVISION_REQUIRED")
    placement_value, _ = _unwrap(placement, Placement)
    if lease_value.placement_id != placement_value.id or lease_value.pool_id != placement_value.pool_id:
        raise InvalidEvent("PLACEMENT_MISMATCH")
    refs = {
        "lease_id": lease_value.id, "placement_id": placement_value.id,
        "vm_project_id": placement_value.vm_project_id, "vm_task_id": placement_value.vm_task_id,
        "vm_attempt_id": placement_value.vm_attempt_id, "worker_id": lease_value.worker_id,
    }
    if lease_value.provider_job_id is not None:
        refs["provider_job_id"] = lease_value.provider_job_id
    if placement_value.ags_session_id is not None:
        refs["ags_session_id"] = placement_value.ags_session_id
    payload = {
        "state": lease_value.state, "lease_revision": revision, "fencing_token": lease_value.fencing_token,
        "expires_at": lease_value.expires_at, "pool_id": lease_value.pool_id,
        "placement_state": placement_value.state,
    }
    digest = hashlib.sha256(f"{lease_value.id}\0{revision}".encode("utf-8")).hexdigest()[:40]
    return SourceEvent("aik_runtime", cursor, f"lease-{digest}-r{revision}", user_scope, "lease", refs, payload)


def from_peer_message(message: object, cursor: int, user_scope: str) -> SourceEvent:
    """ags_peer event for one local PEER delivery step (delivered/claimed/acked/result)."""
    if not isinstance(message, Mapping):
        raise InvalidEvent("INVALID_PEER_MESSAGE")
    if set(message) - set(_PEER_REQUIRED) - set(_PEER_OPTIONAL):
        # Unknown fields may carry message bodies; they are not copied silently.
        raise InvalidEvent("UNKNOWN_PEER_FIELD")
    for name in _PEER_REQUIRED:
        if message.get(name) is None:
            raise InvalidEvent("MISSING_" + name.upper())
    state = message["state"]
    if type(state) is not str or state not in PEER_STATES:
        raise InvalidEvent("INVALID_PEER_STATE")
    refs = {name: message[name] for name in ("ags_message_id", "ags_peer_id", *_PEER_OPTIONAL)
            if message.get(name) is not None}
    return SourceEvent("ags_peer", cursor, message["event_id"], user_scope, "peer_message", refs, {"state": state})
