"""Effect port adapter for RecoveryReconciler over the API agent loop's tool journal.

Source of truth: ``aik_dispatch.DispatchJournal`` tool events, written by
``ApiToolLoop`` in the order intent -> receipt|denied|effect_unknown ->
returned. Only public journal methods are used (``open_tool_requests``,
``record``, ``tool_events``, ``tool_event``).

- ``inflight()`` lists every tool call whose result was not ``returned``:
  intent only or ``effect_unknown`` -> ``has_receipt=False``; ``receipt`` or
  ``denied`` -> ``has_receipt=True`` (a denial is a journaled outcome with no
  effect). Fully returned calls are not listed.
- ``mark_unknown`` appends ``effect_unknown`` to an intent-only call. It never
  re-executes the tool; ``ApiToolLoop.resume`` then stops with
  ``effect_unknown`` and VM's unknown contract decides.
- ``resend_result`` never executes the tool and never writes ``returned``.
  It hands ``(request_id, tool_call_id)`` to the caller's ``resend`` callable
  (``LoopResendScheduler`` calls ``ApiToolLoop.resume``, which replays the
  journaled result and writes ``returned`` when it really sends it). One
  resume sends every pending call of its request; a call found ``returned``
  since the last ``inflight()`` scan is treated as already sent.

Tool effects of the hosted runtime path (``AikEffectJournal``) are not
covered: VM owns the unknown procedure there.
"""

from __future__ import annotations

import hashlib
from typing import Callable

from aik_dispatch import DispatchJournal

from .models import EffectRef, RecoveryError


READ_ONLY_TOOLS = frozenset({"read_file", "unsupported"})  # unsupported tools are denied before any effect
UNKNOWN_CODE = "RECOVERY_RECEIPT_MISSING"


def _idempotency(tool: object) -> str:
    # Mutating tools and verification commands are treated as non-idempotent:
    # DevTools replay by effect ID lives in process memory and does not survive a restart.
    return "read_only" if tool in READ_ONLY_TOOLS else "non_idempotent"


def _receipt_ref(request_id: str, tool_call_id: str) -> str:
    text = f"{request_id}\x00{tool_call_id}".encode("utf-8")
    return "tool-receipt-" + hashlib.sha256(text).hexdigest()[:40]


class ToolEffectAdapter:
    """``effect_port`` for ``RecoveryReconciler``: inflight / mark_unknown / resend_result."""

    def __init__(self, journal: DispatchJournal, *, resend: Callable[[str, str], None],
                 is_cancelled: Callable[[str], bool] | None = None):
        if not isinstance(journal, DispatchJournal):
            raise RecoveryError("INVALID_DISPATCH_JOURNAL")
        if not callable(resend):
            raise RecoveryError("INVALID_RESEND")
        if is_cancelled is not None and not callable(is_cancelled):
            raise RecoveryError("INVALID_CANCELLED_READER")
        self._journal = journal
        self._resend = resend
        self._is_cancelled = is_cancelled
        self._seen: dict = {}  # subject_id -> (request_id, tool_call_id) from the last inflight() scan

    @staticmethod
    def _states(events) -> dict:
        states: dict = {}
        for event in events:
            states.setdefault(event.tool_call_id, {})[event.state] = event.data
        return states

    def _scan(self) -> dict:
        """subject_id -> (request_id, tool_call_id, EffectRef) for every open tool call."""
        found: dict = {}
        for request_id in self._journal.open_tool_requests():
            attempt = self._journal.record(request_id).identity.vm_attempt_id
            cancelled = False
            if self._is_cancelled is not None:
                cancelled = self._is_cancelled(attempt)
                if type(cancelled) is not bool:
                    raise RecoveryError("INVALID_CANCELLED_READER")
            for call_id, seen in self._states(self._journal.tool_events(request_id)).items():
                if "returned" in seen or "intent" not in seen:
                    continue
                tool = seen["intent"].get("tool")
                has_receipt = bool(seen.keys() & {"receipt", "denied"})
                ref = EffectRef(
                    attempt, call_id, tool if isinstance(tool, str) else "unsupported", has_receipt, False,
                    _idempotency(tool), cancelled, _receipt_ref(request_id, call_id) if has_receipt else None,
                )
                previous = found.get(ref.subject_id)
                if previous is not None and previous[0] != request_id:
                    # Two requests claim the same Attempt tool call: refuse rather than guess.
                    raise RecoveryError("AMBIGUOUS_EFFECT")
                found[ref.subject_id] = (request_id, call_id, ref)
        return found

    def inflight(self) -> tuple:
        found = self._scan()
        self._seen = {subject: entry[:2] for subject, entry in found.items()}
        return tuple(entry[2] for entry in found.values())

    def _locate(self, ref: object) -> tuple:
        if type(ref) is not EffectRef:
            raise RecoveryError("INVALID_EFFECT_REF")
        entry = self._scan().get(ref.subject_id)
        if entry is None:
            raise RecoveryError("EFFECT_NOT_OPEN")
        request_id, call_id, _ = entry
        return request_id, call_id, set(self._states(self._journal.tool_events(request_id))[call_id])

    def mark_unknown(self, ref: EffectRef) -> None:
        request_id, call_id, seen = self._locate(ref)
        if "effect_unknown" in seen:
            return
        if seen != {"intent"}:
            # A receipt or denial arrived after the scan; re-evaluate on the next reconcile.
            raise RecoveryError("EFFECT_STATE_CHANGED")
        self._journal.tool_event(request_id, call_id, "effect_unknown", {"code": UNKNOWN_CODE})

    def resend_result(self, ref: EffectRef) -> None:
        if type(ref) is EffectRef and self._returned_since_scan(ref):
            # A resume for a sibling call of the same request already sent this result.
            return
        request_id, call_id, seen = self._locate(ref)
        if not seen & {"receipt", "denied"}:
            raise RecoveryError("EFFECT_STATE_CHANGED")
        self._resend(request_id, call_id)

    def _returned_since_scan(self, ref: EffectRef) -> bool:
        entry = self._seen.get(ref.subject_id)
        if entry is None:
            return False
        request_id, call_id = entry
        return any(event.tool_call_id == call_id and event.state == "returned"
                   for event in self._journal.tool_events(request_id))


__all__ = ["ToolEffectAdapter", "READ_ONLY_TOOLS", "UNKNOWN_CODE"]
