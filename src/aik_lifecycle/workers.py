"""Worker ownership registry: AIK-created workers versus borrowed existing hosts."""

from __future__ import annotations

from dataclasses import dataclass

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier

from .database import LifecycleDatabase, timestamp
from .errors import LifecycleError


OWNERSHIP = ("owned", "borrowed")


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    """owned: AIK created the worker and may terminate it. borrowed: an existing host; never terminated."""

    worker_id: str
    ownership: str
    pool_id: str

    def __post_init__(self) -> None:
        for field in ("worker_id", "pool_id"):
            try:
                identifier(getattr(self, field), field)
            except InvalidRecord:
                raise LifecycleError("INVALID_" + field.upper()) from None
        if type(self.ownership) is not str or self.ownership not in OWNERSHIP:
            raise LifecycleError("INVALID_OWNERSHIP")


class WorkerRegistry:
    """Immutable worker ownership records in the lifecycle database."""

    def __init__(self, path, *, clock=None, timeout: float = 5.0):
        if clock is not None and not callable(clock):
            raise LifecycleError("INVALID_CLOCK")
        self._db = LifecycleDatabase(path, timeout=timeout)
        self._clock = clock

    def register(self, record: WorkerRecord) -> WorkerRecord:
        if type(record) is not WorkerRecord:
            raise LifecycleError("INVALID_WORKER_RECORD")
        # Rebuild: object.__setattr__ forgeries are validated again.
        record = WorkerRecord(record.worker_id, record.ownership, record.pool_id)
        recorded_at = timestamp(self._clock)
        with self._db.transaction() as connection:
            row = connection.execute(
                'SELECT "ownership", "pool_id" FROM "workers" WHERE "worker_id" = ?', (record.worker_id,),
            ).fetchone()
            if row is not None:
                if tuple(row) == (record.ownership, record.pool_id):
                    return record
                raise LifecycleError("WORKER_CONFLICT")
            connection.execute(
                'INSERT INTO "workers" ("worker_id", "ownership", "pool_id", "registered_at") VALUES (?, ?, ?, ?)',
                (record.worker_id, record.ownership, record.pool_id, recorded_at),
            )
        return record

    def get(self, worker_id: str) -> WorkerRecord | None:
        try:
            identifier(worker_id, "worker_id")
        except InvalidRecord:
            raise LifecycleError("INVALID_WORKER_ID") from None
        with self._db.transaction() as connection:
            row = connection.execute(
                'SELECT "worker_id", "ownership", "pool_id" FROM "workers" WHERE "worker_id" = ?', (worker_id,),
            ).fetchone()
        return None if row is None else WorkerRecord(*row)
