"""Append-only SQLite journal of JEV classifications and AGENT selections.

Separate from every owner ledger (application_id AIKJ). RAW text is never
stored, only its digest and byte length. Rows are never updated or deleted.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import fields
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

from aik_runtime.paths import database_lock, validate_database_path

from .errors import JevError
from .models import (
    AgentSelectionRecord, Classification, FallbackCandidate, canonical_json, check_identifier,
    selection_flags,
)


APPLICATION_ID = 0x41494B4A  # AIKJ
SCHEMA_VERSION = 1
_MAGIC = b"SQLite format 3\x00"
_C_COLUMNS = (
    "classification_id", "request_id", "raw_sha256", "raw_bytes", "req_sha256", "req_json", "mode",
    "status", "source", "requested_model", "observed_model", "recommended_skill", "confidence",
    "probabilities_json", "input_tokens", "output_tokens", "failure_code", "fallback_json", "recorded_at",
)
_S_COLUMNS = (
    "selection_id", "classification_id", "agent_id", "selected_skill_id", "provenance",
    "matches_recommendation", "outside_candidates", "recorded_at",
)
_C_SELECT = ", ".join(f'"{name}"' for name in _C_COLUMNS)
_S_SELECT = ", ".join(f'"{name}"' for name in _S_COLUMNS)
_STATEMENTS = (
    """CREATE TABLE "classifications" (
    "classification_id" TEXT PRIMARY KEY NOT NULL,
    "request_id" TEXT NOT NULL UNIQUE,
    "raw_sha256" TEXT NOT NULL CHECK (length("raw_sha256") = 64),
    "raw_bytes" INTEGER NOT NULL CHECK ("raw_bytes" >= 1),
    "req_sha256" TEXT NOT NULL CHECK (length("req_sha256") = 64),
    "req_json" TEXT NOT NULL,
    "mode" TEXT NOT NULL CHECK ("mode" IN ('on', 'off')),
    "status" TEXT NOT NULL CHECK ("status" IN ('classified', 'off', 'unavailable', 'failed', 'rejected_response')),
    "source" TEXT NOT NULL CHECK ("source" IN ('jev', 'fallback_candidate', 'none')),
    "requested_model" TEXT,
    "observed_model" TEXT,
    "recommended_skill" TEXT,
    "confidence" REAL CHECK ("confidence" IS NULL OR ("confidence" >= 0 AND "confidence" <= 1)),
    "probabilities_json" TEXT NOT NULL,
    "input_tokens" INTEGER CHECK ("input_tokens" IS NULL OR "input_tokens" >= 0),
    "output_tokens" INTEGER CHECK ("output_tokens" IS NULL OR "output_tokens" >= 0),
    "failure_code" TEXT,
    "fallback_json" TEXT,
    "recorded_at" TEXT NOT NULL
)""",
    """CREATE TABLE "agent_selections" (
    "selection_id" TEXT PRIMARY KEY NOT NULL,
    "classification_id" TEXT NOT NULL REFERENCES "classifications" ("classification_id"),
    "agent_id" TEXT NOT NULL,
    "selected_skill_id" TEXT,
    "provenance" TEXT NOT NULL CHECK ("provenance" = 'model_reported'),
    "matches_recommendation" INTEGER CHECK ("matches_recommendation" IS NULL OR "matches_recommendation" IN (0, 1)),
    "outside_candidates" INTEGER NOT NULL CHECK ("outside_candidates" IN (0, 1)),
    "recorded_at" TEXT NOT NULL
)""",
    'CREATE INDEX "agent_selections_classification" ON "agent_selections" ("classification_id")',
    'CREATE TRIGGER "classifications_no_update" BEFORE UPDATE ON "classifications" '
    "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    'CREATE TRIGGER "classifications_no_delete" BEFORE DELETE ON "classifications" '
    "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    'CREATE TRIGGER "agent_selections_no_update" BEFORE UPDATE ON "agent_selections" '
    "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    'CREATE TRIGGER "agent_selections_no_delete" BEFORE DELETE ON "agent_selections" '
    "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
)


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


def _req_skill_ids(req_json: str, raw_sha256: str) -> tuple:
    parsed = json.loads(req_json)
    if type(parsed) is not dict or parsed.get("raw_sha256") != raw_sha256:
        raise ValueError("REQ is not bound to RAW")
    return tuple(item["skill_id"] for item in parsed["skills"])


def _decode_classification(row) -> Classification:
    try:
        values = dict(zip(_C_COLUMNS, row))
        req_json = values.pop("req_json")
        if (type(req_json) is not str
                or hashlib.sha256(req_json.encode("utf-8")).hexdigest() != values["req_sha256"]):
            raise JevError("INVALID_JOURNAL")
        candidate_ids = _req_skill_ids(req_json, values["raw_sha256"])
        probabilities = tuple(tuple(pair) for pair in json.loads(values.pop("probabilities_json")))
        fallback_json = values.pop("fallback_json")
        fallback = None
        if fallback_json is not None:
            data = json.loads(fallback_json)
            if type(data) is not dict:
                raise JevError("INVALID_JOURNAL")
            fallback = FallbackCandidate(**data)
        return Classification(
            candidate_skill_ids=candidate_ids, probabilities=probabilities, fallback=fallback,
            usage_input_tokens=values.pop("input_tokens"), usage_output_tokens=values.pop("output_tokens"),
            **values,
        )
    except (JevError, TypeError, KeyError, ValueError, AttributeError):
        raise JevError("INVALID_JOURNAL") from None


def _decode_selection(row) -> AgentSelectionRecord:
    try:
        values = dict(zip(_S_COLUMNS, row))
        matches = values["matches_recommendation"]
        if matches is not None:
            if matches not in (0, 1) or type(matches) is not int:
                raise JevError("INVALID_JOURNAL")
            values["matches_recommendation"] = bool(matches)
        outside = values["outside_candidates"]
        if type(outside) is not int or outside not in (0, 1):
            raise JevError("INVALID_JOURNAL")
        values["outside_candidates"] = bool(outside)
        return AgentSelectionRecord(**values)
    except (JevError, TypeError, KeyError):
        raise JevError("INVALID_JOURNAL") from None


def _same_input(existing: Classification, record: Classification) -> bool:
    return ((existing.raw_sha256, existing.raw_bytes, existing.req_sha256)
            == (record.raw_sha256, record.raw_bytes, record.req_sha256))


class JevJournal:
    """Append-only classification and AGENT selection records."""

    def __init__(self, path: Path | str, *, timeout: float = 5.0):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            raise JevError("INVALID_TIMEOUT")
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
            connection.execute("PRAGMA foreign_keys = ON")
            if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "delete":
                raise JevError("INVALID_JOURNAL")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _check_companions(self) -> None:
        for suffix in ("-wal", "-shm"):
            if self._path.with_name(self._path.name + suffix).exists():
                raise JevError("INVALID_JOURNAL")

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise JevError("INVALID_JOURNAL")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise JevError("INVALID_JOURNAL")
        if _schema_rows(connection) != _expected_schema():
            raise JevError("INVALID_JOURNAL")
        if tuple(row[0] for row in connection.execute("PRAGMA quick_check")) != ("ok",):
            raise JevError("INVALID_JOURNAL")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise JevError("INVALID_JOURNAL")

    def _prepare(self) -> None:
        validate_database_path(self._path)
        self._check_companions()
        try:
            if not self._path.exists() or self._path.stat().st_size == 0:
                with closing(self._connect()) as connection:
                    if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                        raise JevError("INVALID_JOURNAL")
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
                    raise JevError("INVALID_JOURNAL")
            with closing(self._connect()) as connection:
                self._validate(connection)
        except sqlite3.Error:
            raise JevError("INVALID_JOURNAL") from None
        except OSError:
            raise JevError("JOURNAL_UNAVAILABLE") from None

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
                raise JevError("JOURNAL_UNAVAILABLE") from None

    def append_classification(self, record: Classification, req_json: str) -> Classification:
        """Append once per request_id; the same RAW/REQ returns the stored record."""
        if type(record) is not Classification:
            raise JevError("INVALID_CLASSIFICATION")
        record = Classification(**{item.name: getattr(record, item.name) for item in fields(Classification)})
        if type(req_json) is not str:
            raise JevError("REQ_DIGEST_MISMATCH")
        try:
            digest = hashlib.sha256(req_json.encode("utf-8")).hexdigest()
            skill_ids = _req_skill_ids(req_json, record.raw_sha256)
        except (UnicodeEncodeError, ValueError, TypeError, KeyError):
            raise JevError("REQ_DIGEST_MISMATCH") from None
        if digest != record.req_sha256 or skill_ids != record.candidate_skill_ids:
            raise JevError("REQ_DIGEST_MISMATCH")
        values = (
            record.classification_id, record.request_id, record.raw_sha256, record.raw_bytes,
            record.req_sha256, req_json, record.mode, record.status, record.source,
            record.requested_model, record.observed_model, record.recommended_skill, record.confidence,
            canonical_json([[name, value] for name, value in record.probabilities]),
            record.usage_input_tokens, record.usage_output_tokens, record.failure_code,
            None if record.fallback is None else canonical_json(record.fallback.canonical_dict()),
            record.recorded_at,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f'SELECT {_C_SELECT} FROM "classifications" WHERE "request_id" = ?', (record.request_id,),
                ).fetchone()
                if row is not None:
                    existing = _decode_classification(row)
                    if _same_input(existing, record):
                        connection.commit()
                        return existing
                    raise JevError("REQUEST_CONFLICT")
                if connection.execute('SELECT 1 FROM "classifications" WHERE "classification_id" = ?',
                                      (record.classification_id,)).fetchone() is not None:
                    raise JevError("CLASSIFICATION_ID_CONFLICT")
                placeholders = ", ".join("?" for _ in _C_COLUMNS)
                connection.execute(f'INSERT INTO "classifications" ({_C_SELECT}) VALUES ({placeholders})', values)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return record

    def find_by_request(self, request_id: str) -> Classification | None:
        check_identifier(request_id, "INVALID_REQUEST_ID")
        with self._connection() as connection:
            row = connection.execute(
                f'SELECT {_C_SELECT} FROM "classifications" WHERE "request_id" = ?', (request_id,),
            ).fetchone()
        return None if row is None else _decode_classification(row)

    def get_classification(self, classification_id: str) -> Classification | None:
        check_identifier(classification_id, "INVALID_CLASSIFICATION")
        with self._connection() as connection:
            row = connection.execute(
                f'SELECT {_C_SELECT} FROM "classifications" WHERE "classification_id" = ?', (classification_id,),
            ).fetchone()
        return None if row is None else _decode_classification(row)

    def append_selection(self, record: AgentSelectionRecord) -> AgentSelectionRecord:
        """Append an AGENT selection; flags must match the stored classification."""
        if type(record) is not AgentSelectionRecord:
            raise JevError("INVALID_SELECTION")
        record = AgentSelectionRecord(**{item.name: getattr(record, item.name)
                                         for item in fields(AgentSelectionRecord)})
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f'SELECT {_C_SELECT} FROM "classifications" WHERE "classification_id" = ?',
                    (record.classification_id,),
                ).fetchone()
                if row is None:
                    raise JevError("UNKNOWN_CLASSIFICATION")
                matches, outside = selection_flags(_decode_classification(row), record.selected_skill_id)
                if (matches, outside) != (record.matches_recommendation, record.outside_candidates):
                    raise JevError("INVALID_SELECTION")
                if connection.execute('SELECT 1 FROM "agent_selections" WHERE "selection_id" = ?',
                                      (record.selection_id,)).fetchone() is not None:
                    raise JevError("SELECTION_ID_CONFLICT")
                values = (
                    record.selection_id, record.classification_id, record.agent_id, record.selected_skill_id,
                    record.provenance, None if matches is None else int(matches), int(outside), record.recorded_at,
                )
                placeholders = ", ".join("?" for _ in _S_COLUMNS)
                connection.execute(f'INSERT INTO "agent_selections" ({_S_SELECT}) VALUES ({placeholders})', values)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return record

    def selections(self, classification_id: str, *, limit: int = 1000) -> tuple:
        check_identifier(classification_id, "INVALID_CLASSIFICATION")
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise JevError("INVALID_LIMIT")
        with self._connection() as connection:
            rows = connection.execute(
                f'SELECT {_S_SELECT} FROM "agent_selections" WHERE "classification_id" = ? ORDER BY rowid LIMIT ?',
                (classification_id, limit),
            ).fetchall()
        return tuple(_decode_selection(row) for row in rows)
