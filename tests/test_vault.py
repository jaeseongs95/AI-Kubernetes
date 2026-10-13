"""AIK-009 vault tests with synthetic secrets and disposable directories.

No real credential, provider or network is used. Real Windows DPAPI is only
exercised by Windows-only tests, which are NOT_RUN on Linux. Passing this suite
on Linux is development verification, not evidence of Windows support.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_runtime import CredentialRef, Pool, RuntimeStore  # noqa: E402
from aik_runtime import paths as runtime_paths  # noqa: E402
from aik_security import OutputRedactor  # noqa: E402
from aik_vault import (  # noqa: E402
    CRYPTPROTECT_LOCAL_MACHINE, CRYPTPROTECT_UI_FORBIDDEN, MAX_SECRET_BYTES, REF_PREFIX,
    CredentialNotFound, DpapiCallError, DpapiVaultBackend, FakeVaultBackend,
    InvalidCredentialRef, InvalidSecret, UnsafeVaultPath, VaultAccessDenied, VaultBusy,
    VaultCorrupt, VaultDurabilityUnconfirmed, VaultError, VaultStorageError, VaultUnavailable,
    default_vault_directory, open_user_vault,
)
from aik_vault.dpapi import _Crypt32Api  # noqa: E402


SECRET = "sk-SYNTHETICvault0123456789abcdefTEST"
REPLACEMENT = "sk-SYNTHETICrotated9876543210zyxwvuTEST"
_REF_PATTERN = re.compile(r"secret://aik-vault/[0-9a-f]{32}\Z")
RESTART_CHILD = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from aik_vault import FakeVaultBackend
vault = FakeVaultBackend(Path(sys.argv[2]), key=bytes.fromhex(sys.argv[3]))
print(vault.put(sys.stdin.read()))
'''
DPAPI_RESTART_CHILD = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from aik_vault import open_user_vault
print(open_user_vault(Path(sys.argv[2])).put(sys.stdin.read()))
'''


def secret_variants(secret: str) -> list[bytes]:
    raw = secret.encode("utf-8")
    variants = [raw, secret.encode("utf-16-le"), raw.hex().encode(), raw.hex().upper().encode()]
    for encoded in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        variants.extend((encoded, encoded.rstrip(b"=")))
    return variants


def _running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


class RecordingDpapi:
    """Mock DPAPI binding; records flags/entropy and never touches Windows."""

    def __init__(self):
        self.calls = []
        self.fail_protect = None
        self.fail_unprotect = None

    def protect(self, data, entropy, flags):
        self.calls.append(("protect", bytes(entropy), flags))
        if self.fail_protect is not None:
            raise DpapiCallError("protect", self.fail_protect)
        return b"MOCKDPAPI" + bytes(b ^ 0x5A for b in data) + hashlib.sha256(entropy).digest()

    def unprotect(self, blob, entropy, flags):
        self.calls.append(("unprotect", bytes(entropy), flags))
        if self.fail_unprotect is not None:
            raise DpapiCallError("unprotect", self.fail_unprotect)
        if not blob.startswith(b"MOCKDPAPI") or blob[-32:] != hashlib.sha256(entropy).digest():
            raise DpapiCallError("unprotect", 13)
        return bytearray(b ^ 0x5A for b in blob[9:-32])


class VaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-vault-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "사용자 vault"
        self.key = os.urandom(32)
        self.vault = FakeVaultBackend(self.directory, key=self.key)

    def ciphertext(self, ref: str, vault=None) -> Path:
        return (vault or self.vault).directory / (ref.rsplit("/", 1)[1] + ".aikv")

    def ciphertext_files(self) -> list[Path]:
        return sorted(self.vault.directory.glob("*.aikv"))

    def assert_no_secret(self, text: str) -> None:
        for secret in (SECRET, REPLACEMENT):
            self.assertNotIn(secret, text)

    # -- CRUD / restart ---------------------------------------------------
    def test_put_returns_opaque_reference_accepted_by_runtime_metadata(self) -> None:
        first = self.vault.put(SECRET)
        second = self.vault.put(SECRET)
        for ref in (first, second):
            self.assertRegex(ref, _REF_PATTERN)
            self.assert_no_secret(ref)
            CredentialRef("credential-test", ref)  # runtime locator contract
        self.assertNotEqual(first, second)
        self.assertEqual(self.vault.resolve(first), SECRET)
        self.assertEqual(self.vault.resolve(second), SECRET)
        self.assertEqual(len(self.ciphertext_files()), 2)

    def test_replace_overwrites_ciphertext_and_failed_replace_keeps_previous(self) -> None:
        ref = self.vault.put(SECRET)
        before = self.ciphertext(ref).read_bytes()
        self.vault.replace(ref, REPLACEMENT)
        self.assertEqual(self.vault.resolve(ref), REPLACEMENT)
        self.assertNotEqual(self.ciphertext(ref).read_bytes(), before)
        with self.assertRaises(InvalidSecret):
            self.vault.replace(ref, "")
        self.assertEqual(self.vault.resolve(ref), REPLACEMENT)
        self.assertEqual(len(self.ciphertext_files()), 1)
        self.assertEqual(list(self.vault.directory.glob(".aikv-*")), [])

    def test_delete_removes_reference_and_missing_operations_report_not_found(self) -> None:
        ref = self.vault.put(SECRET)
        self.vault.delete(ref)
        self.assertFalse(self.ciphertext(ref).exists())
        with self.assertRaises(CredentialNotFound):
            self.vault.resolve(ref)
        with self.assertRaises(CredentialNotFound):
            self.vault.delete(ref)
        with self.assertRaises(CredentialNotFound):
            self.vault.replace(ref, REPLACEMENT)
        self.assertEqual(self.ciphertext_files(), [])

    def test_reference_survives_actual_process_restart(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", RESTART_CHILD, str(ROOT / "src"), str(self.directory), self.key.hex()],
            input=SECRET, capture_output=True, text=True, encoding="utf-8", check=True, timeout=60, cwd=self.root,
        )
        self.assert_no_secret(result.stdout + result.stderr)
        ref = result.stdout.strip()
        self.assertRegex(ref, _REF_PATTERN)
        reopened = FakeVaultBackend(self.directory, key=self.key)
        self.assertEqual(reopened.resolve(ref), SECRET)
        reopened.replace(ref, REPLACEMENT)
        self.assertEqual(FakeVaultBackend(self.directory, key=self.key).resolve(ref), REPLACEMENT)

    # -- error classification ---------------------------------------------
    def test_other_user_key_and_swapped_ciphertext_are_access_denied(self) -> None:
        ref = self.vault.put(SECRET)
        other_user = FakeVaultBackend(self.directory, key=os.urandom(32))
        with self.assertRaises(VaultAccessDenied) as denied:
            other_user.resolve(ref)
        self.assert_no_secret(str(denied.exception))
        self.assertEqual(denied.exception.code, "VAULT_ACCESS_DENIED")

        other_ref = self.vault.put(REPLACEMENT)
        shutil.copyfile(self.ciphertext(ref), self.ciphertext(other_ref))
        with self.assertRaises(VaultAccessDenied):
            self.vault.resolve(other_ref)  # blob is bound to its own reference

        path = self.ciphertext(ref)
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0x01
        path.write_bytes(bytes(data))
        with self.assertRaises(VaultAccessDenied):
            self.vault.resolve(ref)

    def test_invalid_references_are_rejected_before_file_access(self) -> None:
        for value in ("secret://aik-vault/../credentials", REF_PREFIX + "A" * 32, "secret://other/" + "0" * 32,
                      "0" * 32, REF_PREFIX + "0" * 31, REF_PREFIX + "0" * 32 + "/x", None, 123, b"secret"):
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(InvalidCredentialRef) as rejected:
                    self.vault.resolve(value)
                self.assertEqual(rejected.exception.code, "INVALID_CREDENTIAL_REF")
                with self.assertRaises(InvalidCredentialRef):
                    self.vault.delete(value)
                with self.assertRaises(InvalidCredentialRef):
                    self.vault.replace(value, SECRET)

    def test_invalid_secrets_are_rejected_without_echoing_the_value(self) -> None:
        for value in ("", None, SECRET.encode(), "x" * (MAX_SECRET_BYTES + 1), SECRET + "\ud800",
                      "가" * (MAX_SECRET_BYTES // 3 + 1)):
            with self.subTest(kind=type(value).__name__, length=len(value) if value is not None else 0):
                with self.assertRaises(InvalidSecret) as rejected:
                    self.vault.put(value)
                error = rejected.exception
                self.assertIsNone(error.__context__)
                self.assertIsNone(error.__cause__)
                self.assert_no_secret(str(error) + repr(error) + "".join(traceback.format_exception(error)))
        self.assertEqual(self.ciphertext_files(), [])

    def test_damaged_or_foreign_ciphertext_is_reported_as_corrupt(self) -> None:
        ref = self.vault.put(SECRET)
        path = self.ciphertext(ref)
        original = path.read_bytes()
        cases = (
            ("magic", b"XXXX" + original[4:]),
            ("truncated", original[:5]),
            ("foreign-backend", original[:5] + b"\x01" + original[6:]),
            ("short-body", original[:6] + b"\0" * 10),
            ("oversized", original[:6] + b"\0" * (1 << 20)),
        )
        for label, damaged in cases:
            with self.subTest(case=label):
                path.write_bytes(damaged)
                with self.assertRaises(VaultCorrupt) as corrupt:
                    self.vault.resolve(ref)
                self.assertEqual(corrupt.exception.code, "VAULT_CORRUPT")
        path.write_bytes(original)
        self.assertEqual(self.vault.resolve(ref), SECRET)

    def test_lock_timeout_reports_busy_without_writing(self) -> None:
        waiting = FakeVaultBackend(self.directory, key=self.key, timeout=0.05)
        ref = self.vault.put(SECRET)
        with runtime_paths.database_lock(self.vault._anchor):
            with self.assertRaises(VaultBusy):
                waiting.put(REPLACEMENT)
            with self.assertRaises(VaultBusy):
                waiting.replace(ref, REPLACEMENT)
        self.assertEqual(len(self.ciphertext_files()), 1)
        self.assertEqual(waiting.resolve(ref), SECRET)

    def test_lock_timeout_reports_busy_for_delete_and_resolve(self) -> None:
        # F4(c): delete/resolve also stop at the lock and leave the ciphertext.
        waiting = FakeVaultBackend(self.directory, key=self.key, timeout=0.05)
        ref = self.vault.put(SECRET)
        with runtime_paths.database_lock(self.vault._anchor):
            for label, operation in (("resolve", lambda: waiting.resolve(ref)),
                                     ("delete", lambda: waiting.delete(ref))):
                with self.subTest(operation=label):
                    with self.assertRaises(VaultBusy) as busy:
                        operation()
                    self.assertEqual(busy.exception.code, "VAULT_BUSY")
        self.assertTrue(self.ciphertext(ref).is_file())
        self.assertEqual(waiting.resolve(ref), SECRET)

    def test_unsafe_directories_and_symlinked_ciphertext_are_rejected(self) -> None:
        with self.assertRaises(UnsafeVaultPath):
            FakeVaultBackend(Path("relative-vault"), key=self.key)
        project = self.root / "project"
        (project / ".git").mkdir(parents=True)
        with self.assertRaises(UnsafeVaultPath):
            FakeVaultBackend(project / "vault", key=self.key)
        self.assertFalse((project / "vault").exists())
        with self.assertRaises(UnsafeVaultPath):
            FakeVaultBackend(self.root / ".git" / "vault", key=self.key)

        ref = self.vault.put(SECRET)
        path = self.ciphertext(ref)
        moved = self.root / "elsewhere.aikv"
        shutil.move(path, moved)
        try:
            path.symlink_to(moved)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symbolic links unavailable: {error}")
        with self.assertRaises(UnsafeVaultPath):
            self.vault.resolve(ref)
        with self.assertRaises(UnsafeVaultPath):
            self.vault.delete(ref)
        self.assertTrue(moved.is_file())

    def test_hardlinked_ciphertext_is_rejected_and_preserved(self) -> None:
        # F4(d): a hardlink alias of a ciphertext file is a path-rule violation.
        ref = self.vault.put(SECRET)
        path = self.ciphertext(ref)
        original = path.read_bytes()
        alias = self.root / "hardlinked.aikv"
        try:
            alias.hardlink_to(path)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Hard links unavailable: {error}")
        for label, operation in (("resolve", lambda: self.vault.resolve(ref)),
                                 ("replace", lambda: self.vault.replace(ref, REPLACEMENT)),
                                 ("delete", lambda: self.vault.delete(ref))):
            with self.subTest(operation=label):
                with self.assertRaises(UnsafeVaultPath) as rejected:
                    operation()
                self.assertEqual(rejected.exception.code, "VAULT_UNSAFE_PATH")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(alias.read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits; Windows ACL inheritance is NOT_RUN.")
    def test_ciphertext_files_are_private_on_posix(self) -> None:
        ref = self.vault.put(SECRET)
        self.assertEqual(self.ciphertext(ref).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.vault.directory.stat().st_mode & 0o777, 0o700)

    @unittest.skipIf(os.name == "nt" or _running_as_root(),
                     "POSIX permission denial; root bypasses mode bits and Windows ACL denial is NOT_RUN.")
    def test_directory_permission_denial_reports_access_denied_not_unsafe_path(self) -> None:
        # F1: an inaccessible vault directory is an access failure, not an unsafe path.
        ref = self.vault.put(SECRET)
        os.chmod(self.vault.directory, 0)
        self.addCleanup(os.chmod, self.vault.directory, 0o700)
        operations = (
            ("resolve", lambda: self.vault.resolve(ref)),
            ("delete", lambda: self.vault.delete(ref)),
            ("replace", lambda: self.vault.replace(ref, REPLACEMENT)),
            ("put", lambda: self.vault.put(REPLACEMENT)),
            ("open", lambda: FakeVaultBackend(self.directory, key=self.key)),
        )
        for label, operation in operations:
            with self.subTest(operation=label):
                with self.assertRaises(VaultAccessDenied) as denied:
                    operation()
                self.assertEqual(denied.exception.code, "VAULT_ACCESS_DENIED")
                self.assert_no_secret(str(denied.exception)
                                      + "".join(traceback.format_exception(denied.exception)))
        os.chmod(self.vault.directory, 0o700)
        self.assertEqual(self.vault.resolve(ref), SECRET)
        self.assertEqual(len(self.ciphertext_files()), 1)

    @unittest.skipIf(os.name == "nt", "The directory chmod is POSIX-only.")
    def test_directory_chmod_permission_denial_reports_access_denied(self) -> None:
        # F1: chmod of a directory owned by another user fails with EPERM.
        with mock.patch.object(os, "chmod", side_effect=PermissionError(errno.EPERM, "synthetic")):
            with self.assertRaises(VaultAccessDenied) as denied:
                FakeVaultBackend(self.directory, key=self.key)
        self.assertEqual(denied.exception.code, "VAULT_ACCESS_DENIED")

    def test_file_permission_and_io_failures_map_to_fixed_codes(self) -> None:
        # F4(b): PermissionError -> VAULT_ACCESS_DENIED, other OSError -> VAULT_IO_ERROR.
        ref = self.vault.put(SECRET)
        real_open = os.open

        def failing_open(failure):
            def opener(path, flags, *args, **kwargs):
                if str(path).endswith(".aikv"):
                    raise failure
                return real_open(path, flags, *args, **kwargs)
            return opener

        with mock.patch.object(os, "open", side_effect=failing_open(PermissionError(errno.EACCES, "synthetic"))):
            with self.assertRaises(VaultAccessDenied) as denied:
                self.vault.resolve(ref)
        self.assertEqual(denied.exception.code, "VAULT_ACCESS_DENIED")
        with mock.patch.object(os, "open", side_effect=failing_open(OSError(errno.EIO, "synthetic"))):
            with self.assertRaises(VaultStorageError) as io_error:
                self.vault.resolve(ref)
        self.assertIs(type(io_error.exception), VaultStorageError)
        self.assertEqual(io_error.exception.code, "VAULT_IO_ERROR")

        real_replace = os.replace

        def deny_publication(source, destination):
            if str(destination).endswith(".aikv"):
                raise PermissionError(errno.EACCES, "synthetic sharing violation")
            return real_replace(source, destination)

        with mock.patch.object(os, "replace", side_effect=deny_publication):
            with self.assertRaises(VaultAccessDenied):
                self.vault.put(REPLACEMENT)
            with self.assertRaises(VaultAccessDenied):
                self.vault.replace(ref, REPLACEMENT)
        self.assertEqual(self.vault.resolve(ref), SECRET)
        self.assertEqual(len(self.ciphertext_files()), 1)
        self.assertEqual(list(self.vault.directory.glob(".aikv-*")), [])

    def test_post_publication_sync_failure_reports_unconfirmed_durability_with_reference(self) -> None:
        # F3: the rename/unlink already took effect; this is not a plain I/O failure.
        def failing_sync():
            raise OSError(errno.EIO, "synthetic directory fsync failure")

        with mock.patch.object(self.vault, "_sync_directory", side_effect=failing_sync):
            with self.assertRaises(VaultDurabilityUnconfirmed) as put_failure:
                self.vault.put(SECRET)
        error = put_failure.exception
        self.assertEqual(error.code, "VAULT_DURABILITY_UNCONFIRMED")
        self.assertIsInstance(error, VaultStorageError)
        ref = error.credential_ref
        self.assertRegex(ref, _REF_PATTERN)
        self.assertEqual(self.vault.resolve(ref), SECRET)

        with mock.patch.object(self.vault, "_sync_directory", side_effect=failing_sync):
            with self.assertRaises(VaultDurabilityUnconfirmed) as replace_failure:
                self.vault.replace(ref, REPLACEMENT)
        self.assertEqual(replace_failure.exception.credential_ref, ref)
        self.assertEqual(self.vault.resolve(ref), REPLACEMENT)

        with mock.patch.object(self.vault, "_sync_directory", side_effect=failing_sync):
            with self.assertRaises(VaultDurabilityUnconfirmed) as delete_failure:
                self.vault.delete(ref)
        self.assertEqual(delete_failure.exception.credential_ref, ref)
        with self.assertRaises(CredentialNotFound):
            self.vault.resolve(ref)
        for failure in (error, replace_failure.exception, delete_failure.exception):
            self.assert_no_secret(str(failure) + repr(failure) + "".join(traceback.format_exception(failure)))
        self.assertEqual(list(self.vault.directory.glob(".aikv-*")), [])

    def test_fake_backend_requires_explicit_directory_and_hides_key(self) -> None:
        with self.assertRaises(ValueError):
            FakeVaultBackend(None, key=self.key)
        with self.assertRaises(ValueError):
            FakeVaultBackend(self.directory, key=b"short")
        self.assertNotIn(self.key.hex(), repr(self.vault))
        self.assertNotIn(str(self.directory), repr(self.vault))

    def test_default_directory_reuses_runtime_user_state_rule(self) -> None:
        windows_base, development_base = self.root / "local-app-data", self.root / "xdg-state"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(windows_base), "XDG_STATE_HOME": str(development_base)}):
            observed = default_vault_directory()
        expected = windows_base / "AIK" / "vault" if os.name == "nt" else development_base / "aik" / "vault"
        self.assertEqual(observed, expected)

    # -- plaintext non-detection -------------------------------------------
    def test_plaintext_absent_from_sqlite_settings_logs_support_bundle_and_errors(self) -> None:
        ref = self.vault.put(SECRET)
        self.vault.replace(ref, REPLACEMENT)
        store = RuntimeStore(self.root / "runtime" / "runtime.sqlite3")
        store.save(CredentialRef("credential-api", ref), expected_revision=0)
        store.save(Pool("pool-api", "synthetic", "connection-api", "credential-api"), expected_revision=0)
        settings = self.root / "settings.json"
        settings.write_text(json.dumps({"pool_id": "pool-api", "credential_ref": ref}), encoding="utf-8")

        log_path = self.root / "aik.log"
        handler = logging.FileHandler(log_path, encoding="utf-8")
        audit = logging.getLogger("aik_vault.test-audit")
        package_logger = logging.getLogger("aik_vault")
        for logger in (audit, package_logger):
            logger.addHandler(handler)
        audit.setLevel(logging.INFO)
        audit.propagate = False
        failures = []

        def capture(operation) -> None:
            try:
                operation()
            except VaultError as error:
                failures.append(error)
                audit.warning("vault operation failed code=%s detail=%s repr=%r", error.code, error, error)
                audit.error("vault failure trace", exc_info=error)
                return
            self.fail("A vault error was expected.")

        try:
            capture(lambda: self.vault.resolve(REF_PREFIX + "0" * 32))
            capture(lambda: FakeVaultBackend(self.directory, key=os.urandom(32)).resolve(ref))
            capture(lambda: self.vault.put(SECRET + "\ud800"))
            capture(lambda: self.vault.replace(ref, ""))
            capture(lambda: self.vault.resolve(REF_PREFIX + SECRET))
            audit.info("resolved credential %s for pool %s", ref, "pool-api")
            self.assertEqual(self.vault.resolve(ref), REPLACEMENT)
        finally:
            for logger in (audit, package_logger):
                logger.removeHandler(handler)
            handler.close()

        self.assertEqual({type(error) for error in failures},
                         {CredentialNotFound, VaultAccessDenied, InvalidSecret, InvalidCredentialRef})
        for error in failures:
            self.assert_no_secret(str(error) + repr(error) + "".join(traceback.format_exception(error)))

        redactor = OutputRedactor([SECRET, REPLACEMENT])
        bundle = redactor.output("crash_bundle", {
            "errors": [{"code": error.code, "message": str(error)} for error in failures],
            "credential_ref": ref,
            "log_tail": log_path.read_text(encoding="utf-8")[-4000:],
            "settings": json.loads(settings.read_text(encoding="utf-8")),
        })
        bundle_json = self.root / "bundle.json"
        bundle_json.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        with zipfile.ZipFile(self.root / "support-bundle.zip", "w", compression=zipfile.ZIP_STORED) as archive:
            for path in [settings, log_path, bundle_json, *self.root.joinpath("runtime").iterdir(),
                         *self.vault.directory.iterdir()]:
                if path.is_file():
                    # resolve(): on Windows the temp root may be an 8.3 short name (RUNNER~1).
                    archive.write(path, path.resolve().relative_to(self.root.resolve()).as_posix())

        # Text artifacts: a secret-registered redactor must change nothing that
        # an empty-registry redactor leaves untouched.
        baseline = OutputRedactor()
        for path in (settings, log_path, bundle_json):
            text = path.read_text(encoding="utf-8")
            with self.subTest(text_artifact=path.name):
                self.assertIn("secret://aik-vault/", text) if path is not bundle_json else None
                self.assertEqual(redactor.redact_text(text), baseline.redact_text(text))
        # Every byte artifact, including SQLite, ciphertext, lock files and zip.
        scanned = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                scanned += 1
                for secret in (SECRET, REPLACEMENT):
                    for variant in secret_variants(secret):
                        self.assertNotIn(variant, data, f"{path.name} contains a secret encoding")
        self.assertGreaterEqual(scanned, 6)
        self.assertIn(ref.encode("ascii"), (self.root / "runtime" / "runtime.sqlite3").read_bytes())

    # -- DPAPI (mock only on non-Windows) ------------------------------------
    def test_dpapi_backend_uses_user_scope_flags_and_reference_entropy(self) -> None:
        api = RecordingDpapi()
        vault = DpapiVaultBackend(self.root / "dpapi-vault", _api=api)
        ref = vault.put(SECRET)
        self.assertEqual(vault.resolve(ref), SECRET)
        vault.replace(ref, REPLACEMENT)
        self.assertEqual(DpapiVaultBackend(self.root / "dpapi-vault", _api=api).resolve(ref), REPLACEMENT)
        self.assertEqual([call[0] for call in api.calls], ["protect", "unprotect", "protect", "unprotect"])
        ref_id = ref.rsplit("/", 1)[1].encode("ascii")
        for _operation, entropy, flags in api.calls:
            self.assertEqual(flags & CRYPTPROTECT_LOCAL_MACHINE, 0)
            self.assertEqual(flags & CRYPTPROTECT_UI_FORBIDDEN, CRYPTPROTECT_UI_FORBIDDEN)
            self.assertTrue(entropy.endswith(ref_id))
        data = self.ciphertext(ref, vault).read_bytes()
        self.assertEqual(data[:6], b"AIKV\x01\x01")
        for variant in secret_variants(REPLACEMENT):
            self.assertNotIn(variant, data)
        with self.assertRaises(VaultCorrupt):
            FakeVaultBackend(self.root / "dpapi-vault", key=self.key).resolve(ref)

    def test_ctypes_binding_rejects_machine_scope_flag_before_any_call(self) -> None:
        # F4(a): exercise the real _Crypt32Api guard without loading crypt32.
        api = _Crypt32Api.__new__(_Crypt32Api)
        invoked = []

        def native(*arguments):
            invoked.append(arguments)
            return 1

        for flags in (CRYPTPROTECT_LOCAL_MACHINE, CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN):
            for operation in ("protect", "unprotect"):
                with self.subTest(flags=flags, operation=operation):
                    with self.assertRaises(ValueError):
                        api._call(native, operation, b"synthetic-data", b"synthetic-entropy", flags,
                                  wipe_output=operation == "unprotect")
        self.assertEqual(invoked, [])

    def test_dpapi_failures_map_to_fixed_error_codes(self) -> None:
        api = RecordingDpapi()
        vault = DpapiVaultBackend(self.root / "dpapi-vault", _api=api)
        ref = vault.put(SECRET)
        api.fail_unprotect = 13  # ERROR_INVALID_DATA: other user or damaged blob
        with self.assertRaises(VaultAccessDenied) as denied:
            vault.resolve(ref)
        self.assertEqual(denied.exception.code, "VAULT_ACCESS_DENIED")
        api.fail_unprotect = None
        api.fail_protect = 5
        with self.assertRaises(VaultUnavailable) as unavailable:
            vault.put(REPLACEMENT)
        self.assertEqual(unavailable.exception.code, "VAULT_UNAVAILABLE")
        with self.assertRaises(VaultUnavailable):
            vault.replace(ref, REPLACEMENT)
        api.fail_protect = None
        self.assertEqual(vault.resolve(ref), SECRET)
        self.assertEqual(len(list(vault.directory.glob("*.aikv"))), 1)
        self.assertEqual(list(vault.directory.glob(".aikv-*")), [])
        for error in (denied.exception, unavailable.exception):
            self.assert_no_secret(str(error) + "".join(traceback.format_exception(error)))

    @unittest.skipIf(os.name == "nt", "On Windows the real backend is available.")
    def test_dpapi_backend_is_unavailable_outside_windows_without_fallback(self) -> None:
        with self.assertRaises(VaultUnavailable):
            DpapiVaultBackend(self.root / "dpapi-vault")
        with self.assertRaises(VaultUnavailable):
            open_user_vault(self.root / "dpapi-vault")
        self.assertFalse((self.root / "dpapi-vault").exists())

    @unittest.skipUnless(os.name == "nt", "Real Windows DPAPI round trip is NOT_RUN outside Windows.")
    def test_real_dpapi_round_trip_on_windows(self) -> None:
        directory = self.root / "real-dpapi"
        vault = DpapiVaultBackend(directory)
        ref = vault.put(SECRET)
        self.assertEqual(vault.resolve(ref), SECRET)
        vault.replace(ref, REPLACEMENT)
        self.assertEqual(DpapiVaultBackend(directory).resolve(ref), REPLACEMENT)
        data = self.ciphertext(ref, vault).read_bytes()
        for variant in secret_variants(REPLACEMENT):
            self.assertNotIn(variant, data)
        vault.delete(ref)
        with self.assertRaises(CredentialNotFound):
            vault.resolve(ref)

    @unittest.skipUnless(os.name == "nt", "Real DPAPI process restart is NOT_RUN outside Windows.")
    def test_real_dpapi_reference_survives_actual_process_restart_on_windows(self) -> None:
        # F4(e): a separate child process protects; this process unprotects.
        directory = self.root / "real-dpapi-restart"
        result = subprocess.run(
            [sys.executable, "-c", DPAPI_RESTART_CHILD, str(ROOT / "src"), str(directory)],
            input=SECRET, capture_output=True, text=True, encoding="utf-8", check=True, timeout=60, cwd=self.root,
        )
        self.assert_no_secret(result.stdout + result.stderr)
        ref = result.stdout.strip()
        self.assertRegex(ref, _REF_PATTERN)
        reopened = open_user_vault(directory)
        self.assertEqual(reopened.resolve(ref), SECRET)
        reopened.delete(ref)
        with self.assertRaises(CredentialNotFound):
            reopened.resolve(ref)


if __name__ == "__main__":
    unittest.main()
