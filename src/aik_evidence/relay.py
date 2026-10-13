"""AIK-037 evidence relay: append-only SQLite manifest plus content-addressed store.

The manifest database follows the aik_runtime path rules (outside source
repositories, development ledgers and Git internals; no links or aliases) and
uses its own SQLite application ID. Stored bytes live below ``managed_root``
under ``sha256/<hex>``; an object is written once through a temporary file,
fsync and rename, and is never overwritten.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile

from aik_runtime.errors import UnsafeStoragePath
# Reused repository path rules and directory fsync (see docs/evidence.ko.md).
from aik_runtime.paths import _inspect, database_lock, validate_database_path
from aik_runtime.storage import _sync_directory
from aik_security import OutputRedactor
from aik_security.redaction import OMITTED
from aik_workspace import InputCondition, canonical_root, normalize_input_path, sha256_digest
from aik_workspace.errors import InvalidInputPath, WorkspaceError
from aik_workspace.inputs import read_verified
from aik_workspace.roots import overlaps, root_key

from .errors import (
    EvidenceConflict, EvidenceError, EvidenceExportExists, EvidenceIntegrityError,
    EvidencePathRejected, EvidenceUnavailable, InvalidEvidenceDatabase,
    InvalidEvidenceRecord, UnknownArtifact,
)
from .models import (
    ACCESS_SCOPES, COMPARED_FIELDS, MAX_ARTIFACT_BYTES, ArtifactRecord, ConflictRecord,
    VerifyResult, check_identifier, check_transform,
)


APPLICATION_ID = 0x41494B45  # "AIKE"; not the runtime, planning, VM or AGS database.
SCHEMA_VERSION = 1
REDACTION_TRANSFORM = "aik_security.redact_text"
REDACTION_VERSION = "1"
_DEFAULT_REDACTION_CONFIG = sha256_digest(b"aik_security.OutputRedactor.redact_text;utf-8;v1")
_SQLITE_MAGIC = b"SQLite format 3\x00"
_ARTIFACT_COLUMNS = ("artifact_id", "kind", "sha256", "size", "vm_attempt_id", "lease_id",
                     "callback_id", "workspace_id", "relative_path", "access_scope",
                     "derived_from", "transform", "recorded_at")
_CONFLICT_COLUMNS = ("seq", "code", "subject_id", "artifact_id", "existing_sha256",
                     "observed_sha256", "fields", "recorded_at")


def _append_only(table: str) -> tuple[str, str]:
    return tuple(
        f'CREATE TRIGGER "{table}_append_only_{action.lower()}" BEFORE {action} ON "{table}" '
        "BEGIN SELECT RAISE(ABORT, 'append-only evidence record'); END"
        for action in ("UPDATE", "DELETE")
    )


_STATEMENTS = (
    'CREATE TABLE "artifacts" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "artifact_id" TEXT NOT NULL UNIQUE,\n'
    '    "kind" TEXT NOT NULL CHECK ("kind" IN (\'authoritative\', \'derivative\')),\n'
    '    "sha256" TEXT NOT NULL CHECK (length("sha256") = 71),\n'
    '    "size" INTEGER NOT NULL CHECK (typeof("size") = \'integer\' AND "size" >= 0),\n'
    '    "vm_attempt_id" TEXT NOT NULL,\n'
    '    "lease_id" TEXT NOT NULL,\n'
    '    "callback_id" TEXT UNIQUE,\n'
    '    "workspace_id" TEXT NOT NULL,\n'
    '    "relative_path" TEXT NOT NULL,\n'
    '    "access_scope" TEXT NOT NULL CHECK ("access_scope" IN (\'workspace\', \'managed\')),\n'
    '    "derived_from" TEXT REFERENCES "artifacts" ("artifact_id") ON DELETE RESTRICT ON UPDATE RESTRICT,\n'
    '    "transform" TEXT,\n'
    '    "recorded_at" TEXT NOT NULL,\n'
    '    CHECK (("kind" = \'authoritative\' AND "callback_id" IS NOT NULL AND "derived_from" IS NULL '
    'AND "transform" IS NULL) OR ("kind" = \'derivative\' AND "callback_id" IS NULL '
    'AND "derived_from" IS NOT NULL AND "transform" IS NOT NULL))\n'
    ')',
    'CREATE TABLE "conflicts" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "code" TEXT NOT NULL CHECK ("code" IN (\'CALLBACK_CONFLICT\', \'DERIVATIVE_CONFLICT\')),\n'
    '    "subject_id" TEXT NOT NULL,\n'
    '    "artifact_id" TEXT NOT NULL REFERENCES "artifacts" ("artifact_id") ON DELETE RESTRICT ON UPDATE RESTRICT,\n'
    '    "existing_sha256" TEXT NOT NULL,\n'
    '    "observed_sha256" TEXT NOT NULL,\n'
    '    "fields" TEXT NOT NULL,\n'
    '    "recorded_at" TEXT NOT NULL\n'
    ')',
    'CREATE TABLE "workspace_roots" (\n'
    '    "workspace_id" TEXT PRIMARY KEY NOT NULL,\n'
    '    "root" TEXT NOT NULL,\n'
    '    "recorded_at" TEXT NOT NULL\n'
    ')',
    'CREATE INDEX "artifacts_attempt" ON "artifacts" ("vm_attempt_id")',
    *_append_only("artifacts"),
    *_append_only("conflicts"),
    *_append_only("workspace_roots"),
)


def _create_schema(connection: sqlite3.Connection) -> None:
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
        _create_schema(connection)
        return _schema_rows(connection)


def _authoritative_id(callback_id: str) -> str:
    material = ("aik-evidence/callback\0" + callback_id).encode("utf-8")
    return "art-" + hashlib.sha256(material).hexdigest()[:48]


def _derivative_id(source_id: str, spec) -> str:
    material = "\0".join(("aik-evidence/derivative", source_id, spec["name"], spec["version"],
                          spec["config_digest"])).encode("utf-8")
    return "drv-" + hashlib.sha256(material).hexdigest()[:48]


def _differences(existing: ArtifactRecord, candidate: ArtifactRecord) -> tuple[str, ...]:
    return tuple(name for name in COMPARED_FIELDS if getattr(existing, name) != getattr(candidate, name))


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceRelay:
    """Collect, deduplicate, verify, derive and export worker artifacts.

    Records are append-only. A callback replay with identical results returns
    the stored record; a different digest or metadata appends a conflict and
    raises ``EvidenceConflict`` without changing the stored record.
    """

    def __init__(self, path: Path | str, *, managed_root: Path | str, clock=None,
                 timeout: float = 5.0, workspace_resolver=None):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            raise InvalidEvidenceRecord("INVALID_TIMEOUT")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if workspace_resolver is not None and not callable(workspace_resolver):
            raise TypeError("workspace_resolver must be callable")
        self.timeout = float(timeout)
        self._clock = clock or _default_clock
        self._resolver = workspace_resolver
        self.path = validate_database_path(path, create_parent=True)
        probe = validate_database_path(Path(managed_root) / "sha256" / "layout.probe", create_parent=True)
        self._objects = probe.parent
        self.managed_root = self._objects.parent
        if self.path.is_relative_to(self.managed_root):
            raise UnsafeStoragePath("The manifest database must stay outside the managed object store.")
        with database_lock(self.path, self.timeout):
            self._prepare()

    # ------------------------------------------------------------ database

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise EvidenceError("INVALID_CLOCK")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _connect(self, path: Path, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self.timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidEvidenceDatabase("UNSUPPORTED_JOURNAL_MODE")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _check_header(self) -> None:
        for suffix in ("-wal", "-shm"):
            if self.path.with_name(self.path.name + suffix).exists():
                raise InvalidEvidenceDatabase("UNSUPPORTED_JOURNAL_MODE")
        _inspect(self.path)
        try:
            with open(self.path, "rb") as stream:
                header = stream.read(100)
        except OSError:
            raise InvalidEvidenceDatabase("DATABASE_UNREADABLE") from None
        if (len(header) < 100 or not header.startswith(_SQLITE_MAGIC)
                or int.from_bytes(header[68:72], "big") != APPLICATION_ID):
            raise InvalidEvidenceDatabase("FOREIGN_DATABASE")

    def _validate(self, connection: sqlite3.Connection, *, full: bool) -> None:
        try:
            if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise InvalidEvidenceDatabase("FOREIGN_DATABASE")
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise InvalidEvidenceDatabase("UNSUPPORTED_SCHEMA_VERSION")
            if _schema_rows(connection) != _expected_schema():
                raise InvalidEvidenceDatabase("SCHEMA_MISMATCH")
            if full:
                if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
                    raise InvalidEvidenceDatabase("INTEGRITY_CHECK_FAILED")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise InvalidEvidenceDatabase("REFERENCES_INCONSISTENT")
        except sqlite3.Error:
            raise InvalidEvidenceDatabase("DATABASE_VALIDATION_FAILED") from None

    def _create(self) -> None:
        temporary = Path(tempfile.mkdtemp(prefix=".aik-evidence-new-", dir=self.path.parent))
        try:
            new = temporary / "new.sqlite3"
            with closing(self._connect(new, create=True)) as connection:
                _create_schema(connection)
            os.chmod(new, 0o600)
            with open(new, "r+b") as stream:
                os.fsync(stream.fileno())
            validate_database_path(self.path)
            if self.path.exists():
                raise InvalidEvidenceDatabase("DESTINATION_CHANGED")
            os.replace(new, self.path)
            _sync_directory(self.path.parent)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _prepare(self) -> None:
        try:
            if not self.path.exists():
                self._create()
            self._check_header()
            with closing(self._connect(self.path)) as connection:
                self._validate(connection, full=True)
                for row in connection.execute(self._artifact_select()):
                    self._decode_artifact(row)
                for row in connection.execute(self._conflict_select()):
                    self._decode_conflict(row)
        except sqlite3.Error:
            raise InvalidEvidenceDatabase("DATABASE_OPEN_FAILED") from None
        except OSError:
            raise EvidenceError("DATABASE_FILES_UNAVAILABLE") from None

    @contextmanager
    def _session(self):
        with database_lock(self.path, self.timeout):
            self._check_header()
            try:
                connection = self._connect(self.path)
            except sqlite3.Error:
                raise InvalidEvidenceDatabase("DATABASE_OPEN_FAILED") from None
            try:
                self._validate(connection, full=False)
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except sqlite3.Error:
                self._rollback(connection)
                raise EvidenceError("DATABASE_OPERATION_FAILED") from None
            except BaseException:
                self._rollback(connection)
                raise
            finally:
                connection.close()

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------ records

    @staticmethod
    def _artifact_select(where: str = "") -> str:
        columns = ", ".join(f'"{name}"' for name in _ARTIFACT_COLUMNS)
        return f'SELECT {columns} FROM "artifacts" {where} ORDER BY "seq"'

    @staticmethod
    def _conflict_select() -> str:
        columns = ", ".join(f'"{name}"' for name in _CONFLICT_COLUMNS)
        return f'SELECT {columns} FROM "conflicts" ORDER BY "seq"'

    @staticmethod
    def _decode_artifact(row) -> ArtifactRecord:
        try:
            values = dict(zip(_ARTIFACT_COLUMNS, row))
            if values["transform"] is not None:
                values["transform"] = json.loads(values["transform"])
            return ArtifactRecord(**values)
        except (EvidenceError, ValueError, TypeError, KeyError):
            raise InvalidEvidenceDatabase("STORED_RECORD_INVALID") from None

    @staticmethod
    def _decode_conflict(row) -> ConflictRecord:
        try:
            values = dict(zip(_CONFLICT_COLUMNS, row))
            fields = json.loads(values["fields"])
            if type(fields) is not list:
                raise ValueError
            values["fields"] = tuple(fields)
            return ConflictRecord(**values)
        except (EvidenceError, ValueError, TypeError, KeyError):
            raise InvalidEvidenceDatabase("STORED_RECORD_INVALID") from None

    def _by(self, connection, column: str, value: str) -> ArtifactRecord | None:
        row = connection.execute(self._artifact_select(f'WHERE "{column}" = ?'), (value,)).fetchone()
        return None if row is None else self._decode_artifact(row)

    @staticmethod
    def _insert_artifact(connection, record: ArtifactRecord) -> None:
        values = record.to_dict()
        if values["transform"] is not None:
            values["transform"] = json.dumps(values["transform"], sort_keys=True, separators=(",", ":"))
        columns = ", ".join(f'"{name}"' for name in _ARTIFACT_COLUMNS)
        placeholders = ", ".join("?" for _ in _ARTIFACT_COLUMNS)
        connection.execute(f'INSERT INTO "artifacts" ({columns}) VALUES ({placeholders})',
                           [values[name] for name in _ARTIFACT_COLUMNS])

    @staticmethod
    def _insert_conflict(connection, code: str, subject_id: str, existing: ArtifactRecord,
                         observed_sha256: str, fields: tuple[str, ...], recorded_at: str) -> ConflictRecord:
        cursor = connection.execute(
            'INSERT INTO "conflicts" ("code", "subject_id", "artifact_id", "existing_sha256", '
            '"observed_sha256", "fields", "recorded_at") VALUES (?, ?, ?, ?, ?, ?, ?)',
            (code, subject_id, existing.artifact_id, existing.sha256, observed_sha256,
             json.dumps(list(fields)), recorded_at),
        )
        return ConflictRecord(cursor.lastrowid, code, subject_id, existing.artifact_id,
                              existing.sha256, observed_sha256, fields, recorded_at)

    @staticmethod
    def _pin_workspace(connection, workspace_id: str, root: Path, recorded_at: str) -> None:
        key = str(root)
        row = connection.execute('SELECT "root" FROM "workspace_roots" WHERE "workspace_id" = ?',
                                 (workspace_id,)).fetchone()
        if row is not None:
            if root_key(row[0]) != root_key(key):
                raise EvidencePathRejected("WORKSPACE_ROOT_MISMATCH")
            return
        for _other, other_root in connection.execute('SELECT "workspace_id", "root" FROM "workspace_roots"'):
            if overlaps(other_root, key):
                raise EvidencePathRejected("WORKSPACE_ROOT_BELONGS_TO_ANOTHER_WORKSPACE")
        connection.execute('INSERT INTO "workspace_roots" ("workspace_id", "root", "recorded_at") '
                           "VALUES (?, ?, ?)", (workspace_id, key, recorded_at))

    # ------------------------------------------------------------ object store

    def _object_path(self, digest: str) -> Path:
        return self._objects / digest[len("sha256:"):]

    def _read_object(self, digest: str, size: int) -> tuple[str, bytes | None, str | None]:
        path = self._object_path(digest)
        try:
            for component in (self.managed_root, self._objects, path):
                _inspect(component)
            info = os.lstat(path)
        except FileNotFoundError:
            return "missing", None, None
        except (UnsafeStoragePath, OSError):
            return "tampered", None, None
        if not stat.S_ISREG(info.st_mode):
            return "tampered", None, None
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return "missing", None, None
        except OSError:
            return "tampered", None, None
        try:
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or not os.path.samestat(opened, info)):
                    return "tampered", None, None
                data = stream.read(MAX_ARTIFACT_BYTES + 1)
        except OSError:
            return "tampered", None, None
        if len(data) > MAX_ARTIFACT_BYTES:
            return "tampered", None, None
        observed = sha256_digest(data)
        if observed != digest or len(data) != size:
            return "tampered", None, observed
        return "intact", data, observed

    def _store_object(self, data: bytes, digest: str) -> None:
        """Write once: temporary file, fsync, rename. Never overwrite an existing object."""
        final = validate_database_path(self._object_path(digest), create_parent=True)
        if final.parent != self._objects:
            raise UnsafeStoragePath("The managed object store location changed.")
        status, _, _ = self._read_object(digest, len(data))
        if status == "intact":
            return
        if status == "tampered" or os.path.lexists(final):
            raise EvidenceIntegrityError("STORE_OBJECT_TAMPERED", "tampered")
        descriptor, temporary = tempfile.mkstemp(prefix=".incoming-", dir=self._objects)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            if os.path.lexists(final):
                raise EvidenceIntegrityError("STORE_OBJECT_TAMPERED", "tampered")
            os.replace(temporary, final)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        _sync_directory(self._objects)
        status, _, observed = self._read_object(digest, len(data))
        if status != "intact":
            raise EvidenceIntegrityError("STORE_WRITE_UNVERIFIED", status, observed)

    # ------------------------------------------------------------ public API

    def collect(self, callback_id: str, *, workspace_id: str, workspace_root, relative_path: str,
                vm_attempt_id: str, lease_id: str, max_bytes: int,
                access_scope: str = "managed") -> ArtifactRecord:
        """Record one worker output and hand its bytes to the managed store."""
        for field, value in (("callback_id", callback_id), ("workspace_id", workspace_id),
                             ("vm_attempt_id", vm_attempt_id), ("lease_id", lease_id)):
            check_identifier(value, field)
        if type(access_scope) is not str or access_scope not in ACCESS_SCOPES:
            raise InvalidEvidenceRecord("INVALID_ACCESS_SCOPE")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ARTIFACT_BYTES:
            raise InvalidEvidenceRecord("INVALID_MAX_BYTES")
        try:
            relative = normalize_input_path(relative_path)
        except InvalidInputPath:
            raise EvidencePathRejected("INVALID_RELATIVE_PATH") from None
        try:
            root = canonical_root(workspace_root)
        except WorkspaceError:
            raise EvidencePathRejected("INVALID_WORKSPACE_ROOT") from None
        if self._resolver is not None:
            try:
                expected = canonical_root(self._resolver(workspace_id))
            except Exception:
                raise EvidencePathRejected("WORKSPACE_UNRESOLVED") from None
            if root_key(str(expected)) != root_key(str(root)):
                raise EvidencePathRejected("WORKSPACE_ROOT_MISMATCH")
        if overlaps(str(root), str(self.managed_root)):
            raise EvidencePathRejected("WORKSPACE_OVERLAPS_MANAGED_STORE")
        condition, data = read_verified(root, relative, max_bytes)
        if condition is not InputCondition.CURRENT or data is None:
            raise EvidenceUnavailable("ARTIFACT_" + condition.value.upper(), condition.value)
        digest = sha256_digest(data)
        candidate = ArtifactRecord(
            artifact_id=_authoritative_id(callback_id), kind="authoritative", sha256=digest,
            size=len(data), vm_attempt_id=vm_attempt_id, lease_id=lease_id, callback_id=callback_id,
            workspace_id=workspace_id, relative_path=relative, access_scope=access_scope,
            derived_from=None, transform=None, recorded_at=self._now(),
        )
        conflict = None
        result = None
        with self._session() as connection:
            if self._resolver is None:
                self._pin_workspace(connection, workspace_id, root, candidate.recorded_at)
            existing = self._by(connection, "callback_id", callback_id)
            if existing is None:
                self._store_object(data, digest)
                self._insert_artifact(connection, candidate)
                result = candidate
            else:
                differing = _differences(existing, candidate)
                if differing:
                    conflict = self._insert_conflict(connection, "CALLBACK_CONFLICT", callback_id,
                                                     existing, digest, differing, candidate.recorded_at)
                else:
                    # Authentic bytes for this digest may restore a lost object;
                    # a differing existing object is never overwritten.
                    self._store_object(data, digest)
                    result = existing
        if conflict is not None:
            raise EvidenceConflict("CALLBACK_CONFLICT", conflict)
        return result

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        check_identifier(artifact_id, "artifact_id")
        with self._session() as connection:
            return self._by(connection, "artifact_id", artifact_id)

    def _require(self, artifact_id: str) -> ArtifactRecord:
        record = self.get(artifact_id)
        if record is None:
            raise UnknownArtifact("UNKNOWN_ARTIFACT")
        return record

    def records(self, vm_attempt_id: str | None = None) -> tuple[ArtifactRecord, ...]:
        if vm_attempt_id is not None:
            check_identifier(vm_attempt_id, "vm_attempt_id")
        with self._session() as connection:
            if vm_attempt_id is None:
                rows = connection.execute(self._artifact_select()).fetchall()
            else:
                rows = connection.execute(self._artifact_select('WHERE "vm_attempt_id" = ?'),
                                          (vm_attempt_id,)).fetchall()
            return tuple(self._decode_artifact(row) for row in rows)

    def conflicts(self) -> tuple[ConflictRecord, ...]:
        with self._session() as connection:
            return tuple(self._decode_conflict(row) for row in connection.execute(self._conflict_select()))

    def verify(self, artifact_id: str) -> VerifyResult:
        record = self._require(artifact_id)
        status, _, observed = self._read_object(record.sha256, record.size)
        return VerifyResult(record.artifact_id, status, record.sha256, observed)

    def read_bytes(self, artifact_id: str) -> bytes:
        record = self._require(artifact_id)
        status, data, observed = self._read_object(record.sha256, record.size)
        if status != "intact" or data is None:
            raise EvidenceIntegrityError("ARTIFACT_" + status.upper(), status, observed)
        return data

    def derive(self, artifact_id: str, *, transform_name: str, transform_version: str,
               config_digest: str, transform) -> ArtifactRecord:
        """Record a display derivative with its own identity/digest; the source is unchanged."""
        check_identifier(artifact_id, "artifact_id")
        spec = check_transform({"name": transform_name, "version": transform_version,
                                "config_digest": config_digest})
        if not callable(transform):
            raise TypeError("transform must be callable")
        source = self._require(artifact_id)
        data = self.read_bytes(artifact_id)
        try:
            output = transform(data)
        except Exception:
            raise EvidenceError("TRANSFORM_FAILED") from None
        if type(output) is bytearray:
            output = bytes(output)
        if type(output) is not bytes or len(output) > MAX_ARTIFACT_BYTES:
            raise EvidenceError("INVALID_TRANSFORM_OUTPUT")
        digest = sha256_digest(output)
        candidate = ArtifactRecord(
            artifact_id=_derivative_id(artifact_id, spec), kind="derivative", sha256=digest,
            size=len(output), vm_attempt_id=source.vm_attempt_id, lease_id=source.lease_id,
            callback_id=None, workspace_id=source.workspace_id, relative_path=source.relative_path,
            access_scope="managed", derived_from=artifact_id, transform=spec, recorded_at=self._now(),
        )
        conflict = None
        result = None
        with self._session() as connection:
            existing = self._by(connection, "artifact_id", candidate.artifact_id)
            if existing is None:
                self._store_object(output, digest)
                self._insert_artifact(connection, candidate)
                result = candidate
            else:
                differing = _differences(existing, candidate)
                if differing:
                    conflict = self._insert_conflict(connection, "DERIVATIVE_CONFLICT", artifact_id,
                                                     existing, digest, differing, candidate.recorded_at)
                else:
                    self._store_object(output, digest)
                    result = existing
        if conflict is not None:
            raise EvidenceConflict("DERIVATIVE_CONFLICT", conflict)
        return result

    def export(self, artifact_id: str, destination_dir, *, filename: str | None = None) -> Path:
        """Save verified managed bytes as a new file; never overwrite. GUI opening is out of scope."""
        record = self._require(artifact_id)
        data = self.read_bytes(artifact_id)
        name = PurePosixPath(record.relative_path).name if filename is None else filename
        try:
            name = normalize_input_path(name)
        except InvalidInputPath:
            raise EvidencePathRejected("INVALID_EXPORT_NAME") from None
        if "/" in name:
            raise EvidencePathRejected("INVALID_EXPORT_NAME")
        try:
            destination = canonical_root(destination_dir)
        except WorkspaceError:
            raise EvidencePathRejected("INVALID_EXPORT_DESTINATION") from None
        if overlaps(str(destination), str(self.managed_root)):
            raise EvidencePathRejected("EXPORT_OVERLAPS_MANAGED_STORE")
        target = destination / name
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            descriptor = os.open(target, flags, 0o666)
        except FileExistsError:
            raise EvidenceExportExists("EXPORT_EXISTS") from None
        except OSError:
            raise EvidenceError("EXPORT_FAILED") from None
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(destination)
        except OSError:
            try:
                target.unlink()
            except OSError:
                pass
            raise EvidenceError("EXPORT_FAILED") from None
        condition, saved = read_verified(destination, name, record.size)
        if condition is not InputCondition.CURRENT or saved is None or sha256_digest(saved) != record.sha256:
            # The file is left in place for inspection; the export is not reported as successful.
            raise EvidenceIntegrityError("EXPORT_VERIFICATION_FAILED", "tampered",
                                         None if saved is None else sha256_digest(saved))
        return target


def redact_text_artifact(relay: EvidenceRelay, artifact_id: str, redactor: OutputRedactor, *,
                         config_digest: str | None = None) -> ArtifactRecord:
    """Store a redacted UTF-8 display derivative; the authoritative record stays unchanged.

    ``config_digest`` must be a non-secret identifier of the redaction setup.
    Callers using different secret registries must pass different values.
    """
    if type(relay) is not EvidenceRelay:
        raise TypeError("EvidenceRelay required")
    if type(redactor) is not OutputRedactor:
        raise TypeError("OutputRedactor required")
    data = relay.read_bytes(artifact_id)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise EvidenceError("NON_UTF8_ARTIFACT") from None
    redacted = redactor.redact_text(text)
    if redacted == OMITTED and text != OMITTED:
        raise EvidenceError("REDACTION_LIMIT_EXCEEDED")
    output = redacted.encode("utf-8")

    def transform(source: bytes) -> bytes:
        if source != data:
            raise ValueError("source changed")
        return output

    return relay.derive(artifact_id, transform_name=REDACTION_TRANSFORM, transform_version=REDACTION_VERSION,
                        config_digest=config_digest or _DEFAULT_REDACTION_CONFIG, transform=transform)
