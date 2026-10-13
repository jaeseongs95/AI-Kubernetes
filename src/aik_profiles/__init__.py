"""AIK-023 director and role execution profiles.

The director profile (connection/model/effort/tools/call limit/budget and an
ordered list of allowed fallbacks) and worker/reviewer profiles are stored as
independent records, each with its own revision. A director update therefore
cannot overwrite a worker or reviewer profile.

Immediately before execution, the requested combination is checked against an
aik_capability inventory. The result records both the requested profile and
the actual binding with its provenance. A fallback is never chosen
automatically: only an explicitly approved fallback position is tried.

This package does not call providers, read credentials or grant execution
rights. The settings store (aik_config) is only read, for reference checks.
"""

from .errors import CapabilityMismatch, InvalidProfile, ProfileError
from .execution import (
    CapabilityAttempt, ExecutionDecision, FallbackApproval, rejected_decision,
    validate_approval, verify_execution,
)
from .models import (
    MAX_FALLBACKS, MAX_TOOLS, PROFILE_FORMAT, PROFILE_FORMAT_VERSION, ROLE_NAMES,
    DirectorProfile, ExecutionChoice, RoleProfile, from_agent_profile, load_profile_document,
    profile_document, profile_from_dict, profile_to_dict, rebuild_profile, to_agent_profile,
)
from .storage import APPLICATION_ID, ProfileStore, default_profiles_path

__all__ = [
    "ProfileStore", "default_profiles_path", "APPLICATION_ID",
    "DirectorProfile", "RoleProfile", "ExecutionChoice", "ROLE_NAMES", "MAX_FALLBACKS", "MAX_TOOLS",
    "PROFILE_FORMAT", "PROFILE_FORMAT_VERSION",
    "profile_to_dict", "profile_from_dict", "profile_document", "load_profile_document", "rebuild_profile",
    "to_agent_profile", "from_agent_profile",
    "FallbackApproval", "CapabilityAttempt", "ExecutionDecision", "verify_execution",
    "rejected_decision", "validate_approval",
    "ProfileError", "InvalidProfile", "CapabilityMismatch",
]
