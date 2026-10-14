"""AIK-011 non-secret provider contract and bounded account routing guard.

The adapter layer does not grant VM authorization, qualify provider support,
convert subscription host sessions into API keys, or own a Task/Attempt ledger.
"""
from .contracts import (
    AccountIsolation, AdapterCapabilities, AdapterFailure, AuthKind,
    AuthObservation, AuthState, ConnectionBinding, FaultKind, JobReceipt,
    JobRequest, JobState, Outcome, ProviderAdapter, ProviderFault, Support,
    TERMINAL, fault_from_provider,
)
from .controller import BoundJob, GuardedAdapter
from .turns import (
    CacheMode, CachePolicy, Measured, OpaqueBlock, PromptCacheCapability, Provenance, UsageBreakdown,
)

__all__ = [
    "AccountIsolation", "AdapterCapabilities", "AdapterFailure", "AuthKind",
    "AuthObservation", "AuthState", "ConnectionBinding", "FaultKind", "JobReceipt",
    "JobRequest", "JobState", "Outcome", "ProviderAdapter", "ProviderFault",
    "Support", "TERMINAL", "fault_from_provider", "BoundJob", "GuardedAdapter",
    "CacheMode", "CachePolicy", "Measured", "OpaqueBlock", "PromptCacheCapability", "Provenance",
    "UsageBreakdown",
]
