"""Independent check records. Fields hold IDs, digests and flags, never output text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier

from .errors import ValidationError


LOCAL_DERIVED = "local_derived"
REVIEWER_ROLE = "reviewer"
VERDICTS = ("PASS", "FAIL", "ORACLE_CHANGED")
MAX_TEXT = 512


def check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise ValidationError("INVALID_" + field.upper()) from None


def check_text(value: object, field: str, *, limit: int = MAX_TEXT) -> None:
    if type(value) is not str or not 1 <= len(value) <= limit or not value.isprintable():
        raise ValidationError("INVALID_" + field.upper())


def check_timestamp(value: object, field: str = "recorded_at") -> None:
    if type(value) is not str:
        raise ValidationError("INVALID_" + field.upper())
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("INVALID_" + field.upper()) from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValidationError("INVALID_" + field.upper())


def compute_verdict(passed: bool, oracle_ok: bool) -> str:
    """PASS only when the command passed and the fixed oracle is unchanged."""
    if not oracle_ok:
        return "ORACLE_CHANGED"
    return "PASS" if passed else "FAIL"


def same_session(first: str, second: str) -> bool:
    return first.casefold() == second.casefold()


@dataclass(frozen=True, slots=True)
class CheckRecord:
    """One independent verification run by a reviewer session (AIK-local evidence).

    It is never a VM validation result or a GoalVerdict.
    """

    check_id: str
    vm_attempt_id: str
    command_id: str
    definition_digest: str
    passed: bool
    oracle_ok: bool
    exit_code: int | None
    output_digest: str
    reviewer_role: str
    reviewer_session_id: str
    worker_session_id: str
    tool_call_id: str
    recorded_at: str
    provenance: str = LOCAL_DERIVED

    def __post_init__(self) -> None:
        for name in ("check_id", "vm_attempt_id", "command_id", "reviewer_session_id",
                     "worker_session_id", "tool_call_id"):
            check_id(getattr(self, name), name)
        check_text(self.definition_digest, "definition_digest", limit=256)
        check_text(self.output_digest, "output_digest", limit=256)
        if type(self.passed) is not bool:
            raise ValidationError("INVALID_PASSED")
        if type(self.oracle_ok) is not bool:
            raise ValidationError("INVALID_ORACLE_OK")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValidationError("INVALID_EXIT_CODE")
        if self.reviewer_role != REVIEWER_ROLE:
            raise ValidationError("NOT_INDEPENDENT")
        if same_session(self.reviewer_session_id, self.worker_session_id):
            raise ValidationError("NOT_INDEPENDENT")
        check_timestamp(self.recorded_at)
        if self.provenance != LOCAL_DERIVED:
            raise ValidationError("INVALID_PROVENANCE")

    @property
    def verdict(self) -> str:
        return compute_verdict(self.passed, self.oracle_ok)
