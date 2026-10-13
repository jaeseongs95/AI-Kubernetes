"""VM governance gate input preparation for AIK (part of AIK-086).

This package builds and checks the three inputs that a VM (vibemarshal
32bb0f9) process reads through its governance environment variables. It does
not modify VM or AGS, start an MCP server, contact a provider, or create
private keys. All values such as observed models, class choices and key/pin
locations come from the caller.
"""

from .a2_profile import A2ConfigReport, check_a2_config, compose_a2_config
from .canonical import a2_digest, canonical_json
from .environment import (
    BLOCKED, READY, GovernanceEnvironment, InputStatus,
    prepare_governance_environment, write_atomic,
)
from .errors import GovernanceEnvError
from .model_classes import build_model_class_table, check_model_class_table
from .plugin_root import NodeReport, PluginRootReport, check_node, inspect_plugin_root

__all__ = [
    "GovernanceEnvError",
    "canonical_json", "a2_digest",
    "build_model_class_table", "check_model_class_table",
    "compose_a2_config", "check_a2_config", "A2ConfigReport",
    "inspect_plugin_root", "PluginRootReport", "check_node", "NodeReport",
    "prepare_governance_environment", "GovernanceEnvironment", "InputStatus",
    "write_atomic", "READY", "BLOCKED",
]
