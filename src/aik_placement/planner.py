"""AIK-033 placement planner and eligibility filter.

Profiles are checked in the caller's preference order and the first eligible
one is placed. Nothing is re-ranked or pruned. Every candidate's verdict and
reason codes are recorded. Store-based checks (pool parallel limit, project
serial default, attempt idempotency) and the Placement write happen inside
one RuntimeStore transaction, under its process lock. Concurrent planners
therefore cannot exceed a pool's max_parallel.

The planner never decides readiness, authorization, DAG order or retries:
those remain VM's authority. A released/failed attempt is not placed again;
VM issues a new attempt instead.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from types import MappingProxyType
from typing import NamedTuple
import uuid

from aik_capability import UnknownResource
from aik_pool import QualificationLevel, UnknownRoute
from aik_runtime import ExecutionProfile, Placement, Pool, RevisionConflict, RuntimeStore, Versioned
from aik_runtime.models import MAX_INTEGER

from .journal import PlacementJournal
from .models import (
    ParallelQualification, PlacementDecision, PlacementError, ProfileCheck, ReadyTask, check_id,
    compute_digest, decision_document, rebuild,
)


ACTIVE_STATES = ("pending", "assigned")
# An unknown placement may still have a running job: it keeps its capacity and
# its project's serial slot until an observation resolves it.
OCCUPYING_STATES = ACTIVE_STATES + ("unknown",)
RELEASE_STATES = ("released", "failed", "unknown")
MAX_CANDIDATES = 64
_QUALIFIED = str(QualificationLevel.QUALIFIED)
_CANDIDATE_LEVELS = (str(QualificationLevel.CANDIDATE_FOR_QUALIFICATION), _QUALIFIED)


class _Result(NamedTuple):
    status: str
    placement: Versioned | None
    profile_id: str | None
    pool_id: str | None
    reasons: tuple
    checks: tuple
    resource_revision: int | None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _placement_id(task: ReadyTask) -> str:
    digest = hashlib.sha256(f"{task.vm_instance_id}\0{task.vm_attempt_id}".encode("utf-8")).hexdigest()
    return "placement-" + digest[:40]


def _check_candidates(candidates: object) -> tuple:
    if type(candidates) is not tuple or not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise PlacementError("INVALID_CANDIDATES")
    for candidate in candidates:
        check_id(candidate, "candidate")
    if len(set(candidates)) != len(candidates):
        raise PlacementError("DUPLICATE_CANDIDATE")
    return candidates


def _placements(tx) -> list:
    out, after = [], None
    while True:
        page = tx.list(Placement, limit=1000, after_id=after)
        out.extend(page)
        if len(page) < 1000:
            return out
        after = page[-1].value.id


class PlacementPlanner:
    """Place VM-ready Task attempts on eligible pool/profile routes."""

    def __init__(self, store: RuntimeStore, registry, inventory, journal: PlacementJournal, *, clock=None):
        if type(store) is not RuntimeStore:
            raise PlacementError("INVALID_STORE")
        if type(journal) is not PlacementJournal:
            raise PlacementError("INVALID_JOURNAL")
        if any(not callable(getattr(registry, name, None)) for name in ("status", "operational_inventory")):
            raise PlacementError("INVALID_REGISTRY")
        if not callable(getattr(inventory, "validate", None)):
            raise PlacementError("INVALID_INVENTORY")
        if clock is not None and not callable(clock):
            raise PlacementError("INVALID_CLOCK")
        self._store = store
        self._registry = registry
        self._inventory = inventory
        self._journal = journal
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise PlacementError("CLOCK_UNAVAILABLE") from None
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise PlacementError("INVALID_CLOCK")
        return value

    def _matrix_revision(self) -> int | None:
        try:
            value = self._registry.matrix_revision
        except Exception:
            return None
        return value if type(value) is int else None

    # ------------------------------------------------------------------ plan
    def plan(self, task: ReadyTask, candidates: tuple[str, ...], *,
             parallel: ParallelQualification | None = None) -> PlacementDecision:
        task = rebuild(task, ReadyTask)
        candidates = _check_candidates(candidates)
        if parallel is not None:
            parallel = rebuild(parallel, ParallelQualification)
        now = self._now()
        matrix = self._matrix_revision()
        with self._store.transaction() as tx:
            result = self._evaluate(tx, task, candidates, parallel)
        decision = PlacementDecision(
            decision_id="decision-" + uuid.uuid4().hex,
            status=result.status,
            task=task,
            placement=result.placement,
            profile_id=result.profile_id,
            pool_id=result.pool_id,
            reasons=result.reasons,
            checks=result.checks,
            inventory_revision=MappingProxyType({"matrix_revision": matrix,
                                                 "resource_revision": result.resource_revision}),
            parallel_basis=parallel,
            decided_at=_iso(now),
            digest="",
        )
        decision = replace(decision, digest=compute_digest(decision_document(decision)))
        # Appended after the store commit. If this fails the error propagates;
        # a retry of the same task returns the existing placement idempotently.
        self._journal.append(decision)
        return decision

    def _evaluate(self, tx, task: ReadyTask, candidates: tuple, parallel) -> _Result:
        if task.purpose == "qualification" and task.candidate_route is None:
            return _Result("rejected", None, None, None, ("QUALIFICATION_ROUTE_REQUIRED",), (), None)
        if task.purpose == "operation" and task.candidate_route is not None:
            return _Result("rejected", None, None, None, ("CANDIDATE_ROUTE_NOT_OPERATIONAL",), (), None)
        placements = _placements(tx)
        for entry in placements:
            existing = entry.value
            if existing.vm_attempt_id != task.vm_attempt_id:
                continue
            same_task = (existing.vm_instance_id, existing.vm_project_id, existing.vm_task_id) == (
                task.vm_instance_id, task.vm_project_id, task.vm_task_id)
            if same_task and existing.profile_id in candidates and existing.state in ACTIVE_STATES:
                return _Result("placed", entry, existing.profile_id, existing.pool_id,
                               ("IDEMPOTENT_EXISTING_PLACEMENT",), (), None)
            return _Result("rejected", None, None, None, ("ATTEMPT_ALREADY_PLACED",), (), None)
        active = [entry.value for entry in placements if entry.value.state in OCCUPYING_STATES]
        checks = []
        for profile_id in candidates:
            check, parallel_used = self._check_profile(tx, task, profile_id, active, parallel)
            checks.append(check)
            if check.outcome != "eligible":
                continue
            placement = Placement(
                _placement_id(task), check.pool_id, profile_id, task.vm_instance_id, task.vm_project_id,
                task.vm_task_id, task.vm_attempt_id, state="assigned",
            )
            saved = tx.save(placement, expected_revision=0)
            reasons = ("PLACED",) + (("SAME_PROJECT_PARALLEL_QUALIFIED",) if parallel_used else ())
            return _Result("placed", saved, profile_id, check.pool_id, reasons, tuple(checks),
                           check.resource_revision)
        status = "wait" if any(check.outcome == "wait" for check in checks) else "rejected"
        reasons = []
        for check in checks:
            for code in check.reasons:
                if code not in reasons:
                    reasons.append(code)
        return _Result(status, None, None, None, tuple(reasons), tuple(checks), None)

    # ------------------------------------------------------------ eligibility
    def _check_profile(self, tx, task: ReadyTask, profile_id: str, active: list, parallel) -> tuple:
        rejects: list[str] = []
        waits: list[str] = []

        def finish(pool_id, resource_revision=None, parallel_used=False):
            outcome = "rejected" if rejects else ("wait" if waits else "eligible")
            return ProfileCheck(profile_id, pool_id, outcome, tuple(rejects + waits), resource_revision), \
                parallel_used and outcome == "eligible"

        stored = tx.get(ExecutionProfile, profile_id)
        if stored is None:
            rejects.append("PROFILE_NOT_FOUND")
            return finish(None)
        profile = stored.value
        if profile.role != task.role:
            rejects.append("ROLE_MISMATCH")
        if profile.model != task.model:
            rejects.append("MODEL_MISMATCH")
        if profile.effort is not None and profile.effort != task.effort:
            rejects.append("EFFORT_MISMATCH")
        pool_entry = tx.get(Pool, profile.pool_id)
        if pool_entry is None:
            rejects.append("POOL_NOT_FOUND")
            return finish(profile.pool_id)
        pool = pool_entry.value
        if not pool.enabled:
            rejects.append("POOL_DISABLED")

        route = None
        try:
            route = self._registry.status(pool.id, task.model)
        except UnknownRoute:
            rejects.append("ROUTE_NOT_REGISTERED")
        except Exception:
            waits.append("ROUTE_STATUS_UNAVAILABLE")
        if route is not None:
            self._route_reasons(task, pool, route, rejects, waits)

        resource_revision = None
        try:
            result = self._inventory.validate(pool.connection_id, task.model, task.effort, task.tools)
        except UnknownResource:
            rejects.append("CAPABILITY_RESOURCE_UNKNOWN")
        except Exception:
            rejects.append("CAPABILITY_CHECK_FAILED")
        else:
            revision = getattr(result, "resource_revision", None)
            resource_revision = revision if type(revision) is int else None
            if getattr(result, "accepted", None) is not True:
                codes = [code for code in getattr(result, "reasons", ()) if type(code) is str]
                rejects.extend("CAPABILITY:" + code for code in codes)
                if not codes:
                    rejects.append("CAPABILITY_NOT_SUPPORTED")

        if sum(1 for placement in active if placement.pool_id == pool.id) >= pool.max_parallel:
            waits.append("POOL_PARALLEL_LIMIT")

        parallel_used = False
        if any(placement.vm_project_id == task.vm_project_id for placement in active):
            if parallel is None:
                waits.append("SAME_PROJECT_SERIAL")
            elif route is None or str(route.level) != _QUALIFIED:
                rejects.append("PARALLEL_ROUTE_NOT_QUALIFIED")
            elif resource_revision is None or parallel.inventory_revision != resource_revision:
                rejects.append("PARALLEL_BASIS_STALE")
            else:
                parallel_used = True
        return finish(pool.id, resource_revision, parallel_used)

    def _route_reasons(self, task: ReadyTask, pool: Pool, route, rejects: list, waits: list) -> None:
        before = len(rejects) + len(waits)
        if (route.pool_id, route.model) != (pool.id, task.model):
            rejects.append("ROUTE_MISMATCH")
            return
        freshness = str(route.freshness)
        level = str(route.level)
        if task.purpose == "qualification":
            if (pool.id, task.model) != task.candidate_route:
                rejects.append("NOT_CANDIDATE_ROUTE")
            elif freshness != "fresh":
                waits.append("ROUTE_NOT_FRESH")
            elif level not in _CANDIDATE_LEVELS:
                rejects.append("ROUTE_NOT_CANDIDATE")
        else:
            if freshness != "fresh":
                waits.append("ROUTE_NOT_FRESH")
            elif level != _QUALIFIED:
                rejects.append("ROUTE_NOT_QUALIFIED")
        if freshness == "fresh":
            capacity = route.available_capacity
            if type(capacity) is not int:
                waits.append("CAPACITY_UNKNOWN")
            elif capacity <= 0:
                waits.append("CAPACITY_EXHAUSTED")
            elif route.executable_now is not True:
                rejects.append("ROUTE_NOT_EXECUTABLE")
        if task.purpose == "operation" and len(rejects) + len(waits) == before:
            # The registry's operational inventory is the authority for automatic placement.
            try:
                eligible = self._registry.operational_inventory(model=task.model)
            except Exception:
                waits.append("ROUTE_STATUS_UNAVAILABLE")
                return
            if not any((item.pool_id, item.model) == (pool.id, task.model) for item in eligible):
                rejects.append("ROUTE_NOT_PLACEMENT_ELIGIBLE")

    # ---------------------------------------------------------------- release
    def release_placement(self, placement_id: str, expected_revision: int, state: str = "released") -> Versioned:
        """Only an AIK placement state transition; no lease or execution right is touched."""
        check_id(placement_id, "placement_id")
        if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_INTEGER:
            raise PlacementError("INVALID_EXPECTED_REVISION")
        if type(state) is not str or state not in RELEASE_STATES:
            raise PlacementError("INVALID_RELEASE_STATE")
        with self._store.transaction() as tx:
            stored = tx.get(Placement, placement_id)
            if stored is None:
                raise PlacementError("UNKNOWN_PLACEMENT")
            if stored.revision != expected_revision:
                raise RevisionConflict("The placement revision has changed.")
            current = stored.value.state
            if current not in OCCUPYING_STATES or (current == "unknown" and state == "unknown"):
                raise PlacementError("PLACEMENT_NOT_ACTIVE")
            return tx.save(replace(stored.value, state=state), expected_revision=expected_revision)
