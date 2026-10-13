"""AIK-033 placement inputs and decision records. No field holds secret material."""

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import re
from types import MappingProxyType

from aik_runtime import Placement
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier


ROLES = ("director", "worker", "reviewer")
PURPOSES = ("operation", "qualification")
DECISION_STATUSES = ("placed", "wait", "rejected")
CHECK_OUTCOMES = ("eligible", "wait", "rejected")
MAX_TOOLS = 64
# Same bound as aik_runtime ExecutionProfile.model.
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")


class PlacementError(ValueError):
    """Fixed code-only failure; the message never contains supplied values."""


def check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise PlacementError("INVALID_" + field.upper()) from None


def check_model(value: object) -> None:
    if type(value) is not str or _MODEL.fullmatch(value) is None:
        raise PlacementError("INVALID_MODEL")


@dataclass(frozen=True, slots=True)
class ReadyTask:
    """Only the values VM reported for a ready, authorized Task attempt.

    AIK does not re-judge readiness or authorization; vm_authorization_ref is
    an opaque VM-issued reference that is copied into the decision record.
    """

    vm_instance_id: str
    vm_project_id: str
    vm_task_id: str
    vm_attempt_id: str
    vm_authorization_ref: str
    role: str
    model: str
    effort: str | None = None
    tools: tuple[str, ...] = ()
    purpose: str = "operation"
    candidate_route: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        for name in ("vm_instance_id", "vm_project_id", "vm_task_id", "vm_attempt_id", "vm_authorization_ref"):
            check_id(getattr(self, name), name)
        if type(self.role) is not str or self.role not in ROLES:
            raise PlacementError("INVALID_ROLE")
        check_model(self.model)
        if self.effort is not None:
            check_id(self.effort, "effort")
        if type(self.tools) is not tuple or len(self.tools) > MAX_TOOLS:
            raise PlacementError("INVALID_TOOLS")
        for tool in self.tools:
            check_id(tool, "tool")
        if len({tool.casefold() for tool in self.tools}) != len(self.tools):
            raise PlacementError("DUPLICATE_TOOL")
        if type(self.purpose) is not str or self.purpose not in PURPOSES:
            raise PlacementError("INVALID_PURPOSE")
        if self.candidate_route is not None:
            if type(self.candidate_route) is not tuple or len(self.candidate_route) != 2:
                raise PlacementError("INVALID_CANDIDATE_ROUTE")
            check_id(self.candidate_route[0], "candidate_pool_id")
            check_model(self.candidate_route[1])


@dataclass(frozen=True, slots=True)
class ParallelQualification:
    """Caller-supplied basis for running another attempt of the same project.

    evidence_ref references an external concurrency qualification record.
    inventory_revision must equal the current capability inventory resource
    revision of the chosen route's connection.
    """

    evidence_ref: str
    inventory_revision: int

    def __post_init__(self) -> None:
        check_id(self.evidence_ref, "evidence_ref")
        if type(self.inventory_revision) is not int or not 1 <= self.inventory_revision <= MAX_INTEGER:
            raise PlacementError("INVALID_INVENTORY_REVISION")


@dataclass(frozen=True, slots=True)
class ProfileCheck:
    """One candidate profile's verdict and every reason code found for it."""

    profile_id: str
    pool_id: str | None
    outcome: str
    reasons: tuple[str, ...]
    resource_revision: int | None


@dataclass(frozen=True, slots=True)
class PlacementDecision:
    decision_id: str
    status: str
    task: ReadyTask
    placement: object  # aik_runtime.Versioned[Placement] | None
    profile_id: str | None
    pool_id: str | None
    reasons: tuple[str, ...]
    checks: tuple[ProfileCheck, ...]
    inventory_revision: MappingProxyType
    parallel_basis: ParallelQualification | None
    decided_at: str
    digest: str

    @property
    def placed(self) -> bool:
        return self.status == "placed"


def rebuild(value, model):
    """Reconstruct exact instances so object.__setattr__ forgeries are revalidated."""
    if type(value) is not model:
        raise PlacementError("INVALID_" + model.__name__.upper())
    return model(**{item.name: getattr(value, item.name) for item in fields(model)})


def task_to_dict(task: ReadyTask) -> dict:
    document = {item.name: getattr(task, item.name) for item in fields(ReadyTask)}
    document["tools"] = list(task.tools)
    document["candidate_route"] = None if task.candidate_route is None else list(task.candidate_route)
    return document


def _placement_to_dict(entry) -> dict | None:
    if entry is None:
        return None
    if type(entry.value) is not Placement:
        raise PlacementError("INVALID_DECISION")
    return {"value": {item.name: getattr(entry.value, item.name) for item in fields(Placement)},
            "revision": entry.revision}


def decision_document(decision: PlacementDecision) -> dict:
    """Canonical content of a decision, without its digest."""
    try:
        return {
            "decision_id": decision.decision_id,
            "status": decision.status,
            "task": task_to_dict(decision.task),
            "placement": _placement_to_dict(decision.placement),
            "profile_id": decision.profile_id,
            "pool_id": decision.pool_id,
            "reasons": list(decision.reasons),
            "checks": [
                {"profile_id": check.profile_id, "pool_id": check.pool_id, "outcome": check.outcome,
                 "reasons": list(check.reasons), "resource_revision": check.resource_revision}
                for check in decision.checks
            ],
            "inventory_revision": dict(decision.inventory_revision),
            "parallel_basis": None if decision.parallel_basis is None else {
                "evidence_ref": decision.parallel_basis.evidence_ref,
                "inventory_revision": decision.parallel_basis.inventory_revision,
            },
            "decided_at": decision.decided_at,
        }
    except PlacementError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise PlacementError("INVALID_DECISION") from None


def canonical_json(document: dict) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_digest(document: dict) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
