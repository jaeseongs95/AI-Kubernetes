"""AIK-side JEV bridge: RAW preservation, verified REQ metadata, advisory classification.

Design principles (user-confirmed design):

* JEV is an assisting layer that classifies and evaluates the input and skill
  metadata. Its result is advisory only and has no authority over the final
  skill choice.
* The final skill choice belongs to the AGENT. ``JevBridge`` records the
  JEV classification and the AGENT selection separately and has no API that
  creates or overwrites an AGENT selection from a JEV recommendation.
* The user's RAW prompt is passed through unchanged. No separate front-end LLM
  rewrites or normalizes it. Only metadata already verified by code goes into
  the common REQ, which is bound to RAW through its SHA-256 digest.
* AIK model/worker placement and the AGENT's internal skill selection are
  different decisions.

VM (vibemarshal) and AGS (agent-governance-suite) expose no JEV integration
interface and are fixed contracts. This package therefore implements only the
AIK-side bridge. Real JEV round trips, VM/AGS embedding and fallback LLM
execution are NOT_RUN.
"""

from .bridge import SKILL_INSTRUCTIONS, SKILL_QUESTION, JevBridge
from .client import (
    DEFAULT_BASE_URL, OBSERVED_CONTRACT_VERSION, USER_AGENT, JevClient, ModelInfo,
    SystemOneResult, UrllibTransport,
)
from .errors import JevError
from .journal import APPLICATION_ID, SCHEMA_VERSION, JevJournal
from .models import (
    LOCAL_DERIVED, MAX_RAW_BYTES, METADATA_PROVENANCES, MODEL_REPORTED, PROVIDER_OBSERVED,
    SOURCES, STATUSES, AgentSelection, AgentSelectionRecord, Classification, CommonReq,
    FallbackCandidate, JevAnswer, RawPrompt, SkillCandidate, select_fallback, selection_flags,
)

__all__ = [
    "JevBridge", "JevClient", "JevJournal", "JevError",
    "RawPrompt", "SkillCandidate", "CommonReq", "JevAnswer", "Classification",
    "FallbackCandidate", "select_fallback", "selection_flags",
    "AgentSelection", "AgentSelectionRecord", "ModelInfo", "SystemOneResult", "UrllibTransport",
    "SKILL_QUESTION", "SKILL_INSTRUCTIONS", "DEFAULT_BASE_URL", "OBSERVED_CONTRACT_VERSION", "USER_AGENT",
    "APPLICATION_ID", "SCHEMA_VERSION", "MAX_RAW_BYTES", "METADATA_PROVENANCES",
    "STATUSES", "SOURCES", "PROVIDER_OBSERVED", "LOCAL_DERIVED", "MODEL_REPORTED",
]
