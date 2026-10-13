"""AIK-037 evidence relay tests with synthetic files in disposable directories.

No provider, credential, network, VM or AGS installation is used. Passing on
Linux is development verification, not evidence of Windows support.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_evidence import (  # noqa: E402
    EvidenceConflict, EvidenceError, EvidenceExportExists, EvidenceIntegrityError,
    EvidencePathRejected, EvidenceRelay, EvidenceUnavailable, InvalidEvidenceDatabase,
    redact_text_artifact,
)
from aik_runtime import RuntimeStore, UnsafeStoragePath  # noqa: E402
from aik_security import OutputRedactor  # noqa: E402


FIXED = datetime(2030, 1, 1, tzinfo=timezone.utc)
ORIGINAL = "합성 결과 v1\n".encode("utf-8")
SECRET = "SYNTHETIC-SECRET-VALUE-0123456789"


def clock() -> datetime:
    return FIXED


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class EvidenceRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-evidence-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.database = self.base / "증거 원장" / "evidence relay.sqlite3"
        self.managed = self.base / "관리 보관소"
        self.workspace = self.base / "작업 공간 A"
        (self.workspace / "out").mkdir(parents=True)
        self.artifact = self.workspace / "out" / "report.txt"
        self.artifact.write_bytes(ORIGINAL)
        self.other = self.base / "작업 공간 B"
        self.other.mkdir()
        (self.other / "secret-plan.txt").write_bytes(b"other workspace bytes")

    def relay(self) -> EvidenceRelay:
        return EvidenceRelay(self.database, managed_root=self.managed, clock=clock)

    def collect(self, relay: EvidenceRelay, callback_id: str = "callback-1", **overrides):
        arguments = dict(workspace_id="workspace-a", workspace_root=self.workspace,
                         relative_path="out/report.txt", vm_attempt_id="vm-attempt-1",
                         lease_id="lease-1", max_bytes=4096)
        arguments.update(overrides)
        return relay.collect(callback_id, **arguments)

    def object_path(self, record) -> Path:
        return self.managed.resolve() / "sha256" / record.sha256.split(":", 1)[1]

    def test_collect_records_identity_digest_attempt_scope_and_survives_restart(self) -> None:
        relay = self.relay()
        record = self.collect(relay)
        self.assertEqual(record.kind, "authoritative")
        self.assertEqual(record.sha256, digest(ORIGINAL))
        self.assertEqual(record.size, len(ORIGINAL))
        self.assertEqual((record.vm_attempt_id, record.lease_id, record.callback_id),
                         ("vm-attempt-1", "lease-1", "callback-1"))
        self.assertEqual((record.workspace_id, record.relative_path, record.access_scope),
                         ("workspace-a", "out/report.txt", "managed"))
        self.assertIsNone(record.derived_from)
        self.assertEqual(record.recorded_at, "2030-01-01T00:00:00Z")
        self.assertEqual(self.object_path(record).read_bytes(), ORIGINAL)
        self.assertTrue(relay.verify(record.artifact_id).intact)

        # Worker output disappears after the session ends; managed bytes remain.
        self.artifact.unlink()
        reopened = self.relay()
        self.assertEqual(reopened.records(), (record,))
        self.assertEqual(reopened.records(vm_attempt_id="vm-attempt-1"), (record,))
        self.assertEqual(reopened.records(vm_attempt_id="vm-attempt-other"), ())
        result = reopened.verify(record.artifact_id)
        self.assertEqual((result.status, result.observed_sha256), ("intact", record.sha256))
        self.assertEqual(reopened.read_bytes(record.artifact_id), ORIGINAL)

    def test_duplicate_callback_with_same_result_is_deduplicated(self) -> None:
        relay = self.relay()
        first = self.collect(relay)
        again = self.collect(relay)
        self.assertEqual(first, again)
        self.assertEqual(relay.records(), (first,))
        self.assertEqual(relay.conflicts(), ())
        second_callback = self.collect(relay, "callback-2")
        self.assertNotEqual(second_callback.artifact_id, first.artifact_id)
        self.assertEqual(second_callback.sha256, first.sha256)
        self.assertEqual(len(relay.records()), 2)
        objects = list((self.managed.resolve() / "sha256").iterdir())
        self.assertEqual(objects, [self.object_path(first)])

    def test_same_callback_with_different_digest_or_metadata_conflicts_without_overwrite(self) -> None:
        relay = self.relay()
        original = self.collect(relay)
        changed = "합성 결과 v2 (변경됨)\n".encode("utf-8")
        self.artifact.write_bytes(changed)
        with self.assertRaises(EvidenceConflict) as raised:
            self.collect(relay)
        conflict = raised.exception.conflict
        self.assertEqual(raised.exception.code, "CALLBACK_CONFLICT")
        self.assertIn("sha256", conflict.fields)
        self.assertEqual((conflict.existing_sha256, conflict.observed_sha256),
                         (original.sha256, digest(changed)))
        self.assertEqual(relay.records(), (original,))
        self.assertTrue(relay.verify(original.artifact_id).intact)
        self.assertEqual(self.object_path(original).read_bytes(), ORIGINAL)
        self.assertFalse((self.managed.resolve() / "sha256" / digest(changed)[7:]).exists())

        self.artifact.write_bytes(ORIGINAL)
        with self.assertRaises(EvidenceConflict) as raised:
            self.collect(relay, lease_id="lease-2")
        self.assertEqual(raised.exception.conflict.fields, ("lease_id",))
        reopened = self.relay()
        self.assertEqual([c.code for c in reopened.conflicts()], ["CALLBACK_CONFLICT"] * 2)
        self.assertEqual(reopened.records(), (original,))

    def test_other_workspace_absolute_traversal_and_git_paths_are_rejected(self) -> None:
        relay = self.relay()
        record = self.collect(relay)
        for path in ("/etc/passwd", str(self.other / "secret-plan.txt"), "../작업 공간 B/secret-plan.txt",
                     "out/../../x", ".git/config", "C:/x", "out//report.txt"):
            with self.subTest(path=path):
                with self.assertRaises(EvidencePathRejected) as raised:
                    self.collect(relay, "callback-path", relative_path=path)
                self.assertNotIn(str(self.other), str(raised.exception))
        with self.assertRaises(EvidencePathRejected):
            self.collect(relay, "callback-foreign", workspace_root=self.other, relative_path="secret-plan.txt")
        with self.assertRaises(EvidencePathRejected):
            self.collect(relay, "callback-b", workspace_id="workspace-b")
        with self.assertRaises(EvidencePathRejected):
            self.collect(relay, "callback-c", workspace_id="workspace-c",
                         workspace_root=self.workspace / "out", relative_path="report.txt")
        with self.assertRaises(EvidenceUnavailable) as raised:
            self.collect(relay, "callback-missing", relative_path="out/absent.txt")
        self.assertEqual(raised.exception.condition, "missing")
        self.assertEqual(relay.records(), (record,))
        self.assertEqual(relay.conflicts(), ())

        nested = EvidenceRelay(self.base / "nested.sqlite3", managed_root=self.workspace / "store", clock=clock)
        with self.assertRaises(EvidencePathRejected):
            self.collect(nested, "callback-nested")

    def test_symlinked_files_directories_and_roots_are_rejected(self) -> None:
        relay = self.relay()
        file_link = self.workspace / "out" / "linked.txt"
        directory_link = self.workspace / "other-link"
        root_link = self.base / "workspace-alias"
        try:
            file_link.symlink_to(self.other / "secret-plan.txt")
            directory_link.symlink_to(self.other, target_is_directory=True)
            root_link.symlink_to(self.other, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symbolic links cannot be created here: {type(error).__name__}")
        with self.assertRaises((EvidencePathRejected, EvidenceUnavailable)):
            self.collect(relay, "callback-file-link", relative_path="out/linked.txt")
        with self.assertRaises((EvidencePathRejected, EvidenceUnavailable)):
            self.collect(relay, "callback-dir-link", relative_path="other-link/secret-plan.txt")
        with self.assertRaises(EvidencePathRejected):
            self.collect(relay, "callback-root-link", workspace_id="workspace-alias",
                         workspace_root=root_link, relative_path="secret-plan.txt")
        self.assertEqual(relay.records(), ())

    def test_tampered_or_missing_managed_bytes_are_detected_after_restart(self) -> None:
        record = self.collect(self.relay())
        stored = self.object_path(record)
        stored.chmod(0o644)
        stored.write_bytes(b"tampered bytes")
        reopened = self.relay()
        tampered = reopened.verify(record.artifact_id)
        self.assertEqual(tampered.status, "tampered")
        self.assertEqual(tampered.observed_sha256, digest(b"tampered bytes"))
        with self.assertRaises(EvidenceIntegrityError):
            reopened.read_bytes(record.artifact_id)
        stored.unlink()
        missing = self.relay().verify(record.artifact_id)
        self.assertEqual((missing.status, missing.observed_sha256), ("missing", None))
        with self.assertRaises(EvidenceIntegrityError):
            self.relay().export(record.artifact_id, self.base)

    def test_redacted_derivative_has_its_own_digest_and_the_source_is_unchanged(self) -> None:
        self.artifact.write_bytes(("결과 로그\ntoken=" + SECRET + "\n").encode("utf-8"))
        relay = self.relay()
        source = self.collect(relay)
        derived = redact_text_artifact(relay, source.artifact_id, OutputRedactor((SECRET,)))
        self.assertEqual((derived.kind, derived.derived_from), ("derivative", source.artifact_id))
        self.assertIsNotNone(derived.transform)
        self.assertNotEqual(derived.sha256, source.sha256)
        self.assertNotEqual(derived.artifact_id, source.artifact_id)
        shown = relay.read_bytes(derived.artifact_id).decode("utf-8")
        self.assertNotIn(SECRET, shown)
        self.assertIn(SECRET, relay.read_bytes(source.artifact_id).decode("utf-8"))
        self.assertTrue(relay.verify(source.artifact_id).intact)
        self.assertEqual(relay.get(source.artifact_id), source)
        again = redact_text_artifact(relay, source.artifact_id, OutputRedactor((SECRET,)))
        self.assertEqual(again, derived)
        self.assertEqual(len(relay.records()), 2)

    def test_non_utf8_artifact_is_not_redacted(self) -> None:
        self.artifact.write_bytes(b"\xff\xfe binary")
        relay = self.relay()
        source = self.collect(relay)
        with self.assertRaises(EvidenceError):
            redact_text_artifact(relay, source.artifact_id, OutputRedactor((SECRET,)))
        self.assertEqual(relay.records(), (source,))

    def test_export_writes_a_new_verified_file_and_never_overwrites(self) -> None:
        relay = self.relay()
        record = self.collect(relay)
        destination = self.base / "내보내기 폴더"
        destination.mkdir()
        saved = relay.export(record.artifact_id, destination)
        self.assertEqual(saved.read_bytes(), ORIGINAL)
        self.assertEqual(digest(saved.read_bytes()), record.sha256)
        with self.assertRaises(EvidenceExportExists):
            relay.export(record.artifact_id, destination)
        self.assertEqual(saved.read_bytes(), ORIGINAL)
        renamed = relay.export(record.artifact_id, destination, filename="복사본.txt")
        self.assertEqual(renamed.read_bytes(), ORIGINAL)
        for name in ("../escape.txt", "sub/name.txt", ".git"):
            with self.subTest(name=name):
                with self.assertRaises(EvidencePathRejected):
                    relay.export(record.artifact_id, destination, filename=name)
        with self.assertRaises(EvidencePathRejected):
            relay.export(record.artifact_id, self.managed)

    def test_records_are_append_only_and_foreign_databases_are_refused(self) -> None:
        relay = self.relay()
        self.collect(relay)
        with closing(sqlite3.connect(self.database)) as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                # Row triggers fire only when a row exists; empty tables are skipped.
                if table.startswith("sqlite_") or not connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                    continue
                with self.subTest(table=table):
                    with self.assertRaises(sqlite3.DatabaseError):
                        connection.execute(f'DELETE FROM "{table}"')
        foreign = self.base / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE t (x)")
            connection.commit()
        with self.assertRaises(InvalidEvidenceDatabase):
            EvidenceRelay(foreign, managed_root=self.managed, clock=clock).records()


if __name__ == "__main__":
    unittest.main()
