"""Immutable artifact manifest records. No field holds file contents or secrets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import re
from types import MappingProxyType

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier
from aik_workspace import normalize_input_path
from aik_workspace.errors import InvalidInputPath

from .errors import InvalidEvidenceRecord


KINDS = ("authoritative", "derivative")
ACCESS_SCOPES = ("workspace", "managed")
VERIFY_STATUSES = ("intact", "missing", "tampered")
CONFLICT_CODES = ("CALLBACK_CONFLICT", "DERIVATIVE_CONFLICT")
COMPARED_FIELDS = ("sha256", "size", "vm_attempt_id", "lease_id", "workspace_id",
                   "relative_path", "access_scope")
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TRANSFORM_KEYS = frozenset({"name", "version", "config_digest"})


def check_identifier(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise InvalidEvidenceRecord("INVALID_" + field.upper()) from None


def check_digest(value: object, field: str = "sha256") -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise InvalidEvidenceRecord("INVALID_" + field.upper())


def check_transform(value: object) -> MappingProxyType:
    if not isinstance(value, Mapping) or set(value) != _TRANSFORM_KEYS:
        raise InvalidEvidenceRecord("INVALID_TRANSFORM")
    check_identifier(value["name"], "transform_name")
    check_identifier(value["version"], "transform_version")
    check_digest(value["config_digest"], "config_digest")
    return MappingProxyType({"name": value["name"], "version": value["version"],
                             "config_digest": value["config_digest"]})


def check_timestamp(value: object, field: str = "recorded_at") -> None:
    if type(value) is not str:
        raise InvalidEvidenceRecord("INVALID_" + field.upper())
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidEvidenceRecord("INVALID_" + field.upper()) from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise InvalidEvidenceRecord("INVALID_" + field.upper())


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """One manifest entry: identity, digest, producing attempt and access scope.

    An authoritative record is the collected worker output. A derivative record
    is a separate display artifact with ``derived_from`` and ``transform``
    provenance; it never replaces its authoritative source.
    """

    artifact_id: str
    kind: str
    sha256: str
    size: int
    vm_attempt_id: str
    lease_id: str
    callback_id: str | None
    workspace_id: str
    relative_path: str
    access_scope: str
    derived_from: str | None
    transform: Mapping | None
    recorded_at: str

    def __post_init__(self) -> None:
        check_identifier(self.artifact_id, "artifact_id")
        if type(self.kind) is not str or self.kind not in KINDS:
            raise InvalidEvidenceRecord("INVALID_KIND")
        check_digest(self.sha256)
        if type(self.size) is not int or not 0 <= self.size <= MAX_INTEGER:
            raise InvalidEvidenceRecord("INVALID_SIZE")
        for field in ("vm_attempt_id", "lease_id", "workspace_id"):
            check_identifier(getattr(self, field), field)
        if self.callback_id is not None:
            check_identifier(self.callback_id, "callback_id")
        try:
            normalize_input_path(self.relative_path)
        except InvalidInputPath:
            raise InvalidEvidenceRecord("INVALID_RELATIVE_PATH") from None
        if type(self.access_scope) is not str or self.access_scope not in ACCESS_SCOPES:
            raise InvalidEvidenceRecord("INVALID_ACCESS_SCOPE")
        if self.derived_from is not None:
            check_identifier(self.derived_from, "derived_from")
        if self.transform is not None:
            object.__setattr__(self, "transform", check_transform(self.transform))
        check_timestamp(self.recorded_at)
        if self.kind == "authoritative":
            if self.callback_id is None or self.derived_from is not None or self.transform is not None:
                raise InvalidEvidenceRecord("INCONSISTENT_AUTHORITATIVE_RECORD")
        elif self.callback_id is not None or self.derived_from is None or self.transform is None:
            raise InvalidEvidenceRecord("INCONSISTENT_DERIVATIVE_RECORD")

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id, "kind": self.kind, "sha256": self.sha256,
            "size": self.size, "vm_attempt_id": self.vm_attempt_id, "lease_id": self.lease_id,
            "callback_id": self.callback_id, "workspace_id": self.workspace_id,
            "relative_path": self.relative_path, "access_scope": self.access_scope,
            "derived_from": self.derived_from,
            "transform": None if self.transform is None else dict(self.transform),
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """Append-only record of a replay that disagreed; it lists field names, not values."""

    seq: int
    code: str
    subject_id: str
    artifact_id: str
    existing_sha256: str
    observed_sha256: str
    fields: tuple[str, ...]
    recorded_at: str

    def __post_init__(self) -> None:
        if type(self.seq) is not int or self.seq < 1:
            raise InvalidEvidenceRecord("INVALID_SEQ")
        if self.code not in CONFLICT_CODES:
            raise InvalidEvidenceRecord("INVALID_CONFLICT_CODE")
        check_identifier(self.subject_id, "subject_id")
        check_identifier(self.artifact_id, "artifact_id")
        check_digest(self.existing_sha256, "existing_sha256")
        check_digest(self.observed_sha256, "observed_sha256")
        if (type(self.fields) is not tuple or not self.fields
                or any(field not in COMPARED_FIELDS for field in self.fields)):
            raise InvalidEvidenceRecord("INVALID_CONFLICT_FIELDS")
        check_timestamp(self.recorded_at)


@dataclass(frozen=True, slots=True)
class VerifyResult:
    artifact_id: str
    status: str
    expected_sha256: str
    observed_sha256: str | None

    @property
    def intact(self) -> bool:
        return self.status == "intact"
