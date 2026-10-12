"""Fault-injection unit tests and separately labelled Windows native probes."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import ctypes
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from aik_spike import vault


class _TestDpapi:
    """Unit-test double, explicitly not evidence of Windows DPAPI execution."""

    def __init__(self, location: Path, *, matches: bool = True) -> None:
        self.location = location
        self.matches = matches
        self.seen_payload: bytearray | None = None

    def local_app_data(self) -> Path:
        return self.location

    def protect_synthetic(self, payload: bytearray) -> bytes:
        self.seen_payload = payload
        return b"UNIT-TEST-DOUBLE-ENCRYPTED-BLOB"

    def matches_synthetic(self, ciphertext: bytes, expected: bytearray) -> bool:
        if ciphertext != b"UNIT-TEST-DOUBLE-ENCRYPTED-BLOB":
            raise AssertionError("wrong test ciphertext")
        if expected != vault._SYNTHETIC_PAYLOAD:
            raise AssertionError("wrong synthetic input")
        return self.matches


class VaultContractUnitTests(unittest.TestCase):
    def test_non_windows_returns_not_run_without_loading_native_or_touching_store(self) -> None:
        with patch.object(vault.sys, "platform", "linux"), patch.object(vault, "_Dpapi") as native, patch.object(vault, "_vault_parent") as storage:
            self.assertEqual(vault.roundtrip(), {
                "status": "NOT_RUN", "backend": "windows-user-dpapi", "code": "WINDOWS_REQUIRED",
            })
        native.assert_not_called()
        storage.assert_not_called()

    def test_public_probe_rejects_supplied_secret_and_path(self) -> None:
        with self.assertRaises(TypeError):
            vault.roundtrip(secret="never-accepted")
        with self.assertRaises(TypeError):
            vault.roundtrip(Path("arbitrary-path"))

    def test_unit_roundtrip_wipes_payload_and_removes_only_own_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            existing = base.joinpath(*vault._STORE_PARTS, "prior-run")
            existing.mkdir(parents=True)
            marker = existing / "leave-this-alone.dpapi"
            marker.write_bytes(b"prior-encrypted-data")
            fake = _TestDpapi(base)
            with patch.object(vault.sys, "platform", "win32"), patch.object(vault, "_Dpapi", return_value=fake):
                outcome = vault.roundtrip()
            self.assertEqual(outcome, {"status": "PASS", "backend": "windows-user-dpapi", "code": "VAULT_ROUNDTRIP_OK"})
            self.assertEqual(fake.seen_payload, bytearray(len(vault._SYNTHETIC_PAYLOAD)))
            self.assertEqual(list(existing.parent.iterdir()), [existing])
            self.assertEqual(marker.read_bytes(), b"prior-encrypted-data")
            self.assertNotIn(str(base), json.dumps(outcome))

    def test_unit_mismatch_is_failure_and_still_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = _TestDpapi(base, matches=False)
            with patch.object(vault.sys, "platform", "win32"), patch.object(vault, "_Dpapi", return_value=fake):
                outcome = vault.roundtrip()
            self.assertEqual(outcome["code"], "VAULT_VERIFICATION_FAILED")
            self.assertEqual(outcome["status"], "FAIL")
            self.assertEqual(list(base.joinpath(*vault._STORE_PARTS).iterdir()), [])
            self.assertFalse(any(fake.seen_payload))

    def test_raw_exception_and_paths_do_not_escape(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(vault.sys, "platform", "win32"), patch.object(vault, "_Dpapi", side_effect=ValueError("private-path/private-value")), redirect_stdout(stdout), redirect_stderr(stderr):
            outcome = vault.roundtrip()
        self.assertEqual(outcome, {"status": "FAIL", "backend": "windows-user-dpapi", "code": "VAULT_INTERNAL_ERROR"})
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(vault._VaultError("arbitrary-private-text").code, "VAULT_INTERNAL_ERROR")

    def test_unit_decryption_error_still_wipes_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = _TestDpapi(base)
            fake.matches_synthetic = Mock(side_effect=vault._VaultError("DPAPI_UNPROTECT_FAILED"))
            with patch.object(vault.sys, "platform", "win32"), patch.object(vault, "_Dpapi", return_value=fake):
                outcome = vault.roundtrip()
            self.assertEqual(outcome["code"], "DPAPI_UNPROTECT_FAILED")
            self.assertFalse(any(fake.seen_payload))
            self.assertEqual(list(base.joinpath(*vault._STORE_PARTS).iterdir()), [])

    def test_cleanup_failure_prevents_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = _TestDpapi(Path(directory))
            original_cleanup = vault._cleanup_run

            def cleanup_then_report_failure(path: Path) -> bool:
                original_cleanup(path)
                return False

            with patch.object(vault.sys, "platform", "win32"), patch.object(vault, "_Dpapi", return_value=fake), patch.object(vault, "_cleanup_run", side_effect=cleanup_then_report_failure):
                outcome = vault.roundtrip()
            self.assertEqual(outcome["status"], "FAIL")
            self.assertEqual(outcome["code"], "VAULT_CLEANUP_FAILED")


class VaultStorageUnitTests(unittest.TestCase):
    def test_failed_atomic_replace_keeps_old_file_and_cleans_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "probe.dpapi").write_bytes(b"old-encrypted-data")
            with patch.object(vault.os, "replace", side_effect=OSError("injected storage failure")):
                with self.assertRaises(OSError):
                    vault._atomic_store(path, b"new-encrypted-data")
            self.assertEqual((path / "probe.dpapi").read_bytes(), b"old-encrypted-data")
            self.assertEqual([p.name for p in path.iterdir()], ["probe.dpapi"])

    def test_oversized_read_rejected_before_native_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "probe.dpapi").write_bytes(b"x" * (vault._MAX_BLOB_BYTES + 1))
            with self.assertRaises(vault._VaultError) as caught:
                vault._read_blob(path)
            self.assertEqual(caught.exception.code, "VAULT_BLOB_INVALID")

    def test_symlink_in_application_store_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            actual = path / "unrelated"
            actual.mkdir()
            try:
                (path / vault._STORE_PARTS[0]).symlink_to(actual, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Creating a test directory symlink is unavailable")
            with self.assertRaises(vault._VaultError) as caught:
                vault._vault_parent(_TestDpapi(path))
            self.assertEqual(caught.exception.code, "VAULT_LOCATION_INVALID")
            self.assertEqual(list(actual.iterdir()), [])


class VaultNativeBoundaryUnitTests(unittest.TestCase):
    def test_windows_types_and_pointer_width_are_declared_explicitly(self) -> None:
        self.assertEqual(ctypes.sizeof(vault._DataBlob._fields_[0][1]), 4)
        self.assertEqual(ctypes.sizeof(vault._Guid), 16)
        libraries: dict[str, Mock] = {}

        def load(name: str, **kwargs: object) -> Mock:
            self.assertEqual(kwargs, {"use_last_error": True, "winmode": 0x800})
            libraries[name] = Mock()
            return libraries[name]

        with patch.object(vault.sys, "platform", "win32"), patch.object(vault.ctypes, "WinDLL", side_effect=load, create=True):
            api = vault._Dpapi()
        self.assertIs(api._local_free.restype, ctypes.c_void_p)
        self.assertEqual(api._local_free.argtypes, [ctypes.c_void_p])
        self.assertIs(api._protect.restype, ctypes.c_int32)
        self.assertIs(api._known_folder.restype, ctypes.c_int32)
        self.assertIsNone(api._co_task_free.restype)

    def test_native_plaintext_is_wiped_and_localfree_called_after_compare(self) -> None:
        api = object.__new__(vault._Dpapi)
        allocation = (ctypes.c_ubyte * len(vault._SYNTHETIC_PAYLOAD))(*vault._SYNTHETIC_PAYLOAD)
        freed: list[int] = []

        def unprotect(source: object, description: object, entropy: object, reserved: object, prompt: object, flags: int, out: object) -> int:
            self.assertIsNone(description)
            self.assertIsNone(entropy)
            self.assertIsNone(prompt)
            self.assertEqual(flags, 1)  # UI forbidden only; never machine scope
            target = ctypes.cast(out, ctypes.POINTER(vault._DataBlob)).contents
            target.cbData = len(allocation)
            target.pbData = ctypes.cast(allocation, ctypes.POINTER(ctypes.c_ubyte))
            return 1

        def local_free(pointer: ctypes.c_void_p) -> None:
            self.assertFalse(any(allocation))
            freed.append(pointer.value)
            return None

        api._unprotect = unprotect
        api._local_free = local_free
        expected = bytearray(vault._SYNTHETIC_PAYLOAD)
        try:
            self.assertTrue(api.matches_synthetic(b"test-encrypted-input", expected))
            self.assertEqual(freed, [ctypes.addressof(allocation)])
        finally:
            vault._wipe_mutable(expected)

    def test_native_failure_also_releases_returned_allocation(self) -> None:
        api = object.__new__(vault._Dpapi)
        allocation = (ctypes.c_ubyte * 4)(1, 2, 3, 4)
        free = Mock(return_value=None)

        def failed_unprotect(*args: object) -> int:
            target = ctypes.cast(args[-1], ctypes.POINTER(vault._DataBlob)).contents
            target.cbData = len(allocation)
            target.pbData = ctypes.cast(allocation, ctypes.POINTER(ctypes.c_ubyte))
            return 0

        api._unprotect = failed_unprotect
        api._local_free = free
        with self.assertRaises(vault._VaultError) as caught:
            api.matches_synthetic(b"test-encrypted-input", bytearray(b"data"))
        self.assertEqual(caught.exception.code, "DPAPI_UNPROTECT_FAILED")
        self.assertFalse(any(allocation))
        free.assert_called_once()


@unittest.skipUnless(sys.platform == "win32", "NOT_RUN: actual Windows DPAPI requires Windows")
class WindowsNativeVaultTests(unittest.TestCase):
    def test_real_windows_dpapi_file_roundtrip(self) -> None:
        self.assertEqual(vault.roundtrip(), {"status": "PASS", "backend": "windows-user-dpapi", "code": "VAULT_ROUNDTRIP_OK"})

    def test_real_windows_dpapi_corruption_cannot_match_synthetic_payload(self) -> None:
        api = vault._Dpapi()
        expected = bytearray(vault._SYNTHETIC_PAYLOAD)
        try:
            protected = api.protect_synthetic(expected)
            self.assertNotIn(vault._SYNTHETIC_PAYLOAD, protected)
            self.assertTrue(api.matches_synthetic(protected, expected))
            damaged = bytearray(protected)
            damaged[-1] ^= 1
            try:
                matched = api.matches_synthetic(bytes(damaged), expected)
            except vault._VaultError as error:
                self.assertIn(error.code, {"DPAPI_UNPROTECT_FAILED", "VAULT_BLOB_INVALID"})
            else:
                self.assertFalse(matched)
        finally:
            vault._wipe_mutable(expected)


if __name__ == "__main__":
    unittest.main()
