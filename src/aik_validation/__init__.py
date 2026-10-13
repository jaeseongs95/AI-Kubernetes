"""AIK-042 independent review evidence and VM completion views.

This package keeps provider success, dispatch acknowledgement, the worker's
own completion claim, the AIK-side independent check, VM validation, the AGS
gate (enforced by VM) and the VM GoalVerdict in separate fields with their
provenance. AIK never decides completion: ``CompletionView.vm_verdict_recorded`` is
true only when VM reported a GoalVerdict.

``IndependentChecker`` runs a policy-fixed verification command through
``aik_devtools.DevTools`` with a reviewer session that is different from the
worker session, and appends every result to its own append-only journal. The
result is an evidence candidate for VM. It is not a VM validation or verdict.

No provider, credential, VM or AGS installation is contacted by this package.
"""

from .checker import IndependentChecker
from .completion import (
    ACK_STATES, FIELD_PROVENANCE, CompletionView, build_completion_view,
)
from .errors import ValidationError
from .journal import APPLICATION_ID, SCHEMA_VERSION, CheckJournal
from .models import (
    LOCAL_DERIVED, REVIEWER_ROLE, VERDICTS, CheckRecord, compute_verdict,
)

__all__ = [
    "CompletionView", "build_completion_view", "FIELD_PROVENANCE", "ACK_STATES",
    "IndependentChecker", "CheckJournal", "CheckRecord", "compute_verdict",
    "VERDICTS", "LOCAL_DERIVED", "REVIEWER_ROLE", "APPLICATION_ID", "SCHEMA_VERSION",
    "ValidationError",
]
