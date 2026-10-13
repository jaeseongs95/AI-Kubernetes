"""Append-only director continuity records in a dedicated SQLite file.

Path validation and the cooperating process lock are reused from
aik_runtime.paths, the secret/size heuristic from aik_inbox.models. Every
operation opens a fresh connection under that lock and runs in one IMMEDIATE
transaction with DELETE journal and FULL sync. Triggers reject UPDATE/DELETE.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import errno
from functools import lru_cache
import hmac
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from aik_inbox.models import fingerprint
from aik_runtime.errors import StorageBusy
from aik_runtime.models import MAX_INTEGER
from aik_runtime.paths import database_lock, default_database_path, validate_database_path

from .errors import (
    ContinuityConflict, ContinuityError, InvalidContinuityDatabase, InvalidContinuityRecord,
    RevisionRegression,
)
from .models import (
    CORRECTION_SCOPES, DIRECTIVE_KINDS,
    Appended, Checkpoint, ConfigRevision, ContextSummary, Directive, HandoffPacket,
    StoredCheckpoint, StoredConfig, StoredDirective, StoredSummary, check_id, rebuild,
    resolve_directives,
)


APPLICATION_ID = 0x41494B4E  # AIKN; distinct from runtime (AIKR), inbox (AIKI) and AIKC.
SCHEMA_VERSION = 1
_APPEND_ONLY = ("directives", "summaries", "config_revisions", "checkpoints")


def _in(values: tuple) -> str:
    return ", ".join(f"'{value}'" for value in values)


_STATEMENTS = (
    "CREATE TABLE directives (\n"
    "    seq INTEGER PRIMARY KEY,\n"
    "    directive_id TEXT NOT NULL UNIQUE,\n"
    "    project_id TEXT NOT NULL,\n"
    f"    kind TEXT NOT NULL CHECK (kind IN ({_in(DIRECTIVE_KINDS)})),\n"
    "    text TEXT NOT NULL,\n"
    "    target_id TEXT REFERENCES directives (directive_id) ON DELETE RESTRICT ON UPDATE RESTRICT,\n"
    f"    scope TEXT CHECK (scope IS NULL OR scope IN ({_in(CORRECTION_SCOPES)})),\n"
    "    fingerprint TEXT NOT NULL,\n"
    "    recorded_ms INTEGER NOT NULL CHECK (typeof(recorded_ms) = 'integer'),\n"
    "    CHECK ((target_id IS NULL) = (scope IS NULL)),\n"
    "    CHECK ((kind = 'correction') = (target_id IS NOT NULL))\n"
    ")",
    "CREATE INDEX directives_project_seq ON directives (project_id, seq)",
    "CREATE TABLE summaries (\n"
    "    seq INTEGER PRIMARY KEY,\n"
    "    summary_id TEXT NOT NULL UNIQUE,\n"
    "    project_id TEXT NOT NULL,\n"
    "    text TEXT NOT NULL,\n"
    "    covers_through_seq INTEGER NOT NULL CHECK (typeof(covers_through_seq) = 'integer' "
    "AND covers_through_seq >= 0),\n"
    "    fingerprint TEXT NOT NULL,\n"
    "    recorded_ms INTEGER NOT NULL CHECK (typeof(recorded_ms) = 'integer')\n"
    ")",
    "CREATE INDEX summaries_project_seq ON summaries (project_id, seq)",
    "CREATE TABLE config_revisions (\n"
    "    config_id TEXT NOT NULL,\n"
    "    revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 1),\n"
    "    digest TEXT NOT NULL,\n"
    "    recorded_ms INTEGER NOT NULL CHECK (typeof(recorded_ms) = 'integer'),\n"
    "    PRIMARY KEY (config_id, revision)\n"
    ")",
    "CREATE TABLE checkpoints (\n"
    "    project_id TEXT NOT NULL,\n"
    "    revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 1),\n"
    "    body TEXT NOT NULL,\n"
    "    fingerprint TEXT NOT NULL,\n"
    "    recorded_ms INTEGER NOT NULL CHECK (typeof(recorded_ms) = 'integer'),\n"
    "    PRIMARY KEY (project_id, revision)\n"
    ")",
    *(
        f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
        f"BEGIN SELECT RAISE(ABORT, 'continuity records are append-only'); END"
        for table in _APPEND_ONLY for action in ("UPDATE", "DELETE")
    ),
)

_DIRECTIVE_COLUMNS = "seq, directive_id, project_id, kind, text, target_id, scope, fingerprint, recorded_ms"
_SUMMARY_COLUMNS = "seq, summary_id, project_id, text, covers_through_seq, fingerprint, recorded_ms"
_CONFIG_COLUMNS = "config_id, revision, digest, recorded_ms"
_CHECKPOINT_COLUMNS = "project_id, revision, body, fingerprint, recorded_ms"
_DECODE_ERRORS = (InvalidContinuityRecord, TypeError, ValueError, KeyError, IndexError, AttributeError)


def default_continuity_path() -> Path:
    """Beside the runtime DB: %LOCALAPPDATA%\\AIK\\runtime\\continuity.sqlite3 on Windows."""
    return default_database_path().parent / "continuity.sqlite3"


def create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _STATEMENTS:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=1)
def _expected_schema() -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        create_schema(connection)
        return _schema_rows(connection)


def _validate_schema(connection: sqlite3.Connection, *, full: bool) -> None:
    try:
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if app_id != APPLICATION_ID or version != SCHEMA_VERSION:
            raise InvalidContinuityDatabase("This is not a supported AIK continuity database.")
        if _schema_rows(connection) != _expected_schema():
            raise InvalidContinuityDatabase("The continuity schema differs from its declared version.")
        if full:
            if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
                raise InvalidContinuityDatabase("Continuity database integrity validation failed.")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise InvalidContinuityDatabase("Continuity database references are inconsistent.")
    except sqlite3.Error:
        raise InvalidContinuityDatabase("Continuity database validation could not be completed.") from None


def _sqlite_failure(error: sqlite3.Error) -> None:
    if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise StorageBusy("SQLite could not acquire the continuity database lock.") from None
    raise ContinuityError("The continuity database operation could not be completed.") from None


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # Windows directory fsync is not portable through Python.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


def _directive_digest(directive: Directive) -> str:
    return fingerprint("directive", directive.directive_id, directive.project_id, directive.kind,
                       directive.text, directive.target_id, directive.scope)


def _summary_digest(summary: ContextSummary) -> str:
    return fingerprint("summary", summary.summary_id, summary.project_id, summary.text, summary.covers_through_seq)


def _body_text(checkpoint: Checkpoint) -> str:
    return json.dumps(checkpoint.body(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _checkpoint_digest(project_id: str, body: str) -> str:
    return fingerprint("checkpoint", project_id, body)


def _same(stored: object, digest: str) -> bool:
    return type(stored) is str and stored.isascii() and hmac.compare_digest(stored, digest)


def _require_int(*values) -> None:
    if any(type(value) is not int for value in values):
        raise ValueError


def _decode_directive(row) -> StoredDirective:
    try:
        directive = Directive(row[1], row[2], row[3], row[4], row[5], row[6])
        _require_int(row[0], row[8])
        if not _same(row[7], _directive_digest(directive)):
            raise ValueError
        return StoredDirective(directive, row[0], row[8])
    except _DECODE_ERRORS:
        raise InvalidContinuityDatabase("A stored directive violates its contract.") from None


def _decode_summary(row) -> StoredSummary:
    try:
        summary = ContextSummary(row[1], row[2], row[3], row[4])
        _require_int(row[0], row[6])
        if not _same(row[5], _summary_digest(summary)):
            raise ValueError
        return StoredSummary(summary, row[0], row[6])
    except _DECODE_ERRORS:
        raise InvalidContinuityDatabase("A stored summary violates its contract.") from None


def _decode_config(row) -> StoredConfig:
    try:
        _require_int(row[3])
        return StoredConfig(ConfigRevision(row[0], row[1], row[2]), row[3])
    except _DECODE_ERRORS:
        raise InvalidContinuityDatabase("A stored config revision violates its contract.") from None


def _decode_checkpoint(row) -> StoredCheckpoint:
    try:
        _require_int(row[1], row[4])
        if type(row[2]) is not str:
            raise ValueError
        checkpoint = Checkpoint.from_body(row[0], json.loads(row[2]))
        canonical = _body_text(checkpoint)
        if row[2] != canonical or not _same(row[3], _checkpoint_digest(row[0], canonical)):
            raise ValueError
        return StoredCheckpoint(checkpoint, row[1], row[4])
    except _DECODE_ERRORS:
        raise InvalidContinuityDatabase("A stored checkpoint violates its contract.") from None


def _check_checkpoint_refs(connection: sqlite3.Connection, checkpoint: Checkpoint,
                           previous: Checkpoint | None) -> None:
    """References must exist in this store and must not move backwards."""
    project = checkpoint.project_id
    if checkpoint.directive_seq and connection.execute(
            "SELECT 1 FROM directives WHERE seq = ? AND project_id = ?", (checkpoint.directive_seq, project),
    ).fetchone() is None:
        raise InvalidContinuityRecord("The checkpoint references an unknown directive position.")
    if previous is not None and checkpoint.directive_seq < previous.directive_seq:
        raise RevisionRegression("The checkpoint directive position would move backwards.")
    if checkpoint.summary_id is not None and connection.execute(
            "SELECT 1 FROM summaries WHERE summary_id = ? AND project_id = ?", (checkpoint.summary_id, project),
    ).fetchone() is None:
        raise InvalidContinuityRecord("The checkpoint references an unknown summary.")
    earlier = {} if previous is None else dict(previous.config_refs)
    for config_id, revision in checkpoint.config_refs:
        if connection.execute("SELECT 1 FROM config_revisions WHERE config_id = ? AND revision = ?",
                              (config_id, revision)).fetchone() is None:
            raise InvalidContinuityRecord("The checkpoint references an unknown config revision.")
        if revision < earlier.get(config_id, 0):
            raise RevisionRegression("A checkpoint config revision would move backwards.")


def _validate_records(connection: sqlite3.Connection) -> None:
    """Decode every row and re-check ordering rules; any violation fails closed."""
    try:
        seen: dict[str, str] = {}
        max_seq: dict[str, int] = {}
        for row in connection.execute(f"SELECT {_DIRECTIVE_COLUMNS} FROM directives ORDER BY seq").fetchall():
            stored = _decode_directive(row)
            directive = stored.directive
            if directive.target_id is not None and seen.get(directive.target_id) != directive.project_id:
                raise InvalidContinuityRecord("A stored correction target is inconsistent.")
            seen[directive.directive_id] = directive.project_id
            max_seq[directive.project_id] = stored.seq
        for row in connection.execute(f"SELECT {_SUMMARY_COLUMNS} FROM summaries ORDER BY seq").fetchall():
            summary = _decode_summary(row).summary
            if summary.covers_through_seq > max_seq.get(summary.project_id, 0):
                raise InvalidContinuityRecord("A stored summary covers unknown directives.")
        for row in connection.execute(f"SELECT {_CONFIG_COLUMNS} FROM config_revisions").fetchall():
            _decode_config(row)
        previous: dict[str, StoredCheckpoint] = {}
        for row in connection.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM checkpoints ORDER BY project_id, revision").fetchall():
            stored = _decode_checkpoint(row)
            prior = previous.get(stored.checkpoint.project_id)
            if stored.revision != (1 if prior is None else prior.revision + 1):
                raise InvalidContinuityRecord("Stored checkpoint revisions are not contiguous.")
            _check_checkpoint_refs(connection, stored.checkpoint, None if prior is None else prior.checkpoint)
            previous[stored.checkpoint.project_id] = stored
    except (InvalidContinuityRecord, RevisionRegression):
        raise InvalidContinuityDatabase("Stored continuity records are inconsistent.") from None


def _limit(value: object) -> None:
    if type(value) is not int or not 1 <= value <= 1000:
        raise InvalidContinuityRecord("The query limit must be an integer between 1 and 1000.")


def _after(value: object) -> None:
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise InvalidContinuityRecord("The query position must be a nonnegative bounded integer.")


def _all_directives(connection: sqlite3.Connection, project_id: str) -> tuple:
    return tuple(_decode_directive(row) for row in connection.execute(
        f"SELECT {_DIRECTIVE_COLUMNS} FROM directives WHERE project_id = ? ORDER BY seq", (project_id,)))


def _latest_checkpoint(connection: sqlite3.Connection, project_id: str):
    row = connection.execute(
        f"SELECT {_CHECKPOINT_COLUMNS} FROM checkpoints WHERE project_id = ? ORDER BY revision DESC LIMIT 1",
        (project_id,)).fetchone()
    return None if row is None else _decode_checkpoint(row)


def _latest_summary(connection: sqlite3.Connection, project_id: str):
    row = connection.execute(
        f"SELECT {_SUMMARY_COLUMNS} FROM summaries WHERE project_id = ? ORDER BY seq DESC LIMIT 1",
        (project_id,)).fetchone()
    return None if row is None else _decode_summary(row)


def _latest_directive_seq(connection: sqlite3.Connection, project_id: str) -> int:
    return connection.execute("SELECT coalesce(max(seq), 0) FROM directives WHERE project_id = ?",
                              (project_id,)).fetchone()[0]


class ContinuityStore:
    """Persist director continuity; never Goal/Task authority or secret values."""

    def __init__(self, path: Path | str | None = None, *, timeout: float = 5.0, clock=None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise InvalidContinuityRecord("Storage timeout must be a finite nonnegative number.")
        if clock is not None and not callable(clock):
            raise InvalidContinuityRecord("The clock must be callable.")
        self.timeout = float(timeout)
        self._clock = clock if clock is not None else time.time
        self.path = validate_database_path(default_continuity_path() if path is None else path, create_parent=True)
        with database_lock(self.path, self.timeout):
            self._prepare()

    def _now_ms(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ContinuityError("The injected clock returned an invalid time.")
        return int(value * 1000)

    # -- files and connections -------------------------------------------------

    def _companion(self, suffix: str) -> Path:
        return self.path.with_name(self.path.name + suffix)

    def _check_files(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            candidate = self._companion(suffix)
            validate_database_path(candidate)
            if suffix != "-journal" and candidate.exists():
                raise InvalidContinuityDatabase("Continuity storage requires a DELETE-mode database without WAL files.")

    def _connect(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=self.timeout, isolation_level=None)
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidContinuityDatabase("Continuity storage requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _create(self) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".aik-continuity-init-", suffix=".sqlite3", dir=self.path.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(self._connect(temporary)) as connection:
                create_schema(connection)
                _validate_schema(connection, full=True)
            os.chmod(temporary, 0o600)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            validate_database_path(self.path)
            if self.path.exists():
                raise ContinuityError("The continuity destination changed during initialization.")
            os.replace(temporary, self.path)
            _sync_directory(self.path.parent)
        finally:
            for leftover in (temporary, temporary.with_name(temporary.name + "-journal")):
                if leftover.exists():
                    leftover.unlink()

    def _prepare(self) -> None:
        self._check_files()
        try:
            if not self.path.exists():
                if self._companion("-journal").exists():
                    raise InvalidContinuityDatabase("An orphan rollback journal cannot initialize continuity storage.")
                self._create()
                return
            with closing(self._connect(self.path)) as connection:
                _validate_schema(connection, full=True)
                _validate_records(connection)
        except sqlite3.Error:
            raise InvalidContinuityDatabase("The continuity database could not be validated.") from None
        except OSError:
            raise ContinuityError("The continuity database files could not be processed safely.") from None

    @contextmanager
    def _transaction(self):
        with database_lock(self.path, self.timeout):
            self._check_files()
            if not self.path.exists():
                raise InvalidContinuityDatabase("The continuity database is missing.")
            try:
                connection = self._connect(self.path)
            except sqlite3.Error as error:
                _sqlite_failure(error)
            try:
                _validate_schema(connection, full=False)
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException as error:
                if connection.in_transaction:
                    try:
                        connection.rollback()
                    except sqlite3.Error:
                        pass
                if isinstance(error, sqlite3.Error):
                    _sqlite_failure(error)
                raise
            finally:
                connection.close()

    # -- directives ------------------------------------------------------------

    def append_directive(self, directive: Directive) -> Appended:
        """Append a user directive or correction; order is the storage order (seq).

        A correction must target an earlier directive of the same project that
        is not already replaced or withdrawn; correct its current successor.
        """
        if type(directive) is not Directive:
            raise InvalidContinuityRecord("Only Directive records can be appended.")
        directive = rebuild(directive)
        digest = _directive_digest(directive)
        now = self._now_ms()
        with self._transaction() as connection:
            row = connection.execute(f"SELECT {_DIRECTIVE_COLUMNS} FROM directives WHERE directive_id = ?",
                                     (directive.directive_id,)).fetchone()
            if row is not None:
                stored = _decode_directive(row)
                if not _same(row[7], digest):
                    raise ContinuityConflict("A different directive already uses this directive_id.")
                return Appended("duplicate", stored)
            if directive.target_id is not None:
                target = connection.execute("SELECT project_id FROM directives WHERE directive_id = ?",
                                            (directive.target_id,)).fetchone()
                if target is None or target[0] != directive.project_id:
                    raise InvalidContinuityRecord("A correction must target an earlier directive of the same project.")
                # Status "replaced"/"withdrawn" comes only from such corrections.
                if connection.execute(
                        "SELECT 1 FROM directives WHERE target_id = ? AND scope IN ('replace', 'withdraw') LIMIT 1",
                        (directive.target_id,)).fetchone() is not None:
                    raise InvalidContinuityRecord(
                        "A replaced or withdrawn directive cannot be corrected again; correct its current successor.")
            cursor = connection.execute(
                "INSERT INTO directives (directive_id, project_id, kind, text, target_id, scope, fingerprint, "
                "recorded_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (directive.directive_id, directive.project_id, directive.kind, directive.text,
                 directive.target_id, directive.scope, digest, now))
            return Appended("accepted", StoredDirective(directive, cursor.lastrowid, now))

    def directives(self, project_id: str, *, limit: int = 1000, after_seq: int = 0) -> tuple:
        check_id(project_id, "project_id")
        _limit(limit)
        _after(after_seq)
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {_DIRECTIVE_COLUMNS} FROM directives WHERE project_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (project_id, after_seq, limit))
            return tuple(_decode_directive(row) for row in rows)

    def effective_directives(self, project_id: str) -> tuple:
        check_id(project_id, "project_id")
        with self._transaction() as connection:
            records = _all_directives(connection, project_id)
        try:
            return resolve_directives(records)
        except InvalidContinuityRecord:
            raise InvalidContinuityDatabase("Stored corrections are inconsistent.") from None

    # -- summaries ------------------------------------------------------------

    def record_summary(self, summary: ContextSummary) -> Appended:
        """Store a model_reported compaction summary; it never replaces authority."""
        if type(summary) is not ContextSummary:
            raise InvalidContinuityRecord("Only ContextSummary records can be recorded.")
        summary = rebuild(summary)
        digest = _summary_digest(summary)
        now = self._now_ms()
        with self._transaction() as connection:
            row = connection.execute(f"SELECT {_SUMMARY_COLUMNS} FROM summaries WHERE summary_id = ?",
                                     (summary.summary_id,)).fetchone()
            if row is not None:
                stored = _decode_summary(row)
                if not _same(row[5], digest):
                    raise ContinuityConflict("A different summary already uses this summary_id.")
                return Appended("duplicate", stored)
            top = _latest_directive_seq(connection, summary.project_id)
            if summary.covers_through_seq > top:
                raise InvalidContinuityRecord("A summary cannot cover directives that were not stored.")
            cursor = connection.execute(
                "INSERT INTO summaries (summary_id, project_id, text, covers_through_seq, fingerprint, recorded_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (summary.summary_id, summary.project_id, summary.text, summary.covers_through_seq, digest, now))
            return Appended("accepted", StoredSummary(summary, cursor.lastrowid, now))

    def latest_summary(self, project_id: str) -> StoredSummary | None:
        check_id(project_id, "project_id")
        with self._transaction() as connection:
            return _latest_summary(connection, project_id)

    # -- config revisions --------------------------------------------------------

    def record_config(self, config: ConfigRevision) -> Appended:
        """Revisions per config_id must strictly increase; same revision must match."""
        if type(config) is not ConfigRevision:
            raise InvalidContinuityRecord("Only ConfigRevision records can be recorded.")
        config = rebuild(config)
        now = self._now_ms()
        with self._transaction() as connection:
            row = connection.execute(f"SELECT {_CONFIG_COLUMNS} FROM config_revisions WHERE config_id = ? "
                                     "AND revision = ?", (config.config_id, config.revision)).fetchone()
            if row is not None:
                stored = _decode_config(row)
                if not _same(row[2], config.digest):
                    raise ContinuityConflict("A different digest is already stored for this config revision.")
                return Appended("duplicate", stored)
            last = connection.execute("SELECT max(revision) FROM config_revisions WHERE config_id = ?",
                                      (config.config_id,)).fetchone()[0]
            if last is not None and config.revision <= last:
                raise RevisionRegression("A config revision would move backwards.")
            connection.execute(
                "INSERT INTO config_revisions (config_id, revision, digest, recorded_ms) VALUES (?, ?, ?, ?)",
                (config.config_id, config.revision, config.digest, now))
            return Appended("accepted", StoredConfig(config, now))

    def latest_config(self, config_id: str) -> StoredConfig | None:
        check_id(config_id, "config_id")
        with self._transaction() as connection:
            row = connection.execute(f"SELECT {_CONFIG_COLUMNS} FROM config_revisions WHERE config_id = ? "
                                     "ORDER BY revision DESC LIMIT 1", (config_id,)).fetchone()
            return None if row is None else _decode_config(row)

    # -- checkpoints --------------------------------------------------------------

    def save_checkpoint(self, checkpoint: Checkpoint, expected_revision: int) -> StoredCheckpoint:
        """Append revision expected_revision + 1; zero creates the first checkpoint.

        Repeating the last successful save with identical content returns the
        stored revision (retry after a lost reply). A stale expected revision
        raises ContinuityConflict; backward directive/config positions raise
        RevisionRegression.
        """
        if type(checkpoint) is not Checkpoint:
            raise InvalidContinuityRecord("Only Checkpoint records can be saved.")
        checkpoint = rebuild(checkpoint)
        if type(expected_revision) is not int or not 0 <= expected_revision < MAX_INTEGER:
            raise InvalidContinuityRecord("An expected checkpoint revision must be a bounded integer.")
        body = _body_text(checkpoint)
        digest = _checkpoint_digest(checkpoint.project_id, body)
        now = self._now_ms()
        with self._transaction() as connection:
            previous = _latest_checkpoint(connection, checkpoint.project_id)
            current = 0 if previous is None else previous.revision
            if previous is not None and expected_revision == current - 1 and \
                    _checkpoint_digest(checkpoint.project_id, _body_text(previous.checkpoint)) == digest:
                return previous
            if expected_revision != current:
                raise ContinuityConflict("The checkpoint revision has changed; reload before saving.")
            _check_checkpoint_refs(connection, checkpoint, None if previous is None else previous.checkpoint)
            connection.execute(
                "INSERT INTO checkpoints (project_id, revision, body, fingerprint, recorded_ms) VALUES (?, ?, ?, ?, ?)",
                (checkpoint.project_id, current + 1, body, digest, now))
            return StoredCheckpoint(checkpoint, current + 1, now)

    def latest_checkpoint(self, project_id: str) -> StoredCheckpoint | None:
        check_id(project_id, "project_id")
        with self._transaction() as connection:
            return _latest_checkpoint(connection, project_id)

    def resume_snapshot(self, project_id: str) -> tuple:
        """(latest checkpoint or None, highest stored directive seq or 0) from one transaction."""
        check_id(project_id, "project_id")
        with self._transaction() as connection:
            return _latest_checkpoint(connection, project_id), _latest_directive_seq(connection, project_id)

    def checkpoints(self, project_id: str, *, limit: int = 100, after_revision: int = 0) -> tuple:
        check_id(project_id, "project_id")
        _limit(limit)
        _after(after_revision)
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM checkpoints WHERE project_id = ? AND revision > ? "
                "ORDER BY revision LIMIT ?", (project_id, after_revision, limit))
            return tuple(_decode_checkpoint(row) for row in rows)

    # -- handoff ------------------------------------------------------------------

    def handoff(self, project_id: str) -> HandoffPacket:
        """One consistent snapshot of directives, refs and the auxiliary summary."""
        check_id(project_id, "project_id")
        with self._transaction() as connection:
            records = _all_directives(connection, project_id)
            checkpoint = _latest_checkpoint(connection, project_id)
            summary = _latest_summary(connection, project_id)
            configs = []
            if checkpoint is not None:
                for config_id, revision in checkpoint.checkpoint.config_refs:
                    row = connection.execute(f"SELECT {_CONFIG_COLUMNS} FROM config_revisions WHERE config_id = ? "
                                             "AND revision = ?", (config_id, revision)).fetchone()
                    if row is None:
                        raise InvalidContinuityDatabase("A checkpoint config reference is missing.")
                    configs.append(_decode_config(row))
        try:
            directives = resolve_directives(records)
        except InvalidContinuityRecord:
            raise InvalidContinuityDatabase("Stored corrections are inconsistent.") from None
        return HandoffPacket(project_id, directives, checkpoint, summary, tuple(configs))
