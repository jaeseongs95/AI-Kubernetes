"""Append-only SQLite journal of independent checks, separate from every owner ledger."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import fields
from functools import lru_cache
import math
import os
from pathlib import Path
import sqlite3

from aik_runtime.paths import database_lock, validate_database_path

from .errors import ValidationError
from .models import CheckRecord, check_id


APPLICATION_ID = 0x41494B56  # AIKV
SCHEMA_VERSION = 1
_MAGIC = b"SQLite format 3\x00"
_COLUMNS = (
    "check_id", "vm_attempt_id", "command_id", "definition_digest", "passed", "oracle_ok",
    "exit_code", "output_digest", "reviewer_role", "reviewer_session_id", "worker_session_id",
    "tool_call_id", "recorded_at", "verdict",
)
_SELECT = ", ".join(f'"{name}"' for name in _COLUMNS)
_STATEMENTS = (
    'CREATE TABLE "checks" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "check_id" TEXT NOT NULL UNIQUE,\n'
    '    "vm_attempt_id" TEXT NOT NULL,\n'
    '    "command_id" TEXT NOT NULL,\n'
    '    "definition_digest" TEXT NOT NULL,\n'
    '    "passed" INTEGER NOT NULL CHECK ("passed" IN (0, 1)),\n'
    '    "oracle_ok" INTEGER NOT NULL CHECK ("oracle_ok" IN (0, 1)),\n'
    '    "exit_code" INTEGER,\n'
    '    "output_digest" TEXT NOT NULL,\n'
    '    "reviewer_role" TEXT NOT NULL CHECK ("reviewer_role" = \'reviewer\'),\n'
    '    "reviewer_session_id" TEXT NOT NULL,\n'
    '    "worker_session_id" TEXT NOT NULL,\n'
    '    "tool_call_id" TEXT NOT NULL UNIQUE,\n'
    '    "recorded_at" TEXT NOT NULL,\n'
    '    "verdict" TEXT NOT NULL CHECK ("verdict" IN (\'PASS\', \'FAIL\', \'ORACLE_CHANGED\'))\n'
    ')',
    'CREATE INDEX "checks_attempt" ON "checks" ("vm_attempt_id", "seq")',
    'CREATE TRIGGER "checks_no_update" BEFORE UPDATE ON "checks" '
    "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    'CREATE TRIGGER "checks_no_delete" BEFORE DELETE ON "checks" '
    "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
)
_OBSERVED_FIELDS = tuple(item.name for item in fields(CheckRecord)
                         if item.name not in ("check_id", "recorded_at"))


def _create(connection: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        connection.execute(statement)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=1)
def _expected_schema() -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        _create(connection)
        return _schema_rows(connection)


def _decode(row) -> CheckRecord:
    try:
        values = dict(zip(_COLUMNS, row))
        stored_verdict = values.pop("verdict")
        for name in ("passed", "oracle_ok"):
            if type(values[name]) is not int or values[name] not in (0, 1):
                raise ValidationError("INVALID_JOURNAL")
            values[name] = bool(values[name])
        record = CheckRecord(**values)
        if record.verdict != stored_verdict:
            raise ValidationError("INVALID_JOURNAL")
        return record
    except (ValidationError, TypeError, KeyError):
        raise ValidationError("INVALID_JOURNAL") from None


class CheckJournal:
    """Append-only independent check records; rows are never updated or deleted."""

    def __init__(self, path: Path | str, *, timeout: float = 5.0):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            raise ValidationError("INVALID_TIMEOUT")
        self._timeout = float(timeout)
        self._path = validate_database_path(path, create_parent=True)
        with database_lock(self._path, self._timeout):
            self._prepare()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._path), timeout=self._timeout, isolation_level=None)
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "delete":
                raise ValidationError("INVALID_JOURNAL")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _check_companions(self) -> None:
        for suffix in ("-wal", "-shm"):
            if self._path.with_name(self._path.name + suffix).exists():
                raise ValidationError("INVALID_JOURNAL")

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValidationError("INVALID_JOURNAL")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValidationError("INVALID_JOURNAL")
        if _schema_rows(connection) != _expected_schema():
            raise ValidationError("INVALID_JOURNAL")
        if tuple(row[0] for row in connection.execute("PRAGMA quick_check")) != ("ok",):
            raise ValidationError("INVALID_JOURNAL")

    def _prepare(self) -> None:
        validate_database_path(self._path)
        self._check_companions()
        try:
            if not self._path.exists() or self._path.stat().st_size == 0:
                with closing(self._connect()) as connection:
                    if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                        raise ValidationError("INVALID_JOURNAL")
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        _create(connection)
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                os.chmod(self._path, 0o600)
                return
            with self._path.open("rb") as stream:
                if stream.read(16) != _MAGIC:
                    raise ValidationError("INVALID_JOURNAL")
            with closing(self._connect()) as connection:
                self._validate(connection)
        except sqlite3.Error:
            raise ValidationError("INVALID_JOURNAL") from None
        except OSError:
            raise ValidationError("JOURNAL_UNAVAILABLE") from None

    @contextmanager
    def _connection(self):
        with database_lock(self._path, self._timeout):
            validate_database_path(self._path)
            self._check_companions()
            try:
                with closing(self._connect()) as connection:
                    self._validate(connection)
                    yield connection
            except sqlite3.Error:
                raise ValidationError("JOURNAL_UNAVAILABLE") from None

    def append(self, record: CheckRecord) -> CheckRecord:
        """Append one record. A replay of the same tool call returns the stored record."""
        if type(record) is not CheckRecord:
            raise ValidationError("INVALID_CHECK_RECORD")
        # Rebuild so object.__setattr__ forgeries are validated again.
        record = CheckRecord(**{item.name: getattr(record, item.name) for item in fields(CheckRecord)})
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f'SELECT {_SELECT} FROM "checks" WHERE "tool_call_id" = ?', (record.tool_call_id,),
                ).fetchone()
                if row is not None:
                    existing = _decode(row)
                    if all(getattr(existing, name) == getattr(record, name) for name in _OBSERVED_FIELDS):
                        connection.commit()
                        return existing
                    raise ValidationError("TOOL_CALL_CONFLICT")
                if connection.execute('SELECT 1 FROM "checks" WHERE "check_id" = ?',
                                      (record.check_id,)).fetchone() is not None:
                    raise ValidationError("CHECK_ID_CONFLICT")
                values = []
                for name in _COLUMNS:
                    value = record.verdict if name == "verdict" else getattr(record, name)
                    values.append(int(value) if type(value) is bool else value)
                placeholders = ", ".join("?" for _ in _COLUMNS)
                connection.execute(f'INSERT INTO "checks" ({_SELECT}) VALUES ({placeholders})', values)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return record

    def history(self, vm_attempt_id: str, *, limit: int = 1000) -> tuple[CheckRecord, ...]:
        """All checks of one Attempt in append order; earlier FAIL records are kept."""
        check_id(vm_attempt_id, "vm_attempt_id")
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValidationError("INVALID_LIMIT")
        with self._connection() as connection:
            rows = connection.execute(
                f'SELECT {_SELECT} FROM "checks" WHERE "vm_attempt_id" = ? ORDER BY "seq" LIMIT ?',
                (vm_attempt_id, limit),
            ).fetchall()
        return tuple(_decode(row) for row in rows)

    def get(self, check_id_value: str) -> CheckRecord | None:
        check_id(check_id_value, "check_id")
        with self._connection() as connection:
            row = connection.execute(
                f'SELECT {_SELECT} FROM "checks" WHERE "check_id" = ?', (check_id_value,),
            ).fetchone()
        return None if row is None else _decode(row)
