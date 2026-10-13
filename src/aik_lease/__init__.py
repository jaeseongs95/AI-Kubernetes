"""AIK-034 worker lease, concurrency reservation and fencing.

A lease is stored as an aik_runtime.Lease record. No separate lease table
exists. The lease ID equals the aik_budget reservation ID, which links the
capacity reservation to its budget reservation. Every state change requires
the caller's fencing token. A stale or superseded owner is rejected.

This package does not call providers, does not read credentials and does not
grant execution rights. An expired lease is not treated as a terminated
provider job.
"""

from .manager import (
    HOLDING_STATES, TERMINAL_STATES, InvalidTransition, LeaseError, LeaseExpired,
    LeaseManager, LeaseResult, ReconcileReport, StaleLease, UnknownLease, lease_id_for,
)

__all__ = [
    "LeaseManager", "LeaseResult", "ReconcileReport", "lease_id_for",
    "HOLDING_STATES", "TERMINAL_STATES",
    "LeaseError", "UnknownLease", "StaleLease", "LeaseExpired", "InvalidTransition",
]
