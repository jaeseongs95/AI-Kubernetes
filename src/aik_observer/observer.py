"""AIK-036 WorkerObserver: persisted cursor, append-only events and settlement hand-off.

Storage is a separate SQLite file that follows the aik_runtime.paths rules
(no repository/ledger/Git destinations, no links) and the shared process lock.
Events, conflicts and settlement submissions are append-only (triggers).
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import math
import sqlite3

from aik_budget.models import BudgetError, Usage, usage_from_dict
from aik_provider.contracts import (
    AdapterFailure, AuthKind, ConnectionBinding, JobReceipt, JobRequest, JobState,
    TERMINAL, require_job,
)
from aik_runtime.paths import database_lock, validate_database_path

from .models import (
    NOT_PROVIDED, EventConflict, GapRecord, IngestResult, InvalidEvent, ObservationView,
    ObservedSetting, ObservedSettings, ObserverError, ProviderEvent, RequestedSettings,
    SettlementSubmission, UnknownObservation, canonical_json, check_id, check_model,
    event_settings, merge_usage, parse_usage,
)


APPLICATION_ID = 0x41494B4F  # "AIKO"; not the runtime, budget or planning database.
SCHEMA_VERSION = 1
_TABLES = frozenset({"observations", "events", "gaps", "conflicts", "settlements"})
_SETTLEMENT_KINDS = ("settlement", "late", "release")


def _append_only(table: str) -> tuple[str, str]:
    return (
        f'CREATE TRIGGER "{table}_no_update" BEFORE UPDATE ON "{table}" '
        f"BEGIN SELECT RAISE(ABORT, 'append-only'); END",
        f'CREATE TRIGGER "{table}_no_delete" BEFORE DELETE ON "{table}" '
        f"BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    )


_SCHEMA = (
    """CREATE TABLE "observations" (
        "lease_id" TEXT PRIMARY KEY NOT NULL,
        "vm_attempt_id" TEXT NOT NULL,
        "provider_job_id" TEXT NOT NULL,
        "request_json" TEXT NOT NULL,
        "receipt_json" TEXT NOT NULL,
        "requested_model" TEXT NOT NULL,
        "requested_effort" TEXT,
        "observed_model" TEXT, "observed_model_seq" INTEGER,
        "observed_effort" TEXT, "observed_effort_seq" INTEGER,
        "reported_model" TEXT, "reported_model_seq" INTEGER,
        "reported_effort" TEXT, "reported_effort_seq" INTEGER,
        "job_state" TEXT NOT NULL,
        "job_state_at" TEXT NOT NULL,
        "terminal_at" TEXT,
        "cursor" INTEGER NOT NULL DEFAULT 0 CHECK ("cursor" >= 0),
        "stream_state" TEXT NOT NULL DEFAULT 'ok'
            CHECK ("stream_state" IN ('ok', 'gap_unresolved', 'conflict')),
        "output_events" INTEGER NOT NULL DEFAULT 0,
        "output_bytes" INTEGER NOT NULL DEFAULT 0,
        "tool_calls" INTEGER NOT NULL DEFAULT 0,
        "usage_json" TEXT NOT NULL,
        "usage_seq" INTEGER,
        "settled_usage_json" TEXT,
        "created_at" TEXT NOT NULL,
        "updated_at" TEXT NOT NULL
    )""",
    """CREATE TABLE "events" (
        "lease_id" TEXT NOT NULL REFERENCES "observations" ("lease_id"),
        "sequence" INTEGER NOT NULL CHECK ("sequence" >= 1),
        "event_id" TEXT NOT NULL,
        "kind" TEXT NOT NULL,
        "provenance" TEXT NOT NULL,
        "digest" TEXT NOT NULL,
        "payload_bytes" INTEGER NOT NULL,
        "applied_at" TEXT NOT NULL,
        PRIMARY KEY ("lease_id", "sequence")
    )""",
    """CREATE TABLE "gaps" (
        "id" INTEGER PRIMARY KEY AUTOINCREMENT,
        "lease_id" TEXT NOT NULL REFERENCES "observations" ("lease_id"),
        "start_seq" INTEGER NOT NULL,
        "end_seq" INTEGER NOT NULL,
        "detected_at" TEXT NOT NULL,
        "resolved_at" TEXT
    )""",
    """CREATE TABLE "conflicts" (
        "id" INTEGER PRIMARY KEY AUTOINCREMENT,
        "lease_id" TEXT NOT NULL REFERENCES "observations" ("lease_id"),
        "sequence" INTEGER NOT NULL,
        "stored_digest" TEXT,
        "received_digest" TEXT NOT NULL,
        "detected_at" TEXT NOT NULL
    )""",
    """CREATE TABLE "settlements" (
        "id" INTEGER PRIMARY KEY AUTOINCREMENT,
        "lease_id" TEXT NOT NULL REFERENCES "observations" ("lease_id"),
        "usage_json" TEXT NOT NULL,
        "budget_kind" TEXT NOT NULL,
        "submitted_at" TEXT NOT NULL
    )""",
    *_append_only("events"),
    *_append_only("conflicts"),
    *_append_only("settlements"),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _write(connection: sqlite3.Connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _request_document(request: JobRequest) -> dict:
    binding = request.binding
    return {
        "binding": {
            "provider": binding.provider, "runtime": binding.runtime,
            "connection_id": binding.connection_id, "revision": binding.revision,
            "auth_kind": binding.auth_kind.value, "credential_ref": binding.credential_ref,
            "account_fingerprint": binding.account_fingerprint,
        },
        "request_id": request.request_id, "route_id": request.route_id,
        "vm_attempt_id": request.vm_attempt_id, "model": request.model,
    }


def _request_from(document: dict) -> JobRequest:
    binding = dict(document["binding"])
    binding["auth_kind"] = AuthKind(binding["auth_kind"])
    return JobRequest(ConnectionBinding(**binding), document["request_id"], document["route_id"],
                      document["vm_attempt_id"], document["model"])


def _receipt_document(receipt: JobReceipt) -> dict:
    return {
        "request_id": receipt.request_id, "provider_job_id": receipt.provider_job_id,
        "connection_id": receipt.connection_id, "revision": receipt.revision,
        "account_fingerprint": receipt.account_fingerprint, "state": receipt.state.value,
    }


def _receipt_from(document: dict) -> JobReceipt:
    values = dict(document)
    values["state"] = JobState(values["state"])
    return JobReceipt(**values)


def _setting(value, sequence, provenance: str) -> ObservedSetting:
    if value is None:
        return ObservedSetting(None, provenance, None, NOT_PROVIDED)
    return ObservedSetting(value, provenance, sequence, None)


class WorkerObserver:
    """Observe one provider job per lease through injected ports.

    budget must provide settle(reservation_id, usage). It is called once when
    a terminal provider state is first observed, and again only when the usage
    total changes afterwards (the budget store classifies that as late).
    """

    def __init__(self, path, *, budget, clock=None, timeout: float = 5.0):
        if not callable(getattr(budget, "settle", None)):
            raise TypeError("A budget store with settle() is required.")
        if clock is not None and not callable(clock):
            raise TypeError("The clock must be callable.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ObserverError("INVALID_TIMEOUT")
        self._budget = budget
        self._clock = clock or _utc_now
        self.timeout = float(timeout)
        self.path = validate_database_path(path, create_parent=True)
        with database_lock(self.path, self.timeout):
            self._initialize()

    # ------------------------------------------------------------ storage

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ObserverError("INVALID_CLOCK")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rwc", uri=True,
                                     timeout=self.timeout, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise ObserverError("INVALID_DATABASE")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _validate(connection: sqlite3.Connection, *, integrity: bool = False) -> None:
        if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
            raise ObserverError("INVALID_DATABASE")
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not _TABLES <= names:
            raise ObserverError("INVALID_DATABASE")
        if integrity and connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ObserverError("INVALID_DATABASE")

    def _initialize(self) -> None:
        existed = self.path.exists() and self.path.stat().st_size > 0
        try:
            with closing(self._connect()) as connection:
                if not existed:
                    with _write(connection):
                        if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                            raise ObserverError("INVALID_DATABASE")
                        for statement in _SCHEMA:
                            connection.execute(statement)
                        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._validate(connection, integrity=True)
        except sqlite3.Error:
            raise ObserverError("INVALID_DATABASE") from None

    @contextmanager
    def _session(self):
        with database_lock(self.path, self.timeout):
            try:
                connection = self._connect()
            except sqlite3.Error:
                raise ObserverError("DATABASE_UNAVAILABLE") from None
            try:
                self._validate(connection)
                yield connection
            except sqlite3.Error:
                raise ObserverError("STORAGE_FAILED") from None
            finally:
                connection.close()

    @staticmethod
    def _row(connection: sqlite3.Connection, lease_id: str) -> sqlite3.Row:
        row = connection.execute('SELECT * FROM "observations" WHERE "lease_id" = ?', (lease_id,)).fetchone()
        if row is None:
            raise UnknownObservation("UNKNOWN_LEASE")
        return row

    # ------------------------------------------------------------ start

    def start(self, lease_id: str, *, vm_attempt_id: str, provider_job_id: str, requested_model: str,
              requested_effort: str | None = None, request: JobRequest, receipt: JobReceipt) -> ObservationView:
        check_id(lease_id, "lease_id")
        check_id(vm_attempt_id, "vm_attempt_id")
        check_id(provider_job_id, "provider_job_id")
        check_model(requested_model, "requested_model")
        if requested_effort is not None:
            check_id(requested_effort, "requested_effort")
        if type(request) is not JobRequest:
            raise ObserverError("INVALID_REQUEST")
        try:
            receipt = require_job(receipt, request)
        except AdapterFailure:
            raise ObserverError("INVALID_RECEIPT") from None
        if request.vm_attempt_id != vm_attempt_id:
            raise ObserverError("VM_ATTEMPT_MISMATCH")
        if receipt.provider_job_id != provider_job_id:
            raise ObserverError("PROVIDER_JOB_MISMATCH")
        if request.model != requested_model:
            raise ObserverError("REQUESTED_MODEL_MISMATCH")
        request_json = canonical_json(_request_document(request))
        receipt_json = canonical_json(_receipt_document(receipt))
        with self._session() as connection:
            existing = connection.execute(
                'SELECT "vm_attempt_id", "provider_job_id", "request_json", "requested_model", "requested_effort" '
                'FROM "observations" WHERE "lease_id" = ?', (lease_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (vm_attempt_id, provider_job_id, request_json, requested_model, requested_effort):
                    raise ObserverError("LEASE_ALREADY_OBSERVED")
                return self._view(connection, lease_id)
            now = self._now()
            with _write(connection):
                connection.execute(
                    'INSERT INTO "observations" ("lease_id", "vm_attempt_id", "provider_job_id", "request_json", '
                    '"receipt_json", "requested_model", "requested_effort", "job_state", "job_state_at", '
                    '"terminal_at", "usage_json", "created_at", "updated_at") '
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (lease_id, vm_attempt_id, provider_job_id, request_json, receipt_json, requested_model,
                     requested_effort, receipt.state.value, now, now if receipt.state in TERMINAL else None,
                     canonical_json(Usage().to_dict()), now, now),
                )
            return self._view(connection, lease_id)

    # ------------------------------------------------------------ events

    @staticmethod
    def _read(read, cursor: int, limit: int):
        try:
            batch = read(None if cursor == 0 else cursor, limit)
        except Exception:
            return None  # disconnected; resume from the persisted cursor later
        if type(batch) not in (tuple, list) or len(batch) > limit:
            raise InvalidEvent("INVALID_EVENT_BATCH")
        return tuple(batch)

    def _process(self, connection, lease_id: str, cursor: int, batch: tuple):
        events = []
        for item in batch:
            if type(item) is not ProviderEvent:
                raise InvalidEvent("INVALID_EVENT")
            # Rebuild: object.__setattr__ forgeries are validated again.
            events.append(ProviderEvent(item.sequence, item.event_id, item.kind, item.payload, item.provenance))
        events.sort(key=lambda event: event.sequence)
        fresh: dict[int, ProviderEvent] = {}
        duplicates = 0
        conflicts = []
        for event in events:
            digest = event.digest
            if event.sequence <= cursor:
                stored = connection.execute(
                    'SELECT "digest" FROM "events" WHERE "lease_id" = ? AND "sequence" = ?',
                    (lease_id, event.sequence),
                ).fetchone()
                if stored is not None and stored[0] == digest:
                    duplicates += 1
                else:
                    conflicts.append((event.sequence, None if stored is None else stored[0], digest))
            elif event.sequence in fresh:
                if fresh[event.sequence].digest == digest:
                    duplicates += 1
                else:
                    conflicts.append((event.sequence, fresh[event.sequence].digest, digest))
            else:
                fresh[event.sequence] = event
        now = self._now()
        if conflicts:
            with _write(connection):
                for sequence, stored, received in conflicts:
                    connection.execute(
                        'INSERT INTO "conflicts" ("lease_id", "sequence", "stored_digest", "received_digest", '
                        '"detected_at") VALUES (?, ?, ?, ?, ?)', (lease_id, sequence, stored, received, now),
                    )
                connection.execute('UPDATE "observations" SET "stream_state" = \'conflict\', "updated_at" = ? '
                                   'WHERE "lease_id" = ?', (now, lease_id))
            raise EventConflict("EVENT_CONFLICT")
        applied = 0
        following = cursor + 1
        with _write(connection):
            while following in fresh:
                self._apply(connection, lease_id, fresh[following], now)
                following += 1
                applied += 1
            new_cursor = following - 1
            if applied:
                connection.execute('UPDATE "observations" SET "cursor" = ?, "updated_at" = ? WHERE "lease_id" = ?',
                                   (new_cursor, now, lease_id))
                connection.execute('UPDATE "gaps" SET "resolved_at" = ? WHERE "lease_id" = ? '
                                   'AND "resolved_at" IS NULL AND "end_seq" <= ?', (now, lease_id, new_cursor))
                open_gaps = connection.execute('SELECT COUNT(*) FROM "gaps" WHERE "lease_id" = ? '
                                               'AND "resolved_at" IS NULL', (lease_id,)).fetchone()[0]
                if open_gaps == 0:
                    connection.execute('UPDATE "observations" SET "stream_state" = \'ok\' WHERE "lease_id" = ? '
                                       'AND "stream_state" = \'gap_unresolved\'', (lease_id,))
        remaining = [sequence for sequence in fresh if sequence > new_cursor]
        gap = (new_cursor + 1, min(remaining) - 1) if remaining else None
        return applied, duplicates, gap

    @staticmethod
    def _apply(connection, lease_id: str, event: ProviderEvent, now: str) -> None:
        connection.execute(
            'INSERT INTO "events" ("lease_id", "sequence", "event_id", "kind", "provenance", "digest", '
            '"payload_bytes", "applied_at") VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (lease_id, event.sequence, event.event_id, event.kind, event.provenance, event.digest,
             event.payload_bytes, now),
        )
        if event.kind == "output_delta":
            connection.execute('UPDATE "observations" SET "output_events" = "output_events" + 1, '
                               '"output_bytes" = "output_bytes" + ? WHERE "lease_id" = ?',
                               (event.payload_bytes, lease_id))
        elif event.kind == "tool_call":
            connection.execute('UPDATE "observations" SET "tool_calls" = "tool_calls" + 1 WHERE "lease_id" = ?',
                               (lease_id,))
        elif event.kind == "usage":
            # Only provider-observed usage is accounting data; a model's own claim is not.
            if event.provenance == "provider_observed":
                current = usage_from_dict(json.loads(connection.execute(
                    'SELECT "usage_json" FROM "observations" WHERE "lease_id" = ?', (lease_id,)).fetchone()[0]))
                merged = merge_usage(current, parse_usage(event.payload))
                connection.execute('UPDATE "observations" SET "usage_json" = ?, "usage_seq" = ? WHERE "lease_id" = ?',
                                   (canonical_json(merged.to_dict()), event.sequence, lease_id))
        else:
            model, effort = event_settings(event.payload)
            prefix = ("observed" if event.kind == "state" and event.provenance == "provider_observed"
                      else "reported")
            if model is not None:
                connection.execute(f'UPDATE "observations" SET "{prefix}_model" = ?, "{prefix}_model_seq" = ? '
                                   'WHERE "lease_id" = ?', (model, event.sequence, lease_id))
            if effort is not None:
                connection.execute(f'UPDATE "observations" SET "{prefix}_effort" = ?, "{prefix}_effort_seq" = ? '
                                   'WHERE "lease_id" = ?', (effort, event.sequence, lease_id))

    def _record_gap(self, connection, lease_id: str, gap: tuple[int, int]) -> None:
        now = self._now()
        with _write(connection):
            exists = connection.execute(
                'SELECT 1 FROM "gaps" WHERE "lease_id" = ? AND "start_seq" = ? AND "end_seq" = ? '
                'AND "resolved_at" IS NULL', (lease_id, gap[0], gap[1]),
            ).fetchone()
            if exists is None:
                connection.execute('INSERT INTO "gaps" ("lease_id", "start_seq", "end_seq", "detected_at") '
                                   "VALUES (?, ?, ?, ?)", (lease_id, gap[0], gap[1], now))

    def _mark_gap_unresolved(self, connection, lease_id: str) -> None:
        with _write(connection):
            connection.execute('UPDATE "observations" SET "stream_state" = \'gap_unresolved\', "updated_at" = ? '
                               'WHERE "lease_id" = ?', (self._now(), lease_id))

    def ingest(self, lease_id: str, source, *, limit: int = 100, max_pages: int = 100) -> IngestResult:
        """Read events after the persisted cursor; detect duplicates, conflicts and gaps."""
        check_id(lease_id, "lease_id")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ObserverError("INVALID_LIMIT")
        if type(max_pages) is not int or not 1 <= max_pages <= 10000:
            raise ObserverError("INVALID_MAX_PAGES")
        read = getattr(source, "read", None)
        if not callable(read):
            raise TypeError("An event source with read() is required.")
        applied = duplicates = gaps = 0
        disconnected = False
        with self._session() as connection:
            if self._row(connection, lease_id)["stream_state"] == "conflict":
                raise EventConflict("STREAM_IN_CONFLICT")
            for _ in range(max_pages):
                cursor = self._row(connection, lease_id)["cursor"]
                batch = self._read(read, cursor, limit)
                if batch is None:
                    disconnected = True
                    break
                if not batch:
                    break
                step_applied, step_duplicates, gap = self._process(connection, lease_id, cursor, batch)
                applied += step_applied
                duplicates += step_duplicates
                if gap is None:
                    if step_applied == 0:
                        break
                    continue
                gaps += 1
                self._record_gap(connection, lease_id, gap)
                cursor = self._row(connection, lease_id)["cursor"]
                retry = self._read(read, cursor, limit)
                if retry is None:
                    disconnected = True
                    self._mark_gap_unresolved(connection, lease_id)
                    break
                step_applied, step_duplicates, gap = self._process(connection, lease_id, cursor, retry)
                applied += step_applied
                duplicates += step_duplicates
                if gap is not None:
                    self._record_gap(connection, lease_id, gap)
                    self._mark_gap_unresolved(connection, lease_id)
                    break
            row = self._row(connection, lease_id)
            return IngestResult(applied, duplicates, gaps, 0, row["cursor"], row["stream_state"], disconnected)

    # ------------------------------------------------------------ job state

    def poll(self, lease_id: str, port) -> ObservationView:
        """Record the provider job state; settle once a terminal state is observed."""
        check_id(lease_id, "lease_id")
        observe = getattr(port, "observe", None)
        if not callable(observe):
            raise TypeError("A job port with observe() is required.")
        with self._session() as connection:
            row = self._row(connection, lease_id)
            if JobState(row["job_state"]) not in TERMINAL:
                try:
                    request = _request_from(json.loads(row["request_json"]))
                    receipt = _receipt_from(json.loads(row["receipt_json"]))
                except (KeyError, TypeError, ValueError):
                    raise ObserverError("INVALID_DATABASE") from None
                try:
                    observed = observe(request, receipt)
                except Exception:
                    raise ObserverError("OBSERVE_FAILED") from None
                try:
                    observed = require_job(observed, request)
                except AdapterFailure:
                    raise ObserverError("INVALID_RECEIPT") from None
                if observed.provider_job_id != row["provider_job_id"]:
                    raise ObserverError("INVALID_RECEIPT")
                now = self._now()
                with _write(connection):
                    connection.execute(
                        'UPDATE "observations" SET "job_state" = ?, "receipt_json" = ?, "job_state_at" = ?, '
                        '"terminal_at" = ?, "updated_at" = ? WHERE "lease_id" = ?',
                        (observed.state.value, canonical_json(_receipt_document(observed)), now,
                         now if observed.state in TERMINAL else None, now, lease_id),
                    )
                row = self._row(connection, lease_id)
            if JobState(row["job_state"]) in TERMINAL and row["settled_usage_json"] != row["usage_json"]:
                usage = usage_from_dict(json.loads(row["usage_json"]))
                try:
                    result = self._budget.settle(lease_id, usage)
                except Exception:
                    raise ObserverError("SETTLEMENT_FAILED") from None
                kind = getattr(result, "kind", None)
                if type(kind) is not str or kind not in _SETTLEMENT_KINDS:
                    kind = "unreported"
                now = self._now()
                with _write(connection):
                    connection.execute(
                        'INSERT INTO "settlements" ("lease_id", "usage_json", "budget_kind", "submitted_at") '
                        "VALUES (?, ?, ?, ?)", (lease_id, row["usage_json"], kind, now),
                    )
                    connection.execute('UPDATE "observations" SET "settled_usage_json" = ?, "updated_at" = ? '
                                       'WHERE "lease_id" = ?', (row["usage_json"], now, lease_id))
            return self._view(connection, lease_id)

    # ------------------------------------------------------------ views

    def snapshot(self, lease_id: str) -> ObservationView:
        check_id(lease_id, "lease_id")
        with self._session() as connection:
            return self._view(connection, lease_id)

    def _view(self, connection, lease_id: str) -> ObservationView:
        row = self._row(connection, lease_id)
        try:
            usage = usage_from_dict(json.loads(row["usage_json"]))
            state = JobState(row["job_state"])
            gaps = tuple(GapRecord(item[0], item[1], item[2] is not None) for item in connection.execute(
                'SELECT "start_seq", "end_seq", "resolved_at" FROM "gaps" WHERE "lease_id" = ? ORDER BY "id"',
                (lease_id,)))
            settlements = tuple(
                SettlementSubmission(item[0], usage_from_dict(json.loads(item[1])), item[2])
                for item in connection.execute(
                    'SELECT "budget_kind", "usage_json", "submitted_at" FROM "settlements" '
                    'WHERE "lease_id" = ? ORDER BY "id"', (lease_id,)))
        except (BudgetError, KeyError, TypeError, ValueError):
            raise ObserverError("INVALID_DATABASE") from None
        conflicts = connection.execute('SELECT COUNT(*) FROM "conflicts" WHERE "lease_id" = ?',
                                       (lease_id,)).fetchone()[0]
        return ObservationView(
            lease_id=lease_id,
            vm_attempt_id=row["vm_attempt_id"],
            provider_job_id=row["provider_job_id"],
            requested=RequestedSettings(row["requested_model"], row["requested_effort"]),
            observed=ObservedSettings(
                _setting(row["observed_model"], row["observed_model_seq"], "provider_observed"),
                _setting(row["observed_effort"], row["observed_effort_seq"], "provider_observed"),
            ),
            model_reported=ObservedSettings(
                _setting(row["reported_model"], row["reported_model_seq"], "model_reported"),
                _setting(row["reported_effort"], row["reported_effort_seq"], "model_reported"),
            ),
            job_state=state,
            terminal=state in TERMINAL,
            usage=usage,
            cursor=row["cursor"],
            stream_state=row["stream_state"],
            gaps=gaps,
            conflicts=conflicts,
            output_events=row["output_events"],
            output_bytes=row["output_bytes"],
            tool_calls=row["tool_calls"],
            settlements=settlements,
        )


__all__ = ["WorkerObserver", "APPLICATION_ID", "SCHEMA_VERSION"]
