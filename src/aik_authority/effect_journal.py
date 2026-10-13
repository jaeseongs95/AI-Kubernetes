"""AIK-owned tool-call effect journal and VM external-effect submission adapter.

The VM design (vibemarshal 32bb0f9 code and design documents) separates two
kinds of effect records. AIK follows that split here.

* Tool-call intent, receipt and lookup. VM has no tool-call-level intent API
  and no ``read_effect`` query. VM validates local tool effects with diff/hash
  evidence and governance-gate snapshots. ``AikEffectJournal`` implements
  ``VmEffectAuthorityPort`` as an AIK-owned durable journal. Its states are
  AIK observations. They are never VM authority, VM evidence, or a completion
  verdict.
* External effects. They are Task-level typed ``EffectIdentity`` records that
  VM accepts only through ``EngineService.record_effect_receipt``.
  ``ExternalEffectSubmitter`` forwards such a receipt to an injected service.
  It never confirms the effect: VM confirms it only after a later target
  re-observation (``ExternalValidationObservation``).

Errors carry fixed codes and field names only, never supplied values or raw
upstream messages. Installed VM, Windows and real service calls are NOT_RUN.
"""

from __future__ import annotations

from contextlib import closing
import copy
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
from types import MappingProxyType
from typing import Any, Callable, Mapping

from aik_runtime.errors import InvalidDatabase, InvalidRecord, StorageBusy, StorageError
from aik_runtime.models import MAX_INTEGER, identifier
from aik_runtime.paths import database_lock, validate_database_path
from aik_vm_bridge import EffectOutcome
from aik_vm_bridge.binding import DIGEST_PATTERN


_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")

# ----------------------------------------------------------------- journal

JOURNAL_APPLICATION_ID = 0x41494B45  # "AIKE"; not the runtime, planning, VM or AGS database.
JOURNAL_SCHEMA_VERSION = 1
# Same values as gateway.RECEIPT_STATUSES; kept local to avoid a module cycle.
JOURNAL_RECEIPT_STATUSES = ("succeeded", "failed", "cancelled", "unknown")
INTENT_RECORDED = "intent_recorded"
JOURNAL_STATES = (INTENT_RECORDED, *JOURNAL_RECEIPT_STATUSES)
INTENT_KEYS = (
    "effect_id", "tool_call_id", "request_digest", "effect_kind", "tool_id", "vm_project_id",
    "vm_task_id", "vm_attempt_id", "vm_authorization_id", "authorization_revision", "lease_id",
    "lease_fencing_token", "binding_digest",
)
RECEIPT_KEYS = (
    "effect_id", "tool_call_id", "request_digest", "vm_attempt_id", "vm_authorization_id",
    "authorization_revision", "lease_id", "lease_fencing_token", "reported_status", "receipt_ref",
)
_RECEIPT_MATCH_KEYS = ("tool_call_id", "request_digest", "vm_attempt_id", "vm_authorization_id",
                       "authorization_revision", "lease_id", "lease_fencing_token")
_INTENT_ID_KEYS = ("effect_id", "tool_call_id", "tool_id", "vm_project_id", "vm_task_id", "vm_attempt_id",
                   "vm_authorization_id", "lease_id")
_RECORDED_EFFECT_KINDS = ("idempotent", "non_idempotent")

# Codes raised by the journal. Only JOURNAL_OUTCOME_UNKNOWN contains the
# UNKNOWN token, so the gateway reports every other code as VM_REJECTED
# (a known refusal: nothing was written).
JOURNAL_ERROR_CODES = frozenset({
    "INTENT_INVALID", "RECEIPT_INVALID", "INVALID_EFFECT_ID", "BINDING_NOT_CONFIRMED", "BINDING_MISMATCH",
    "BINDING_READ_FAILED", "ATTEMPT_NOT_RUNNING", "ATTEMPT_MISMATCH", "ATTEMPT_READ_FAILED",
    "INTENT_CONFLICT", "INTENT_ALREADY_RECORDED", "INTENT_NOT_RECORDED", "RECEIPT_MISMATCH",
    "RECEIPT_CONFLICT", "CLOCK_UNAVAILABLE", "JOURNAL_BUSY", "JOURNAL_WRITE_FAILED",
    "JOURNAL_UNAVAILABLE", "JOURNAL_RECORD_INVALID", "JOURNAL_OUTCOME_UNKNOWN",
})


class EffectJournalError(Exception):
    """A journal refusal with a fixed ``code`` (read by the gateway as a VM-style code)."""

    def __init__(self, code: str, *, subject: str | None = None):
        self.code = code
        self.subject = subject
        text = code if not subject else f"{code} subject={subject}"
        super().__init__(text)


def _is_id(value: object) -> bool:
    try:
        identifier(value)
    except InvalidRecord:
        return False
    return True


def _is_positive(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_INTEGER


def _is_digest(value: object) -> bool:
    return type(value) is str and DIGEST_PATTERN.fullmatch(value) is not None


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_intent(intent: object, code: str = "INTENT_INVALID") -> dict:
    if not isinstance(intent, Mapping):
        raise EffectJournalError(code, subject="intent")
    try:
        keys = set(intent.keys())
        record = {key: intent[key] for key in INTENT_KEYS if key in keys}
    except Exception:
        record, keys = None, set()
    if record is None or keys != set(INTENT_KEYS):
        raise EffectJournalError(code, subject="intent")
    for key in _INTENT_ID_KEYS:
        if not _is_id(record[key]):
            raise EffectJournalError(code, subject=key)
    for key in ("request_digest", "binding_digest"):
        if not _is_digest(record[key]):
            raise EffectJournalError(code, subject=key)
    if type(record["effect_kind"]) is not str or record["effect_kind"] not in _RECORDED_EFFECT_KINDS:
        raise EffectJournalError(code, subject="effect_kind")
    for key in ("authorization_revision", "lease_fencing_token"):
        if not _is_positive(record[key]):
            raise EffectJournalError(code, subject=key)
    return record


def _validate_receipt(receipt: object) -> dict:
    code = "RECEIPT_INVALID"
    if not isinstance(receipt, Mapping):
        raise EffectJournalError(code, subject="receipt")
    try:
        keys = set(receipt.keys())
        record = {key: receipt[key] for key in RECEIPT_KEYS if key in keys}
    except Exception:
        record, keys = None, set()
    if record is None or keys != set(RECEIPT_KEYS):
        raise EffectJournalError(code, subject="receipt")
    for key in ("effect_id", "tool_call_id", "vm_attempt_id", "vm_authorization_id", "lease_id"):
        if not _is_id(record[key]):
            raise EffectJournalError(code, subject=key)
    if not _is_digest(record["request_digest"]):
        raise EffectJournalError(code, subject="request_digest")
    for key in ("authorization_revision", "lease_fencing_token"):
        if not _is_positive(record[key]):
            raise EffectJournalError(code, subject=key)
    status = record["reported_status"]
    if type(status) is not str or status not in JOURNAL_RECEIPT_STATUSES:
        raise EffectJournalError(code, subject="reported_status")
    if record["receipt_ref"] is not None and not _is_id(record["receipt_ref"]):
        raise EffectJournalError(code, subject="receipt_ref")
    return record


def _id_column(name: str, *, primary: bool = False) -> str:
    key = " PRIMARY KEY" if primary else ""
    return f'"{name}" TEXT{key} NOT NULL CHECK (typeof("{name}") = \'text\' AND length("{name}") BETWEEN 1 AND 128)'


def _text_column(name: str) -> str:
    return f'"{name}" TEXT NOT NULL CHECK (typeof("{name}") = \'text\' AND length("{name}") > 0)'


_STATE_LIST = ", ".join(f"'{state}'" for state in JOURNAL_RECEIPT_STATUSES)
_SCHEMA_SQL = (
    'CREATE TABLE "effect_intents" (\n    ' + ",\n    ".join([
        _id_column("effect_id", primary=True), _id_column("vm_attempt_id"),
        _text_column("intent_json"), _text_column("recorded_at"),
    ]) + "\n)",
    'CREATE TABLE "effect_receipts" (\n    ' + ",\n    ".join([
        _id_column("effect_id", primary=True),
        f'"state" TEXT NOT NULL CHECK ("state" IN ({_STATE_LIST}))',
        _text_column("receipt_json"), _text_column("recorded_at"),
        'FOREIGN KEY ("effect_id") REFERENCES "effect_intents" ("effect_id") ON DELETE RESTRICT ON UPDATE RESTRICT',
    ]) + "\n)",
    'CREATE INDEX "effect_intents_attempt" ON "effect_intents" ("vm_attempt_id")',
)


def _create_schema(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_SQL:
        connection.execute(statement)
    connection.execute(f"PRAGMA application_id = {JOURNAL_APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {JOURNAL_SCHEMA_VERSION}")


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=1)
def _expected_schema() -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _create_schema(connection)
        connection.commit()
        return _schema_rows(connection)


def _validate_schema(connection: sqlite3.Connection, *, full: bool) -> None:
    if connection.execute("PRAGMA application_id").fetchone()[0] != JOURNAL_APPLICATION_ID \
            or connection.execute("PRAGMA user_version").fetchone()[0] != JOURNAL_SCHEMA_VERSION:
        raise InvalidDatabase("This is not a supported AIK effect journal.")
    if _schema_rows(connection) != _expected_schema():
        raise InvalidDatabase("The effect journal schema differs from its declared version.")
    if full:
        if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
            raise InvalidDatabase("Effect journal integrity validation failed.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise InvalidDatabase("Effect journal references are inconsistent.")


def _header_state(path: Path) -> str:
    """'empty' for a zero-byte file, 'journal' for our header; anything else is foreign."""
    with path.open("rb") as stream:
        header = stream.read(100)
    if not header:
        return "empty"
    if len(header) < 100 or header[:16] != b"SQLite format 3\x00" \
            or int.from_bytes(header[68:72], "big") != JOURNAL_APPLICATION_ID:
        raise InvalidDatabase("The effect journal path holds a foreign or damaged file.")
    return "journal"


def _companion(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


class AikEffectJournal:
    """AIK-owned durable tool-call effect journal implementing ``VmEffectAuthorityPort``.

    This is not a VM API and not VM authority. A recorded intent means only
    that AIK confirmed the persisted Attempt binding (and, when an attempt
    reader is supplied, that VM reported the Attempt as running) before
    recording. A stored receipt state is the executor's reported observation
    as journaled by AIK. It is never a VM verdict, VM evidence, or proof that
    a tool effect happened; VM validates local effects independently.

    ``binding_reader(vm_attempt_id)`` returns ``{"binding_digest": ...}`` or
    None (for example ``ExecutionBindingStore.read_execution_binding``).
    ``attempt_reader(vm_attempt_id)`` returns an ``attempt_detail`` mapping.
    """

    def __init__(self, path: Path | str, *, binding_reader: Callable[[str], Any],
                 attempt_reader: Callable[[str], Any] | None = None,
                 now: Callable[[], datetime] | None = None, timeout: float = 5.0):
        if not callable(binding_reader):
            raise TypeError("A binding reader callable is required.")
        if attempt_reader is not None and not callable(attempt_reader):
            raise TypeError("The attempt reader must be callable.")
        if now is not None and not callable(now):
            raise TypeError("The clock must be callable.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not math.isfinite(timeout) or timeout < 0:
            raise TypeError("The journal timeout must be a finite nonnegative number.")
        self._binding_reader = binding_reader
        self._attempt_reader = attempt_reader
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))
        self._timeout = float(timeout)
        self._path = validate_database_path(path, create_parent=True)
        with database_lock(self._path, self._timeout):
            self._prepare()

    def __repr__(self) -> str:
        return "AikEffectJournal()"

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------ storage
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path.as_uri() + "?mode=rw", uri=True, timeout=self._timeout,
                                     isolation_level=None)
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidDatabase("The effect journal requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _prepare(self) -> None:
        try:
            path = validate_database_path(self._path)
            for suffix in ("-wal", "-shm"):
                candidate = _companion(path, suffix)
                validate_database_path(candidate)
                if candidate.exists():
                    raise InvalidDatabase("The effect journal requires DELETE mode without WAL companions.")
            rollback = _companion(path, "-journal")
            validate_database_path(rollback)
            created = False
            if not path.exists():
                if rollback.exists():
                    raise InvalidDatabase("An orphan rollback journal cannot initialize the effect journal.")
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                os.close(os.open(path, flags, 0o600))
                created = True
                state = "empty"
            else:
                # Inspect the header before SQLite may recover a hot journal,
                # so a foreign file is never modified.
                state = _header_state(path)
                if state == "empty" and rollback.exists():
                    raise InvalidDatabase("An empty journal file with a rollback journal is not recoverable.")
            try:
                with closing(self._connect()) as connection:
                    if state == "empty":
                        if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                            raise InvalidDatabase("The effect journal changed during initialization.")
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            _create_schema(connection)
                            connection.commit()
                        except BaseException:
                            connection.rollback()
                            raise
                    _validate_schema(connection, full=True)
            except BaseException:
                if created:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                raise
            os.chmod(path, 0o600)
        except StorageError:
            raise
        except sqlite3.Error:
            raise InvalidDatabase("The effect journal could not be validated.") from None
        except OSError:
            raise StorageError("The effect journal files could not be processed safely.") from None

    def _execute(self, operation, *, write: bool):
        code = None
        result = None
        committing = False
        try:
            with database_lock(self._path, self._timeout):
                with closing(self._connect()) as connection:
                    _validate_schema(connection, full=False)
                    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    try:
                        result = operation(connection)
                        committing = write
                        connection.commit()
                        committing = False
                    except BaseException:
                        if connection.in_transaction:
                            try:
                                connection.rollback()
                            except sqlite3.Error:
                                pass
                        raise
        except EffectJournalError:
            raise
        except StorageBusy:
            code = "JOURNAL_BUSY"
        except (sqlite3.Error, StorageError, OSError, ValueError):
            if committing:
                code = "JOURNAL_OUTCOME_UNKNOWN"
            else:
                code = "JOURNAL_WRITE_FAILED" if write else "JOURNAL_UNAVAILABLE"
        if code is not None:
            # Raised outside the handler: no storage exception is retained.
            raise EffectJournalError(code) from None
        return result

    def _stamp(self) -> str:
        value = None
        try:
            value = self._now()
        except Exception:
            value = None
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise EffectJournalError("CLOCK_UNAVAILABLE")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    # ---------------------------------------------------- pre-record checks
    def _confirm_binding(self, record: dict) -> None:
        failed, value, digest, attempt = False, None, None, None
        try:
            value = self._binding_reader(record["vm_attempt_id"])
            if isinstance(value, Mapping):
                digest = value.get("binding_digest")
                attempt = value.get("vm_attempt_id")
        except Exception:
            failed = True
        if failed:
            raise EffectJournalError("BINDING_READ_FAILED", subject="binding_reader")
        if value is None:
            raise EffectJournalError("BINDING_NOT_CONFIRMED", subject="binding_digest")
        if not isinstance(value, Mapping) or not _is_digest(digest):
            raise EffectJournalError("BINDING_READ_FAILED", subject="binding_reader")
        if attempt is not None and attempt != record["vm_attempt_id"]:
            raise EffectJournalError("BINDING_MISMATCH", subject="vm_attempt_id")
        if digest != record["binding_digest"]:
            raise EffectJournalError("BINDING_MISMATCH", subject="binding_digest")

    def _confirm_attempt(self, record: dict) -> None:
        if self._attempt_reader is None:
            return
        failed, detail = False, None
        status = attempt_id = task_id = project_id = None
        try:
            detail = self._attempt_reader(record["vm_attempt_id"])
            if isinstance(detail, Mapping):
                project_id = detail.get("project_id")
                attempt = detail.get("attempt")
                if isinstance(attempt, Mapping):
                    status = attempt.get("status")
                    attempt_id = attempt.get("attempt_id")
                    task_id = attempt.get("task_id")
                else:
                    status = detail.get("attempt_status")
                    attempt_id = detail.get("attempt_id")
                    task_id = detail.get("task_id")
        except Exception:
            failed = True
        if failed or not isinstance(detail, Mapping) or type(status) is not str:
            raise EffectJournalError("ATTEMPT_READ_FAILED", subject="attempt_reader")
        if attempt_id != record["vm_attempt_id"]:
            raise EffectJournalError("ATTEMPT_MISMATCH", subject="vm_attempt_id")
        if task_id != record["vm_task_id"]:
            raise EffectJournalError("ATTEMPT_MISMATCH", subject="vm_task_id")
        if project_id is not None and project_id != record["vm_project_id"]:
            raise EffectJournalError("ATTEMPT_MISMATCH", subject="vm_project_id")
        if status != "running":
            raise EffectJournalError("ATTEMPT_NOT_RUNNING", subject="attempt_status")

    # ------------------------------------------------- VmEffectAuthorityPort
    def record_intent(self, intent: Mapping[str, Any]) -> Mapping[str, Any]:
        """Record a tool-call intent after confirming the persisted Attempt binding.

        Re-recording the identical ``idempotent`` intent is idempotent. An
        identical ``non_idempotent`` intent is refused with
        ``INTENT_ALREADY_RECORDED``, so a restarted gateway cannot obtain a
        second permission for the same effect. Different content for an
        existing effect ID is ``INTENT_CONFLICT``.
        """
        record = _validate_intent(intent)
        self._confirm_binding(record)
        self._confirm_attempt(record)
        text = _canonical(record)
        stamp = self._stamp()

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute('SELECT "intent_json" FROM "effect_intents" WHERE "effect_id" = ?',
                                     (record["effect_id"],)).fetchone()
            if row is not None:
                if row[0] != text:
                    raise EffectJournalError("INTENT_CONFLICT", subject="effect_id")
                if record["effect_kind"] == "non_idempotent":
                    raise EffectJournalError("INTENT_ALREADY_RECORDED", subject="effect_id")
                return
            connection.execute(
                'INSERT INTO "effect_intents" ("effect_id", "vm_attempt_id", "intent_json", "recorded_at") '
                "VALUES (?, ?, ?, ?)",
                (record["effect_id"], record["vm_attempt_id"], text, stamp),
            )

        self._execute(operation, write=True)
        return {"effect_id": record["effect_id"], "request_digest": record["request_digest"],
                "state": INTENT_RECORDED}

    def record_receipt(self, receipt: Mapping[str, Any]) -> Mapping[str, Any]:
        """Journal an executor's reported status for a recorded intent.

        The returned ``state`` is the journaled AIK observation, not a VM
        verdict. A stored receipt is never overwritten: an identical receipt is
        idempotent, any other receipt (including after ``unknown``) is
        ``RECEIPT_CONFLICT``.
        """
        record = _validate_receipt(receipt)
        text = _canonical(record)
        stamp = self._stamp()

        def operation(connection: sqlite3.Connection) -> str:
            row = connection.execute('SELECT "intent_json" FROM "effect_intents" WHERE "effect_id" = ?',
                                     (record["effect_id"],)).fetchone()
            if row is None:
                raise EffectJournalError("INTENT_NOT_RECORDED", subject="effect_id")
            try:
                stored = json.loads(row[0])
            except ValueError:
                stored = None
            stored = _validate_intent(stored, "JOURNAL_RECORD_INVALID")
            for key in _RECEIPT_MATCH_KEYS:
                if stored[key] != record[key]:
                    raise EffectJournalError("RECEIPT_MISMATCH", subject=key)
            existing = connection.execute(
                'SELECT "state", "receipt_json" FROM "effect_receipts" WHERE "effect_id" = ?',
                (record["effect_id"],),
            ).fetchone()
            if existing is not None:
                if existing[1] != text:
                    raise EffectJournalError("RECEIPT_CONFLICT", subject="effect_id")
                return existing[0]
            connection.execute(
                'INSERT INTO "effect_receipts" ("effect_id", "state", "receipt_json", "recorded_at") '
                "VALUES (?, ?, ?, ?)",
                (record["effect_id"], record["reported_status"], text, stamp),
            )
            return record["reported_status"]

        state = self._execute(operation, write=True)
        return {"effect_id": record["effect_id"], "state": state}

    def read_effect(self, effect_id: str) -> Mapping[str, Any] | None:
        """``{"effect_id", "state"}`` from the journal, or None when no intent exists."""
        if not _is_id(effect_id):
            raise EffectJournalError("INVALID_EFFECT_ID", subject="effect_id")

        def operation(connection: sqlite3.Connection):
            return connection.execute(
                'SELECT i."effect_id", r."state" FROM "effect_intents" AS i '
                'LEFT JOIN "effect_receipts" AS r ON r."effect_id" = i."effect_id" WHERE i."effect_id" = ?',
                (effect_id,),
            ).fetchone()

        row = self._execute(operation, write=False)
        if row is None:
            return None
        state = INTENT_RECORDED if row[1] is None else row[1]
        if state not in JOURNAL_STATES:
            raise EffectJournalError("JOURNAL_RECORD_INVALID", subject="state")
        return {"effect_id": effect_id, "state": state}


# ------------------------------------------------- external effect adapter

CHECKPOINT_POLICIES = ("none", "before_irreversible", "always")
TARGET_REOBSERVATION_REQUIRED = "target_reobservation_required"
_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{16,256}\Z")
_SERVICE_KEYWORDS = (
    "task_id", "attempt_id", "provider_call_id", "runtime_intent_id", "runtime_receipt_id", "thread_id",
    "turn_id", "effect_identity", "provider_operation_id", "response_digest", "target_observation_digest",
)


class ExternalEffectError(Exception):
    """External-effect submission failure; ``vm_code`` keeps VM's original code unchanged."""

    def __init__(self, code: str, outcome=EffectOutcome.KNOWN_FAILURE, *, vm_code: str | None = None,
                 subject: str | None = None):
        self.code = code
        self.outcome = EffectOutcome(outcome)
        self.vm_code = vm_code
        self.subject = subject
        text = code
        if vm_code:
            text += f" vm_code={vm_code}"
        if subject:
            text += f" subject={subject}"
        super().__init__(text)


def _bounded_text(value: object, field: str, limit: int = 256) -> None:
    if type(value) is not str or not value or value != value.strip() or len(value) > limit \
            or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ExternalEffectError("INVALID_REQUEST", subject=field)


def _request_id(value: object, field: str) -> None:
    if not _is_id(value):
        raise ExternalEffectError("INVALID_REQUEST", subject=field)


@dataclass(frozen=True, slots=True)
class EffectIdentity:
    """AIK view of the VM Task-level typed ``EffectIdentity`` (no secret field)."""

    provider: str
    system: str
    target: str
    account: str
    operation: str
    scope: str
    idempotency_key: str
    checkpoint_policy: str

    def __post_init__(self) -> None:
        for name in ("provider", "system", "account", "operation", "scope"):
            _bounded_text(getattr(self, name), name)
        _bounded_text(self.target, "target", 2048)
        if type(self.idempotency_key) is not str or _IDEMPOTENCY_KEY.fullmatch(self.idempotency_key) is None:
            raise ExternalEffectError("INVALID_REQUEST", subject="idempotency_key")
        if type(self.checkpoint_policy) is not str or self.checkpoint_policy not in CHECKPOINT_POLICIES:
            raise ExternalEffectError("INVALID_REQUEST", subject="checkpoint_policy")

    def to_mapping(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(EffectIdentity)}


def _rebuild_identity(value: object) -> EffectIdentity:
    if type(value) is not EffectIdentity:
        raise ExternalEffectError("INVALID_REQUEST", subject="identity")
    return EffectIdentity(**value.to_mapping())


@dataclass(frozen=True, slots=True)
class ExternalEffectReceipt:
    """Arguments of VM ``EngineService.record_effect_receipt`` for one external effect."""

    task_id: str
    attempt_id: str
    provider_call_id: str
    runtime_intent_id: str
    runtime_receipt_id: str
    thread_id: str
    turn_id: str
    identity: EffectIdentity
    provider_operation_id: str
    response_digest: str
    target_observation_digest: str | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "attempt_id", "provider_call_id", "runtime_intent_id", "runtime_receipt_id",
                     "thread_id", "turn_id"):
            _request_id(getattr(self, name), name)
        _rebuild_identity(self.identity)
        _bounded_text(self.provider_operation_id, "provider_operation_id", 512)
        if not _is_digest(self.response_digest):
            raise ExternalEffectError("INVALID_REQUEST", subject="response_digest")
        if self.target_observation_digest is not None and not _is_digest(self.target_observation_digest):
            raise ExternalEffectError("INVALID_REQUEST", subject="target_observation_digest")

    def to_mapping(self) -> dict:
        values = {item.name: getattr(self, item.name) for item in fields(ExternalEffectReceipt)}
        values["identity"] = self.identity.to_mapping()
        return values

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(_canonical(self.to_mapping()).encode("ascii")).hexdigest()


def _rebuild_receipt(value: object) -> ExternalEffectReceipt:
    if type(value) is not ExternalEffectReceipt:
        raise ExternalEffectError("INVALID_REQUEST", subject="receipt")
    values = {item.name: getattr(value, item.name) for item in fields(ExternalEffectReceipt)}
    values["identity"] = _rebuild_identity(values["identity"])
    return ExternalEffectReceipt(**values)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ExternalEffectSubmission:
    """VM accepted the receipt record. The effect is not confirmed by this.

    ``state`` is always ``target_reobservation_required``: VM confirms an
    external effect only after a target re-observation recorded after the
    receipt. Provider terminal output never completes it.
    """

    task_id: str
    attempt_id: str
    provider: str
    system: str
    provider_operation_id: str
    receipt_digest: str
    vm_response: Mapping[str, Any]
    state: str = TARGET_REOBSERVATION_REQUIRED

    @property
    def effect_confirmed(self) -> bool:
        return False


class ExternalEffectSubmitter:
    """Submit Task-level external-effect receipts to an injected VM service.

    ``service`` must expose ``record_effect_receipt(**keywords)`` with the VM
    keyword names. ``approved_identities`` is either a mapping
    ``task_id -> identities`` (preferred) or an iterable of identities for a
    submitter dedicated to one Task. ``identity_factory`` may convert the
    identity mapping into the VM's own type. Local replay guards are in
    process memory only; VM remains the authority that rejects replays.
    """

    def __init__(self, service: object, *, approved_identities,
                 identity_factory: Callable[[dict], Any] | None = None):
        if not callable(getattr(service, "record_effect_receipt", None)):
            raise TypeError("A service with record_effect_receipt is required.")
        if identity_factory is not None and not callable(identity_factory):
            raise TypeError("The identity factory must be callable.")
        self._service = service
        self._identity_factory = identity_factory
        self._per_task = isinstance(approved_identities, Mapping)
        try:
            if self._per_task:
                approved = {}
                for task_id, identities in approved_identities.items():
                    _request_id(task_id, "approved_identities")
                    approved[task_id] = frozenset(_rebuild_identity(item) for item in identities)
            else:
                approved = {None: frozenset(_rebuild_identity(item) for item in approved_identities)}
        except ExternalEffectError:
            raise
        except Exception:
            raise ExternalEffectError("INVALID_REQUEST", subject="approved_identities") from None
        self._approved = approved
        self._lock = threading.Lock()
        self._operations: dict[tuple, tuple[str, str]] = {}
        self._inflight: set[tuple] = set()
        self._unknown: set[tuple] = set()

    def __repr__(self) -> str:
        return "ExternalEffectSubmitter()"

    @property
    def unresolved_operations(self) -> frozenset:
        """(provider, system, provider_operation_id) keys whose submission outcome is unknown."""
        with self._lock:
            return frozenset(self._unknown)

    def _allowed(self, task_id: str) -> frozenset:
        if self._per_task:
            return self._approved.get(task_id, frozenset())
        return self._approved[None]

    def submit(self, receipt: ExternalEffectReceipt) -> ExternalEffectSubmission:
        receipt = _rebuild_receipt(receipt)
        identity = receipt.identity
        if identity not in self._allowed(receipt.task_id):
            raise ExternalEffectError("IDENTITY_NOT_APPROVED", subject="identity")
        key = (identity.provider, identity.system, receipt.provider_operation_id)
        content = receipt.digest()
        with self._lock:
            existing = self._operations.get(key)
            if existing is not None:
                if existing[0] != receipt.attempt_id:
                    raise ExternalEffectError("OPERATION_REPLAY_CONFLICT", subject="provider_operation_id")
                if existing[1] != content:
                    raise ExternalEffectError("RECEIPT_CONFLICT", subject="provider_operation_id")
            if key in self._inflight:
                raise ExternalEffectError("SUBMISSION_IN_FLIGHT", subject="provider_operation_id")
            # The claim is kept after any outcome so another Attempt can never reuse it.
            self._operations[key] = (receipt.attempt_id, content)
            self._inflight.add(key)
        try:
            response = self._call(receipt, key)
        finally:
            with self._lock:
                self._inflight.discard(key)
        with self._lock:
            self._unknown.discard(key)
        return ExternalEffectSubmission(receipt.task_id, receipt.attempt_id, identity.provider, identity.system,
                                        receipt.provider_operation_id, content, _freeze(response))

    def _fail_unknown(self, key: tuple, error: ExternalEffectError) -> ExternalEffectError:
        if error.outcome is EffectOutcome.UNKNOWN:
            with self._lock:
                self._unknown.add(key)
        return error

    def _call(self, receipt: ExternalEffectReceipt, key: tuple) -> dict:
        identity_value = receipt.identity.to_mapping()
        failure = None
        effect_identity = identity_value
        if self._identity_factory is not None:
            try:
                effect_identity = self._identity_factory(dict(identity_value))
            except Exception:
                failure = ExternalEffectError("INVALID_REQUEST", subject="effect_identity")
        if failure is not None:
            raise failure from None
        keywords = {
            "task_id": receipt.task_id, "attempt_id": receipt.attempt_id,
            "provider_call_id": receipt.provider_call_id, "runtime_intent_id": receipt.runtime_intent_id,
            "runtime_receipt_id": receipt.runtime_receipt_id, "thread_id": receipt.thread_id,
            "turn_id": receipt.turn_id, "effect_identity": effect_identity,
            "provider_operation_id": receipt.provider_operation_id, "response_digest": receipt.response_digest,
            "target_observation_digest": receipt.target_observation_digest,
        }
        response = None
        try:
            response = self._service.record_effect_receipt(**keywords)
        except Exception as error:
            code = None
            try:
                candidate = getattr(error, "code", None)
                if type(candidate) is str and _CODE.fullmatch(candidate):
                    code = candidate
            except Exception:
                code = None
            if code is None:
                failure = ExternalEffectError("OUTCOME_UNKNOWN", EffectOutcome.UNKNOWN,
                                              subject="record_effect_receipt")
            else:
                outcome = EffectOutcome.UNKNOWN if "UNKNOWN" in code.split("_") else EffectOutcome.KNOWN_FAILURE
                failure = ExternalEffectError("VM_REJECTED", outcome, vm_code=code, subject="record_effect_receipt")
        if failure is not None:
            # Raised outside the handler: the VM exception and message are not retained.
            raise self._fail_unknown(key, failure) from None
        data = None
        try:
            dump = getattr(response, "model_dump", None)
            if callable(dump) and not isinstance(response, type):
                response = dump(mode="json")
            if isinstance(response, Mapping):
                data = copy.deepcopy(dict(response))
        except Exception:
            data = None
        if data is None:
            raise self._fail_unknown(key, ExternalEffectError(
                "INVALID_RESPONSE", EffectOutcome.UNKNOWN, subject="record_effect_receipt")) from None
        for name in ("task_id", "attempt_id", "provider_call_id", "provider_operation_id"):
            observed = data.get(name)
            if observed is not None and observed != keywords[name]:
                raise self._fail_unknown(key, ExternalEffectError(
                    "INVALID_RESPONSE", EffectOutcome.UNKNOWN, subject=name))
        return data


assert set(_SERVICE_KEYWORDS) == {
    "task_id", "attempt_id", "provider_call_id", "runtime_intent_id", "runtime_receipt_id", "thread_id",
    "turn_id", "effect_identity", "provider_operation_id", "response_digest", "target_observation_digest",
}
