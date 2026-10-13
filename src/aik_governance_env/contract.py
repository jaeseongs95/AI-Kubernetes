"""VM governance input contract constants.

Source: vibemarshal commit 32bb0f9 (VM is a fixed contract; AIK never edits it).
- flowmarshal/engine/governance_gate.py: environment variable names,
  MINIMUM_NODE, CONSUMED_SURFACE["entry_points"], load_model_classes(),
  GovernanceGate.preflight() manifest structure checks.
- flowmarshal/engine/ags_observation_producer.py: A2_CONFIG_FORMAT,
  A2_PROFILE_ID, A2_PROFILE, _load_a2_producer(), _a2_digest(), _canonical().
"""

from __future__ import annotations

from types import MappingProxyType

VM_COMMIT = "32bb0f9"

# governance_gate environment variables.
ENV_PLUGIN_ROOT = "FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT"
ENV_MODEL_CLASSES = "FLOWMARSHAL_GOVERNANCE_MODEL_CLASSES"
ENV_A2_PROFILE = "FLOWMARSHAL_GOVERNANCE_A2_PROFILE"
ENVIRONMENT_VARIABLES = (ENV_PLUGIN_ROOT, ENV_MODEL_CLASSES, ENV_A2_PROFILE)

# governance_gate.MINIMUM_NODE and CONSUMED_SURFACE["entry_points"].
MINIMUM_NODE = (24, 0)
REQUIRED_ENTRY_POINTS = ("mcp-server", "scope-baseline", "scope-compare", "acceptance-cli")

# governance_gate.load_model_classes.
MODEL_CLASSES_FORMAT = "flowmarshal-governance-model-classes-v1"
MODEL_CLASSES = ("lightweight", "general", "deep", "frontier")

# GovernanceGate.preflight plugin manifest.
MANIFEST = "host-integration.json"
MANIFEST_FORMAT = "agent-governance-suite.host-integration.v1"

# ags_observation_producer A2 producer configuration.
A2_CONFIG_FORMAT = "flowmarshal-governance-a2-profile-v1"
A2_PROFILE_ID = "flowmarshal-same-user-v1"
A2_SELECTION_SOURCE = "server-local-operator-config"
A2_HOST_ID = "flowmarshal"
A2_RESOURCE_KINDS = ("key", "pin", "state")
A2_MAX_FILE_BYTES = 1 << 20

# All values are strings, so a read-only view over a private copy is deep frozen.
A2_PROFILE = MappingProxyType(dict({
    "profileId": A2_PROFILE_ID,
    "assuranceTier": "same-user",
    "hostId": A2_HOST_ID,
    "receiptDomain": "fm-same-user-provider-terminal-to-governance-v1",
    "dispatchDomain": "ags-fm-same-user-dispatch-registration-v1",
    "modelClassSource": "flowmarshal-signed-assertion",
    "actorSource": "flowmarshal-signed-assertion",
    "keyNamespace": A2_PROFILE_ID,
    "pinNamespace": A2_PROFILE_ID,
    "stateNamespace": A2_PROFILE_ID,
}))

# Reserved for the protected VM producer B; an A2 key/pin must never use them.
PROTECTED_PRODUCER_PATHS = (
    "/etc/flowmarshal/ags-producer-key.json",
    "/etc/agent-governance-suite/vm-operator-policy.json",
    "C:\\ProgramData\\flowmarshal\\ags-producer-key.json",
    "C:\\ProgramData\\agent-governance-suite\\vm-operator-policy.json",
)


def a2_profile_dict() -> dict[str, str]:
    """A fresh mutable copy for JSON serialization."""
    return dict(A2_PROFILE)
