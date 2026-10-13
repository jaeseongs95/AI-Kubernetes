"""AIK-022 synthetic auth/vault failure paths on the real product modules.

Runs inside the installed app (frozen sidecar) on real Windows. It uses the
product packages ``aik_vault`` (Windows user DPAPI), ``aik_connection``,
``aik_auth_lifecycle`` and ``aik_security``. The spike vault probe uses its
own ctypes code instead.

Every secret is a public synthetic value generated for this run. No real
vendor credential, network call or provider is used. Only fixed case codes
leave this module; plaintext, digests, paths and raw exceptions never do.

Cross-user DPAPI rejection needs a second Windows account. A single-user
session reports it as NOT_RUN, and a foreign-blob simulation is recorded
separately because DPAPI cannot tell another user's blob from a damaged one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

BACKEND = "windows-user-dpapi"
CASES = (
    "dpapi_round_trip", "lock_contention", "malformed_reference", "truncated_blob",
    "foreign_blob_simulated", "deleted_reference", "auth_clock_expiry",
    "credential_removed", "output_redaction", "other_user_profile",
)
EXPECTED = {
    "dpapi_round_trip": "ROUND_TRIP_OK",
    "lock_contention": "VAULT_BUSY",
    "malformed_reference": "INVALID_REFERENCE",
    "truncated_blob": "VAULT_CORRUPT",
    "foreign_blob_simulated": "ACCESS_DENIED",
    "deleted_reference": "CREDENTIAL_NOT_FOUND",
    "auth_clock_expiry": "EXPIRED_NOT_PLACEABLE",
    "credential_removed": "REFERENCE_UNAVAILABLE_NOT_PLACEABLE",
    "output_redaction": "REDACTED",
    "other_user_profile": "NOT_RUN_SINGLE_USER_SESSION",
}
_FINGERPRINT = "a" * 64
_AIKV_SUFFIX = ".aikv"


class _ManualClock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


def _code_of(action: Callable[[], Any], expected: tuple[type, ...], code: str) -> str:
    try:
        action()
    except expected:
        return code
    except Exception as error:
        return "UNEXPECTED_" + type(error).__name__.upper()[:40]
    return "NOT_RAISED"


def _cases(vault_factory, workdir: Path, generated: list[str]) -> dict[str, str]:
    from aik_auth_lifecycle import AuthLifecycle, AuthState
    from aik_connection import AuthenticationRequired, VaultCredentialRefChecker
    from aik_provider.contracts import AuthKind, AuthObservation, ConnectionBinding
    from aik_provider.contracts import AuthState as ProviderAuthState
    from aik_runtime.paths import database_lock
    from aik_security import OutputRedactor
    from aik_vault import (
        CredentialNotFound, InvalidCredentialRef, VaultAccessDenied, VaultBusy, VaultCorrupt,
        parse_credential_ref,
    )

    def synthetic() -> str:
        value = "aik-synthetic-not-a-key-" + secrets.token_hex(16)
        generated.append(value)
        return value

    def blob_path(vault, ref: str) -> Path:
        return Path(vault.directory) / (parse_credential_ref(ref) + _AIKV_SUFFIX)

    results: dict[str, str] = {}
    vault = vault_factory(workdir / "vault", 2.0)

    first, second = synthetic(), synthetic()
    ref = vault.put(first)
    ok = vault.resolve(ref) == first
    vault.replace(ref, second)
    ok = ok and vault_factory(workdir / "vault", 2.0).resolve(ref) == second
    results["dpapi_round_trip"] = "ROUND_TRIP_OK" if ok else "ROUND_TRIP_MISMATCH"

    # Another holder keeps the vault's own inter-process lock; a short-timeout
    # backend must fail closed with VaultBusy instead of waiting or bypassing.
    impatient = vault_factory(workdir / "vault", 0.2)
    with database_lock(vault._anchor, 2.0):  # noqa: SLF001 - same lock file the backend uses
        results["lock_contention"] = _code_of(lambda: impatient.resolve(ref), (VaultBusy,), "VAULT_BUSY")

    results["malformed_reference"] = _code_of(
        lambda: vault.resolve("secret://not-the-vault/abc"), (InvalidCredentialRef,), "INVALID_REFERENCE")

    truncated = vault.put(synthetic())
    path = blob_path(vault, truncated)
    path.write_bytes(path.read_bytes()[:6])
    results["truncated_blob"] = _code_of(lambda: vault.resolve(truncated), (VaultCorrupt,), "VAULT_CORRUPT")

    foreign = vault.put(synthetic())
    path = blob_path(vault, foreign)
    path.write_bytes(path.read_bytes()[:6] + secrets.token_bytes(96))
    results["foreign_blob_simulated"] = _code_of(
        lambda: vault.resolve(foreign), (VaultAccessDenied, VaultCorrupt), "ACCESS_DENIED")

    vault.delete(ref)
    results["deleted_reference"] = _code_of(lambda: vault.resolve(ref), (CredentialNotFound,),
                                            "CREDENTIAL_NOT_FOUND")

    clock = _ManualClock()
    checker = VaultCredentialRefChecker(vault)
    life = AuthLifecycle(clock=clock, credential_checker=checker)

    def valid(connection_id: str, credential_ref: str):
        binding = ConnectionBinding("synthetic", "synthetic-api", connection_id, 1, AuthKind.API_KEY,
                                    credential_ref, _FINGERPRINT)
        life.register(binding, expires_at=clock.now + timedelta(seconds=60))
        observation = AuthObservation(connection_id, 1, _FINGERPRINT, ProviderAuthState.AUTHENTICATED)
        return life.revalidate(connection_id, 1, observation=observation), observation

    expiring, _ = valid("conn-expiry", vault.put(synthetic()))
    clock.now += timedelta(seconds=120)
    snapshot = life.snapshot("conn-expiry")
    refused = _code_of(lambda: life.begin_run("conn-expiry", "run-after-expiry"), (AuthenticationRequired,), "REFUSED")
    results["auth_clock_expiry"] = (
        "EXPIRED_NOT_PLACEABLE"
        if expiring.state is AuthState.VALID and snapshot.state is AuthState.EXPIRED
        and not snapshot.placeable and refused == "REFUSED" else "EXPIRY_NOT_ENFORCED")

    removed_ref = vault.put(synthetic())
    current, observation = valid("conn-removed", removed_ref)
    vault.delete(removed_ref)
    after = life.revalidate("conn-removed", current.revision, observation=observation)
    refused = _code_of(lambda: life.begin_run("conn-removed", "run-after-delete"), (AuthenticationRequired,), "REFUSED")
    results["credential_removed"] = (
        "REFERENCE_UNAVAILABLE_NOT_PLACEABLE"
        if current.state is AuthState.VALID and not after.placeable and refused == "REFUSED"
        else "REMOVAL_NOT_ENFORCED")

    redactor = OutputRedactor(tuple(generated))
    text = "probe output " + " ".join(generated)
    redacted = redactor.redact_text(text)
    results["output_redaction"] = (
        "REDACTED" if not any(value in redacted for value in generated) else "SECRET_VISIBLE")

    results["other_user_profile"] = "NOT_RUN_SINGLE_USER_SESSION"
    return results


def probe(*, vault_factory: Callable[[Path, float], Any] | None = None) -> dict[str, Any]:
    """Run every case once and return ``{status, backend, code, cases}``.

    PASS: every case returned its expected code on real Windows (the
    cross-user case is the declared NOT_RUN). FAIL: any other code, a
    leaked synthetic value or a cleanup failure. NOT_RUN: not Windows and
    no injected backend (no storage or native call happened).
    """
    if vault_factory is None:
        if sys.platform != "win32":
            return {"status": "NOT_RUN", "backend": BACKEND, "code": "WINDOWS_REQUIRED", "cases": {}}
        from aik_vault import open_user_vault

        def vault_factory(directory: Path, timeout: float):
            return open_user_vault(directory, timeout=timeout)

    generated: list[str] = []
    workdir = Path(tempfile.mkdtemp(prefix="aik-auth-vault-"))
    cases: dict[str, str] = {}
    code = "AUTH_VAULT_INTERNAL_ERROR"
    try:
        cases = _cases(vault_factory, workdir, generated)
        mismatched = [name for name in CASES if cases.get(name) != EXPECTED[name]]
        code = "AUTH_VAULT_FAILURE_PATHS_OK" if not mismatched else "AUTH_VAULT_CASE_MISMATCH"
    except Exception as error:
        code = "AUTH_VAULT_" + type(error).__name__.upper()[:40]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if workdir.exists():
            code = "AUTH_VAULT_CLEANUP_FAILED"
    result = {"status": "PASS" if code == "AUTH_VAULT_FAILURE_PATHS_OK" else "FAIL", "backend": BACKEND,
              "code": code, "cases": {name: cases.get(name, "NOT_REACHED") for name in CASES}}
    serialized = json.dumps(result)
    if any(value in serialized for value in generated):
        result = {"status": "FAIL", "backend": BACKEND, "code": "AUTH_VAULT_SECRET_LEAKED",
                  "cases": {name: "WITHHELD" for name in CASES}}
    generated.clear()
    return result


if __name__ == "__main__":
    outcome = probe()
    print(json.dumps(outcome, sort_keys=True))
    raise SystemExit({"PASS": 0, "FAIL": 1, "NOT_RUN": 2}[outcome["status"]])
