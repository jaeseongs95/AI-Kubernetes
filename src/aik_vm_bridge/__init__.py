"""AIK-031: AIK-side bridge to the VM EngineApplication command/query façade.

This package does not import VM internals at import time. It uses the declared
public surface and an injected port. ``EngineApplicationPort`` adapts an
injected upstream EngineApplication instance (pinned revision 32bb0f9) to that
port. VM still owns Task, Attempt and verdict authority.

The upstream binding extension does not exist. The hosted path
(``aik-hosted-runtime-v1``) instead injects ``AikHostedRuntime`` as the VM
runtime port: it binds the AIK assignment durably (``ExecutionBindingStore``)
before every provider effect and routes existing threads only to their
original connection. ``VmBridge.run_once_hosted``/``confirm_attempt`` complete
the owner-confirmed binding. A successful unit test is not proof of an
installed or qualified VM integration (NOT_RUN).
"""

from .binding import (
    AIK_BINDING_FIELDS, BINDING_FIELDS, TaskExecutionBinding, check_assignment, validate_binding_field,
    verify_assignment,
)
from .bridge import (
    HOSTED_STATES, JOB_STATUS_PHASES, AttemptQuery, AuthorizeRequest, BridgeReceipt, CancelRequest,
    FinalReportRequest, GoalRequest, HostedConfirmation, HostedRunOnceRequest, InventoryRequest,
    JobObservation, JobPhase, ObserveRequest, PauseRequest, ProjectQuery, ReplanRequest, RunOnceRequest,
    VmApplicationPort, VmBindingExtensionPort, VmBridge, VmRefs,
)
from .engine_port import EngineApplicationPort, EnginePortError, EngineProject, build_hosted_application
from .errors import BridgeCode, EffectOutcome, VmBridgeError
from .hosted_runtime import (
    EFFECT_OPERATIONS, HOSTED_ASSIGNMENT_FIELDS, TRACE_MODULE, AikHostedRuntime, ExecutionBindingStore,
    HostedAssignment, StoredAttempt, StoredIntent, ThreadRoute, TraceRef, read_operation_trace,
)
from .surface import (
    BINDING_EXTENSION, BLOCKING_GATES, HOSTED_RUNTIME_EXTENSION, KNOWN_EXTENSIONS, PINNED_SURFACE_REVISION,
    SUPPORTED_ENGINE_SCHEMA_REVISIONS, CapabilityReport, Command, DeclaredSurface, Handshake,
    check_handshake, load_surface, parameter_names, preflight,
)

__all__ = [
    "VmBridge", "VmApplicationPort", "VmBindingExtensionPort",
    "GoalRequest", "AuthorizeRequest", "RunOnceRequest", "ObserveRequest", "CancelRequest",
    "PauseRequest", "ReplanRequest", "ProjectQuery", "FinalReportRequest", "AttemptQuery",
    "InventoryRequest", "BridgeReceipt", "VmRefs", "JobObservation", "JobPhase", "JOB_STATUS_PHASES",
    "HostedRunOnceRequest", "HostedConfirmation", "HOSTED_STATES",
    "TaskExecutionBinding", "BINDING_FIELDS", "AIK_BINDING_FIELDS", "verify_assignment",
    "check_assignment", "validate_binding_field",
    "Command", "DeclaredSurface", "CapabilityReport", "Handshake", "load_surface", "preflight",
    "check_handshake", "parameter_names", "BINDING_EXTENSION", "HOSTED_RUNTIME_EXTENSION",
    "KNOWN_EXTENSIONS", "BLOCKING_GATES", "PINNED_SURFACE_REVISION", "SUPPORTED_ENGINE_SCHEMA_REVISIONS",
    "BridgeCode", "EffectOutcome", "VmBridgeError",
    "EngineApplicationPort", "EnginePortError", "EngineProject", "build_hosted_application",
    "AikHostedRuntime", "ExecutionBindingStore", "HostedAssignment", "TraceRef", "ThreadRoute",
    "StoredIntent", "StoredAttempt", "read_operation_trace", "HOSTED_ASSIGNMENT_FIELDS",
    "EFFECT_OPERATIONS", "TRACE_MODULE",
]
