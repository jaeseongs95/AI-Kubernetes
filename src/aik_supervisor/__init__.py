"""AIK local supervisor: one runtime owner per user, workspace and component.

The GUI window lifetime is separate from the runtime lifetime. A second launch
attaches to the existing owner via its reconnect endpoint record. It does not
create a new instance. The socket/pipe server itself is outside this package.
"""

from .instance import (
    ENDPOINT_KINDS, Endpoint, EndpointRecord, InstanceLock, InstancePaths,
    InvalidEndpoint, InvalidWorkspace, PlacementRejected, SupervisorError,
    SupervisorUnavailable, default_endpoint, default_state_root, instance_paths,
    read_endpoint, try_acquire, validate_component, validate_workspace_id, write_endpoint,
)
from .supervisor import (
    IN_FLIGHT_LEASE_STATES, LIFECYCLE_STATES, WINDOW_STATES, Attachment, ShutdownReport,
    Supervisor, launch,
)

__all__ = [
    "launch", "Supervisor", "Attachment", "ShutdownReport",
    "LIFECYCLE_STATES", "WINDOW_STATES", "IN_FLIGHT_LEASE_STATES",
    "Endpoint", "EndpointRecord", "ENDPOINT_KINDS", "InstanceLock", "InstancePaths",
    "instance_paths", "try_acquire", "read_endpoint", "write_endpoint",
    "default_state_root", "default_endpoint", "validate_workspace_id", "validate_component",
    "SupervisorError", "InvalidWorkspace", "InvalidEndpoint", "SupervisorUnavailable",
    "PlacementRejected",
]
