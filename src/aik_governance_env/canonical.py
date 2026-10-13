"""AGS canonicalJson subset, byte-identical to VM _canonical/_a2_digest (32bb0f9)."""

from __future__ import annotations

import hashlib
import json

from .errors import GovernanceEnvError

_MAX_SAFE_INTEGER = 2**53 - 1
_UNSUPPORTED = "CANONICAL_INPUT_UNSUPPORTED"


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def canonical_json(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise GovernanceEnvError(_UNSUPPORTED)
        return str(int(value))
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise GovernanceEnvError(_UNSUPPORTED) from None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise GovernanceEnvError(_UNSUPPORTED)
        for key in value:
            canonical_json(key)
        ordered = sorted(value.items(), key=lambda item: _utf16_key(item[0]))
        return "{" + ",".join(canonical_json(k) + ":" + canonical_json(v) for k, v in ordered) + "}"
    raise GovernanceEnvError(_UNSUPPORTED)


def a2_digest(value) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
