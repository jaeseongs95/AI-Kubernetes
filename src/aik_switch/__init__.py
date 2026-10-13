"""AIK-028 director model switch, allowed fallback and handoff (partial implementation).

``DirectorSwitch`` is the profile switch state machine of the director only.
A switch request made while calls are in flight waits until every in-flight
call is observed as terminal (completed, failed or cancelled). Only then is the
new binding applied. An in-flight call is never continued on another model.
Call and switch event IDs are processed at most once.

Fallback candidates come only from the user's ``aik_profiles`` director
profile, in its fallback order, restricted to the positions a user
``SwitchPolicy`` allows. A candidate must satisfy the quality floor, the
required tools, the data scope, the budget and the ``aik_capability``
inventory check. Otherwise nothing switches automatically and the state stops
for a user decision. Authentication failures are separated into "re-login the
same profile" and an explicitly permitted fallback.

Every request and application is appended to ``SwitchJournal`` with the reason,
previous/new model, requested and observed effort (null with a reason when not
observed), and the existing ``aik_continuity`` checkpoint revision. The
handoff packet is ``aik_continuity.ContinuityStore.handoff``. Worker and
reviewer profiles are never read for writing or changed.

No provider is called here. Real model calls and Windows behaviour are NOT_RUN.
"""

from .controller import DirectorSwitch
from .errors import (
    InvalidSwitch, InvalidSwitchJournal, SwitchBlocked, SwitchConflict, SwitchError, SwitchPending,
)
from .journal import APPLICATION_ID, SCHEMA_VERSION, SwitchJournal, default_switch_path
from .models import (
    AUTH_REASONS, BLOCKED_STATES, CALL_TERMINAL_KINDS, SWITCH_REASONS, SWITCH_STATES,
    CallEvent, CallOutcome, CandidateCheck, FallbackRule, SwitchOutcome, SwitchPolicy, SwitchRequest,
    binding_to_dict,
)
from .policy import check_capability, choice_at, evaluate, make_binding, profile_positions

__all__ = [
    "DirectorSwitch", "SwitchJournal", "default_switch_path", "APPLICATION_ID", "SCHEMA_VERSION",
    "SwitchPolicy", "FallbackRule", "SwitchRequest", "CallEvent", "CandidateCheck",
    "SwitchOutcome", "CallOutcome", "binding_to_dict",
    "SWITCH_REASONS", "AUTH_REASONS", "CALL_TERMINAL_KINDS", "SWITCH_STATES", "BLOCKED_STATES",
    "evaluate", "check_capability", "choice_at", "make_binding", "profile_positions",
    "SwitchError", "InvalidSwitch", "SwitchPending", "SwitchBlocked", "SwitchConflict",
    "InvalidSwitchJournal",
]
