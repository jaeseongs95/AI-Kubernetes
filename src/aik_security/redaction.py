"""AIK-017 bounded output sanitization; never mutates an agent's RAW prompt.

Use inside the trusted credential/backend process, before data crosses
IPC, log, PEER, evidence, export or support-bundle boundaries.
"""
from __future__ import annotations

import base64
import hashlib
import math
import re
from typing import Any
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"
OMITTED = "[OMITTED]"
MAX_TEXT_BYTES = 65536
MAX_DEPTH = 8
MAX_CHILDREN = 128
MAX_NODES = 512
MAX_OUTPUT_BYTES = 65536
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SAFE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_URL = re.compile(r"(?i)\bhttps?://[^\s<>\"']+")
_HEADER = re.compile(r"(?im)\b(?:proxy-authorization|authorization|x-api-key|cookie|set-cookie)\s*[:=]\s*[^\r\n]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}")
_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|auth[_-]?token|password|passphrase|private[_-]?key)"
    r"\s*[:=]\s*(?:[\"']?)[^\s,;&<>\"']+"
)
_PROVIDER = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|AIza[A-Za-z0-9_-]{20,})"
    r"(?![A-Za-z0-9])"
)
_SECRET_FIELDS = frozenset({
    "authorization", "proxy_authorization", "x_api_key", "apikey", "api_key",
    "password", "passphrase", "secret", "secret_key", "client_secret", "private_key",
    "access_token", "refresh_token", "id_token", "auth_token", "session_token",
    "cookie", "set_cookie", "credential", "credentials", "raw_secret", "secret_value",
    "vault_bytes", "prompt", "raw_prompt", "user_prompt", "command_line", "stderr",
    "token", "secret_token", "headers", "request_headers", "response_headers",
    "traceback", "stack", "environment", "env", "process_env", "argv",
})
_OUTPUT_KINDS = frozenset({
    "prompt_preview", "ipc_response", "peer", "evidence", "log",
    "crash_bundle", "export",
})
_CODES = frozenset({
    "INVALID_REQUEST", "AUTH_EXPIRED", "ACCESS_DENIED", "PROVIDER_UNAVAILABLE",
    "RATE_LIMITED", "UNKNOWN_EXECUTION", "INTERNAL_ERROR", "BACKEND_UNAVAILABLE",
    "OUTPUT_REDACTED",
})
_MESSAGES = {
    "INVALID_REQUEST": "The request was rejected.",
    "AUTH_EXPIRED": "The connection requires authentication.",
    "ACCESS_DENIED": "The request is outside the allowed scope.",
    "PROVIDER_UNAVAILABLE": "The configured provider is unavailable.",
    "RATE_LIMITED": "The provider declined the request due to a limit.",
    "UNKNOWN_EXECUTION": "The execution result needs reconciliation.",
    "INTERNAL_ERROR": "The request failed. Inspect the sanitized audit reference.",
    "BACKEND_UNAVAILABLE": "The backend is unavailable.",
    "OUTPUT_REDACTED": "The output was withheld for safety.",
}


class RedactionError(ValueError):
    """Only fixed codes belong in diagnostics."""


def _field_is_secret(key: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).casefold().replace("-", "_").replace(".", "_")
    return (normalized in _SECRET_FIELDS or normalized.endswith((
        "_secret", "_password", "_passphrase", "_private_key", "_api_key",
        "_access_token", "_refresh_token", "_auth_token", "_session_token",
        "_token", "_credential", "_cookie",
    )) or any(part in {
        "secret", "token", "password", "passphrase", "credential", "cookie", "authorization",
    } for part in normalized.split("_")))


class OutputRedactor:
    """Never log, repr, pickle or serialize this secret-aware object."""

    def __init__(self, secrets: tuple[str, ...] | list[str] = ()) -> None:
        if type(secrets) not in (tuple, list) or len(secrets) > 16:
            raise RedactionError("INVALID_SECRET_REGISTRY")
        self._secrets: list[str] = []
        self._variants: list[str] = []
        for secret in secrets:
            self.add_secret(secret)

    def add_secret(self, secret: str) -> None:
        if type(secret) is not str or not 12 <= len(secret) <= 256 or len(self._secrets) >= 16:
            raise RedactionError("INVALID_SECRET_REGISTRY")
        if secret in self._secrets:
            return
        self._secrets.append(secret)
        raw = secret.encode("utf-8")
        options = {secret, quote(secret, safe=""), quote_plus(secret, safe=""),
                   base64.b64encode(raw).decode("ascii"), base64.urlsafe_b64encode(raw).decode("ascii"),
                   raw.hex()}
        # Catch high-entropy partial copies, not every arbitrary transformation.
        if len(secret) >= 20:
            for i in range(len(secret) - 11):
                chunk = secret[i:i + 12]
                if len(set(chunk)) >= 5:
                    options.add(chunk)
        self._variants = sorted(set(self._variants).union(options), key=len, reverse=True)

    def redact_text(self, text: str) -> str:
        if type(text) is not str:
            raise RedactionError("INVALID_TEXT")
        if len(text.encode("utf-8", errors="replace")) > MAX_TEXT_BYTES:
            return OMITTED
        value = _URL.sub("[URL_REDACTED]", text)
        value = _HEADER.sub("[HEADER_REDACTED]", value)
        value = _BEARER.sub(REDACTED, value)
        value = _ASSIGNMENT.sub("[FIELD_REDACTED]", value)
        value = _PROVIDER.sub(REDACTED, value)
        for variant in self._variants:
            value = value.replace(variant, REDACTED)
        return value

    def _sanitize(self, value: Any, depth: int, budget: list[int]) -> Any:
        budget[0] -= 1
        if depth >= MAX_DEPTH or budget[0] < 0:
            return OMITTED
        if value is None or type(value) is bool:
            return value
        if type(value) is int:
            return value if -(2**63) <= value < 2**63 else OMITTED
        if type(value) is float:
            return value if math.isfinite(value) else OMITTED
        if type(value) is str:
            return self.redact_text(value)
        if type(value) in (bytes, bytearray, memoryview):
            return "[BINARY_OMITTED]"
        if type(value) is dict:
            result = {}
            for i, (key, item) in enumerate(value.items()):
                if i >= MAX_CHILDREN:
                    result["_more_fields_omitted"] = True
                    break
                if type(key) is not str or _SAFE_KEY.fullmatch(key) is None or self.redact_text(key) != key:
                    result["_unsafe_field_omitted"] = True
                    continue
                result[key] = REDACTED if _field_is_secret(key) else self._sanitize(item, depth + 1, budget)
            return result
        if type(value) in (list, tuple):
            output = [self._sanitize(item, depth + 1, budget) for item in value[:MAX_CHILDREN]]
            if len(value) > MAX_CHILDREN:
                output.append(OMITTED)
            return output
        return "[UNSUPPORTED_OMITTED]"  # never invoke untrusted repr/str

    def sanitize(self, value: Any) -> Any:
        """Return JSON-safe data without invoking user-supplied serializers."""
        result = self._sanitize(value, 0, [MAX_NODES])
        import json
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_OUTPUT_BYTES:
            return OMITTED
        return result

    def safe_id(self, value: object) -> str | None:
        if type(value) is not str or _IDENT.fullmatch(value) is None:
            return None
        return value if self.redact_text(value) == value else None

    def sanitized_error(
        self, code: str, *, request_id: object = None,
        connection_id: object = None, task_id: object = None,
        exception: object = None,
    ) -> dict[str, Any]:
        # The raw exception is intentionally ignored.
        selected = code if type(code) is str and code in _CODES else "INTERNAL_ERROR"
        return {
            "status": "FAIL", "error": {"code": selected, "message": _MESSAGES[selected]},
            "request_id": self.safe_id(request_id),
            "connection_id": self.safe_id(connection_id),
            "task_id": self.safe_id(task_id),
        }

    def output(self, kind: str, payload: Any) -> dict[str, Any]:
        if type(kind) is not str or kind not in _OUTPUT_KINDS:
            raise RedactionError("UNSUPPORTED_OUTPUT_KIND")
        if kind == "prompt_preview":
            # RAW prompt stays unchanged in its actual execution channel.
            if type(payload) is not str:
                raise RedactionError("INVALID_TEXT")
            raw = payload.encode("utf-8")
            body: Any = {"content": REDACTED, "bytes": len(raw),
                         "sha256": hashlib.sha256(raw).hexdigest()}
        else:
            body = self.sanitize(payload)
        return {"schema_version": 1, "kind": kind, "body": body}


__all__ = ["OutputRedactor", "RedactionError", "REDACTED", "MAX_TEXT_BYTES"]
