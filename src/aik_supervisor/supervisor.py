"""Owner lifecycle: window, pause and full shutdown are separate operations.

Window state does not stop the runtime. PAUSED and SHUTTING_DOWN reject new
placements. In-flight leases are never deleted or rewritten here; restart lists
them as observation targets. Lease/fencing transitions belong to AIK-033/034.
Without a runtime store no scan happens, so results are reported as unknown
(incomplete), never as "no in-flight jobs".
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import threading
import time
import uuid

from aik_runtime import Lease, Placement, RuntimeStore, StorageError

from .instance import (
    EndpointRecord, InstanceLock, InstancePaths, InvalidEndpoint, PlacementRejected,
    SupervisorError, SupervisorUnavailable, default_endpoint, instance_paths,
    read_endpoint, remove_endpoint, try_acquire, utc_now, validate_component,
    validate_workspace_id, write_endpoint,
)


LIFECYCLE_STATES = ("RUNNING", "PAUSED", "SHUTTING_DOWN", "STOPPED")
WINDOW_STATES = ("WINDOW_OPEN", "WINDOW_CLOSED")
IN_FLIGHT_LEASE_STATES = ("reserved", "active", "draining", "unknown")
_PAGE = 1000


@dataclass(frozen=True, slots=True)
class Attachment:
    """A second launch: reconnect to the existing owner, no new instance."""

    record: EndpointRecord
    role: str = "attached"


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    """scan_complete=False means in-flight jobs are unknown, not absent."""

    in_flight_lease_ids: tuple
    scan_complete: bool


def _scan_in_flight(store: RuntimeStore) -> tuple:
    # Scope is the whole store: Lease/Placement carry no workspace field.
    found = []
    after = None
    with store.transaction() as tx:
        while True:
            page = tx.list(Lease, limit=_PAGE, after_id=after)
            found.extend(entry for entry in page if entry.value.state in IN_FLIGHT_LEASE_STATES)
            if len(page) < _PAGE:
                return tuple(found)
            after = page[-1].value.id


class Supervisor:
    role = "owner"

    def __init__(self, paths: InstancePaths, lock: InstanceLock, record: EndpointRecord,
                 store: RuntimeStore | None, observation_targets: tuple,
                 previous_instance_id: str | None, stale_endpoint_replaced: bool,
                 observation_scan_complete: bool | None = None):
        self.paths = paths
        self.record = record
        self.observation_targets = observation_targets
        # False means the targets are unknown (no store was scanned), not empty.
        self.observation_scan_complete = (
            store is not None if observation_scan_complete is None else bool(observation_scan_complete)
        )
        self.previous_instance_id = previous_instance_id
        self.stale_endpoint_replaced = stale_endpoint_replaced
        self._lock = lock
        self._store = store
        self._mutex = threading.RLock()
        self._lifecycle = "RUNNING"
        self._window = "WINDOW_OPEN"
        self._report: ShutdownReport | None = None

    @property
    def instance_id(self) -> str:
        return self.record.instance_id

    @property
    def lifecycle(self) -> str:
        return self._lifecycle

    @property
    def window(self) -> str:
        return self._window

    def status(self) -> dict:
        with self._mutex:
            return {"instance_id": self.instance_id, "workspace_id": self.record.workspace_id,
                    "component": self.record.component, "lifecycle": self._lifecycle, "window": self._window}

    def _require_live(self) -> None:
        if self._lifecycle not in ("RUNNING", "PAUSED"):
            raise SupervisorError("The supervisor is shutting down or stopped.")

    def close_window(self) -> None:
        """GUI window closed; the runtime and its jobs keep running."""
        with self._mutex:
            self._require_live()
            self._window = "WINDOW_CLOSED"

    def open_window(self) -> None:
        with self._mutex:
            self._require_live()
            self._window = "WINDOW_OPEN"

    def pause(self) -> None:
        """Stop admitting new placements; in-flight jobs are left untouched."""
        with self._mutex:
            self._require_live()
            self._lifecycle = "PAUSED"

    def resume(self) -> None:
        with self._mutex:
            self._require_live()
            self._lifecycle = "RUNNING"

    def admit_placement(self, placement: Placement):
        """Persist a new placement only while RUNNING; storage is not execution."""
        if type(placement) is not Placement:
            raise SupervisorError("Only placement records can be admitted.")
        with self._mutex:
            if self._lifecycle != "RUNNING":
                raise PlacementRejected(f"New placements are rejected while {self._lifecycle}.")
            if self._store is None:
                raise SupervisorError("No runtime store is configured for placements.")
            return self._store.save(placement, expected_revision=0)

    def _withdraw_endpoint(self) -> None:
        try:
            current = read_endpoint(self.paths.endpoint)
        except InvalidEndpoint:
            current = None
        if current is None or current.instance_id == self.instance_id:
            remove_endpoint(self.paths.endpoint)

    def shutdown(self) -> ShutdownReport:
        """Full exit: reject new placements, keep in-flight leases, release the lock."""
        with self._mutex:
            if self._report is not None:
                return self._report
            self._lifecycle = "SHUTTING_DOWN"
            ids: tuple = ()
            complete = self._store is not None
            try:
                # Withdraw the endpoint before the (possibly slow) scan: a
                # relaunch then waits for the lock instead of attaching to a
                # dying owner.
                self._withdraw_endpoint()
            finally:
                try:
                    if self._store is not None:
                        ids = tuple(entry.value.id for entry in _scan_in_flight(self._store))
                except (StorageError, OSError):
                    complete = False
                    ids = ()
                finally:
                    self._lock.release()
                    self._window = "WINDOW_CLOSED"
                    self._lifecycle = "STOPPED"
            self._report = ShutdownReport(ids, complete)
            return self._report


def _become_owner(paths: InstancePaths, lock: InstanceLock, workspace_id: str, component: str,
                  store: RuntimeStore | None) -> Supervisor:
    # We hold the lock, so any existing record belongs to a dead owner.
    previous = None
    stale = False
    try:
        old = read_endpoint(paths.endpoint)
        if old is not None:
            previous, stale = old.instance_id, True
    except InvalidEndpoint:
        stale = True
    if stale:
        remove_endpoint(paths.endpoint)
    try:
        targets = _scan_in_flight(store) if store is not None else ()
    except (StorageError, OSError):
        raise SupervisorError("In-flight jobs could not be observed; the supervisor did not start.") from None
    instance_id = uuid.uuid4().hex
    record = EndpointRecord(instance_id, workspace_id, component, os.getpid(), utc_now(),
                            default_endpoint(paths, workspace_id, component, instance_id))
    write_endpoint(paths.endpoint, record)
    return Supervisor(paths, lock, record, store, targets, previous, stale,
                      observation_scan_complete=store is not None)


def launch(workspace_id: str, *, component: str = "director", state_root=None,
           store: RuntimeStore | None = None, attach_timeout: float = 5.0):
    """Become the single owner, or attach to the live owner's endpoint."""
    validate_workspace_id(workspace_id)
    validate_component(component)
    if (isinstance(attach_timeout, bool) or not isinstance(attach_timeout, (int, float))
            or not math.isfinite(attach_timeout) or attach_timeout < 0):
        raise SupervisorError("Attach timeout must be a finite nonnegative number.")
    if store is not None and type(store) is not RuntimeStore:
        raise SupervisorError("The store must be an aik_runtime.RuntimeStore.")
    paths = instance_paths(workspace_id, component, state_root, create=True)
    deadline = time.monotonic() + attach_timeout
    while True:
        lock = try_acquire(paths)
        if lock is not None:
            try:
                return _become_owner(paths, lock, workspace_id, component, store)
            except BaseException:
                lock.release()
                raise
        try:
            record = read_endpoint(paths.endpoint)
        except InvalidEndpoint:
            record = None
        if (record is not None and record.workspace_id.lower() == workspace_id.lower()
                and record.component == component):
            return Attachment(record)
        if time.monotonic() >= deadline:
            raise SupervisorUnavailable("A live owner holds the instance lock without a usable endpoint.")
        time.sleep(0.02)
