"""Resend scheduler: ToolEffectAdapter's ``resend`` callable over the real ``ApiToolLoop.resume``.

The reconciler hands ``(request_id, tool_call_id)`` for a tool call whose
receipt or denial is journaled but whose result never reached the model. This
scheduler runs ``ApiToolLoop.resume`` for that request: the loop replays the
journaled result (never re-executing the effect), sends it and writes
``returned`` before its next model call.

- The resume inputs (loop, session, revisions and the message transcript with
  the assistant ``tool_calls``) are owned by the caller and looked up per
  request. The dispatch journal stores no payloads or messages, so nothing is
  rebuilt or invented here; a missing context is ``RESUME_CONTEXT_MISSING``.
- ``resume`` handles every pending call of a request at once. A call already
  ``returned`` (for example by a sibling's resume) is done without another
  resume, so the model is not called again. A resume outcome is reused while
  the request's journal is unchanged, so a failing replay is not repeated.
- A resend counts as done only when the journal shows ``returned`` for that
  call. Otherwise a fixed ``RecoveryError`` code is raised, so the reconciler
  records ``failed`` instead of ``applied``.

DevTools replay by effect ID lives in process memory. After a real process
restart a receipt-backed replay stops with ``RESEND_REPLAY_MISMATCH`` and no
``returned`` is written. No new effect intent is made, but a new DevTools may
re-observe an idempotent create and forward one more receipt to the authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from aik_dispatch import ApiToolLoop, DispatchError, DispatchIdentity, DispatchJournal, DispatchOutcomeUnknown

from .models import RecoveryError


@dataclass(frozen=True, slots=True)
class ResumeContext:
    """Caller-owned inputs that ``ApiToolLoop.resume`` needs for one request."""

    loop: ApiToolLoop
    identity: DispatchIdentity
    session: object
    input_revision: str
    authorization_revision: int
    messages: tuple


class LoopResendScheduler:
    """``resend(request_id, tool_call_id)`` for ``ToolEffectAdapter``, backed by ``ApiToolLoop.resume``."""

    def __init__(self, journal: DispatchJournal, contexts: Callable[[str], ResumeContext | None]):
        if not isinstance(journal, DispatchJournal):
            raise RecoveryError("INVALID_DISPATCH_JOURNAL")
        if not callable(contexts):
            raise RecoveryError("INVALID_RESUME_CONTEXTS")
        self._journal = journal
        self._contexts = contexts
        self._outcomes: dict = {}  # (request_id, last journal seq) -> LoopResult or a fixed failure code

    def __call__(self, request_id: str, tool_call_id: str) -> None:
        events = self._journal.tool_events(request_id)
        if self._returned(events, tool_call_id):
            return  # already sent, for example by the resume of a sibling call
        # A resume outcome holds only while the request's journal is unchanged: a repeat would only
        # replay the same failure, while a changed journal is judged again.
        key = (request_id, events[-1].seq if events else 0)
        outcome = self._outcomes.get(key)
        if outcome is None:
            outcome = self._resume(request_id)
            if outcome not in ("RESUME_CONTEXT_MISSING", "RESUME_CONTEXT_MISMATCH"):
                self._outcomes[key] = outcome  # no resume ran for a context failure; look it up again next time
        if isinstance(outcome, str):
            raise RecoveryError(outcome)
        if not self._returned(self._journal.tool_events(request_id), tool_call_id):
            raise RecoveryError("RESEND_NOT_DELIVERED")

    @staticmethod
    def _returned(events, tool_call_id: str) -> bool:
        return any(event.tool_call_id == tool_call_id and event.state == "returned" for event in events)

    def _resume(self, request_id: str):
        context = self._contexts(request_id)
        if context is None:
            return "RESUME_CONTEXT_MISSING"
        if (type(context) is not ResumeContext or not isinstance(context.loop, ApiToolLoop)
                or type(context.identity) is not DispatchIdentity or context.identity.request_id != request_id):
            return "RESUME_CONTEXT_MISMATCH"
        try:
            return context.loop.resume(
                context.identity, context.session, input_revision=context.input_revision,
                authorization_revision=context.authorization_revision, messages=context.messages,
            )
        except DispatchOutcomeUnknown:
            return "RESEND_OUTCOME_UNKNOWN"
        except DispatchError as error:
            return "RESEND_" + error.code


__all__ = ["LoopResendScheduler", "ResumeContext"]
