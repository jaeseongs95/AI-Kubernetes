"""Director profile switch state machine.

States: active, switch_pending, reauth_required, decision_required.
- A switch accepted while calls are in flight is pending; new calls are refused
  (SwitchPending) so the application point is exactly "the last in-flight call
  was observed terminal". Cancellation counts only once it is observed.
- An in-flight call keeps the binding it started with; it is never resumed on
  the new model. The runner receives only the aik_continuity handoff packet.
- At application the profile revision, policy and inventory are checked again.
- Failure reasons without a qualifying candidate block new calls until a later
  switch or an explicit resume_current().
Only the director profile is read; nothing here writes any profile.
"""

from __future__ import annotations

from datetime import datetime, timezone
import threading

from aik_config.models import Revisioned
from aik_continuity import HandoffPacket
from aik_director.models import DirectorBinding, Observation, RunnerSession
from aik_profiles import DirectorProfile, InvalidProfile, rebuild_profile
from aik_runtime.models import MAX_INTEGER

from .errors import InvalidSwitch, SwitchBlocked, SwitchConflict, SwitchError, SwitchPending
from .journal import SwitchJournal
from .models import (
    AUTH_REASONS, BLOCKED_STATES, CallEvent, CallOutcome, CandidateCheck, SwitchOutcome, SwitchPolicy,
    SwitchRequest, binding_to_dict, check_id, check_position, rebuild,
)
from .policy import approval_dict, check_capability, choice_at, evaluate, make_binding, profile_positions


_OBSERVATION_FIELDS = (
    "requested_model", "requested_effort", "observed_model", "observed_model_reason",
    "observed_effort", "observed_effort_reason", "observation_matches", "session_id",
)


class DirectorSwitch:
    """Switch the director binding at explicit application points; never workers."""

    def __init__(self, *, profiles, profile_id: str, inventory, runner, continuity, project_id: str,
                 journal: SwitchJournal, policy: SwitchPolicy | None = None, clock=None):
        check_id(profile_id, "profile_id")
        check_id(project_id, "project_id")
        for port, method in ((profiles, "get"), (inventory, "validate"), (runner, "activate"),
                             (continuity, "handoff")):
            if not callable(getattr(port, method, None)):
                raise InvalidSwitch(f"A required port does not provide {method}().")
        if type(journal) is not SwitchJournal:
            raise InvalidSwitch("A SwitchJournal is required.")
        if policy is not None:
            if type(policy) is not SwitchPolicy:
                raise InvalidSwitch("The policy must be a SwitchPolicy.")
            policy = rebuild(policy)
            if policy.profile_id != profile_id:
                raise InvalidSwitch("The policy belongs to another profile.")
        if clock is not None and not callable(clock):
            raise InvalidSwitch("The clock must be callable.")
        self._profiles = profiles
        self._profile_id = profile_id
        self._inventory = inventory
        self._runner = runner
        self._continuity = continuity
        self._project_id = project_id
        self._journal = journal
        self._policy = policy
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        profile, revision = self._load_profile()
        self._binding = make_binding(profile, revision, "primary")
        self._state = "active"
        self._block_codes: tuple = ()
        self._calls: dict[str, DirectorBinding] = {}
        self._ended: set[str] = set()
        self._call_events: dict[str, str] = {}
        self._pending: dict | None = None
        self._restore(profile, revision)

    # ------------------------------------------------------------ properties
    @property
    def state(self) -> str:
        return self._state

    @property
    def binding(self) -> DirectorBinding:
        return self._binding

    @property
    def block_codes(self) -> tuple:
        return self._block_codes

    @property
    def in_flight_call_ids(self) -> tuple:
        with self._lock:
            return tuple(sorted(self._calls))

    @property
    def pending_event_id(self) -> str | None:
        with self._lock:
            return None if self._pending is None else self._pending["event_id"]

    def history(self, *, limit: int = 100, after_seq: int = 0) -> tuple:
        return self._journal.records(self._profile_id, limit=limit, after_seq=after_seq)

    # ------------------------------------------------------------ internals
    def _now(self) -> str:
        try:
            value = self._clock()
        except Exception:
            raise SwitchError("The switch clock is unavailable.") from None
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise SwitchError("The switch clock must return a timezone-aware datetime.")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _load_profile(self) -> tuple:
        entry = self._profiles.get(self._profile_id)
        if entry is None:
            raise InvalidSwitch("The director profile does not exist.")
        if type(entry) is not Revisioned:
            raise InvalidSwitch("The profile source must return a Revisioned profile.")
        try:
            profile = rebuild_profile(entry.value)
        except InvalidProfile:
            raise InvalidSwitch("The director profile is malformed.") from None
        if type(profile) is not DirectorProfile:
            raise InvalidSwitch("Only the director profile is switched; worker and reviewer profiles are not.")
        if profile.id != self._profile_id:
            raise InvalidSwitch("The profile source returned another profile.")
        if type(entry.revision) is not int or not 1 <= entry.revision <= MAX_INTEGER:
            raise InvalidSwitch("The director profile revision is invalid.")
        return profile, entry.revision

    def _try_load(self):
        try:
            return self._load_profile()
        except Exception:
            return None

    def _restore(self, profile, revision: int) -> None:
        applied = self._journal.latest_applied(self._profile_id)
        if applied is not None:
            restored = self._binding_from_record(applied.get("new"), profile, revision)
            if restored is None:
                self._state, self._block_codes = "decision_required", ("PROFILE_CHANGED_SINCE_LAST_SWITCH",)
                return
            self._binding = restored
        latest = self._journal.latest(self._profile_id)
        if latest is None:
            return
        state = latest.get("state_after")
        if state == "switch_pending":
            # Pending requests and in-flight calls live in memory only.
            self._state, self._block_codes = "decision_required", ("PENDING_SWITCH_LOST_ON_RESTART",)
        elif state in BLOCKED_STATES:
            codes = latest.get("block_codes")
            codes = tuple(code for code in codes if type(code) is str) if type(codes) is list else ()
            self._state, self._block_codes = state, codes or ("RESTORED_BLOCK",)

    @staticmethod
    def _binding_from_record(document, profile, revision: int) -> DirectorBinding | None:
        if type(document) is not dict:
            return None
        position = document.get("position")
        try:
            check_position(position)
        except InvalidSwitch:
            return None
        if document.get("profile_revision") != revision or choice_at(profile, position) is None:
            return None
        candidate = make_binding(profile, revision, position)
        return candidate if binding_to_dict(candidate) == document else None

    def _base(self, event_id: str, phase: str, kind: str, status: str, now: str) -> dict:
        return {"event_id": event_id, "phase": phase, "kind": kind, "status": status,
                "director_id": self._profile_id, "project_id": self._project_id, "recorded_at": now}

    def _duplicate(self, event_id: str) -> SwitchOutcome | None:
        stored = self._journal.get(event_id, "request")
        if stored is None:
            return None
        if stored.get("director_id") != self._profile_id:
            raise SwitchConflict("Another director already used this event ID.")
        return SwitchOutcome("duplicate", event_id, self._state, self._binding, stored,
                             self._journal.get(event_id, "apply"))

    def _commit(self, document: dict, state_after: str, block: tuple) -> dict:
        """Change state and record it; a lost record blocks instead of hiding the change."""
        document["state_after"] = state_after
        document["block_codes"] = list(block)
        self._state, self._block_codes = state_after, tuple(block)
        try:
            return self._journal.append(document)[2]
        except BaseException:
            self._state, self._block_codes = "decision_required", ("JOURNAL_WRITE_FAILED",)
            raise

    # ------------------------------------------------------------ calls
    def begin_call(self, call_id: str) -> DirectorBinding:
        """Register an in-flight call; it keeps this binding until it ends."""
        check_id(call_id, "call_id")
        with self._lock:
            if self._state == "switch_pending":
                raise SwitchPending("A director switch is waiting for in-flight calls.")
            if self._state in BLOCKED_STATES:
                raise SwitchBlocked("Re-authentication or a user decision is required.")
            if call_id in self._calls or call_id in self._ended:
                raise InvalidSwitch("This call ID was already used.")
            self._calls[call_id] = self._binding
            return self._binding

    def observe(self, event: CallEvent) -> CallOutcome:
        """Terminal observation; the last one applies a pending switch exactly once."""
        if type(event) is not CallEvent:
            raise InvalidSwitch("Only CallEvent records can be observed.")
        event = rebuild(event)
        with self._lock:
            known = self._call_events.get(event.event_id)
            if known is not None:
                if known != event.call_id:
                    raise SwitchConflict("A different call already used this event ID.")
                return CallOutcome("duplicate", event.event_id, event.call_id, event.kind)
            self._call_events[event.event_id] = event.call_id
            if event.call_id in self._ended:
                return CallOutcome("already_terminal", event.event_id, event.call_id, event.kind)
            if event.call_id not in self._calls:
                return CallOutcome("unknown_call", event.event_id, event.call_id, event.kind)
            del self._calls[event.call_id]
            self._ended.add(event.call_id)
            applied = None
            if self._state == "switch_pending" and self._pending is not None and not self._calls:
                applied = self._apply_pending()
            return CallOutcome("recorded", event.event_id, event.call_id, event.kind, applied)

    # ------------------------------------------------------------ switching
    def request_switch(self, request: SwitchRequest) -> SwitchOutcome:
        if type(request) is not SwitchRequest:
            raise InvalidSwitch("Only SwitchRequest records can be processed.")
        request = rebuild(request)
        with self._lock:
            duplicate = self._duplicate(request.event_id)
            if duplicate is not None:
                return duplicate
            now = self._now()
            previous = self._binding
            auth = request.reason in AUTH_REASONS
            candidates: list[CandidateCheck] = []
            planned = None
            revision = None
            profile = None
            reasons: tuple = ()
            if self._state == "switch_pending":
                status, reasons = "rejected", ("SWITCH_ALREADY_PENDING",)
            else:
                loaded = self._try_load()
                if loaded is None:
                    status = "reauth_required" if auth else "decision_required"
                    reasons = ("PROFILE_UNAVAILABLE",)
                else:
                    profile, revision = loaded
                    if auth and (self._policy is None or not self._policy.fallback_on_auth):
                        status, reasons = "reauth_required", ("REAUTH_REQUIRED",)
                    else:
                        if request.reason == "user_request":
                            positions = (request.target,)
                        else:
                            positions = tuple(position for position in profile_positions(profile)
                                              if not (position == previous.position
                                                      and previous.profile_revision == revision))
                        for position in positions:
                            check = evaluate(position, profile=profile, revision=revision, policy=self._policy,
                                             inventory=self._inventory, current=previous, reason=request.reason,
                                             budget_remaining=request.budget_remaining)
                            candidates.append(check)
                            if check.accepted:
                                planned = position
                                break
                        if planned is not None:
                            status = "accepted"
                        else:
                            status = "reauth_required" if auth else "decision_required"
                            reasons = ("NO_QUALIFYING_CANDIDATE",) + (("REAUTH_REQUIRED",) if auth else ())
            if status == "accepted":
                state_after, block = "switch_pending", ()
            elif status == "reauth_required":
                state_after, block = "reauth_required", reasons
            elif status == "decision_required" and request.reason != "user_request":
                state_after, block = "decision_required", reasons
            else:
                state_after, block = self._state, self._block_codes
            document = self._base(request.event_id, "request", "switch_request", status, now)
            document.update({
                "reason": request.reason, "target": request.target,
                "budget_remaining": request.budget_remaining, "profile_revision": revision,
                "previous": binding_to_dict(previous),
                "planned": None if planned is None else binding_to_dict(make_binding(profile, revision, planned)),
                "approval": None if planned is None else approval_dict(self._profile_id, revision, planned,
                                                                        self._policy),
                "candidates": [check.to_dict() for check in candidates],
                "reasons": list(reasons),
                "waiting_call_ids": sorted(self._calls) if status == "accepted" else [],
                "applies": None if status != "accepted" else ("after_calls" if self._calls else "immediately"),
            })
            stored = self._commit(document, state_after, block)
            if status != "accepted":
                return SwitchOutcome(status, request.event_id, self._state, self._binding, stored)
            self._pending = {"event_id": request.event_id, "reason": request.reason, "position": planned,
                             "profile_revision": revision, "budget_remaining": request.budget_remaining,
                             "record": stored}
            if self._calls:
                return SwitchOutcome("pending", request.event_id, self._state, self._binding, stored)
            return self._apply_pending()

    def _apply_pending(self) -> SwitchOutcome:
        plan = self._pending
        self._pending = None
        try:
            now = self._now()
        except SwitchError:
            self._state, self._block_codes = "decision_required", ("CLOCK_UNAVAILABLE",)
            raise
        previous = self._binding
        failure = None
        check = None
        profile = revision = None
        loaded = self._try_load()
        if loaded is None:
            failure = ("recheck_failed", ("PROFILE_UNAVAILABLE",))
        else:
            profile, revision = loaded
            if revision != plan["profile_revision"]:
                failure = ("recheck_failed", ("PROFILE_CHANGED",))
            else:
                check = evaluate(plan["position"], profile=profile, revision=revision, policy=self._policy,
                                 inventory=self._inventory, current=previous, reason=plan["reason"],
                                 budget_remaining=plan["budget_remaining"])
                if not check.accepted:
                    failure = ("recheck_failed", ("RECHECK_REJECTED", *check.reasons))
        handoff = None
        session = None
        new = None
        if failure is None:
            new = make_binding(profile, revision, plan["position"])
            try:
                handoff = self._continuity.handoff(self._project_id)
            except Exception:
                failure = ("activation_failed", ("CONTINUITY_UNAVAILABLE",))
            else:
                if type(handoff) is not HandoffPacket or handoff.project_id != self._project_id:
                    handoff, failure = None, ("activation_failed", ("INVALID_HANDOFF",))
                elif handoff.checkpoint is None and self._policy.require_checkpoint:
                    failure = ("activation_failed", ("CHECKPOINT_REQUIRED",))
        if failure is None:
            try:
                session = self._runner.activate(new, handoff)
            except Exception:
                failure = ("activation_failed", ("RUNNER_UNAVAILABLE",))
            else:
                if type(session) is not RunnerSession:
                    session, failure = None, ("activation_failed", ("INVALID_RUNNER_RESULT",))
        document = self._base(plan["event_id"], "apply", "switch_apply",
                              "applied" if failure is None else failure[0], now)
        document.update({"reason": plan["reason"], "position": plan["position"],
                         "previous": binding_to_dict(previous), "new": None, "approval": None,
                         "recheck": None if check is None else check.to_dict(),
                         "checkpoint": None, "checkpoint_reason": None, "handoff": None})
        document.update({name: None for name in _OBSERVATION_FIELDS})
        if failure is not None:
            status, reasons = failure
            document["reasons"] = list(reasons)
            stored = self._commit(document, "decision_required", reasons)
            return SwitchOutcome(status, plan["event_id"], self._state, self._binding, plan["record"], stored)
        observation = Observation.of(new, session)
        block = ("OBSERVED_BINDING_MISMATCH",) if observation.matches is False else ()
        checkpoint = handoff.checkpoint
        document.update({
            "new": binding_to_dict(new),
            "approval": approval_dict(self._profile_id, revision, plan["position"], self._policy),
            "reasons": list(block),
            "requested_model": new.model,
            "requested_effort": new.effort,
            # Requested values are never promoted to observed values.
            "observed_model": session.observed_model,
            "observed_model_reason": None if session.observed_model is not None else "NOT_OBSERVED",
            "observed_effort": session.observed_effort,
            "observed_effort_reason": None if session.observed_effort is not None else "NOT_OBSERVED",
            "observation_matches": observation.matches,
            "session_id": session.session_id,
            "checkpoint": None if checkpoint is None else {
                "project_id": checkpoint.checkpoint.project_id, "revision": checkpoint.revision,
                "goal_id": checkpoint.checkpoint.goal_id,
                "goal_revision_id": checkpoint.checkpoint.goal_revision_id,
                "plan_revision_id": checkpoint.checkpoint.plan_revision_id,
            },
            "checkpoint_reason": None if checkpoint is not None else "NO_CHECKPOINT",
            "handoff": {
                "directive_count": len(handoff.directives),
                "active_directive_ids": list(handoff.active_directive_ids),
                "summary_id": None if handoff.summary is None else handoff.summary.summary.summary_id,
                "config_refs": [[item.config.config_id, item.config.revision] for item in handoff.configs],
            },
        })
        # The runner has already switched; record the binding it now uses.
        self._binding = new
        stored = self._commit(document, "decision_required" if block else "active", block)
        return SwitchOutcome("applied", plan["event_id"], self._state, self._binding, plan["record"], stored,
                             session, handoff)

    def resume_current(self, event_id: str) -> SwitchOutcome:
        """After re-login or a user decision: recheck the current binding and unblock."""
        check_id(event_id, "event_id")
        with self._lock:
            duplicate = self._duplicate(event_id)
            if duplicate is not None:
                return duplicate
            now = self._now()
            previous = self._binding
            check = None
            state_after, block = self._state, self._block_codes
            if self._state not in BLOCKED_STATES:
                status, reasons = "rejected", ("NOT_BLOCKED",)
            else:
                loaded = self._try_load()
                if loaded is None:
                    status, reasons = "decision_required", ("PROFILE_UNAVAILABLE",)
                elif loaded[1] != previous.profile_revision:
                    status, reasons = "decision_required", ("PROFILE_CHANGED",)
                else:
                    profile, _ = loaded
                    check = check_capability(self._inventory, previous.position,
                                             choice_at(profile, previous.position),
                                             None if self._policy is None else self._policy.min_context)
                    if check.accepted:
                        status, reasons = "resumed", ()
                        state_after, block = "active", ()
                    else:
                        status, reasons = "decision_required", check.reasons
            document = self._base(event_id, "request", "resume", status, now)
            document.update({"reason": "resume_current", "previous": binding_to_dict(previous),
                             "recheck": None if check is None else check.to_dict(), "reasons": list(reasons)})
            stored = self._commit(document, state_after, block)
            return SwitchOutcome(status, event_id, self._state, self._binding, stored)
