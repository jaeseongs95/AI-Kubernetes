"""Append-only recovery decision journal in its own SQLite file.

The file follows aik_runtime.paths rules (no development ledger, no Git path,
no symlink/hardlink aliases) and carries its own application ID. UPDATE and
DELETE are blocked by triggers. A foreign or damaged file is rejected without
being rewritten.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from types import MappingProxyType
import uuid

from aik_runtime.errors import InvalidDatabase, StorageBusy, StorageError
from aik_runtime.models import MAX_INTEGER
from aik_runtime.paths import database_lock, validate_database_path

from .models import ACTIONS, OUTCOMES, SUBJECT_KINDS, RecoveryDecision, RecoveryError


APPLICATION_ID = 0x41494B59  # AIKY; not the runtime, budget, observer or evidence database.
SCHEMA_VERSION = 1
_SQLITE_MAGIC = b"SQLite format 3\x00"
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
_SUBJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}\Z")
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_REASONS = 32
_MAX_REFS = 64
_MAX_OBSERVED = 32
_COLUMNS = ("seq", "decision_id", "subject_kind", "subject_id", "action", "outcome", "reasons",
            "observed", "evidence_refs", "decided_at", "fingerprint")

_STATEMENTS = (
    'CREATE TABLE "decisions" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "decision_id" TEXT NOT NULL UNIQUE,\n'
    '    "subject_kind" TEXT NOT NULL CHECK ("subject_kind" IN (\'lease\', \'effect\', \'scan\')),\n'
    '    "subject_id" TEXT NOT NULL,\n'
    '    "action" TEXT NOT NULL,\n'
    '    "outcome" TEXT NOT NULL CHECK ("outcome" IN (\'recorded\', \'planned\', \'applied\', \'failed\')),\n'
    '    "reasons" TEXT NOT NULL,\n'
    '    "observed" TEXT NOT NULL,\n'
    '    "evidence_refs" TEXT NOT NULL,\n'
    '    "decided_at" TEXT NOT NULL,\n'
    '    "fingerprint" TEXT NOT NULL\n'
    ')',
    'CREATE INDEX "decisions_subject" ON "decisions" ("subject_kind", "subject_id", "seq")',
    'CREATE TRIGGER "decisions_no_update" BEFORE UPDATE ON "decisions" '
    'BEGIN SELECT RAISE(ABORT, \'recovery decisions are append-only\'); END',
    'CREATE TRIGGER "decisions_no_delete" BEFORE DELETE ON "decisions" '
    'BEGIN SELECT RAISE(ABORT, \'recovery decisions are append-only\'); END',
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _content(subject_kind, subject_id, action, outcome, reasons, observed, evidence_refs) -> dict:
    if type(subject_kind) is not str or subject_kind not in SUBJECT_KINDS:
        raise RecoveryError("INVALID_SUBJECT_KIND")
    if type(subject_id) is not str or _SUBJECT.fullmatch(subject_id) is None:
        raise RecoveryError("INVALID_SUBJECT_ID")
    if type(action) is not str or action not in ACTIONS:
        raise RecoveryError("INVALID_ACTION")
    if type(outcome) is not str or outcome not in OUTCOMES:
        raise RecoveryError("INVALID_OUTCOME")
    if type(reasons) not in (tuple, list) or not 1 <= len(reasons) <= _MAX_REASONS:
        raise RecoveryError("INVALID_REASONS")
    for reason in reasons:
        if type(reason) is not str or _CODE.fullmatch(reason) is None:
            raise RecoveryError("INVALID_REASONS")
    if type(observed) is not dict or len(observed) > _MAX_OBSERVED:
        raise RecoveryError("INVALID_OBSERVED")
    for key, value in observed.items():
        if type(key) is not str or _KEY.fullmatch(key) is None:
            raise RecoveryError("INVALID_OBSERVED")
        if value is None or type(value) is bool:
            continue
        if type(value) is int and -MAX_INTEGER <= value <= MAX_INTEGER:
            continue
        if type(value) is str and _VALUE.fullmatch(value) is not None:
            continue
        raise RecoveryError("INVALID_OBSERVED")
    if type(evidence_refs) not in (tuple, list) or len(evidence_refs) > _MAX_REFS:
        raise RecoveryError("INVALID_EVIDENCE_REFS")
    for ref in evidence_refs:
        if type(ref) is not str or _VALUE.fullmatch(ref) is None:
            raise RecoveryError("INVALID_EVIDENCE_REFS")
    return {
        "subject_kind": subject_kind, "subject_id": subject_id, "action": action, "outcome": outcome,
        "reasons": list(reasons), "observed": dict(observed), "evidence_refs": list(evidence_refs),
    }


def decision_fingerprint(subject_kind, subject_id, action, outcome, reasons, observed, evidence_refs) -> str:
    """Digest of the decision content, excluding its ID and time."""
    document = _content(subject_kind, subject_id, action, outcome, reasons, observed, evidence_refs)
    return "sha256:" + hashlib.sha256(_canonical(document).encode("ascii")).hexdigest()


def _check_timestamp(value: object) -> None:
    if type(value) is not str:
        raise RecoveryError("INVALID_DECIDED_AT")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RecoveryError("INVALID_DECIDED_AT") from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise RecoveryError("INVALID_DECIDED_AT")


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=1)
def _expected_schema() -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        for statement in _STATEMENTS:
            connection.execute(statement)
        return _schema_rows(connection)


def _validate(connection: sqlite3.Connection) -> None:
    app_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if app_id != APPLICATION_ID or version != SCHEMA_VERSION:
        raise InvalidDatabase("This is not a supported AIK recovery journal.")
    if _schema_rows(connection) != _expected_schema():
        raise InvalidDatabase("The recovery journal schema differs from its declared version.")


def _busy(error: sqlite3.Error) -> bool:
    return getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


class RecoveryJournal:
    """Append-only recovery decisions; nothing is ever updated or deleted."""

    def __init__(self, path: Path | str, *, timeout: float = 5.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise RecoveryError("INVALID_TIMEOUT")
        self._timeout = float(timeout)
        self._path = validate_database_path(path, create_parent=True)
        with database_lock(self._path, self._timeout):
            self._prepare()

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------ plumbing
    def _check_files(self) -> None:
        validate_database_path(self._path)
        for suffix in ("-wal", "-shm"):
            companion = self._path.with_name(self._path.name + suffix)
            validate_database_path(companion)
            if companion.exists():
                raise InvalidDatabase("The recovery journal requires DELETE mode without WAL companions.")
        if self._path.exists() and self._path.stat().st_size > 0:
            with self._path.open("rb") as stream:
                if stream.read(16) != _SQLITE_MAGIC:
                    raise InvalidDatabase("The recovery journal is not an SQLite database.")

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self._timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "delete":
                raise InvalidDatabase("The recovery journal requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _prepare(self) -> None:
        self._check_files()
        existed = self._path.exists()
        try:
            with closing(self._connect(create=True)) as connection:
                rows = _schema_rows(connection)
                app_id = connection.execute("PRAGMA application_id").fetchone()[0]
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if not rows and app_id == 0 and version == 0:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        for statement in _STATEMENTS:
                            connection.execute(statement)
                        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                        connection.commit()
                    except BaseException:
                        if connection.in_transaction:
                            connection.rollback()
                        raise
                _validate(connection)
                if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
                    raise InvalidDatabase("Recovery journal integrity validation failed.")
        except sqlite3.Error as error:
            if _busy(error):
                raise StorageBusy("SQLite could not acquire the recovery journal lock.") from None
            raise InvalidDatabase("The recovery journal could not be validated.") from None
        if not existed:
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    def _run(self, operation):
        with database_lock(self._path, self._timeout):
            self._check_files()
            try:
                with closing(self._connect()) as connection:
                    _validate(connection)
                    return operation(connection)
            except sqlite3.Error as error:
                if _busy(error):
                    raise StorageBusy("SQLite could not acquire the recovery journal lock.") from None
                raise StorageError("The recovery journal operation could not be completed.") from None

    @staticmethod
    def _decode(row) -> RecoveryDecision:
        try:
            values = dict(zip(_COLUMNS, row))
            reasons = tuple(json.loads(values["reasons"]))
            observed = json.loads(values["observed"])
            refs = tuple(json.loads(values["evidence_refs"]))
            if type(observed) is not dict:
                raise ValueError
            return RecoveryDecision(
                values["seq"], values["decision_id"], values["subject_kind"], values["subject_id"],
                values["action"], values["outcome"], reasons, MappingProxyType(observed), refs,
                values["decided_at"], values["fingerprint"],
            )
        except (TypeError, ValueError, KeyError):
            raise InvalidDatabase("A stored recovery decision is malformed.") from None

    # ------------------------------------------------------------- public
    def append(self, *, subject_kind: str, subject_id: str, action: str, outcome: str, reasons,
               observed: dict, evidence_refs, decided_at: str) -> RecoveryDecision:
        document = _content(subject_kind, subject_id, action, outcome, reasons, observed, evidence_refs)
        _check_timestamp(decided_at)
        fingerprint = decision_fingerprint(subject_kind, subject_id, action, outcome, reasons, observed,
                                           evidence_refs)
        decision_id = "decision-" + uuid.uuid4().hex

        def write(connection):
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    'INSERT INTO "decisions" ("decision_id", "subject_kind", "subject_id", "action", "outcome", '
                    '"reasons", "observed", "evidence_refs", "decided_at", "fingerprint") '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (decision_id, subject_kind, subject_id, action, outcome, _canonical(document["reasons"]),
                     _canonical(document["observed"]), _canonical(document["evidence_refs"]), decided_at,
                     fingerprint),
                )
                seq = cursor.lastrowid
                connection.commit()
                return seq
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

        seq = self._run(write)
        return RecoveryDecision(
            seq, decision_id, subject_kind, subject_id, action, outcome, tuple(document["reasons"]),
            MappingProxyType(document["observed"]), tuple(document["evidence_refs"]), decided_at, fingerprint,
        )

    def decisions(self, subject_kind: str | None = None, subject_id: str | None = None) -> tuple:
        columns = ", ".join(f'"{name}"' for name in _COLUMNS)
        clauses, parameters = [], []
        if subject_kind is not None:
            clauses.append('"subject_kind" = ?')
            parameters.append(subject_kind)
        if subject_id is not None:
            clauses.append('"subject_id" = ?')
            parameters.append(subject_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        def read(connection):
            return tuple(self._decode(row) for row in connection.execute(
                f'SELECT {columns} FROM "decisions"{where} ORDER BY "seq"', parameters))

        return self._run(read)

    def last(self, subject_kind: str, subject_id: str) -> RecoveryDecision | None:
        columns = ", ".join(f'"{name}"' for name in _COLUMNS)

        def read(connection):
            row = connection.execute(
                f'SELECT {columns} FROM "decisions" WHERE "subject_kind" = ? AND "subject_id" = ? '
                'ORDER BY "seq" DESC LIMIT 1', (subject_kind, subject_id),
            ).fetchone()
            return None if row is None else self._decode(row)

        return self._run(read)

    def has_applied(self, subject_kind: str, subject_id: str, action: str) -> bool:
        def read(connection):
            return connection.execute(
                'SELECT 1 FROM "decisions" WHERE "subject_kind" = ? AND "subject_id" = ? AND "action" = ? '
                'AND "outcome" = \'applied\' LIMIT 1', (subject_kind, subject_id, action),
            ).fetchone() is not None

        return self._run(read)
