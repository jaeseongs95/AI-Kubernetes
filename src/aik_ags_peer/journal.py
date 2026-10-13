"""Append-only PEER journal: send steps and continuous-cursor peer events.

Bodies are never stored; only their sha256 and length. ``peer_events`` cursors
are continuous from 1, so a read model sees no artificial gaps. Send intents
live in a separate table and do not consume event cursors.
"""

from __future__ import annotations

from pathlib import Path

from ._store import AppendOnlyDatabase
from .errors import AgsPeerError
from .models import PeerEvent, SendRecord


_SEND_COLUMNS = (
    "seq", "ags_message_id", "phase", "sender_host", "sender_session_id", "target_host",
    "target_session_id", "worker_id", "worker_address", "ags_peer_id", "vm_attempt_id",
    "body_sha256", "body_length", "expires_at", "error_code", "recorded_at",
)
_SEND_SELECT = ", ".join(f'"{name}"' for name in _SEND_COLUMNS)
_EVENT_COLUMNS = (
    "cursor", "event_id", "state", "ags_message_id", "ags_peer_id", "ags_session_id",
    "worker_address", "worker_id", "vm_attempt_id", "body_sha256", "recorded_at",
)
_EVENT_SELECT = ", ".join(f'"{name}"' for name in _EVENT_COLUMNS)


def _send_record(row) -> SendRecord:
    try:
        values = dict(zip(_SEND_COLUMNS, row))
        return SendRecord(
            seq=values["seq"], ags_message_id=values["ags_message_id"], state=values["phase"],
            sender=(values["sender_host"], values["sender_session_id"]),
            target=(values["target_host"], values["target_session_id"]),
            worker_id=values["worker_id"], worker_address=values["worker_address"],
            ags_peer_id=values["ags_peer_id"], vm_attempt_id=values["vm_attempt_id"],
            body_sha256=values["body_sha256"], body_length=values["body_length"],
            expires_at=values["expires_at"], error_code=values["error_code"],
            recorded_at=values["recorded_at"],
        )
    except (AgsPeerError, TypeError, KeyError, ValueError):
        raise AgsPeerError("INVALID_STORE") from None


def _peer_event(row) -> PeerEvent:
    try:
        return PeerEvent(**dict(zip(_EVENT_COLUMNS, row)))
    except (AgsPeerError, TypeError, ValueError):
        raise AgsPeerError("INVALID_STORE") from None


class PeerJournal(AppendOnlyDatabase):
    STATEMENTS = (
        """CREATE TABLE "send_records" (
    "seq" INTEGER PRIMARY KEY,
    "ags_message_id" TEXT NOT NULL,
    "phase" TEXT NOT NULL CHECK ("phase" IN ('intent', 'sent', 'duplicate', 'send_unknown', 'send_failed')),
    "sender_host" TEXT NOT NULL,
    "sender_session_id" TEXT NOT NULL,
    "target_host" TEXT NOT NULL,
    "target_session_id" TEXT NOT NULL,
    "worker_id" TEXT NOT NULL,
    "worker_address" TEXT NOT NULL,
    "ags_peer_id" TEXT NOT NULL,
    "vm_attempt_id" TEXT,
    "body_sha256" TEXT NOT NULL,
    "body_length" INTEGER NOT NULL CHECK ("body_length" >= 0),
    "expires_at" TEXT,
    "error_code" TEXT,
    "recorded_at" TEXT NOT NULL
)""",
        'CREATE INDEX "send_records_message" ON "send_records" ("ags_message_id", "seq")',
        """CREATE TABLE "peer_events" (
    "cursor" INTEGER PRIMARY KEY CHECK ("cursor" >= 1),
    "event_id" TEXT NOT NULL UNIQUE,
    "state" TEXT NOT NULL CHECK ("state" IN ('delivered', 'claimed', 'acked', 'result')),
    "ags_message_id" TEXT NOT NULL,
    "ags_peer_id" TEXT NOT NULL,
    "ags_session_id" TEXT,
    "worker_address" TEXT,
    "worker_id" TEXT,
    "vm_attempt_id" TEXT,
    "body_sha256" TEXT,
    "recorded_at" TEXT NOT NULL,
    UNIQUE ("ags_message_id", "state")
)""",
        'CREATE TRIGGER "send_records_no_update" BEFORE UPDATE ON "send_records" '
        "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
        'CREATE TRIGGER "send_records_no_delete" BEFORE DELETE ON "send_records" '
        "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
        'CREATE TRIGGER "peer_events_no_update" BEFORE UPDATE ON "peer_events" '
        "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
        'CREATE TRIGGER "peer_events_no_delete" BEFORE DELETE ON "peer_events" '
        "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    )

    def __init__(self, path: Path | str, *, timeout: float = 5.0):
        super().__init__(path, timeout=timeout)

    def _check_rows(self, connection) -> None:
        count, maximum = connection.execute(
            'SELECT COUNT(*), COALESCE(MAX("cursor"), 0) FROM "peer_events"'
        ).fetchone()
        if count != maximum:
            raise AgsPeerError("INVALID_STORE")

    def record_send(self, values: dict) -> SendRecord:
        row = {
            "ags_message_id": values["ags_message_id"], "phase": values["phase"],
            "sender_host": values["sender"][0], "sender_session_id": values["sender"][1],
            "target_host": values["target"][0], "target_session_id": values["target"][1],
            "worker_id": values["worker_id"], "worker_address": values["worker_address"],
            "ags_peer_id": values["ags_peer_id"], "vm_attempt_id": values["vm_attempt_id"],
            "body_sha256": values["body_sha256"], "body_length": values["body_length"],
            "expires_at": values["expires_at"], "error_code": values["error_code"],
            "recorded_at": values["recorded_at"],
        }
        # Validate before writing; seq is a placeholder for the check only.
        _send_record([1, *row.values()])
        names = tuple(row)
        with self._write() as connection:
            cursor = connection.execute(
                f'INSERT INTO "send_records" ({", ".join(chr(34) + n + chr(34) for n in names)}) '
                f'VALUES ({", ".join("?" for _ in names)})',
                [row[name] for name in names],
            )
            seq = cursor.lastrowid
        return _send_record([seq, *row.values()])

    def send_history(self, message_id: str) -> tuple[SendRecord, ...]:
        with self._session() as connection:
            rows = connection.execute(
                f'SELECT {_SEND_SELECT} FROM "send_records" WHERE "ags_message_id" = ? ORDER BY "seq"',
                (message_id,),
            ).fetchall()
        return tuple(_send_record(row) for row in rows)

    def intent(self, message_id: str) -> SendRecord | None:
        with self._session() as connection:
            row = connection.execute(
                f'SELECT {_SEND_SELECT} FROM "send_records" WHERE "ags_message_id" = ? AND "phase" = \'intent\' '
                'ORDER BY "seq" LIMIT 1',
                (message_id,),
            ).fetchone()
        return None if row is None else _send_record(row)

    def record_events(self, rows: list[dict]) -> tuple[PeerEvent, ...]:
        """Append events not yet recorded for (message, state); return only new ones."""
        inserted = []
        names = _EVENT_COLUMNS[1:]
        with self._write() as connection:
            for row in rows:
                if connection.execute(
                    'SELECT 1 FROM "peer_events" WHERE "ags_message_id" = ? AND "state" = ?',
                    (row["ags_message_id"], row["state"]),
                ).fetchone() is not None:
                    continue
                cursor = connection.execute('SELECT COALESCE(MAX("cursor"), 0) + 1 FROM "peer_events"').fetchone()[0]
                event = PeerEvent(cursor=cursor, **{name: row[name] for name in names})
                connection.execute(
                    f'INSERT INTO "peer_events" ({_EVENT_SELECT}) VALUES ({", ".join("?" for _ in _EVENT_COLUMNS)})',
                    [getattr(event, name) for name in _EVENT_COLUMNS],
                )
                inserted.append(event)
        return tuple(inserted)

    def get_event(self, message_id: str, state: str) -> PeerEvent | None:
        with self._session() as connection:
            row = connection.execute(
                f'SELECT {_EVENT_SELECT} FROM "peer_events" WHERE "ags_message_id" = ? AND "state" = ?',
                (message_id, state),
            ).fetchone()
        return None if row is None else _peer_event(row)

    def events_after(self, cursor: int = 0, *, limit: int = 1000) -> tuple[PeerEvent, ...]:
        if type(cursor) is not int or cursor < 0:
            raise AgsPeerError("INVALID_ARGUMENT")
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise AgsPeerError("INVALID_LIMIT")
        with self._session() as connection:
            rows = connection.execute(
                f'SELECT {_EVENT_SELECT} FROM "peer_events" WHERE "cursor" > ? ORDER BY "cursor" LIMIT ?',
                (cursor, limit),
            ).fetchall()
        return tuple(_peer_event(row) for row in rows)
