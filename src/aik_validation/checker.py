"""Run a policy-fixed verification command as an independent reviewer and record it."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid

from aik_authority import ToolSession
from aik_devtools import DevTools
from aik_devtools.tools import VerificationResult

from .errors import ValidationError
from .journal import CheckJournal
from .models import REVIEWER_ROLE, CheckRecord, check_id, same_session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("INVALID_CLOCK")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class IndependentChecker:
    """Independent check of worker output through DevTools.run_verification only.

    The command, argv and oracle come from the trusted DevTools policy. The
    reviewer cannot supply argv either. The record is an AIK-local evidence
    candidate for VM; it is never a VM validation or a GoalVerdict.
    """

    def __init__(self, tools: DevTools, journal_path: Path | str, *, clock=None, timeout: float = 5.0):
        if not isinstance(tools, DevTools):
            raise ValidationError("INVALID_TOOLS")
        if clock is not None and not callable(clock):
            raise ValidationError("INVALID_CLOCK")
        self._tools = tools
        self._clock = clock or _utc_now
        self._journal = CheckJournal(journal_path, timeout=timeout)

    @property
    def journal(self) -> CheckJournal:
        return self._journal

    def run(self, vm_attempt_id: str, *, worker_session_id: str, reviewer_session: ToolSession,
            invocation, command_id: str) -> CheckRecord:
        check_id(vm_attempt_id, "vm_attempt_id")
        check_id(worker_session_id, "worker_session_id")
        check_id(command_id, "command_id")
        if not isinstance(reviewer_session, ToolSession):
            raise ValidationError("INVALID_REVIEWER_SESSION")
        session_id = getattr(reviewer_session, "session_id", None)
        role = getattr(reviewer_session, "role", None)
        check_id(session_id, "reviewer_session_id")
        # Independence is decided before anything runs: a worker cannot review
        # itself and a non-reviewer role cannot produce review evidence.
        if role != REVIEWER_ROLE or same_session(session_id, worker_session_id):
            raise ValidationError("NOT_INDEPENDENT")
        result = self._tools.run_verification(reviewer_session, invocation, command_id)
        if type(result) is not VerificationResult or not self._tools.verify_result(result):
            raise ValidationError("UNVERIFIED_RESULT")
        if result.command_id != command_id:
            raise ValidationError("UNVERIFIED_RESULT")
        record = CheckRecord(
            check_id="check-" + uuid.uuid4().hex,
            vm_attempt_id=vm_attempt_id,
            command_id=result.command_id,
            definition_digest=result.definition_digest,
            passed=bool(result.passed),
            oracle_ok=bool(result.oracle_ok),
            exit_code=result.exit_code,
            output_digest=result.output_digest,
            reviewer_role=REVIEWER_ROLE,
            reviewer_session_id=session_id,
            worker_session_id=worker_session_id,
            tool_call_id=result.tool_call_id,
            recorded_at=_stamp(self._clock()),
        )
        return self._journal.append(record)

    def history(self, vm_attempt_id: str) -> tuple[CheckRecord, ...]:
        return self._journal.history(vm_attempt_id)
