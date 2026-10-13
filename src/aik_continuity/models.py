"""Continuity records: user directives, model summaries, config and VM references.

Nothing here is an authority copy. Goals, plans, tasks, attempts, verdicts and
evidence belong to the VM/AGS ledgers; a checkpoint stores only their IDs and
revision IDs so that a restarted director can re-read them from VM.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import re

from aik_inbox.errors import InvalidMessage
from aik_inbox.models import canonical_payload
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier

from .errors import InvalidContinuityRecord


DIRECTIVE_KINDS = ("instruction", "correction")
CORRECTION_SCOPES = ("replace", "supplement", "withdraw")
DIRECTIVE_STATUSES = ("active", "replaced", "withdrawn", "applied")
SUMMARY_AUTHORITY = "model_reported"
MAX_TASK_REFS = 1024
MAX_EVIDENCE_REFS = 1024
MAX_CONFIG_REFS = 64
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BODY_KEYS = frozenset((
    "goal_id", "goal_revision_id", "plan_revision_id", "authorization_revision_id", "tasks",
    "evidence_refs", "directive_seq", "config_refs", "summary_id",
))
_TASK_KEYS = frozenset(("task_id", "attempt_id", "evidence"))


def check_id(value: object, name: str) -> None:
    try:
        identifier(value, name)
    except InvalidRecord:
        raise InvalidContinuityRecord(f"Invalid identifier field: {name}.") from None


def is_id(value: object) -> bool:
    try:
        identifier(value)
    except InvalidRecord:
        return False
    return True


def _optional_id(value: object, name: str) -> None:
    if value is not None:
        check_id(value, name)


def _check_text(value: object, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise InvalidContinuityRecord(f"The {name} must be nonempty text.")
    try:
        # Reuse the inbox bound (16 KiB) and its secret-value heuristic.
        canonical_payload({"text": value})
    except InvalidMessage:
        raise InvalidContinuityRecord(f"The {name} is too large or looks like secret material.") from None


def _nonnegative(value: object, name: str) -> None:
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise InvalidContinuityRecord(f"Invalid nonnegative integer field: {name}.")


def _positive(value: object, name: str) -> None:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise InvalidContinuityRecord(f"Invalid positive integer field: {name}.")


def _id_tuple(value: object, name: str, maximum: int) -> None:
    if type(value) is not tuple or len(value) > maximum:
        raise InvalidContinuityRecord(f"The {name} must be a bounded tuple.")
    for item in value:
        check_id(item, name)
    if len(set(value)) != len(value):
        raise InvalidContinuityRecord(f"The {name} must not repeat identifiers.")


def rebuild(value):
    """Reconstruct a frozen record: object.__setattr__ can bypass __post_init__."""
    return type(value)(**{field.name: getattr(value, field.name) for field in fields(value)})


@dataclass(frozen=True, slots=True)
class Directive:
    """A user's verbatim instruction, or a correction of an earlier directive.

    A correction names its target and scope: ``replace`` (the target no longer
    applies, this text does), ``supplement`` (both apply) or ``withdraw`` (the
    target no longer applies; this text records why).
    """

    directive_id: str
    project_id: str
    kind: str
    text: str
    target_id: str | None = None
    scope: str | None = None

    def __post_init__(self) -> None:
        check_id(self.directive_id, "directive_id")
        check_id(self.project_id, "project_id")
        if type(self.kind) is not str or self.kind not in DIRECTIVE_KINDS:
            raise InvalidContinuityRecord("Unknown directive kind.")
        _check_text(self.text, "directive text")
        if self.kind == "instruction":
            if self.target_id is not None or self.scope is not None:
                raise InvalidContinuityRecord("An instruction cannot name a correction target or scope.")
        else:
            check_id(self.target_id, "target_id")
            if type(self.scope) is not str or self.scope not in CORRECTION_SCOPES:
                raise InvalidContinuityRecord("A correction requires a known scope.")
            if self.target_id == self.directive_id:
                raise InvalidContinuityRecord("A correction cannot target itself.")


@dataclass(frozen=True, slots=True)
class ContextSummary:
    """A model-written compaction summary. It is auxiliary, never authority."""

    summary_id: str
    project_id: str
    text: str
    covers_through_seq: int = 0

    def __post_init__(self) -> None:
        check_id(self.summary_id, "summary_id")
        check_id(self.project_id, "project_id")
        _check_text(self.text, "summary text")
        _nonnegative(self.covers_through_seq, "covers_through_seq")

    @property
    def authority(self) -> str:
        return SUMMARY_AUTHORITY


@dataclass(frozen=True, slots=True)
class ConfigRevision:
    """A setting revision and the digest of its content; the content is not stored."""

    config_id: str
    revision: int
    digest: str

    def __post_init__(self) -> None:
        check_id(self.config_id, "config_id")
        _positive(self.revision, "revision")
        if type(self.digest) is not str or _DIGEST.fullmatch(self.digest) is None:
            raise InvalidContinuityRecord("A config digest must be 64 lowercase hex characters.")


@dataclass(frozen=True, slots=True)
class TaskRef:
    """VM task/attempt IDs and evidence IDs; deliberately no state field."""

    task_id: str
    attempt_id: str | None = None
    evidence: tuple = ()

    def __post_init__(self) -> None:
        check_id(self.task_id, "task_id")
        _optional_id(self.attempt_id, "attempt_id")
        _id_tuple(self.evidence, "evidence", MAX_EVIDENCE_REFS)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """References into the VM ledger only. No Goal/Task state, no verdict copy."""

    project_id: str
    goal_id: str
    goal_revision_id: str
    plan_revision_id: str
    tasks: tuple = ()
    evidence_refs: tuple = ()
    authorization_revision_id: str | None = None
    directive_seq: int = 0
    config_refs: tuple = ()
    summary_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("project_id", "goal_id", "goal_revision_id", "plan_revision_id"):
            check_id(getattr(self, name), name)
        _optional_id(self.authorization_revision_id, "authorization_revision_id")
        _optional_id(self.summary_id, "summary_id")
        if type(self.tasks) is not tuple or len(self.tasks) > MAX_TASK_REFS:
            raise InvalidContinuityRecord("Checkpoint tasks must be a bounded tuple.")
        tasks = []
        for item in self.tasks:
            if type(item) is not TaskRef:
                raise InvalidContinuityRecord("Checkpoint tasks must be TaskRef records.")
            tasks.append(rebuild(item))
        if len({task.task_id for task in tasks}) != len(tasks):
            raise InvalidContinuityRecord("Checkpoint task IDs must be unique.")
        object.__setattr__(self, "tasks", tuple(tasks))
        _id_tuple(self.evidence_refs, "evidence_refs", MAX_EVIDENCE_REFS)
        _nonnegative(self.directive_seq, "directive_seq")
        if type(self.config_refs) is not tuple or len(self.config_refs) > MAX_CONFIG_REFS:
            raise InvalidContinuityRecord("Config references must be a bounded tuple.")
        for item in self.config_refs:
            if type(item) is not tuple or len(item) != 2:
                raise InvalidContinuityRecord("A config reference is a (config_id, revision) pair.")
            check_id(item[0], "config_id")
            _positive(item[1], "config revision")
        if len({item[0] for item in self.config_refs}) != len(self.config_refs):
            raise InvalidContinuityRecord("Config references must name each config once.")

    def body(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "goal_revision_id": self.goal_revision_id,
            "plan_revision_id": self.plan_revision_id,
            "authorization_revision_id": self.authorization_revision_id,
            "tasks": [{"task_id": task.task_id, "attempt_id": task.attempt_id, "evidence": list(task.evidence)}
                      for task in self.tasks],
            "evidence_refs": list(self.evidence_refs),
            "directive_seq": self.directive_seq,
            "config_refs": [[config_id, revision] for config_id, revision in self.config_refs],
            "summary_id": self.summary_id,
        }

    def to_dict(self) -> dict:
        return {"project_id": self.project_id, **self.body()}

    @classmethod
    def from_body(cls, project_id: str, body: object) -> "Checkpoint":
        if type(body) is not dict or set(body) != _BODY_KEYS:
            raise InvalidContinuityRecord("Invalid checkpoint body.")
        if type(body["tasks"]) is not list or type(body["evidence_refs"]) is not list \
                or type(body["config_refs"]) is not list:
            raise InvalidContinuityRecord("Invalid checkpoint body.")
        tasks = []
        for item in body["tasks"]:
            if type(item) is not dict or set(item) != _TASK_KEYS or type(item["evidence"]) is not list:
                raise InvalidContinuityRecord("Invalid checkpoint task reference.")
            tasks.append(TaskRef(item["task_id"], item["attempt_id"], tuple(item["evidence"])))
        configs = []
        for item in body["config_refs"]:
            if type(item) is not list:
                raise InvalidContinuityRecord("Invalid checkpoint config reference.")
            configs.append(tuple(item))
        return cls(
            project_id=project_id, goal_id=body["goal_id"], goal_revision_id=body["goal_revision_id"],
            plan_revision_id=body["plan_revision_id"], tasks=tuple(tasks),
            evidence_refs=tuple(body["evidence_refs"]),
            authorization_revision_id=body["authorization_revision_id"],
            directive_seq=body["directive_seq"], config_refs=tuple(configs), summary_id=body["summary_id"],
        )


@dataclass(frozen=True, slots=True)
class StoredDirective:
    directive: Directive
    seq: int
    recorded_ms: int


@dataclass(frozen=True, slots=True)
class StoredSummary:
    summary: ContextSummary
    seq: int
    recorded_ms: int


@dataclass(frozen=True, slots=True)
class StoredConfig:
    config: ConfigRevision
    recorded_ms: int


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    checkpoint: Checkpoint
    revision: int
    recorded_ms: int


@dataclass(frozen=True, slots=True)
class Appended:
    """status is 'accepted' or 'duplicate' (identical record already stored)."""

    status: str
    record: object


@dataclass(frozen=True, slots=True)
class EffectiveDirective:
    """A stored directive with its resolved status; history is never dropped."""

    directive_id: str
    seq: int
    kind: str
    text: str
    target_id: str | None
    scope: str | None
    status: str
    superseded_by: str | None
    supplemented_by: tuple

    def to_dict(self) -> dict:
        return {
            "directive_id": self.directive_id, "seq": self.seq, "kind": self.kind, "text": self.text,
            "target_id": self.target_id, "scope": self.scope, "status": self.status,
            "superseded_by": self.superseded_by, "supplemented_by": list(self.supplemented_by),
        }


def resolve_directives(records) -> tuple:
    """Apply corrections in seq order. Withdrawing a correction revives nothing."""
    ordered = sorted(records, key=lambda record: record.seq)
    status: dict[str, str] = {}
    superseded: dict[str, str] = {}
    supplements: dict[str, list] = {}
    for record in ordered:
        directive = record.directive
        if directive.kind == "correction":
            target = directive.target_id
            if target not in status:
                raise InvalidContinuityRecord("A correction target does not precede its correction.")
            if directive.scope == "supplement":
                supplements.setdefault(target, []).append(directive.directive_id)
            else:
                status[target] = "replaced" if directive.scope == "replace" else "withdrawn"
                superseded[target] = directive.directive_id
        status[directive.directive_id] = "applied" if directive.scope == "withdraw" else "active"
    return tuple(
        EffectiveDirective(
            record.directive.directive_id, record.seq, record.directive.kind, record.directive.text,
            record.directive.target_id, record.directive.scope, status[record.directive.directive_id],
            superseded.get(record.directive.directive_id),
            tuple(supplements.get(record.directive.directive_id, ())),
        )
        for record in ordered
    )


@dataclass(frozen=True, slots=True)
class HandoffPacket:
    """Directive text, model summary (non-authoritative) and VM references only.

    It has no field for credentials or secret values; directive and summary
    text pass the inbox secret heuristic before they are stored.
    """

    project_id: str
    directives: tuple
    checkpoint: StoredCheckpoint | None
    summary: StoredSummary | None
    configs: tuple

    @property
    def active_directive_ids(self) -> tuple:
        return tuple(item.directive_id for item in self.directives if item.status == "active")

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "directives": [item.to_dict() for item in self.directives],
            "active_directive_ids": list(self.active_directive_ids),
            "checkpoint": None if self.checkpoint is None else {
                "revision": self.checkpoint.revision, **self.checkpoint.checkpoint.to_dict(),
            },
            "summary": None if self.summary is None else {
                "summary_id": self.summary.summary.summary_id,
                "text": self.summary.summary.text,
                "covers_through_seq": self.summary.summary.covers_through_seq,
                "authority": SUMMARY_AUTHORITY,
            },
            "configs": [[item.config.config_id, item.config.revision, item.config.digest] for item in self.configs],
            "authority_note": "VM_LEDGER_AUTHORITATIVE_REVERIFY_WITH_RESUME",
        }
