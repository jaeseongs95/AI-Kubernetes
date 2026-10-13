"""Elasticity controller: ready order -> placement -> capacity/reserve check -> gated lease.

Every decision is returned in ``TickResult``. The authoritative state stays in
the runtime store, the lease manager and VM. The only in-memory data is the
time at which this controller first saw a lease idle. It is used for idle
reclaim and errs on the late side: after a restart, the idle period counts
again from the first observation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
import math
import re
from types import MappingProxyType

from aik_lease import HOLDING_STATES, LeaseError
from aik_lifecycle import GatedLeaseManager, LifecycleController, LifecycleError
from aik_placement import ACTIVE_STATES, PlacementError, PlacementPlanner, ReadyTask
from aik_runtime import (
    ExecutionProfile, InvalidRecord, Lease, Placement, Pool, RevisionConflict, RuntimeStore, Versioned,
)
from aik_runtime.models import MAX_INTEGER, identifier


CONTROL_RESERVE = "CONTROL_RESERVE"
CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
NO_CANDIDATE_PROFILE = "NO_CANDIDATE_PROFILE"
LEASE_RESERVE_FAILED = "LEASE_RESERVE_FAILED"
LEASE_RESULT_UNREADABLE = "LEASE_RESULT_UNREADABLE"
PLACEMENT_RELEASE_FAILED = "PLACEMENT_RELEASE_FAILED"
_PAGE = 1000
_CODE = re.compile(r"[A-Z][A-Z0-9_:.-]{0,127}\Z")


class ElasticError(ValueError):
    """Fixed code-only failure; the message never contains supplied values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise ElasticError("INVALID_" + field.upper()) from None


@dataclass(frozen=True, slots=True)
class TickResult:
    """granted: ((vm_attempt_id, lease_id), ...); waiting: ((vm_attempt_id, reasons), ...)."""

    granted: tuple
    waiting: tuple
    reclaimed: tuple


def _result_parts(result) -> tuple:
    """Read (status, lease, reasons) from an aik_lease.LeaseResult.

    Named attributes are preferred. Otherwise the leading positional fields are
    used: (status, lease, reasons, ...).
    """
    positional = ()
    if is_dataclass(result) and not isinstance(result, type):
        positional = tuple(getattr(result, item.name) for item in fields(result))
    elif isinstance(result, tuple):
        positional = tuple(result)

    def pick(name: str, index: int):
        if hasattr(result, name):
            return getattr(result, name)
        return positional[index] if len(positional) > index else None

    status = pick("status", 0)
    status = str(status) if isinstance(status, str) else "unknown"
    lease = pick("lease", 1)
    raw = pick("reasons", 2)
    reasons = tuple(str(reason) for reason in raw if reason is not None) if isinstance(raw, (tuple, list)) else ()
    return status, lease, reasons


def _error_code(error: BaseException, default: str) -> str:
    code = getattr(error, "code", None)
    code = str(code) if code is not None else None
    return code if code is not None and _CODE.fullmatch(code) else default


class ElasticityController:
    """Grow workers up to the verified capacity and return idle, undispatched leases."""

    def __init__(self, planner, leases, store, *, control_reserve: Mapping[str, int] | None = None,
                 idle_seconds: float, clock=None):
        if not isinstance(planner, PlacementPlanner):
            raise ElasticError("INVALID_PLANNER")
        if not isinstance(leases, GatedLeaseManager):
            # Ungated managers would bypass pause/drain; fail closed.
            raise ElasticError("UNGATED_LEASE_MANAGER")
        if not isinstance(store, RuntimeStore):
            raise ElasticError("INVALID_STORE")
        reserve: dict[str, int] = {}
        if control_reserve is not None:
            if not isinstance(control_reserve, Mapping):
                raise ElasticError("INVALID_CONTROL_RESERVE")
            for pool_id, count in control_reserve.items():
                _check_id(pool_id, "pool_id")
                if type(count) is not int or not 0 <= count <= MAX_INTEGER:
                    raise ElasticError("INVALID_CONTROL_RESERVE")
                reserve[pool_id] = count
        if (isinstance(idle_seconds, bool) or not isinstance(idle_seconds, (int, float))
                or not math.isfinite(idle_seconds) or idle_seconds < 0):
            raise ElasticError("INVALID_IDLE_SECONDS")
        if clock is not None and not callable(clock):
            raise ElasticError("INVALID_CLOCK")
        self._planner = planner
        self._leases = leases
        self._store = store
        self._reserve = MappingProxyType(reserve)
        self._idle_seconds = float(idle_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._idle_since: dict[tuple[str, int], datetime] = {}

    @property
    def control_reserve(self) -> Mapping[str, int]:
        return self._reserve

    @property
    def idle_seconds(self) -> float:
        return self._idle_seconds

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ElasticError("INVALID_CLOCK")
        return value

    # -- reading the authoritative store ---------------------------------

    def _all_leases(self) -> tuple:
        entries: list = []
        after = None
        with self._store.transaction() as tx:
            while True:
                page = tx.list(Lease, limit=_PAGE, after_id=after)
                entries.extend(page)
                if len(page) < _PAGE:
                    return tuple(entries)
                after = page[-1].value.id

    def _block(self, pool_id: str, leases: tuple) -> str | None:
        """Classify why a pool must not receive another worker, or None."""
        entry = self._store.get(Pool, pool_id)
        if entry is None:
            return None  # The planner rejects unknown pools with its own reason.
        max_parallel = entry.value.max_parallel
        reserve = self._reserve.get(pool_id, 0)
        if max_parallel <= reserve:
            return CONTROL_RESERVE
        holding = sum(1 for item in leases
                      if item.value.pool_id == pool_id and item.value.state in HOLDING_STATES)
        if holding >= max_parallel:
            return CAPACITY_EXHAUSTED
        if holding >= max_parallel - reserve:
            return CONTROL_RESERVE
        return None

    def _closed(self, pool_id: str, vm_project_id: str) -> tuple:
        """Gate reasons checked before planning, so a paused scope creates no placement.

        A placement is one per VM Attempt; one created and released here could not
        be placed again after resume. GatedLeaseManager still re-checks at reserve.
        """
        try:
            is_open, reasons = self._leases.gate.is_open(pool_id, vm_project_id)
        except Exception:
            return ("GATE_UNAVAILABLE",)
        return () if is_open else tuple(reasons)

    def _cleanup(self, placement_id: str) -> tuple:
        """Release an AIK placement left without a lease. Returns extra reasons on failure."""
        entry = self._store.get(Placement, placement_id)
        if entry is None or entry.value.state not in ACTIVE_STATES:
            return ()
        try:
            self._planner.release_placement(placement_id, entry.revision, "released")
        except (RevisionConflict, PlacementError, InvalidRecord):
            return (PLACEMENT_RELEASE_FAILED,)
        return ()

    # -- inputs -------------------------------------------------------------

    @staticmethod
    def _candidates(task: ReadyTask, candidates) -> tuple:
        value = candidates(task) if callable(candidates) else candidates
        if type(value) not in (tuple, list):
            raise ElasticError("INVALID_CANDIDATES")
        for profile_id in value:
            _check_id(profile_id, "candidate")
        return tuple(value)

    @staticmethod
    def _options(task: ReadyTask, placement: Placement, reserve_options) -> dict:
        value = reserve_options(task, placement) if callable(reserve_options) else reserve_options
        if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
            raise ElasticError("INVALID_RESERVE_OPTIONS")
        if "placement_id" in value:
            raise ElasticError("INVALID_RESERVE_OPTIONS")
        return dict(value)

    # -- public operations --------------------------------------------------

    def tick(self, ready: Sequence[ReadyTask], *, candidates, reserve_options, parallel=None) -> TickResult:
        """Process VM's ready list in its given order. Nothing is reordered, retried or chained.

        candidates: profile IDs (tuple) or callable(task) -> tuple, in preference order.
        reserve_options: kwargs for leases.reserve, or callable(task, placement) -> kwargs.
        parallel: None or callable(task) -> ParallelQualification | None.
        """
        if isinstance(ready, (str, bytes)) or not isinstance(ready, Sequence):
            raise ElasticError("INVALID_READY")
        tasks = tuple(ready)
        if any(type(task) is not ReadyTask for task in tasks):
            raise ElasticError("INVALID_READY_TASK")
        if parallel is not None and not callable(parallel):
            raise ElasticError("INVALID_PARALLEL")
        reclaimed = self.reclaim_idle()
        granted: list = []
        waiting: list = []
        seen: set[str] = set()
        for task in tasks:
            if task.vm_attempt_id in seen:
                continue  # Only the first entry of an attempt is considered.
            seen.add(task.vm_attempt_id)
            kind, value = self._place(task, candidates, reserve_options, parallel)
            (granted if kind == "granted" else waiting).append((task.vm_attempt_id, value))
        return TickResult(tuple(granted), tuple(waiting), reclaimed)

    def _place(self, task: ReadyTask, candidates, reserve_options, parallel) -> tuple:
        profiles = self._candidates(task, candidates)
        if not profiles:
            return "waiting", (NO_CANDIDATE_PROFILE,)
        leases = self._all_leases()
        allowed: list = []
        blocked: list = []
        for profile_id in profiles:
            profile = self._store.get(ExecutionProfile, profile_id)
            codes = () if profile is None else self._closed(profile.value.pool_id, task.vm_project_id)
            if not codes and profile is not None:
                code = self._block(profile.value.pool_id, leases)
                codes = () if code is None else (code,)
            if not codes:
                allowed.append(profile_id)
            for code in codes:
                if code not in blocked:
                    blocked.append(code)
        if not allowed:
            # Decided before planning: no placement record is created.
            return "waiting", tuple(blocked)
        qualification = None if parallel is None else parallel(task)
        try:
            decision = self._planner.plan(task, tuple(allowed), parallel=qualification)
        except PlacementError as error:
            return "waiting", (_error_code(error, "PLACEMENT_ERROR"),)
        if not decision.placed or decision.placement is None:
            reasons = tuple(decision.reasons)
            return "waiting", reasons or ("PLACEMENT_" + str(decision.status).upper(),)
        placement = decision.placement.value
        code = self._block(placement.pool_id, self._all_leases())
        if code is not None:
            return "waiting", (code,) + self._cleanup(placement.id)
        options = self._options(task, placement, reserve_options)
        try:
            result = self._leases.reserve(placement.id, **options)
        except (LeaseError, LifecycleError) as error:
            return "waiting", (_error_code(error, LEASE_RESERVE_FAILED),) + self._cleanup(placement.id)
        except BaseException:
            self._cleanup(placement.id)
            raise
        status, lease, reasons = _result_parts(result)
        if status == "granted" and isinstance(lease, Versioned) and type(lease.value) is Lease:
            self._idle_since[(lease.value.id, lease.revision)] = self._now()
            return "granted", lease.value.id
        if status == "granted":
            # A lease may exist that cannot be read; keep the placement (fail closed).
            return "waiting", (LEASE_RESULT_UNREADABLE,)
        return "waiting", (reasons or ("LEASE_" + status.upper(),)) + self._cleanup(placement.id)

    def reclaim_idle(self) -> tuple:
        """Release reserved, never-dispatched leases idle for at least idle_seconds.

        active, draining and unknown leases are never touched: they are running
        or their state is unclear, not idle.
        """
        now = self._now()
        still_idle: set = set()
        reclaimed: list = []
        for entry in self._all_leases():
            lease = entry.value
            if lease.state != "reserved" or lease.provider_job_id is not None:
                continue
            key = (lease.id, entry.revision)
            first = self._idle_since.setdefault(key, now)
            if (now - first).total_seconds() < self._idle_seconds:
                still_idle.add(key)
                continue
            try:
                self._leases.release(lease.id, lease.fencing_token, dispatched=False, provider_terminal=False)
            except (LeaseError, LifecycleError):
                still_idle.add(key)
                continue
            current = self._store.get(Lease, lease.id)
            if current is not None and current.value.state in HOLDING_STATES:
                still_idle.add(key)
                continue
            reclaimed.append(lease.id)
            self._cleanup(lease.placement_id)
        self._idle_since = {key: value for key, value in self._idle_since.items() if key in still_idle}
        return tuple(reclaimed)

    def finish(self, lease_id: str, fencing_token: int, lifecycle, *, required_artifacts: tuple):
        """Delegate post-completion/failure cleanup to LifecycleController.teardown.

        Evidence, usage and termination rules are teardown's own. Only after a
        complete teardown is the lease-less AIK placement released.
        """
        _check_id(lease_id, "lease_id")
        if not isinstance(lifecycle, LifecycleController):
            raise ElasticError("INVALID_LIFECYCLE")
        view = lifecycle.teardown(lease_id, fencing_token, required_artifacts=required_artifacts)
        if view.teardown_complete:
            self._idle_since = {key: value for key, value in self._idle_since.items() if key[0] != lease_id}
            entry = self._store.get(Lease, lease_id)
            if entry is not None and entry.value.state not in HOLDING_STATES:
                self._cleanup(entry.value.placement_id)
        return view
