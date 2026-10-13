"""AIK-041 single-cluster elastic parallelism and idle cleanup.

The controller only combines existing authorities:

- VM's ready order and readiness/authorization judgement (``ReadyTask``),
- AIK-033 placement (``aik_placement.PlacementPlanner``). This covers the serial
  project default, qualified routes and capability checks.
- AIK-034/038 leases through ``aik_lifecycle.GatedLeaseManager``. Pause and drain
  gates therefore cannot be bypassed.

It adds a per-pool capacity check on held leases with a control reserve, and
returns idle leases that were never dispatched. It keeps no persistent state of
its own. It never decides Task completion, retries, or the next Task.
"""

from .controller import (
    CAPACITY_EXHAUSTED, CONTROL_RESERVE, LEASE_RESERVE_FAILED, LEASE_RESULT_UNREADABLE,
    NO_CANDIDATE_PROFILE, PLACEMENT_RELEASE_FAILED, ElasticError, ElasticityController, TickResult,
)

__all__ = [
    "ElasticityController", "TickResult", "ElasticError",
    "CAPACITY_EXHAUSTED", "CONTROL_RESERVE", "LEASE_RESERVE_FAILED", "LEASE_RESULT_UNREADABLE",
    "NO_CANDIDATE_PROFILE", "PLACEMENT_RELEASE_FAILED",
]
