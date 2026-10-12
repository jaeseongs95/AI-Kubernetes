"""AIK-002 disposable Windows DPAPI probe; never use with real credentials.

The only public operation is an argument-free synthetic roundtrip.  The module
loads no native library on import and has no non-Windows or plaintext fallback.
Private helpers exist for focused fault-injection tests, not for credential CRUD.
"""

from __future__ import annotations

import ctypes
import hmac
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

__all__ = ["roundtrip"]

_BACKEND = "windows-user-dpapi"
# Publicly known test data.  This module must not accept an actual secret.
_SYNTHETIC_PAYLOAD = b"AIK-002 synthetic DPAPI probe; not a credential.\x00v1"
_MAX_BLOB_BYTES = 65_536
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x800
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_STORE_PARTS = ("AI-Kubernetes", "spikes", "windows-foundation", "vault")
_ERROR_CODES = frozenset({
    "VAULT_NATIVE_UNAVAILABLE", "VAULT_LOCATION_INVALID", "VAULT_STORAGE_FAILED",
    "VAULT_BLOB_INVALID", "VAULT_VERIFICATION_FAILED", "VAULT_CLEANUP_FAILED",
    "DPAPI_PROTECT_FAILED", "DPAPI_UNPROTECT_FAILED", "DPAPI_MEMORY_RELEASE_FAILED",
    "VAULT_INTERNAL_ERROR",
})


class _VaultError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES else "VAULT_INTERNAL_ERROR"
        super().__init__(self.code)


class _DataBlob(ctypes.Structure):
    # Windows DWORD is 32-bit on both x86 and x64.  c_ulong is NOT portable.
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8),
    ]


_FOLDERID_LOCAL_APP_DATA = _Guid(
    0xF1B32785, 0x6FBA, 0x4FCF,
    (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
)


def _wipe_mutable(value: bytearray) -> None:
    if value:
        ctypes.memset(ctypes.addressof((ctypes.c_ubyte * len(value)).from_buffer(value)), 0, len(value))


class _Dpapi:
    """Small, explicitly typed native boundary, used only for the fixed probe."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise _VaultError("VAULT_NATIVE_UNAVAILABLE")
        try:
            # Do not search the current working directory for Windows DLLs.
            self._crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True, winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)
            self._kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)
            self._shell32 = ctypes.WinDLL("shell32.dll", use_last_error=True, winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)
            self._ole32 = ctypes.WinDLL("ole32.dll", use_last_error=True, winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)

            self._protect = self._crypt32.CryptProtectData
            self._protect.argtypes = [
                ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.POINTER(_DataBlob),
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DataBlob),
            ]
            self._protect.restype = ctypes.c_int32  # BOOL
            self._unprotect = self._crypt32.CryptUnprotectData
            self._unprotect.argtypes = [
                ctypes.POINTER(_DataBlob), ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(_DataBlob),
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DataBlob),
            ]
            self._unprotect.restype = ctypes.c_int32
            self._local_free = self._kernel32.LocalFree
            self._local_free.argtypes = [ctypes.c_void_p]
            self._local_free.restype = ctypes.c_void_p  # HLOCAL, pointer-sized
            self._known_folder = self._shell32.SHGetKnownFolderPath
            self._known_folder.argtypes = [
                ctypes.POINTER(_Guid), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p),
            ]
            self._known_folder.restype = ctypes.c_int32  # HRESULT
            self._co_task_free = self._ole32.CoTaskMemFree
            self._co_task_free.argtypes = [ctypes.c_void_p]
            self._co_task_free.restype = None
        except (AttributeError, OSError):
            raise _VaultError("VAULT_NATIVE_UNAVAILABLE") from None

    def local_app_data(self) -> Path:
        value = ctypes.c_wchar_p()
        try:
            result = self._known_folder(ctypes.byref(_FOLDERID_LOCAL_APP_DATA), 0, None, ctypes.byref(value))
            if result < 0 or not value.value:
                raise _VaultError("VAULT_LOCATION_INVALID")
            location = Path(value.value)
            if not location.is_absolute():
                raise _VaultError("VAULT_LOCATION_INVALID")
            return location
        finally:
            # SHGetKnownFolderPath allocates using COM, not LocalAlloc.
            if value:
                self._co_task_free(ctypes.cast(value, ctypes.c_void_p))

    def _release_blob(self, blob: _DataBlob) -> None:
        if not blob.pbData:
            return
        pointer = ctypes.cast(blob.pbData, ctypes.c_void_p)
        # ctypes.memset is an executed FFI call, not an optimizable C local wipe.
        # This covers our native buffers; Python/OS-wide zeroization is not promised.
        if blob.cbData:
            ctypes.memset(pointer, 0, blob.cbData)
        try:
            if self._local_free(pointer):
                raise _VaultError("DPAPI_MEMORY_RELEASE_FAILED")
        finally:
            blob.pbData = ctypes.POINTER(ctypes.c_ubyte)()
            blob.cbData = 0

    def protect_synthetic(self, payload: bytearray) -> bytes:
        if not payload or len(payload) > _MAX_BLOB_BYTES:
            raise _VaultError("VAULT_BLOB_INVALID")
        storage = (ctypes.c_ubyte * len(payload)).from_buffer(payload)
        source = _DataBlob(len(payload), ctypes.cast(storage, ctypes.POINTER(ctypes.c_ubyte)))
        output = _DataBlob()
        try:
            if not self._protect(
                ctypes.byref(source), None, None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
            ):
                raise _VaultError("DPAPI_PROTECT_FAILED")
            if not output.pbData or not (0 < output.cbData <= _MAX_BLOB_BYTES):
                raise _VaultError("VAULT_BLOB_INVALID")
            return ctypes.string_at(output.pbData, output.cbData)  # ciphertext only
        finally:
            self._release_blob(output)

    def matches_synthetic(self, ciphertext: bytes, expected: bytearray) -> bool:
        if not ciphertext or len(ciphertext) > _MAX_BLOB_BYTES:
            raise _VaultError("VAULT_BLOB_INVALID")
        storage = (ctypes.c_ubyte * len(ciphertext)).from_buffer_copy(ciphertext)
        source = _DataBlob(len(ciphertext), ctypes.cast(storage, ctypes.POINTER(ctypes.c_ubyte)))
        output = _DataBlob()
        try:
            # The optional description output is NULL, so no description allocation
            # or plaintext metadata ever crosses the boundary.
            if not self._unprotect(
                ctypes.byref(source), None, None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
            ):
                raise _VaultError("DPAPI_UNPROTECT_FAILED")
            if not output.pbData or output.cbData != len(expected):
                return False
            # Compare the native buffer without making an immutable plaintext copy.
            array = (ctypes.c_ubyte * output.cbData).from_address(ctypes.addressof(output.pbData.contents))
            with memoryview(array).cast("B") as actual:
                return hmac.compare_digest(actual, expected)
        finally:
            self._release_blob(output)


def _require_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise _VaultError("VAULT_LOCATION_INVALID")


def _vault_parent(api: _Dpapi) -> Path:
    # The public API has no path override and does not trust LOCALAPPDATA env.
    parent = api.local_app_data()
    _require_directory(parent)
    for segment in _STORE_PARTS:
        parent = parent / segment
        parent.mkdir(exist_ok=True)
        _require_directory(parent)
    return parent


def _atomic_store(run_dir: Path, ciphertext: bytes) -> None:
    if not ciphertext or len(ciphertext) > _MAX_BLOB_BYTES:
        raise _VaultError("VAULT_BLOB_INVALID")
    temporary: Path | None = None
    fd = -1
    try:
        fd, name = tempfile.mkstemp(prefix=".probe-", suffix=".tmp", dir=run_dir)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            fd = -1  # stream owns it now, including on write/fsync failure
            stream.write(ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, run_dir / "probe.dpapi")
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_blob(run_dir: Path) -> bytes:
    target = run_dir / "probe.dpapi"
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise _VaultError("VAULT_BLOB_INVALID")
    # Read bound applies even if the file grows after lstat.
    with target.open("rb") as stream:
        blob = stream.read(_MAX_BLOB_BYTES + 1)
    if not blob or len(blob) > _MAX_BLOB_BYTES:
        raise _VaultError("VAULT_BLOB_INVALID")
    return blob


def _cleanup_run(run_dir: Path) -> bool:
    try:
        # Delete only the known file and the directory created by this invocation.
        # Never enumerate another run, credential store or provider directory.
        (run_dir / "probe.dpapi").unlink(missing_ok=True)
        run_dir.rmdir()
        return True
    except OSError:
        return False


def _result(status: str, code: str) -> dict[str, str]:
    return {"status": status, "backend": _BACKEND, "code": code}


def roundtrip() -> dict[str, str]:
    """Protect, persist, read, compare and delete one public synthetic value.

    PASS means this invocation completed all five operations on real Windows.
    NOT_RUN means no native call or storage action occurred on this platform.
    Neither result establishes a production vault or an adversarial OS boundary.
    Only fixed status codes escape; no plaintext, digest, path or raw exception.
    """
    if sys.platform != "win32":
        return _result("NOT_RUN", "WINDOWS_REQUIRED")
    payload = bytearray(_SYNTHETIC_PAYLOAD)
    run_dir: Path | None = None
    result = _result("FAIL", "VAULT_INTERNAL_ERROR")
    try:
        api = _Dpapi()
        parent = _vault_parent(api)
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
        _require_directory(run_dir)
        ciphertext = api.protect_synthetic(payload)
        _atomic_store(run_dir, ciphertext)
        if not api.matches_synthetic(_read_blob(run_dir), payload):
            raise _VaultError("VAULT_VERIFICATION_FAILED")
        result = _result("PASS", "VAULT_ROUNDTRIP_OK")
    except _VaultError as error:
        result = _result("FAIL", error.code)
    except OSError:
        result = _result("FAIL", "VAULT_STORAGE_FAILED")
    except Exception:
        result = _result("FAIL", "VAULT_INTERNAL_ERROR")
    finally:
        _wipe_mutable(payload)
        if run_dir is not None and not _cleanup_run(run_dir):
            result = _result("FAIL", "VAULT_CLEANUP_FAILED")
    return result


if __name__ == "__main__":
    outcome = roundtrip()
    print(json.dumps(outcome, sort_keys=True))
    raise SystemExit({"PASS": 0, "FAIL": 1, "NOT_RUN": 2}[outcome["status"]])
