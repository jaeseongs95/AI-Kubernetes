"""AIK-020 model inventory and capability validator.

Rules:
- A resource is one aik_provider.ConnectionBinding (connection_id). The same
  model name on two resources is two unrelated capability records.
- Every snapshot and entry has a provenance: requested, provider_observed,
  configured_catalog or local_derived. Only provider_observed and
  configured_catalog (a manual registration with a verification reference)
  can support a combination. requested/local_derived are never promoted.
- If a provider_observed snapshot exists, it alone decides. A configured
  catalog is used only when the resource has no observed list. A stale
  observed list rejects; it does not fall back to the catalog.
- A provider_observed list requires the caller-supplied observation time and
  an observation source reference. The load time is recorded separately, so
  a cached old response cannot be made fresh by reloading it.
- Evidence recorded for an earlier connection revision (including a manual
  catalog) is rejected as BINDING_MISMATCH until it is registered again.
- Raw list JSON is checked for duplicate keys/models/efforts/tools, null and
  empty values before typed conversion. Original order and SHA-256 digest of
  the raw bytes are kept. An entry may carry an optional context_tokens limit.
- Any change of a resource's lists or binding increments its revision, which
  turns earlier validation results stale (aik_pool.Freshness).
No provider is called here. Error messages are fixed codes without input text.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import math
import re
from threading import RLock

from aik_pool import Freshness
from aik_provider import ConnectionBinding
from aik_runtime import InvalidRecord
from aik_runtime.models import identifier


_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_MAX_RAW_BYTES = 1 << 20
_MAX_MODELS = 1000
_MAX_EFFORTS = 32
_MAX_TOOLS = 64
_MAX_CONTEXT_TOKENS = 2**31 - 1
_DOCUMENT_KEYS = frozenset({"models"})
_ENTRY_KEYS = frozenset({"model", "efforts", "tools"})
_OPTIONAL_ENTRY_KEYS = frozenset({"context_tokens"})


class CapabilityError(ValueError):
    """Fixed code-only failure; never contains raw list text or secrets."""


class UnknownResource(CapabilityError):
    """The connection (resource) is not registered in this inventory."""


class Provenance(StrEnum):
    REQUESTED = "requested"
    PROVIDER_OBSERVED = "provider_observed"
    CONFIGURED_CATALOG = "configured_catalog"
    LOCAL_DERIVED = "local_derived"


_NON_EVIDENCE = (Provenance.REQUESTED, Provenance.LOCAL_DERIVED)
_EVIDENCE = (Provenance.PROVIDER_OBSERVED, Provenance.CONFIGURED_CATALOG)
_ACCOUNT_SCOPED = (Provenance.PROVIDER_OBSERVED, Provenance.LOCAL_DERIVED)


def _check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise CapabilityError("INVALID_" + field.upper()) from None


def _check_model(value: object) -> None:
    if type(value) is not str or _MODEL.fullmatch(value) is None:
        raise CapabilityError("INVALID_MODEL")


def _check_names(values: object, field: str, maximum: int) -> None:
    if type(values) is not tuple or len(values) > maximum:
        raise CapabilityError("INVALID_" + field.upper() + "S")
    for value in values:
        _check_id(value, field)
    if len({value.casefold() for value in values}) != len(values):
        raise CapabilityError("DUPLICATE_" + field.upper())


def _check_context(value: object, code: str) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_CONTEXT_TOKENS:
        raise CapabilityError(code)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ModelCapability:
    """One model entry of one resource; provenance has no default."""

    connection_id: str
    model: str
    efforts: tuple[str, ...]
    tools: tuple[str, ...]
    provenance: Provenance
    context_tokens: int | None = None

    def __post_init__(self) -> None:
        _check_id(self.connection_id, "connection_id")
        _check_model(self.model)
        _check_names(self.efforts, "effort", _MAX_EFFORTS)
        _check_names(self.tools, "tool", _MAX_TOOLS)
        if type(self.provenance) is not Provenance:
            raise CapabilityError("INVALID_PROVENANCE")
        if self.context_tokens is not None:
            _check_context(self.context_tokens, "INVALID_CONTEXT_TOKENS")


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    """A typed list for one (resource, provenance), with raw order and digest.

    observed_at is the observation time supplied by the caller (required for
    provider_observed); loaded_at is when this inventory accepted the list.
    """

    connection_id: str
    connection_revision: int
    provenance: Provenance
    digest: str
    models: tuple[str, ...]
    entries: tuple[ModelCapability, ...]
    observed_at: str
    source_ref: str | None
    loaded_at: str | None = None

    def find(self, model: str) -> ModelCapability | None:
        for entry in self.entries:
            if entry.model == model:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The requested combination stays labeled requested, accepted or not."""

    connection_id: str
    connection_revision: int
    resource_revision: int
    model: str
    effort: str | None
    tools: tuple[str, ...]
    request_provenance: Provenance
    accepted: bool
    supported_by: Provenance | None
    evidence_digest: str | None
    reasons: tuple[str, ...]
    validated_at: str
    min_context: int | None = None


# ----- raw list parsing ------------------------------------------------------
def _unique_pairs(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise CapabilityError("DUPLICATE_KEY")
    return dict(pairs)


def _reject_constant(_name):
    raise CapabilityError("INVALID_JSON_CONSTANT")


def _reject_null_or_empty(document: object) -> None:
    stack = [document]
    while stack:
        value = stack.pop()
        if value is None:
            raise CapabilityError("NULL_VALUE")
        if type(value) is str:
            if not value.strip():
                raise CapabilityError("EMPTY_VALUE")
        elif type(value) is list:
            stack.extend(value)
        elif type(value) is dict:
            stack.extend(value.values())


def _raw_names(values: object, field: str, maximum: int) -> tuple[str, ...]:
    if type(values) is not list or len(values) > maximum:
        raise CapabilityError("INVALID_" + field.upper() + "S")
    seen = set()
    for value in values:
        if type(value) is not str:
            raise CapabilityError("INVALID_" + field.upper())
        key = value.casefold()
        if key in seen:
            raise CapabilityError("DUPLICATE_" + field.upper())
        seen.add(key)
    return tuple(values)


def _parse_model_list(raw: object) -> tuple[str, tuple[tuple[str, tuple, tuple, int | None], ...]]:
    """Return (sha256 of raw bytes, raw entries in original order)."""
    try:
        if type(raw) is str:
            # A code point needs at least one UTF-8 byte, so this pre-check
            # rejects oversized text without first making an encoded copy.
            if len(raw) > _MAX_RAW_BYTES:
                raise CapabilityError("RAW_TOO_LARGE")
            data = raw.encode("utf-8")
        elif isinstance(raw, (bytes, bytearray)):
            if len(raw) > _MAX_RAW_BYTES:
                raise CapabilityError("RAW_TOO_LARGE")
            data = bytes(raw)
        else:
            raise CapabilityError("INVALID_RAW_TYPE")
    except UnicodeEncodeError:
        raise CapabilityError("INVALID_ENCODING") from None
    if len(data) > _MAX_RAW_BYTES:
        raise CapabilityError("RAW_TOO_LARGE")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise CapabilityError("INVALID_ENCODING") from None
    try:
        document = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except CapabilityError:
        raise
    except (ValueError, RecursionError):
        raise CapabilityError("INVALID_JSON") from None
    _reject_null_or_empty(document)
    if type(document) is not dict or set(document) != _DOCUMENT_KEYS:
        raise CapabilityError("INVALID_DOCUMENT")
    models = document["models"]
    if type(models) is not list or len(models) > _MAX_MODELS:
        raise CapabilityError("INVALID_MODELS")
    seen = set()
    parsed = []
    for entry in models:
        if type(entry) is not dict:
            raise CapabilityError("INVALID_ENTRY")
        keys = set(entry)
        if not _ENTRY_KEYS <= keys or not keys <= _ENTRY_KEYS | _OPTIONAL_ENTRY_KEYS:
            raise CapabilityError("INVALID_ENTRY")
        model = entry["model"]
        if type(model) is not str:
            raise CapabilityError("INVALID_MODEL")
        key = model.casefold()
        if key in seen:
            raise CapabilityError("DUPLICATE_MODEL")
        seen.add(key)
        context = None
        if "context_tokens" in entry:
            context = entry["context_tokens"]
            _check_context(context, "INVALID_CONTEXT_TOKENS")
        parsed.append((model, _raw_names(entry["efforts"], "effort", _MAX_EFFORTS),
                       _raw_names(entry["tools"], "tool", _MAX_TOOLS), context))
    return hashlib.sha256(data).hexdigest(), tuple(parsed)


# ----- inventory ---------------------------------------------------------------
class _Resource:
    __slots__ = ("binding", "revision", "snapshots")

    def __init__(self, binding: ConnectionBinding):
        self.binding = binding
        self.revision = 1
        self.snapshots: dict[Provenance, InventorySnapshot] = {}


class CapabilityInventory:
    """Per-resource model lists and (model, effort, tools, context) validation."""

    def __init__(self, *, ttl_seconds: float = 300.0, clock=None):
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 86400):
            raise CapabilityError("INVALID_TTL")
        if clock is not None and not callable(clock):
            raise CapabilityError("INVALID_CALLBACK")
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._ttl = timedelta(seconds=float(ttl_seconds))
        self._resources: dict[str, _Resource] = {}
        self._lock = RLock()

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise CapabilityError("CLOCK_UNAVAILABLE") from None
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise CapabilityError("INVALID_CLOCK")
        return value

    def _resource(self, connection_id: object) -> _Resource:
        _check_id(connection_id, "connection_id")
        try:
            return self._resources[connection_id]
        except KeyError:
            raise UnknownResource("UNKNOWN_RESOURCE") from None

    # ----- resources ---------------------------------------------------------
    def register_resource(self, binding: ConnectionBinding) -> int:
        if type(binding) is not ConnectionBinding:
            raise CapabilityError("INVALID_CONNECTION_BINDING")
        with self._lock:
            if binding.connection_id in self._resources:
                raise CapabilityError("DUPLICATE_RESOURCE")
            self._resources[binding.connection_id] = _Resource(binding)
            return 1

    def update_binding(self, binding: ConnectionBinding) -> int:
        """A new connection revision discards account-scoped lists.

        A retained configured catalog keeps its old connection revision and is
        rejected as BINDING_MISMATCH until it is registered for the new one.
        """
        if type(binding) is not ConnectionBinding:
            raise CapabilityError("INVALID_CONNECTION_BINDING")
        with self._lock:
            resource = self._resource(binding.connection_id)
            old = resource.binding
            if (old.provider, old.runtime, old.auth_kind) != (binding.provider, binding.runtime, binding.auth_kind):
                raise CapabilityError("BINDING_IDENTITY_CHANGED")
            if binding.revision <= old.revision:
                raise CapabilityError("STALE_BINDING_REVISION")
            resource.binding = binding
            for provenance in _ACCOUNT_SCOPED:
                resource.snapshots.pop(provenance, None)
            resource.revision += 1
            return resource.revision

    def resource_revision(self, connection_id: str) -> int:
        with self._lock:
            return self._resource(connection_id).revision

    # ----- lists -------------------------------------------------------------
    def load_list(self, connection_id: str, provenance: Provenance, raw, *,
                  source_ref: str | None = None,
                  observed_at: datetime | None = None) -> InventorySnapshot:
        """Load one list. provider_observed requires source_ref and observed_at."""
        if type(provenance) is not Provenance:
            raise CapabilityError("INVALID_PROVENANCE")
        if source_ref is not None:
            _check_id(source_ref, "source_ref")
        elif provenance is Provenance.CONFIGURED_CATALOG:
            raise CapabilityError("CATALOG_REFERENCE_REQUIRED")
        elif provenance is Provenance.PROVIDER_OBSERVED:
            raise CapabilityError("OBSERVATION_SOURCE_REQUIRED")
        if observed_at is not None:
            if (type(observed_at) is not datetime or observed_at.tzinfo is None
                    or observed_at.utcoffset() is None):
                raise CapabilityError("INVALID_OBSERVED_AT")
        elif provenance is Provenance.PROVIDER_OBSERVED:
            raise CapabilityError("OBSERVED_AT_REQUIRED")
        digest, parsed = _parse_model_list(raw)
        with self._lock:
            resource = self._resource(connection_id)
            entries = tuple(ModelCapability(connection_id, model, efforts, tools, provenance, context)
                            for model, efforts, tools, context in parsed)
            now = self._now()
            stamp = now if observed_at is None else observed_at
            if stamp > now:
                raise CapabilityError("OBSERVED_AT_IN_FUTURE")
            snapshot = InventorySnapshot(
                connection_id=connection_id,
                connection_revision=resource.binding.revision,
                provenance=provenance,
                digest=digest,
                models=tuple(entry.model for entry in entries),
                entries=entries,
                observed_at=_iso(stamp),
                source_ref=source_ref,
                loaded_at=_iso(now),
            )
            old = resource.snapshots.get(provenance)
            if old is None or (old.digest, old.connection_revision, old.source_ref) != (
                    snapshot.digest, snapshot.connection_revision, snapshot.source_ref):
                resource.revision += 1
            resource.snapshots[provenance] = snapshot
            return snapshot

    def register_manual(self, connection_id: str, raw, *, catalog_ref: str) -> InventorySnapshot:
        """Verified manual registration: a configured catalog with its verification reference."""
        if catalog_ref is None:
            raise CapabilityError("CATALOG_REFERENCE_REQUIRED")
        return self.load_list(connection_id, Provenance.CONFIGURED_CATALOG, raw, source_ref=catalog_ref)

    def remove_list(self, connection_id: str, provenance: Provenance) -> None:
        if type(provenance) is not Provenance:
            raise CapabilityError("INVALID_PROVENANCE")
        with self._lock:
            resource = self._resource(connection_id)
            if resource.snapshots.pop(provenance, None) is not None:
                resource.revision += 1

    def snapshot(self, connection_id: str, provenance: Provenance) -> InventorySnapshot | None:
        if type(provenance) is not Provenance:
            raise CapabilityError("INVALID_PROVENANCE")
        with self._lock:
            return self._resource(connection_id).snapshots.get(provenance)

    def _snapshot_freshness(self, snapshot: InventorySnapshot, now: datetime) -> Freshness:
        observed = datetime.fromisoformat(snapshot.observed_at.replace("Z", "+00:00"))
        age = now - observed
        # A clock that moves backwards is not proof of freshness.
        if age < timedelta(0) or age > self._ttl:
            return Freshness.STALE
        return Freshness.FRESH

    # ----- validation --------------------------------------------------------
    def validate(self, connection_id: str, model: str, effort: str | None = None,
                 tools: tuple[str, ...] = (), *, min_context: int | None = None) -> ValidationResult:
        _check_model(model)
        if effort is not None:
            _check_id(effort, "effort")
        _check_names(tools, "tool", _MAX_TOOLS)
        if min_context is not None:
            _check_context(min_context, "INVALID_MIN_CONTEXT")
        with self._lock:
            resource = self._resource(connection_id)
            now = self._now()
            observed = resource.snapshots.get(Provenance.PROVIDER_OBSERVED)
            catalog = resource.snapshots.get(Provenance.CONFIGURED_CATALOG)
            snapshot = observed if observed is not None else catalog
            reasons: list[str] = []
            supported_by = None
            digest = None
            if snapshot is None:
                reasons.append("NO_SUPPORT_EVIDENCE")
            else:
                prefix = snapshot.provenance.value
                entry = None
                if snapshot.connection_revision != resource.binding.revision:
                    reasons.append(prefix + ":BINDING_MISMATCH")
                elif (snapshot.provenance is Provenance.PROVIDER_OBSERVED
                      and self._snapshot_freshness(snapshot, now) is not Freshness.FRESH):
                    reasons.append(prefix + ":INVENTORY_STALE")
                else:
                    entry = snapshot.find(model)
                    if entry is None:
                        reasons.append(prefix + ":MODEL_NOT_LISTED")
                if entry is not None:
                    if effort is not None and effort not in entry.efforts:
                        reasons.append(prefix + ":EFFORT_NOT_SUPPORTED")
                    for tool in tools:
                        if tool not in entry.tools:
                            reasons.append(prefix + ":TOOL_NOT_SUPPORTED:" + tool)
                    if min_context is not None:
                        if entry.context_tokens is None:
                            reasons.append(prefix + ":CONTEXT_UNKNOWN")
                        elif entry.context_tokens < min_context:
                            reasons.append(prefix + ":CONTEXT_NOT_SUPPORTED")
                    if not reasons:
                        supported_by, digest = snapshot.provenance, snapshot.digest
            if supported_by is None:
                for provenance in _NON_EVIDENCE:
                    other = resource.snapshots.get(provenance)
                    if other is not None and other.find(model) is not None:
                        reasons.append(provenance.value + ":NOT_SUPPORT_EVIDENCE")
            return ValidationResult(
                connection_id=connection_id,
                connection_revision=resource.binding.revision,
                resource_revision=resource.revision,
                model=model,
                effort=effort,
                tools=tools,
                request_provenance=Provenance.REQUESTED,
                accepted=supported_by is not None,
                supported_by=supported_by,
                evidence_digest=digest,
                reasons=tuple(reasons),
                validated_at=_iso(now),
                min_context=min_context,
            )

    def freshness(self, result: ValidationResult) -> Freshness:
        """STALE once the lists or binding change, the observed list expires, or
        an acceptance does not match the current evidence snapshot and digest."""
        if type(result) is not ValidationResult:
            raise CapabilityError("INVALID_VALIDATION_RESULT")
        with self._lock:
            resource = self._resources.get(result.connection_id)
            if resource is None:
                return Freshness.UNKNOWN
            if (resource.binding.revision, resource.revision) != (
                    result.connection_revision, result.resource_revision):
                return Freshness.STALE
            if result.accepted is not (result.supported_by is not None):
                return Freshness.STALE
            if result.supported_by is None:
                return Freshness.FRESH
            if type(result.supported_by) is not Provenance or result.supported_by not in _EVIDENCE:
                return Freshness.STALE
            snapshot = resource.snapshots.get(result.supported_by)
            if (snapshot is None or snapshot.digest != result.evidence_digest
                    or snapshot.connection_revision != resource.binding.revision):
                return Freshness.STALE
            if result.supported_by is Provenance.PROVIDER_OBSERVED:
                return self._snapshot_freshness(snapshot, self._now())
            # An observed list takes precedence over any catalog acceptance.
            if Provenance.PROVIDER_OBSERVED in resource.snapshots:
                return Freshness.STALE
            return Freshness.FRESH

    def revalidate(self, result: ValidationResult) -> ValidationResult:
        if type(result) is not ValidationResult:
            raise CapabilityError("INVALID_VALIDATION_RESULT")
        return self.validate(result.connection_id, result.model, result.effort, result.tools,
                             min_context=result.min_context)


__all__ = [
    "CapabilityError", "CapabilityInventory", "InventorySnapshot", "ModelCapability",
    "Provenance", "UnknownResource", "ValidationResult",
]
