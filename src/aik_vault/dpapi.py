"""Windows per-user DPAPI backend (CryptProtectData/CryptUnprotectData).

CRYPTPROTECT_LOCAL_MACHINE is never passed: blobs are bound to the logged-on
Windows user, not to every account on the machine. This module is active only
on Windows; elsewhere it raises VaultUnavailable and never falls back to a
fake backend. Real DPAPI behavior is NOT_RUN in this repository's Linux tests.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

from .backend import _FileVaultBackend
from .errors import VaultAccessDenied, VaultUnavailable
from aik_platform import is_windows


CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4  # Documented for the guard only; never used.
PROTECT_FLAGS = CRYPTPROTECT_UI_FORBIDDEN


class DpapiCallError(Exception):
    """A DPAPI call failed; carries only the operation and Windows error code."""

    def __init__(self, operation: str, winerror: int):
        super().__init__(f"DPAPI {operation} failed with Windows error {int(winerror)}.")
        self.operation = operation
        self.winerror = int(winerror)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


class _Crypt32Api:
    """Thin ctypes binding. Loaded only on Windows."""

    def __init__(self):
        if not is_windows():
            raise VaultUnavailable()
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            blob = ctypes.POINTER(_DataBlob)
            self._protect_function = crypt32.CryptProtectData
            self._protect_function.argtypes = [
                blob, ctypes.c_wchar_p, blob, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob,
            ]
            self._protect_function.restype = ctypes.c_int
            self._unprotect_function = crypt32.CryptUnprotectData
            self._unprotect_function.argtypes = [
                blob, ctypes.c_void_p, blob, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob,
            ]
            self._unprotect_function.restype = ctypes.c_int
            self._local_free = kernel32.LocalFree
            self._local_free.argtypes = [ctypes.c_void_p]
            self._local_free.restype = ctypes.c_void_p
        except (OSError, AttributeError):
            raise VaultUnavailable() from None

    @staticmethod
    def _input(data) -> tuple:
        size = len(data)
        buffer = (ctypes.c_char * size).from_buffer_copy(data)
        return _DataBlob(size, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    def _call(self, function, operation: str, data, entropy: bytes, flags: int, *, wipe_output: bool) -> bytearray:
        if flags & CRYPTPROTECT_LOCAL_MACHINE:
            raise ValueError("Machine-scope DPAPI is not permitted for the AIK vault.")
        data_blob, data_buffer = self._input(data)
        entropy_blob, _entropy_buffer = self._input(entropy)
        output = _DataBlob()
        try:
            ok = function(ctypes.byref(data_blob), None, ctypes.byref(entropy_blob), None, None,
                          flags, ctypes.byref(output))
            if not ok:
                raise DpapiCallError(operation, ctypes.get_last_error())
            size = int(output.cbData)
            try:
                result = bytearray(size)
                if size:
                    target = (ctypes.c_char * size).from_buffer(result)
                    ctypes.memmove(ctypes.addressof(target), output.pbData, size)
                    del target
                return result
            finally:
                if output.pbData:
                    if wipe_output and size:
                        ctypes.memset(ctypes.cast(output.pbData, ctypes.c_void_p), 0, size)
                    self._local_free(ctypes.cast(output.pbData, ctypes.c_void_p))
        finally:
            ctypes.memset(ctypes.addressof(data_buffer), 0, len(data))

    def protect(self, data, entropy: bytes, flags: int) -> bytes:
        return bytes(self._call(self._protect_function, "protect", data, entropy, flags, wipe_output=False))

    def unprotect(self, blob: bytes, entropy: bytes, flags: int) -> bytearray:
        return self._call(self._unprotect_function, "unprotect", blob, entropy, flags, wipe_output=True)


class DpapiVaultBackend(_FileVaultBackend):
    """Product backend: user-scope DPAPI ciphertext files under %LOCALAPPDATA%.

    ``_api`` is a private test seam for mock unit tests on non-Windows hosts.
    """

    _backend_tag = 0x01

    def __init__(self, directory: Path | str | None = None, *, timeout: float = 5.0, _api=None):
        if _api is None:
            if not is_windows():
                raise VaultUnavailable()
            _api = _Crypt32Api()
        self._api = _api
        super().__init__(directory, timeout=timeout)

    def _protect(self, data: bytearray, entropy: bytes) -> bytes:
        if PROTECT_FLAGS & CRYPTPROTECT_LOCAL_MACHINE:  # pragma: no cover - constant guard
            raise VaultUnavailable()
        try:
            blob = self._api.protect(data, entropy, PROTECT_FLAGS)
        except (DpapiCallError, OSError):
            raise VaultUnavailable("Windows user data protection could not protect the credential.") from None
        if type(blob) not in (bytes, bytearray) or not blob:
            raise VaultUnavailable("Windows user data protection returned no ciphertext.")
        return bytes(blob)

    def _unprotect(self, blob: bytes, entropy: bytes) -> bytearray:
        try:
            plaintext = self._api.unprotect(blob, entropy, PROTECT_FLAGS)
        except (DpapiCallError, OSError):
            # DPAPI does not distinguish another user's blob from a damaged one.
            raise VaultAccessDenied(
                "The current Windows user cannot unprotect this credential."
            ) from None
        return plaintext if type(plaintext) is bytearray else bytearray(plaintext)
