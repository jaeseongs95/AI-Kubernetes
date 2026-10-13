"""AIK-033 resource placement for Tasks that VM has already declared ready.

VM's ready/authorization judgement and AGS role contracts are fixed inputs.
This package does not build a Task DAG, does not retry and does not choose
the next Task. It checks the caller's ordered execution profile candidates
against the pool inventory (aik_pool), the capability inventory
(aik_capability), the stored pools/profiles/placements (aik_runtime) and the
project serial default, then records every decision in an append-only journal.

A placement is AIK assignment metadata. It does not grant execution rights or
create a lease; that belongs to AIK-034.
"""

from .journal import APPLICATION_ID, PlacementJournal, default_journal_path
from .models import (
    PURPOSES, ROLES, ParallelQualification, PlacementDecision, PlacementError, ProfileCheck,
    ReadyTask, compute_digest, decision_document,
)
from .planner import ACTIVE_STATES, RELEASE_STATES, PlacementPlanner

__all__ = [
    "PlacementPlanner", "PlacementJournal", "default_journal_path", "APPLICATION_ID",
    "ReadyTask", "ParallelQualification", "PlacementDecision", "ProfileCheck", "PlacementError",
    "compute_digest", "decision_document", "ROLES", "PURPOSES", "ACTIVE_STATES", "RELEASE_STATES",
]
