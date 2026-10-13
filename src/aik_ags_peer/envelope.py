"""AIK message body convention ``aik.peer.v1``.

This is an AIK convention carried inside the AGS ``body`` string. It is not
part of the AGS contract: AGS treats the body as opaque text. Messages that do
not follow the convention (for example ordinary messages from other hosts)
decode to ``None`` and are treated as normal data, not as errors.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
from types import MappingProxyType

from .errors import AgsPeerError
from .models import is_identifier, strict_json_loads


SCHEMA = "aik.peer.v1"
KINDS = ("task", "result", "control")
MAX_BODY_BYTES = 256 * 1024
_KEYS = frozenset({"schema", "kind", "correlation_id", "payload"})
_MAX_DEPTH = 32


@dataclass(frozen=True, slots=True)
class Envelope:
    kind: str
    correlation_id: str
    payload: Mapping


def _plain(value: object, depth: int = 0) -> object:
    if depth > _MAX_DEPTH:
        raise AgsPeerError("INVALID_ENVELOPE")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise AgsPeerError("INVALID_ENVELOPE")
        return value
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise AgsPeerError("INVALID_ENVELOPE")
            result[key] = _plain(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_plain(item, depth + 1) for item in value]
    raise AgsPeerError("INVALID_ENVELOPE")


def encode(kind: str, correlation_id: str, payload: Mapping) -> str:
    """Canonical JSON body; raises INVALID_ENVELOPE when the convention cannot hold."""
    if type(kind) is not str or kind not in KINDS or not is_identifier(correlation_id):
        raise AgsPeerError("INVALID_ENVELOPE")
    if not isinstance(payload, Mapping):
        raise AgsPeerError("INVALID_ENVELOPE")
    document = {"schema": SCHEMA, "kind": kind, "correlation_id": correlation_id, "payload": _plain(payload)}
    try:
        body = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        size = len(body.encode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError):
        raise AgsPeerError("INVALID_ENVELOPE") from None
    if size > MAX_BODY_BYTES:
        raise AgsPeerError("INVALID_ENVELOPE")
    return body


def decode(body: object) -> Envelope | None:
    """Return the envelope, or None when the body is not ``aik.peer.v1``."""
    if type(body) is not str:
        return None
    try:
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            return None
        document = strict_json_loads(body)
    except (UnicodeEncodeError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or set(document) != _KEYS:
        return None
    if document["schema"] != SCHEMA or document["kind"] not in KINDS:
        return None
    if not is_identifier(document["correlation_id"]) or not isinstance(document["payload"], dict):
        return None
    return Envelope(document["kind"], document["correlation_id"], MappingProxyType(document["payload"]))
