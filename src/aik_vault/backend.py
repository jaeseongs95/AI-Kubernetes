"""Vault backend interface and the shared ciphertext file store.

Each credential is one ciphertext file named by a random opaque ID. The ID is
not derived from the secret. Paths, symlink/hardlink rejection, repository
exclusion and the inter-process lock reuse ``aik_runtime.paths``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
import hashlib
import hmac
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from aik_runtime.errors import StorageBusy, UnsafeStoragePath
from aik_platform import PlatformUnavailable
from aik_platform.fs import restrict_to_owner, sync_directory
from aik_platform.paths import component_dir
from aik_runtime.paths import database_lock, validate_database_path

from .errors import (
    CredentialNotFound, InvalidCredentialRef, InvalidSecret, UnsafeVaultPath,
    VaultAccessDenied, VaultBusy, VaultCorrupt, VaultDurabilityUnconfirmed, VaultError,
    VaultStorageError,
)


REF_NAMESPACE = "aik-vault"
REF_PREFIX = f"secret://{REF_NAMESPACE}/"
_REF = re.compile(r"secret://aik-vault/([0-9a-f]{32})\Z")
MAX_SECRET_BYTES = 16384
MAX_BLOB_BYTES = 1 << 20
_MAGIC = b"AIKV"
_FORMAT_VERSION = 1
_HEADER_SIZE = len(_MAGIC) + 2
_SUFFIX = ".aikv"
_ANCHOR = "credentials"
_ENTROPY_PREFIX = b"AIK-vault/v1/"
_FILE_DENIED = "The current OS user cannot access the vault files."


def parse_credential_ref(value: object) -> str:
    """Return the opaque ID of a vault reference, or raise a fixed error."""
    match = _REF.fullmatch(value) if type(value) is str else None
    if match is None:
        raise InvalidCredentialRef()
    return match.group(1)


def is_credential_ref(value: object) -> bool:
    return type(value) is str and _REF.fullmatch(value) is not None


def default_vault_directory() -> Path:
    """Per-user vault directory from ``aik_platform.paths``.

    Windows: %LOCALAPPDATA%\\AIK\\vault. Linux: $XDG_STATE_HOME/aik/vault (or
    ~/.local/state/aik/vault).
    """
    try:
        return component_dir("vault")
    except PlatformUnavailable:
        raise UnsafeVaultPath() from None


def _permission_denied(target: object) -> bool:
    """Re-probe a path rejected by ``aik_runtime.paths``.

    ``aik_runtime`` reports every inspection failure as UnsafeStoragePath and
    drops the cause. Return True only when the OS denies the current user
    access to the vault directory chain, the anchor or its lockfile, so the
    caller can report VAULT_ACCESS_DENIED instead of VAULT_UNSAFE_PATH. The
    probe never creates or modifies anything. Any other outcome is False.
    """
    try:
        target = Path(target)
        if not target.is_absolute():
            return False
        directory = target.parent
        previous = None
        for component in (*reversed(directory.parents), directory):
            try:
                os.lstat(component)
            except PermissionError:
                return True
            except FileNotFoundError:
                # Creating the missing directory needs write access to its parent.
                return previous is not None and not os.access(previous, os.W_OK | os.X_OK)
            previous = component
        try:
            os.lstat(target)
        except PermissionError:
            return True
        except FileNotFoundError:
            pass
        lockfile = target.with_name(target.name + ".storage.lock")
        try:
            info = os.lstat(lockfile)
        except PermissionError:
            return True
        except FileNotFoundError:
            # The lockfile is created on first use inside the vault directory.
            return not os.access(directory, os.W_OK | os.X_OK)
        if not stat.S_ISREG(info.st_mode):
            return False
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(lockfile, flags)
        except PermissionError:
            return True
        os.close(descriptor)
        return False
    except (OSError, TypeError, ValueError):
        return False


def _encode_secret(secret: object) -> bytearray:
    # Raise outside the except block: UnicodeEncodeError.object holds the whole
    # input, and an implicit __context__ would keep it reachable from the error.
    if type(secret) is not str or not secret or len(secret) > MAX_SECRET_BYTES:
        raise InvalidSecret()
    try:
        raw = secret.encode("utf-8")
    except UnicodeEncodeError:
        raw = None
    if raw is None or len(raw) > MAX_SECRET_BYTES:
        raise InvalidSecret()
    return bytearray(raw)


def _wipe(buffer: bytearray) -> None:
    """Best effort only: immutable Python str/bytes copies cannot be erased."""
    if buffer:
        buffer[:] = bytes(len(buffer))


class VaultBackend(ABC):
    """put/replace/delete/resolve by opaque credential_ref only."""

    @abstractmethod
    def put(self, secret: str) -> str:
        """Store a new secret and return a new opaque credential_ref."""

    @abstractmethod
    def replace(self, credential_ref: str, secret: str) -> None:
        """Replace the secret of an existing credential_ref."""

    @abstractmethod
    def delete(self, credential_ref: str) -> None:
        """Delete the stored ciphertext of an existing credential_ref."""

    @abstractmethod
    def resolve(self, credential_ref: str) -> str:
        """Return the plaintext; the caller must not persist or log it."""


class _FileVaultBackend(VaultBackend):
    """Ciphertext-per-file store; subclasses provide _protect/_unprotect."""

    _backend_tag = 0

    def __init__(self, directory: Path | str | None = None, *, timeout: float = 5.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Vault lock timeout must be a finite nonnegative number.")
        self.timeout = float(timeout)
        try:
            requested = Path(default_vault_directory() if directory is None else directory)
            candidate = requested / _ANCHOR
        except (TypeError, ValueError):
            raise UnsafeVaultPath() from None
        try:
            anchor = validate_database_path(candidate, create_parent=True)
        except (UnsafeStoragePath, TypeError, ValueError, OSError):
            denied = _permission_denied(candidate)
            if denied:
                raise VaultAccessDenied(_FILE_DENIED) from None
            raise UnsafeVaultPath() from None
        self._anchor = anchor
        self.directory = anchor.parent
        if not self.directory.is_dir():
            raise UnsafeVaultPath()
        try:
            restrict_to_owner(self.directory, directory=True)
        except PermissionError:
            raise VaultAccessDenied(_FILE_DENIED) from None
        except OSError:
            raise UnsafeVaultPath() from None

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    # -- protection hooks -------------------------------------------------
    def _protect(self, data: bytearray, entropy: bytes) -> bytes:
        raise NotImplementedError

    def _unprotect(self, blob: bytes, entropy: bytes) -> bytearray:
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------
    def _header(self) -> bytes:
        return _MAGIC + bytes((_FORMAT_VERSION, self._backend_tag))

    @staticmethod
    def _entropy(ref_id: str) -> bytes:
        # Binds each blob to its reference; it is not a secret.
        return _ENTROPY_PREFIX + ref_id.encode("ascii")

    def _unsafe_or_denied(self) -> VaultError:
        if _permission_denied(self._anchor):
            return VaultAccessDenied(_FILE_DENIED)
        return UnsafeVaultPath()

    @contextmanager
    def _locked(self):
        try:
            with database_lock(self._anchor, self.timeout):
                yield
        except StorageBusy:
            raise VaultBusy() from None
        except UnsafeStoragePath:
            error = self._unsafe_or_denied()
            raise error from None
        except PermissionError:
            raise VaultAccessDenied(_FILE_DENIED) from None
        except OSError:
            raise VaultStorageError() from None

    @contextmanager
    def _io(self):
        try:
            yield
        except VaultError:
            raise
        except UnsafeStoragePath:
            error = self._unsafe_or_denied()
            raise error from None
        except PermissionError:
            raise VaultAccessDenied(_FILE_DENIED) from None
        except OSError:
            raise VaultStorageError() from None

    def _file(self, ref_id: str) -> Path:
        return validate_database_path(self.directory / f"{ref_id}{_SUFFIX}")

    def _sync_directory(self) -> None:
        sync_directory(self.directory)

    def _sync_published(self, ref_id: str) -> None:
        """Sync after a rename/unlink that is already visible.

        A failure here is not an ordinary I/O failure: the change took effect.
        Report it separately with the opaque reference instead of VAULT_IO_ERROR.
        """
        try:
            self._sync_directory()
        except OSError:
            raise VaultDurabilityUnconfirmed(credential_ref=REF_PREFIX + ref_id) from None

    def _write(self, path: Path, ref_id: str, plaintext: bytearray) -> None:
        blob = self._header() + self._protect(plaintext, self._entropy(ref_id))
        descriptor, temporary_name = tempfile.mkstemp(prefix=".aikv-", suffix=".tmp", dir=self.directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            restrict_to_owner(temporary)
            os.replace(temporary, path)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._sync_published(ref_id)

    @staticmethod
    def _read(path: Path) -> bytes:
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_BINARY", 0))
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise CredentialNotFound() from None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeVaultPath()
            return stream.read(MAX_BLOB_BYTES + 1)

    # -- public API -------------------------------------------------------
    def put(self, secret: str) -> str:
        plaintext = _encode_secret(secret)
        try:
            with self._locked(), self._io():
                for _ in range(8):
                    ref_id = secrets.token_hex(16)
                    path = self._file(ref_id)
                    if not path.exists():
                        break
                else:
                    raise VaultStorageError()
                self._write(path, ref_id, plaintext)
        finally:
            _wipe(plaintext)
        return REF_PREFIX + ref_id

    def replace(self, credential_ref: str, secret: str) -> None:
        ref_id = parse_credential_ref(credential_ref)
        plaintext = _encode_secret(secret)
        try:
            with self._locked(), self._io():
                path = self._file(ref_id)
                if not path.is_file():
                    raise CredentialNotFound()
                self._write(path, ref_id, plaintext)
        finally:
            _wipe(plaintext)

    def delete(self, credential_ref: str) -> None:
        ref_id = parse_credential_ref(credential_ref)
        with self._locked(), self._io():
            path = self._file(ref_id)
            try:
                path.unlink()
            except FileNotFoundError:
                raise CredentialNotFound() from None
            self._sync_published(ref_id)

    def resolve(self, credential_ref: str) -> str:
        ref_id = parse_credential_ref(credential_ref)
        with self._locked(), self._io():
            blob = self._read(self._file(ref_id))
        if len(blob) > MAX_BLOB_BYTES or len(blob) <= _HEADER_SIZE or blob[:_HEADER_SIZE] != self._header():
            raise VaultCorrupt()
        plaintext = self._unprotect(blob[_HEADER_SIZE:], self._entropy(ref_id))
        if type(plaintext) is not bytearray:
            plaintext = bytearray(plaintext)
        try:
            text = plaintext.decode("utf-8")
        except UnicodeDecodeError:
            # Do not raise here: UnicodeDecodeError.object holds the plaintext.
            text = None
        finally:
            _wipe(plaintext)
        if not text:
            raise VaultCorrupt()
        return text


class FakeVaultBackend(_FileVaultBackend):
    """TEST-ONLY backend. Not a security boundary and never a product fallback.

    It uses an explicit in-memory key with a SHA-256 keystream and HMAC so that
    tests observe ciphertext files, restart recovery and "other user" failures
    (a different key) without Windows. It requires an explicit directory.
    """

    _backend_tag = 0xFE
    _NONCE = 16
    _MAC = 32

    def __init__(self, directory: Path | str, *, key: bytes, timeout: float = 5.0):
        if directory is None:
            raise ValueError("The fake vault requires an explicit test directory.")
        if type(key) is not bytes or len(key) != 32:
            raise ValueError("The fake vault key must be 32 bytes.")
        self._key = key
        super().__init__(directory, timeout=timeout)

    def _keystream(self, nonce: bytes, entropy: bytes, size: int) -> bytearray:
        output = bytearray()
        counter = 0
        while len(output) < size:
            output += hashlib.sha256(self._key + nonce + entropy + counter.to_bytes(8, "big")).digest()
            counter += 1
        return output[:size]

    def _protect(self, data: bytearray, entropy: bytes) -> bytes:
        nonce = secrets.token_bytes(self._NONCE)
        stream = self._keystream(nonce, entropy, len(data))
        body = bytes(a ^ b for a, b in zip(data, stream))
        _wipe(stream)
        mac = hmac.new(self._key, nonce + entropy + body, hashlib.sha256).digest()
        return nonce + body + mac

    def _unprotect(self, blob: bytes, entropy: bytes) -> bytearray:
        if len(blob) < self._NONCE + 1 + self._MAC:
            raise VaultCorrupt()
        nonce, body, mac = blob[:self._NONCE], blob[self._NONCE:-self._MAC], blob[-self._MAC:]
        expected = hmac.new(self._key, nonce + entropy + body, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            # Mirrors DPAPI: another user's key and a tampered blob look alike.
            raise VaultAccessDenied()
        stream = self._keystream(nonce, entropy, len(body))
        try:
            return bytearray(a ^ b for a, b in zip(body, stream))
        finally:
            _wipe(stream)
