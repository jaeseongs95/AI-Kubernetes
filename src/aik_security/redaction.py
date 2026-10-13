"""AIK-017 bounded output sanitization; never mutates an agent's RAW prompt.

Use inside the trusted credential/backend process, before data crosses
IPC, log, PEER, evidence, export or support-bundle boundaries.
"""
from __future__ import annotations

import base64
import hashlib
import math
import json
from threading import RLock
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
MAX_MATCHES = 4096
MAX_SCAN_WORK = 16_777_216
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SAFE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_URL = re.compile(r"(?i)\bhttps?://[^\s<>\"']+")
_HEADER = re.compile(r"(?im)\b(?:proxy-authorization|authorization|x-api-key|cookie|set-cookie)\s*[:=]\s*[^\r\n]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}")
# Match quoted diagnostic values as a unit, including escaped quotes/spaces.
# An unterminated quoted credential consumes the remainder, never only a prefix.
_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[\"']?"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|auth[_-]?token|password|passphrase|private[_-]?key|"
    r"authorization|proxy[_-]?authorization|x[_-]?api[_-]?key|cookie|set[_-]?cookie)"
    r"[\"']?\s*[:=]\s*"
    r'(?:"(?:\\.|[^"\\])*(?:"|$)|' r"'(?:\\.|[^'\\])*(?:'|$)|[^\s,;&<>\"']+)"
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
    # Split acronym boundaries as well as ordinary camelCase: userAPIKey.
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized).casefold()
    normalized = normalized.replace("-", "_").replace(".", "_")
    return (normalized in _SECRET_FIELDS or normalized.endswith((
        "_secret", "_password", "_passphrase", "_private_key", "_api_key",
        "_access_token", "_refresh_token", "_auth_token", "_session_token",
        "_token", "_credential", "_cookie",
    )) or any(part in {
        "secret", "token", "password", "passphrase", "credential", "cookie", "authorization",
    } for part in normalized.split("_")))


class _LimitExceeded(Exception):
    pass


class _Budget:
    """One budget for an entire output tree, not one budget per leaf."""

    def __init__(self) -> None:
        self.nodes = MAX_NODES
        self.text_bytes = MAX_TEXT_BYTES
        self.scan_work = MAX_SCAN_WORK

    def node(self) -> None:
        self.nodes -= 1
        if self.nodes < 0:
            raise _LimitExceeded

    def text(self, size: int, scan_work: int) -> None:
        self.text_bytes -= size
        self.scan_work -= scan_work
        if self.text_bytes < 0 or self.scan_work < 0:
            raise _LimitExceeded


def _utf8(text: str) -> bytes | None:
    # Reject before encoding a huge input, so this allocation is bounded.
    if len(text) > MAX_TEXT_BYTES:
        return None
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return raw if len(raw) <= MAX_TEXT_BYTES else None


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class OutputRedactor:
    """Never log, repr, pickle or serialize this secret-aware object.

    Every call uses one consistent secret registry. Registration is atomic;
    outputs use only plain JSON primitives and do not change the caller's RAW.
    """

    def __init__(self, secrets: tuple[str, ...] | list[str] = ()) -> None:
        if type(secrets) not in (tuple, list) or len(secrets) > 16:
            raise RedactionError("INVALID_SECRET_REGISTRY")
        self._lock = RLock()
        self._secrets: tuple[str, ...] = ()
        self._variants: tuple[str, ...] = ()
        self._url_variants: tuple[str, ...] = ()
        for secret in secrets:
            self.add_secret(secret)

    def add_secret(self, secret: str) -> None:
        if type(secret) is not str or not 12 <= len(secret) <= 256:
            raise RedactionError("INVALID_SECRET_REGISTRY")
        try:
            raw = secret.encode("utf-8")
        except UnicodeEncodeError:
            raise RedactionError("INVALID_SECRET_REGISTRY") from None
        with self._lock:
            if secret in self._secrets:
                return
            if len(self._secrets) >= 16:
                raise RedactionError("INVALID_SECRET_REGISTRY")
            urls = {quote(secret, safe=""), quote_plus(secret, safe="")}
            base64s = {base64.b64encode(raw).decode("ascii"), base64.urlsafe_b64encode(raw).decode("ascii")}
            options = {secret, raw.hex(), raw.hex().upper(), *urls, *base64s,
                       *(value.rstrip("=") for value in base64s)}
            if len(secret) >= 20:
                for i in range(len(secret) - 11):
                    chunk = secret[i:i + 12]
                    if len(set(chunk)) >= 5:
                        options.add(chunk)
            variants = tuple(sorted(set(self._variants).union(options), key=lambda v: (-len(v), v)))
            url_variants = tuple(sorted(set(self._url_variants).union(v for v in urls if "%" in v)))
            # Build/validate everything before publishing any part of the update.
            self._variants, self._url_variants = variants, url_variants
            self._secrets += (secret,)

    def _redact_text(self, text: str, budget: _Budget) -> str:
        raw = _utf8(text)
        if raw is None:
            return OMITTED
        urls = self._url_variants if "%" in text else ()
        budget.text(len(raw), len(text) * (len(self._variants) + len(urls)))
        spans: list[tuple[int, int, int]] = []
        markers = ("[URL_REDACTED]", "[HEADER_REDACTED]", REDACTED,
                   "[FIELD_REDACTED]", REDACTED, REDACTED)

        def mark(start: int, end: int, rank: int) -> None:
            if len(spans) >= MAX_MATCHES:
                raise _LimitExceeded
            spans.append((start, end, rank))

        # Collect *all* matches on the unmodified input. Sequential replacement
        # can destroy another match and leak its otherwise overlapping tail.
        for rank, pattern in enumerate((_URL, _HEADER, _BEARER, _ASSIGNMENT, _PROVIDER)):
            for match in pattern.finditer(text):
                mark(match.start(), match.end(), rank)
        normalized_url = re.sub(r"%[0-9a-fA-F]{2}", lambda m: m[0].upper(), text) if urls else text
        for source, variants in ((text, self._variants), (normalized_url, urls)):
            for variant in variants:
                start = source.find(variant)
                while start != -1:
                    mark(start, start + len(variant), 5)
                    start = source.find(variant, start + 1)  # include overlapping occurrences
        if not spans:
            return text
        spans.sort()
        merged: list[tuple[int, int, int]] = []
        start, end, rank = spans[0]
        for left, right, priority in spans[1:]:
            if left <= end:
                end, rank = max(end, right), min(rank, priority)
            else:
                merged.append((start, end, rank))
                start, end, rank = left, right, priority
        merged.append((start, end, rank))
        parts, cursor = [], 0
        for start, end, rank in merged:
            parts.extend((text[cursor:start], markers[rank]))
            cursor = end
        parts.append(text[cursor:])
        result = "".join(parts)
        return result if _utf8(result) is not None else OMITTED

    def redact_text(self, text: str) -> str:
        if type(text) is not str:
            raise RedactionError("INVALID_TEXT")
        with self._lock:
            try:
                return self._redact_text(text, _Budget())
            except _LimitExceeded:
                return OMITTED

    def _sanitize(self, value: Any, depth: int, budget: _Budget) -> Any:
        budget.node()
        if depth >= MAX_DEPTH:
            return OMITTED
        if value is None or type(value) is bool:
            return value
        if type(value) is int:
            return value if -(2**63) <= value < 2**63 else OMITTED
        if type(value) is float:
            return value if math.isfinite(value) else OMITTED
        if type(value) is str:
            return self._redact_text(value, budget)
        if type(value) in (bytes, bytearray, memoryview):
            return "[BINARY_OMITTED]"
        if type(value) is dict:
            result = {}
            for i, (key, item) in enumerate(value.items()):
                if i >= MAX_CHILDREN:
                    result["_more_fields_omitted"] = True
                    break
                budget.node()
                if (type(key) is not str or _SAFE_KEY.fullmatch(key) is None or
                        key in {"_unsafe_field_omitted", "_more_fields_omitted"} or
                        self._redact_text(key, budget) != key):
                    result["_unsafe_field_omitted"] = True
                    continue
                if _field_is_secret(key):
                    budget.node()
                    result[key] = REDACTED
                else:
                    result[key] = self._sanitize(item, depth + 1, budget)
            return result
        if type(value) in (list, tuple):
            output = [self._sanitize(item, depth + 1, budget) for item in value[:MAX_CHILDREN]]
            if len(value) > MAX_CHILDREN:
                output.append(OMITTED)
            return output
        return "[UNSUPPORTED_OMITTED]"  # never invoke untrusted repr/str/iter

    def sanitize(self, value: Any) -> Any:
        """Return detached JSON-safe data, or OMITTED if a global budget is exceeded."""
        with self._lock:
            try:
                result = self._sanitize(value, 0, _Budget())
                return result if len(_json_bytes(result)) <= MAX_OUTPUT_BYTES else OMITTED
            except (_LimitExceeded, RuntimeError):
                # Includes a native dict concurrently resized by its owner.
                return OMITTED

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
        with self._lock:
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
            if type(payload) is not str:
                raise RedactionError("INVALID_TEXT")
            raw = _utf8(payload)
            body: Any = (OMITTED if raw is None else
                         {"content": REDACTED, "bytes": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()})
        else:
            body = self.sanitize(payload)
        result = {"schema_version": 1, "kind": kind, "body": body}
        if len(_json_bytes(result)) > MAX_OUTPUT_BYTES:
            result["body"] = OMITTED
        return result


__all__ = ["OutputRedactor", "RedactionError", "REDACTED", "MAX_TEXT_BYTES"]
