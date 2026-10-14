"""Linux per-user vault backend on the freedesktop Secret Service (DEC09).

Secrets live in the user's desktop keyring (GNOME Keyring, KWallet, ...)
through the ``secret-tool`` command of libsecret. Each credential is one item
with the attributes ``aik-vault-namespace`` and ``aik-vault-ref``; the opaque
``secret://aik-vault/<id>`` reference is the only thing callers keep.

- The secret is written to ``secret-tool store`` on stdin, never in argv.
- Every call has an absolute timeout, no shell and no retry.
- No ``secret-tool``, no D-Bus session or a locked/absent keyring raises
  ``VaultUnavailable`` (fail-closed). There is no file fallback.
- stderr is only checked for being empty; its text is never kept or shown.

Keyring access control is the desktop session's: other processes of the same
logged-in user can read the same items. That is the same-user boundary as the
Windows DPAPI backend, not process isolation.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
import shutil
import subprocess

from .backend import REF_PREFIX, VaultBackend, _encode_secret, _wipe, parse_credential_ref
from .errors import CredentialNotFound, VaultCorrupt, VaultStorageError, VaultUnavailable

_LABEL = "AIK credential"
_NAMESPACE_ATTRIBUTE = "aik-vault-namespace"
_REF_ATTRIBUTE = "aik-vault-ref"
_NOT_FOUND = object()


def _run_secret_tool(argv: list[str], stdin: bytes | None, timeout: float) -> tuple[int, bytes, bool]:
    """Run one secret-tool command; return (exit code, stdout, stderr was empty)."""
    completed = subprocess.run(argv, input=stdin if stdin is not None else b"", capture_output=True,
                               timeout=timeout, check=False, shell=False)
    return completed.returncode, completed.stdout, not completed.stderr.strip()


class SecretServiceVaultBackend(VaultBackend):
    """Store, replace, delete and resolve credentials by opaque reference only.

    ``namespace`` keeps separate vaults apart in one keyring. ``_runner`` and
    ``_which`` are private test seams; the product path uses the real tool.
    """

    def __init__(self, namespace: str = "default", *, timeout: float = 5.0, _runner=None, _which=None):
        if type(namespace) is not str or not namespace or len(namespace) > 64 or not namespace.isascii():
            raise ValueError("The vault namespace must be a short ASCII name.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ValueError("The Secret Service timeout must be between 0 and 60 seconds.")
        executable = (_which or shutil.which)("secret-tool")
        if not executable or not os.path.isabs(executable):
            raise VaultUnavailable()
        self._executable = executable
        self._namespace = namespace
        self._timeout = float(timeout)
        self._runner = _runner or _run_secret_tool

    def __repr__(self) -> str:
        return f"SecretServiceVaultBackend(namespace={self._namespace!r})"

    def _attributes(self, ref_id: str) -> list[str]:
        return [_NAMESPACE_ATTRIBUTE, self._namespace, _REF_ATTRIBUTE, ref_id]

    def _call(self, operation: str, ref_id: str, stdin: bytes | None = None) -> tuple[int, bytes, bool]:
        argv = [self._executable, operation]
        if operation == "store":
            argv.append(f"--label={_LABEL}")
        argv += self._attributes(ref_id)
        try:
            return self._runner(argv, stdin, self._timeout)
        except (OSError, subprocess.SubprocessError):
            raise VaultUnavailable() from None

    def _lookup(self, ref_id: str):
        code, stdout, quiet = self._call("lookup", ref_id)
        if code == 0 and stdout:
            return bytearray(stdout)
        if code == 1 and not stdout and quiet:
            return _NOT_FOUND
        if code == 0:
            raise VaultCorrupt()
        raise VaultUnavailable()

    def _store(self, ref_id: str, plaintext: bytearray) -> None:
        code, _, _ = self._call("store", ref_id, bytes(plaintext))
        if code != 0:
            raise VaultUnavailable()

    def put(self, secret: str) -> str:
        plaintext = _encode_secret(secret)
        try:
            for _ in range(8):
                ref_id = secrets.token_hex(16)
                if self._lookup(ref_id) is _NOT_FOUND:
                    break
            else:
                raise VaultStorageError()
            self._store(ref_id, plaintext)
        finally:
            _wipe(plaintext)
        return REF_PREFIX + ref_id

    def replace(self, credential_ref: str, secret: str) -> None:
        ref_id = parse_credential_ref(credential_ref)
        plaintext = _encode_secret(secret)
        try:
            existing = self._lookup(ref_id)
            if existing is _NOT_FOUND:
                raise CredentialNotFound()
            _wipe(existing)
            self._store(ref_id, plaintext)  # an item with the same attributes is replaced
        finally:
            _wipe(plaintext)

    def delete(self, credential_ref: str) -> None:
        ref_id = parse_credential_ref(credential_ref)
        existing = self._lookup(ref_id)
        if existing is _NOT_FOUND:
            raise CredentialNotFound()
        _wipe(existing)
        code, _, _ = self._call("clear", ref_id)
        if code != 0 or self._lookup(ref_id) is not _NOT_FOUND:
            raise VaultStorageError()

    def resolve(self, credential_ref: str) -> str:
        ref_id = parse_credential_ref(credential_ref)
        plaintext = self._lookup(ref_id)
        if plaintext is _NOT_FOUND:
            raise CredentialNotFound()
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


def namespace_for(directory: Path | str | None) -> str:
    """The keyring namespace that stands in for a vault directory on Linux."""
    if directory is None:
        return "default"
    return "dir-" + hashlib.sha256(os.fsencode(str(Path(directory).resolve()))).hexdigest()[:32]
