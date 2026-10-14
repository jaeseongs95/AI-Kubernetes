"""AIK-046/047 connection onboarding and management, vendor neutral.

``ports`` defines what a vendor connector offers (API key check or official
host login); ``service.ConnectionService`` composes the connection metadata
store, the per-user vault and the connectors into GUI operations with fixed
result codes. This package never imports a vendor adapter or host bridge.
"""

from .ports import (
    METHODS, CheckResult, CheckState, ConnectorInfo, HostLogin, KeyVerifier, LoginState, LoginStatus,
)
from .service import DEFAULT_WORKSPACE, ConnectError, ConnectionService

__all__ = [
    "ConnectionService", "ConnectError", "DEFAULT_WORKSPACE",
    "ConnectorInfo", "CheckState", "CheckResult", "LoginState", "LoginStatus", "KeyVerifier", "HostLogin",
    "METHODS",
]
