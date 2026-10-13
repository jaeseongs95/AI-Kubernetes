"""AIK-038 worker lifecycle: pause/drain gate, cancel, settle and teardown.

The package separates two things:

- Stopping *new* work. ``LifecycleGate`` records paused or draining scopes
  (global, pool, VM project). ``GatedLeaseManager`` checks the gate before and
  after every reservation, so a closed scope never yields a granted lease.
- Ending *running* work. ``LifecycleController`` sends a provider interrupt
  only when the adapter discloses support. An interrupt answer is never treated
  as termination: the lease stays draining until a terminal provider state is
  observed through ``aik_observer.WorkerObserver``.

Teardown releases resources only after tool effects are resolved, required
evidence is verified and the provider job is observed terminal. Only workers
that AIK created are terminated; borrowed hosts only get their lease back.

No provider, credential, VM or AGS is contacted by this package directly. All
external effects go through injected ports.
"""

from .adapters import BindingEffectAdapter
from .controller import (
    EVENT_KINDS, OUTCOME_KINDS, LifecycleController, LifecycleEvent, LifecycleView,
    interrupt_supported,
)
from .database import APPLICATION_ID, SCHEMA_VERSION
from .errors import LifecycleError
from .gate import (
    GATE_STATES, GateEvent, GateState, GatedLeaseManager, LifecycleGate, check_scope,
    runtime_placement_lookup,
)
from .workers import OWNERSHIP, WorkerRecord, WorkerRegistry

__all__ = [
    "LifecycleGate", "GatedLeaseManager", "GateState", "GateEvent", "GATE_STATES",
    "check_scope", "runtime_placement_lookup",
    "WorkerRegistry", "WorkerRecord", "OWNERSHIP",
    "LifecycleController", "LifecycleView", "LifecycleEvent", "EVENT_KINDS", "OUTCOME_KINDS",
    "interrupt_supported", "BindingEffectAdapter",
    "LifecycleError", "APPLICATION_ID", "SCHEMA_VERSION",
]
