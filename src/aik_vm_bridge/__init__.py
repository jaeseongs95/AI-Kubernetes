"""AIK-031: AIK-side bridge to the VM EngineApplication command/query façade.

This package does not import VM internals. It uses the declared public surface
and an injected port. VM still owns Task, Attempt and verdict authority. A
successful unit test is not proof of an installed or qualified VM integration.
"""

from .binding import BINDING_FIELDS, TaskExecutionBinding, verify_assignment
from .bridge import (
    AttemptQuery, AuthorizeRequest, BridgeReceipt, CancelRequest, FinalReportRequest,
    GoalRequest, InventoryRequest, JobObservation, JobPhase, ObserveRequest, PauseRequest,
    ProjectQuery, ReplanRequest, RunOnceRequest, VmApplicationPort, VmBindingExtensionPort,
    VmBridge, VmRefs,
)
from .errors import BridgeCode, EffectOutcome, VmBridgeError
from .surface import (
    BINDING_EXTENSION, BLOCKING_GATES, PINNED_SURFACE_REVISION, SUPPORTED_ENGINE_SCHEMA_REVISIONS,
    CapabilityReport, Command, DeclaredSurface, Handshake, check_handshake, load_surface,
    parameter_names, preflight,
)

__all__ = [
    "VmBridge", "VmApplicationPort", "VmBindingExtensionPort",
    "GoalRequest", "AuthorizeRequest", "RunOnceRequest", "ObserveRequest", "CancelRequest",
    "PauseRequest", "ReplanRequest", "ProjectQuery", "FinalReportRequest", "AttemptQuery",
    "InventoryRequest", "BridgeReceipt", "VmRefs", "JobObservation", "JobPhase",
    "TaskExecutionBinding", "BINDING_FIELDS", "verify_assignment",
    "Command", "DeclaredSurface", "CapabilityReport", "Handshake", "load_surface", "preflight",
    "check_handshake", "parameter_names", "BINDING_EXTENSION", "BLOCKING_GATES",
    "PINNED_SURFACE_REVISION", "SUPPORTED_ENGINE_SCHEMA_REVISIONS",
    "BridgeCode", "EffectOutcome", "VmBridgeError",
]
