"""Exercise runtime persistence against disposable, external SQLite databases.

All models describe synthetic metadata. No provider is contacted, no credential
value is needed, and VM Task authority is not copied into the runtime store.
The crash test kills an actual writer process before its transaction commits.
Running this suite on Linux is development verification, not Windows support.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, fields, replace
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_runtime import (  # noqa: E402
    CredentialRef, ExecutionProfile, InvalidDatabase, InvalidRecord, Lease,
    MigrationError, Placement, Pool, RevisionConflict, RuntimeStore, StorageBusy,
    UnsafeStoragePath, Versioned,
)
from aik_runtime import paths, schema, storage  # noqa: E402


SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_BE_STORED"
CRASH_WRITER = r'''
from pathlib import Path
import sys
import time
sys.path.insert(0, sys.argv[1])
from aik_runtime import Pool, RuntimeStore

store = RuntimeStore(Path(sys.argv[2]))
with store.transaction() as tx:
    # This private connection is used only to force disk spill in the fault
    # harness; the product repository API exposes no raw SQL capability.
    tx._connection.execute("PRAGMA cache_size=4")
    tx._connection.execute("PRAGMA cache_spill=ON")
    for number in range(512):
        tx.save(Pool(f"pool-uncommitted-{number:04d}", "synthetic",
                     f"connection-pending-{number:04d}-" + "x" * 60), expected_revision=0)
    Path(sys.argv[3]).write_text("512 writes reached; COMMIT has not run", encoding="utf-8")
    time.sleep(30)
'''
V1_CRASH_WRITER = r'''
from pathlib import Path
import sqlite3
import sys
import time

# A supported old-schema writer fixture: using RuntimeStore here would upgrade
# to schema 2 before the old process can leave its journal behind.
connection = sqlite3.connect(sys.argv[2], isolation_level=None)
connection.execute("PRAGMA foreign_keys=ON")
connection.execute("PRAGMA synchronous=FULL")
connection.execute("PRAGMA cache_size=4")
connection.execute("PRAGMA cache_spill=ON")
connection.execute("BEGIN IMMEDIATE")
for number in range(512):
    connection.execute(
        "INSERT INTO pools (id,provider,connection_id,max_parallel,enabled,revision) VALUES (?,?,?,1,0,1)",
        (f"pool-uncommitted-{number:04d}", "synthetic", f"connection-pending-{number:04d}-" + "x" * 60),
    )
Path(sys.argv[3]).write_text("old-schema writes reached; COMMIT has not run", encoding="utf-8")
time.sleep(30)
'''


def example_models() -> tuple:
    """Use only references and external IDs, never a secret or a VM Task body."""
    return (
        CredentialRef("credential-1", "secret://local/test-key"),
        Pool("pool-1", "synthetic", "connection-1", "credential-1", max_parallel=2),
        ExecutionProfile("profile-1", "pool-1", "worker", "synthetic-model", effort="high"),
        Placement("placement-1", "pool-1", "profile-1", "vm-instance-1", "vm-project-1",
                  "vm-task-1", "vm-attempt-1", ags_session_id="ags-session-1"),
        Lease("lease-1", "placement-1", "pool-1", "worker-1", 1, "2030-01-01T00:00:00Z"),
    )


class RuntimeStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-runtime-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "사용자 공간" / "runtime data.sqlite3"

    def seeded_store(self) -> tuple[RuntimeStore, tuple]:
        store = RuntimeStore(self.database)
        models = example_models()
        with store.transaction() as tx:
            for model in models:
                self.assertEqual(tx.save(model, expected_revision=0), Versioned(model, 1))
        return store, models

    def assert_integrity(self, path: Path | None = None) -> None:
        target = path or self.database
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def create_v1_database(self, *, incoherent: bool = False) -> tuple:
        """Construct the supported old schema, with data and noninitial revisions."""
        self.database.parent.mkdir(parents=True, exist_ok=True)
        models = example_models()
        tables = {CredentialRef: "credential_refs", Pool: "pools", ExecutionProfile: "execution_profiles",
                  Placement: "placements", Lease: "leases"}
        with closing(sqlite3.connect(self.database)) as connection:
            schema.create_schema(connection, version=1)
            for model in models:
                columns = [field.name for field in fields(model)]
                values = [getattr(model, column) for column in columns]
                connection.execute(
                    f"INSERT INTO {tables[type(model)]} ({', '.join(columns)}, revision) "
                    f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))})", [*values, 2],
                )
            if incoherent:
                connection.execute(
                    "INSERT INTO pools (id, provider, connection_id, max_parallel, enabled, revision) "
                    "VALUES ('pool-other', 'synthetic', 'connection-other', 1, 0, 1)"
                )
                # Schema 1 has valid individual FKs but permits these pool
                # references to disagree; schema 2 must enforce their coherence.
                connection.execute("UPDATE placements SET pool_id='pool-other' WHERE id='placement-1'")
            connection.commit()
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        return models

    def test_all_five_metadata_models_persist_across_reopen_with_external_ids(self) -> None:
        store, models = self.seeded_store()
        reopened = RuntimeStore(self.database)
        for model in models:
            with self.subTest(model=type(model).__name__):
                self.assertEqual(store.get(type(model), model.id), Versioned(model, 1))
                self.assertEqual(reopened.get(type(model), model.id), Versioned(model, 1))
                self.assertEqual(reopened.list(type(model)), (Versioned(model, 1),))
        self.assert_integrity()

    def test_default_location_uses_per_user_state_outside_the_working_directory(self) -> None:
        windows_base, development_base = self.root / "windows-profile", self.root / "xdg-state"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(windows_base), "XDG_STATE_HOME": str(development_base)}):
            store = RuntimeStore()
            model = Pool("pool-default", "synthetic", "connection-default")
            store.save(model, expected_revision=0)
            self.assertEqual(store.get(Pool, model.id), Versioned(model, 1))
        expected = (windows_base / "AIK/runtime/runtime.sqlite3" if os.name == "nt"
                    else development_base / "aik/runtime.sqlite3")
        self.assertTrue(expected.is_file())
        self.assert_integrity(expected)

    def test_stale_update_and_delete_from_a_second_store_preserve_newer_revision(self) -> None:
        first, models = self.seeded_store()
        second = RuntimeStore(self.database)
        original = models[1]
        stale = second.get(Pool, original.id)
        updated = replace(original, max_parallel=4)
        self.assertEqual(first.save(updated, expected_revision=stale.revision), Versioned(updated, 2))
        with self.assertRaises(RevisionConflict):
            second.save(replace(original, max_parallel=8), expected_revision=stale.revision)
        with self.assertRaises(RevisionConflict):
            second.delete(Pool, original.id, expected_revision=stale.revision)
        self.assertEqual(second.get(Pool, original.id), Versioned(updated, 2))

    def test_duplicate_create_conflicts_and_revision_checked_delete_removes_only_its_record(self) -> None:
        store = RuntimeStore(self.database)
        original = CredentialRef("credential-one", "secret://local/key-one")
        store.save(original, expected_revision=0)
        second = RuntimeStore(self.database)
        with self.assertRaises(RevisionConflict):
            second.save(replace(original, reference="secret://local/replacement"), expected_revision=0)
        self.assertEqual(second.get(CredentialRef, original.id), Versioned(original, 1))
        second.delete(CredentialRef, original.id, expected_revision=1)
        self.assertIsNone(store.get(CredentialRef, original.id))
        with self.assertRaises(RevisionConflict):
            store.delete(CredentialRef, original.id, expected_revision=1)

    def test_paginated_queries_are_bounded_and_have_no_duplicate_or_skipped_ids(self) -> None:
        store = RuntimeStore(self.database)
        for number in (4, 1, 5, 2, 3):
            store.save(Pool(f"pool-{number}", "synthetic", f"connection-{number}"), expected_revision=0)
        first = store.list(Pool, limit=2)
        second = store.list(Pool, limit=2, after_id=first[-1].value.id)
        third = store.list(Pool, limit=2, after_id=second[-1].value.id)
        self.assertIsInstance(first, tuple)
        self.assertEqual([entry.value.id for entry in first + second + third], [f"pool-{n}" for n in range(1, 6)])
        self.assertEqual([len(first), len(second), len(third)], [2, 2, 1])
        self.assertEqual(store.list(Pool, limit=2, after_id=third[-1].value.id), ())

    def test_exception_rolls_back_every_write_in_a_multi_record_transaction(self) -> None:
        store, models = self.seeded_store()
        original = models[1]
        with self.assertRaisesRegex(RuntimeError, "synthetic transaction interruption"):
            with store.transaction() as tx:
                tx.save(replace(original, max_parallel=5), expected_revision=1)
                tx.save(CredentialRef("credential-uncommitted", "secret://local/uncommitted"), expected_revision=0)
                raise RuntimeError("synthetic transaction interruption")
        self.assertEqual(store.get(Pool, original.id), Versioned(original, 1))
        self.assertIsNone(store.get(CredentialRef, "credential-uncommitted"))
        self.assert_integrity()

    def test_later_revision_conflict_rolls_back_earlier_successful_batch_writes(self) -> None:
        store, models = self.seeded_store()
        credential, pool = models[:2]
        newer_credential = replace(credential, reference="secret://local/revised-key")
        store.save(newer_credential, expected_revision=1)
        with self.assertRaises(RevisionConflict):
            with store.transaction() as tx:
                tx.save(replace(pool, max_parallel=6), expected_revision=1)
                tx.save(replace(credential, reference="secret://local/stale-key"), expected_revision=1)
        self.assertEqual(store.get(Pool, pool.id), Versioned(pool, 1))
        self.assertEqual(store.get(CredentialRef, credential.id), Versioned(newer_credential, 2))

    @staticmethod
    def stop_process(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    def kill_writer_after_disk_spill(self, script: str) -> tuple[bytes, bytes]:
        committed_bytes = self.database.read_bytes()
        marker = self.root / "transaction-written.marker"
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT / "src"), str(self.database), str(marker)],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        self.addCleanup(self.stop_process, process)
        deadline = time.monotonic() + 10
        while not marker.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                self.fail(f"Writer exited before its uncommitted writes: {stdout}{stderr}")
            if time.monotonic() >= deadline:
                self.fail("Writer did not reach the pre-COMMIT marker within ten seconds.")
            time.sleep(0.01)
        journal = self.database.with_name(self.database.name + "-journal")
        self.assertTrue(journal.is_file(), "The crash harness did not produce a rollback journal.")
        self.assertGreater(journal.stat().st_size, 512)
        self.assertEqual(journal.read_bytes()[:8], b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7")
        self.assertNotEqual(self.database.read_bytes(), committed_bytes, "No uncommitted pages reached disk.")
        process.kill()
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        return self.database.read_bytes(), journal.read_bytes()

    def test_actual_process_death_before_commit_recovers_without_partial_records(self) -> None:
        store, models = self.seeded_store()
        self.kill_writer_after_disk_spill(CRASH_WRITER)
        reopened = RuntimeStore(self.database)
        for model in models:
            self.assertEqual(reopened.get(type(model), model.id), Versioned(model, 1))
        self.assertEqual(reopened.list(Pool, limit=1000), (Versioned(models[1], 1),))
        after = Pool("pool-after-recovery", "synthetic", "connection-after-recovery")
        self.assertEqual(reopened.save(after, expected_revision=0), Versioned(after, 1))
        self.assertEqual(store.get(Pool, after.id), Versioned(after, 1))
        self.assert_integrity()

    def test_busy_writer_times_out_without_partial_write_and_can_retry(self) -> None:
        first = RuntimeStore(self.database)
        second = RuntimeStore(self.database, timeout=0.05)
        held = Pool("pool-held", "synthetic", "connection-held")
        waiting = Pool("pool-waiting", "synthetic", "connection-waiting")
        with first.transaction() as tx:
            tx.save(held, expected_revision=0)
            with self.assertRaises(StorageBusy):
                second.save(waiting, expected_revision=0)
        self.assertIsNone(second.get(Pool, waiting.id))
        self.assertEqual(second.save(waiting, expected_revision=0), Versioned(waiting, 1))

    def test_missing_parents_and_cross_pool_relationships_cannot_be_saved(self) -> None:
        store, models = self.seeded_store()
        store.save(Pool("pool-other", "synthetic", "connection-other"), expected_revision=0)
        invalid = (
            Pool("pool-invalid", "synthetic", "connection-invalid", "credential-missing"),
            ExecutionProfile("profile-invalid", "pool-missing", "worker", "synthetic-model"),
            replace(models[3], id="placement-invalid", pool_id="pool-other"),
            replace(models[3], id="placement-missing-profile", profile_id="profile-missing"),
            replace(models[4], id="lease-invalid", pool_id="pool-other"),
            replace(models[4], id="lease-missing-placement", placement_id="placement-missing"),
        )
        for model in invalid:
            with self.subTest(model=type(model).__name__, identifier=model.id):
                with self.assertRaises((InvalidRecord, sqlite3.IntegrityError)):
                    store.save(model, expected_revision=0)
                self.assertIsNone(store.get(type(model), model.id))
        self.assert_integrity()

    def test_referenced_parent_delete_and_cross_pool_update_preserve_the_graph(self) -> None:
        store, models = self.seeded_store()
        store.save(Pool("pool-other", "synthetic", "connection-other"), expected_revision=0)
        with self.assertRaises((InvalidRecord, sqlite3.IntegrityError)):
            store.delete(Pool, "pool-1", expected_revision=1)
        with self.assertRaises((InvalidRecord, sqlite3.IntegrityError)):
            store.save(replace(models[2], pool_id="pool-other"), expected_revision=1)
        with self.assertRaises((InvalidRecord, sqlite3.IntegrityError)):
            store.save(replace(models[4], pool_id="pool-other"), expected_revision=1)
        for model in models:
            self.assertEqual(store.get(type(model), model.id), Versioned(model, 1))
        self.assert_integrity()

    def test_secret_payload_extra_fields_and_forged_frozen_records_are_rejected(self) -> None:
        store = RuntimeStore(self.database)
        with self.assertRaises(TypeError):
            CredentialRef("credential-raw", "secret://local/raw", raw_secret=SECRET_MARKER)
        with self.assertRaises((TypeError, InvalidRecord)):
            store.save({"id": "credential-raw", "raw_secret": SECRET_MARKER}, expected_revision=0)

        @dataclass(frozen=True)
        class SecretBearingPool(Pool):
            raw_secret: str = SECRET_MARKER

        with self.assertRaises((TypeError, InvalidRecord)):
            store.save(SecretBearingPool("pool-raw", "synthetic", "connection-raw"), expected_revision=0)
        for reference in (SECRET_MARKER, "secret://local/key?token=" + SECRET_MARKER):
            with self.subTest(reference_kind="raw" if reference == SECRET_MARKER else "query"):
                with self.assertRaises(InvalidRecord) as rejected:
                    CredentialRef("credential-raw", reference)
                self.assertNotIn(SECRET_MARKER, str(rejected.exception))
        forged = CredentialRef("credential-forged", "secret://local/valid")
        object.__setattr__(forged, "reference", SECRET_MARKER)
        with self.assertRaises(InvalidRecord) as rejected:
            store.save(forged, expected_revision=0)
        self.assertNotIn(SECRET_MARKER, str(rejected.exception))
        self.assertEqual(store.list(CredentialRef), ())
        self.assertEqual(store.list(Pool), ())
        self.assertNotIn(SECRET_MARKER.encode("ascii"), self.database.read_bytes())

    def test_corrupt_foreign_and_unknown_version_databases_are_preserved(self) -> None:
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"SQLite format 3\0SYNTHETIC DAMAGED DATABASE" * 12)
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('synthetic unrelated application data')")
            connection.commit()
        unknown = self.root / "future.sqlite3"
        RuntimeStore(unknown)
        with closing(sqlite3.connect(unknown)) as connection:
            connection.execute("PRAGMA user_version=999")
            connection.commit()
        for path in (corrupt, foreign, unknown):
            with self.subTest(source=path.name):
                before = path.read_bytes()
                with self.assertRaises(InvalidDatabase):
                    RuntimeStore(path)
                self.assertEqual(path.read_bytes(), before)

    def test_schema_one_migration_preserves_all_models_and_their_revisions(self) -> None:
        models = self.create_v1_database()
        store = RuntimeStore(self.database)
        for model in models:
            self.assertEqual(store.get(type(model), model.id), Versioned(model, 2))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 0x41494B52)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        updated = replace(models[1], max_parallel=3)
        self.assertEqual(store.save(updated, expected_revision=2), Versioned(updated, 3))
        store.save(Pool("pool-other", "synthetic", "connection-other"), expected_revision=0)
        with self.assertRaises(InvalidRecord):
            store.save(replace(models[3], id="placement-cross-pool", pool_id="pool-other"), expected_revision=0)
        self.assert_integrity()

    def test_partial_migration_failure_preserves_original_bytes_schema_and_records(self) -> None:
        models = self.create_v1_database()
        before = self.database.read_bytes()
        original_migration = schema.MIGRATIONS[1]

        def fail_after_changes(connection: sqlite3.Connection) -> None:
            original_migration(connection)
            connection.execute("CREATE TABLE synthetic_partial_migration (marker TEXT)")
            connection.execute("INSERT INTO synthetic_partial_migration VALUES ('partial mutation reached')")
            connection.execute("UPDATE pools SET max_parallel=23, revision=revision+1 WHERE id='pool-1'")
            raise RuntimeError("synthetic interruption after actual migration mutations")

        with mock.patch.dict(schema.MIGRATIONS, {1: fail_after_changes}):
            with self.assertRaises(MigrationError):
                RuntimeStore(self.database)
        self.assertEqual(self.database.read_bytes(), before)
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT max_parallel, revision FROM pools WHERE id='pool-1'").fetchone(), (2, 2))
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='synthetic_partial_migration'").fetchone())
        # Removing the injected failure permits a real subsequent migration.
        reopened = RuntimeStore(self.database)
        for model in models:
            self.assertEqual(reopened.get(type(model), model.id), Versioned(model, 2))

    def test_incoherent_old_schema_data_blocks_migration_without_rewriting_the_source(self) -> None:
        self.create_v1_database(incoherent=True)
        before = self.database.read_bytes()
        self.assert_integrity()
        with self.assertRaises(MigrationError):
            RuntimeStore(self.database)
        self.assertEqual(self.database.read_bytes(), before)
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT pool_id FROM placements WHERE id='placement-1'").fetchone(), ("pool-other",))

    def test_failed_migration_publication_and_restore_retain_a_recoverable_raw_backup(self) -> None:
        models = self.create_v1_database()
        original_database, original_journal = self.kill_writer_after_disk_spill(V1_CRASH_WRITER)
        real_replace = storage.os.replace
        fault_target = self.database.resolve(strict=True)
        rejected_sources = []

        def reject_database_replacement(source, destination):
            # Windows TEMP may use an 8.3 name while RuntimeStore publishes to
            # the resolved long path. Match the same canonical destination.
            if Path(destination).resolve(strict=False) == fault_target:
                rejected_sources.append(Path(source).name)
                raise PermissionError("synthetic publication and rollback sharing violation")
            return real_replace(source, destination)

        failure = None
        with mock.patch.object(storage.os, "replace", side_effect=reject_database_replacement):
            try:
                RuntimeStore(self.database)
            except MigrationError as error:
                failure = error
        self.assertEqual(rejected_sources, ["checked.sqlite3", "restore-database"],
                         "Both publication and source restoration must receive exactly one injected failure.")
        self.assertIsInstance(failure, MigrationError)
        recovery = failure.recovery_directory
        self.assertTrue(recovery.is_dir())
        self.assertEqual((recovery / "original.sqlite3").read_bytes(), original_database)
        self.assertEqual((recovery / "original.sqlite3-journal").read_bytes(), original_journal)

        # Demonstrate that the retained pair restores the committed data and
        # upgrades successfully after the simulated I/O fault is removed.
        restored = self.root / "restored-runtime.sqlite3"
        shutil.copyfile(recovery / "original.sqlite3", restored)
        shutil.copyfile(recovery / "original.sqlite3-journal", restored.with_name(restored.name + "-journal"))
        reopened = RuntimeStore(restored)
        for model in models:
            self.assertEqual(reopened.get(type(model), model.id), Versioned(model, 2))
        self.assertEqual(reopened.list(Pool, limit=1000), (Versioned(models[1], 2),))
        self.assert_integrity(restored)
        self.assertEqual((recovery / "original.sqlite3").read_bytes(), original_database)
        self.assertEqual((recovery / "original.sqlite3-journal").read_bytes(), original_journal)

    def test_backlog_and_tracked_untracked_or_ignored_git_destinations_are_rejected(self) -> None:
        backlog = self.root / "planning/backlog.sqlite3"
        backlog.parent.mkdir()
        shutil.copyfile(ROOT / "planning/backlog.sqlite3", backlog)
        before_backlog = backlog.read_bytes()
        with self.assertRaises(UnsafeStoragePath):
            RuntimeStore(backlog)
        self.assertEqual(backlog.read_bytes(), before_backlog)

        for name in ("backlog.sqlite3-wal", "backlog.sqlite3-shm", "backlog.sqlite3-journal",
                     "backlog.sqlite3.maintenance.lock", "backlog.sqlite3.storage.lock"):
            with self.subTest(reserved_sidecar=name):
                reserved = self.root / name
                reserved.write_bytes(b"synthetic existing ledger companion")
                before = reserved.read_bytes()
                with self.assertRaises(UnsafeStoragePath):
                    RuntimeStore(reserved)
                self.assertEqual(reserved.read_bytes(), before)
        for name in (".git", "COM\u00b9.sqlite3", "LPT\u00b2.sqlite3"):
            with self.subTest(reserved_destination=name):
                reserved = self.root / name
                with self.assertRaises(UnsafeStoragePath):
                    RuntimeStore(reserved)
                self.assertFalse(reserved.exists())

        project = self.root / "source-project"
        subprocess.run(["git", "init", "--quiet", str(project)], capture_output=True, text=True, check=True, timeout=10)
        tracked = project / "tracked.sqlite3"
        tracked.write_bytes(b"synthetic tracked sentinel")
        (project / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "tracked.sqlite3", ".gitignore"],
                       capture_output=True, text=True, check=True, timeout=10)
        for path in (tracked, project / "untracked/runtime.sqlite3", project / "ignored/runtime.sqlite3"):
            with self.subTest(destination=path.relative_to(project).as_posix()):
                before = path.read_bytes() if path.exists() else None
                parent_existed = path.parent.exists()
                with self.assertRaises(UnsafeStoragePath):
                    RuntimeStore(path)
                self.assertEqual(path.read_bytes() if path.exists() else None, before)
                self.assertEqual(path.parent.exists(), parent_existed)

    def test_symlink_file_and_parent_destinations_are_rejected_without_touching_targets(self) -> None:
        self.seeded_store()
        original = self.database.read_bytes()
        alias = self.root / "runtime-alias.sqlite3"
        directory_alias = self.root / "linked-parent"
        try:
            alias.symlink_to(self.database)
            directory_alias.symlink_to(self.database.parent, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symbolic links unavailable: {error}")
        for path in (alias, directory_alias / "new-runtime.sqlite3"):
            with self.subTest(destination=path.name):
                with self.assertRaises(UnsafeStoragePath):
                    RuntimeStore(path)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertFalse((self.database.parent / "new-runtime.sqlite3").exists())

    def test_resolved_git_alias_is_rejected_before_creating_any_parent_directory(self) -> None:
        project = self.root / "alias-target-project"
        (project / ".git").mkdir(parents=True)
        requested = self.root / "external-alias" / "not-created" / "runtime.sqlite3"
        canonical = project / "not-created" / "runtime.sqlite3"
        real_resolve = Path.resolve

        def resolve_alias(path, strict=False):
            return canonical if path == requested else real_resolve(path, strict=strict)

        with mock.patch.object(Path, "resolve", new=resolve_alias):
            with self.assertRaises(UnsafeStoragePath):
                paths.validate_database_path(requested, create_parent=True)
        self.assertFalse(requested.parent.exists())
        self.assertFalse(canonical.parent.exists())

    def test_hardlinked_database_alias_is_rejected_and_original_bytes_are_preserved(self) -> None:
        self.seeded_store()
        original = self.database.read_bytes()
        alias = self.root / "hardlinked-runtime.sqlite3"
        try:
            alias.hardlink_to(self.database)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Hard links unavailable: {error}")
        for path in (alias, self.database):
            with self.subTest(destination=path.name):
                with self.assertRaises(UnsafeStoragePath):
                    RuntimeStore(path)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(alias.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
