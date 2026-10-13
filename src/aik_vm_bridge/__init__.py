"""AIK-031: AIK-side bridge to the VM EngineApplication command/query façade.

This package does not import VM internals at import time. It uses the declared
public surface and an injected port. ``EngineApplicationPort`` adapts an
injected upstream EngineApplication instance (pinned revision 32bb0f9) to that
port. VM still owns Task, Attempt and verdict authority. A successful unit test
is not proof of an installed or qualified VM integration.
"""

from .binding import BINDING_FIELDS, TaskExecutionBinding, verify_assignment
from .bridge import (
    JOB_STATUS_PHASES, AttemptQuery, AuthorizeRequest, BridgeReceipt, CancelRequest, FinalReportRequest,
    GoalRequest, InventoryRequest, JobObservation, JobPhase, ObserveRequest, PauseRequest,
    ProjectQuery, ReplanRequest, RunOnceRequest, VmApplicationPort, VmBindingExtensionPort,
    VmBridge, VmRefs,
)
from .engine_port import EngineApplicationPort, EnginePortError, EngineProject
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
    "InventoryRequest", "BridgeReceipt", "VmRefs", "JobObservation", "JobPhase", "JOB_STATUS_PHASES",
    "TaskExecutionBinding", "BINDING_FIELDS", "verify_assignment",
    "Command", "DeclaredSurface", "CapabilityReport", "Handshake", "load_surface", "preflight",
    "check_handshake", "parameter_names", "BINDING_EXTENSION", "BLOCKING_GATES",
    "PINNED_SURFACE_REVISION", "SUPPORTED_ENGINE_SCHEMA_REVISIONS",
    "BridgeCode", "EffectOutcome", "VmBridgeError",
    "EngineApplicationPort", "EnginePortError", "EngineProject",
]
