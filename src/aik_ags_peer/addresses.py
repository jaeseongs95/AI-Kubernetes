"""Append-only binding of AIK worker addresses to AGS {host, sessionId}.

``aik-worker:<worker_id>`` is an AIK convention. AGS has no worker concept;
it knows only {host, sessionId}. One worker binds to exactly one session and
one session to exactly one worker; rows are never updated or deleted.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ._store import AppendOnlyDatabase
from .errors import AgsPeerError
from .models import (
    WorkerAddress, endpoint_part, is_identifier, optional_identifier, require_identifier, utc_now,
)


WORKER_ADDRESS_PREFIX = "aik-worker:"
_COLUMNS = ("worker_id", "worker_address", "host", "session_id", "ags_peer_id",
            "lease_id", "vm_attempt_id", "bound_at")
_SELECT = ", ".join(f'"{name}"' for name in _COLUMNS)


def worker_address(worker_id: str) -> str:
    return WORKER_ADDRESS_PREFIX + require_identifier(worker_id)


def ags_peer_id(host: str, session_id: str) -> str:
    """Readable ``host:session`` when unambiguous and identifier-safe, else a digest."""
    endpoint_part(host)
    endpoint_part(session_id)
    direct = f"{host}:{session_id}"
    if ":" not in host and is_identifier(direct):
        return direct
    return "peer-" + hashlib.sha256(f"{host}\0{session_id}".encode("utf-8")).hexdigest()[:40]


class AddressBook(AppendOnlyDatabase):
    STATEMENTS = (
        """CREATE TABLE "worker_addresses" (
    "seq" INTEGER PRIMARY KEY,
    "worker_id" TEXT NOT NULL UNIQUE,
    "worker_address" TEXT NOT NULL UNIQUE,
    "host" TEXT NOT NULL,
    "session_id" TEXT NOT NULL,
    "ags_peer_id" TEXT NOT NULL,
    "lease_id" TEXT,
    "vm_attempt_id" TEXT,
    "bound_at" TEXT NOT NULL,
    UNIQUE ("host", "session_id")
)""",
        'CREATE TRIGGER "worker_addresses_no_update" BEFORE UPDATE ON "worker_addresses" '
        "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
        'CREATE TRIGGER "worker_addresses_no_delete" BEFORE DELETE ON "worker_addresses" '
        "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    )

    def __init__(self, path: Path | str, *, timeout: float = 5.0, clock=None):
        if clock is not None and not callable(clock):
            raise AgsPeerError("INVALID_CLOCK")
        self._clock = clock
        super().__init__(path, timeout=timeout)

    @staticmethod
    def _decode(row) -> WorkerAddress:
        try:
            return WorkerAddress(**dict(zip(_COLUMNS, row)))
        except (AgsPeerError, TypeError, ValueError):
            raise AgsPeerError("INVALID_STORE") from None

    def bind(self, worker_id: str, host: str, session_id: str, *,
             lease_id: str | None = None, vm_attempt_id: str | None = None) -> WorkerAddress:
        require_identifier(worker_id)
        endpoint_part(host)
        endpoint_part(session_id)
        optional_identifier(lease_id)
        optional_identifier(vm_attempt_id)
        candidate = WorkerAddress(
            worker_id=worker_id, worker_address=worker_address(worker_id), host=host,
            session_id=session_id, ags_peer_id=ags_peer_id(host, session_id), lease_id=lease_id,
            vm_attempt_id=vm_attempt_id, bound_at=utc_now(self._clock),
        )
        with self._write() as connection:
            by_worker = connection.execute(
                f'SELECT "seq", {_SELECT} FROM "worker_addresses" WHERE "worker_id" = ?', (worker_id,),
            ).fetchone()
            by_session = connection.execute(
                f'SELECT "seq", {_SELECT} FROM "worker_addresses" WHERE "host" = ? AND "session_id" = ?',
                (host, session_id),
            ).fetchone()
            if by_worker is not None or by_session is not None:
                if by_worker is not None and by_session is not None and by_worker[0] == by_session[0]:
                    return self._decode(by_worker[1:])
                raise AgsPeerError("ADDRESS_CONFLICT")
            placeholders = ", ".join("?" for _ in _COLUMNS)
            connection.execute(
                f'INSERT INTO "worker_addresses" ({_SELECT}) VALUES ({placeholders})',
                [getattr(candidate, name) for name in _COLUMNS],
            )
        return candidate

    def resolve_worker(self, worker_id: str) -> WorkerAddress | None:
        require_identifier(worker_id)
        with self._session() as connection:
            row = connection.execute(
                f'SELECT {_SELECT} FROM "worker_addresses" WHERE "worker_id" = ?', (worker_id,),
            ).fetchone()
        return None if row is None else self._decode(row)

    def resolve_session(self, host: str, session_id: str) -> WorkerAddress | None:
        endpoint_part(host)
        endpoint_part(session_id)
        with self._session() as connection:
            row = connection.execute(
                f'SELECT {_SELECT} FROM "worker_addresses" WHERE "host" = ? AND "session_id" = ?',
                (host, session_id),
            ).fetchone()
        return None if row is None else self._decode(row)
