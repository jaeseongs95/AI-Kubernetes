"""Pure candidate evaluation: policy limits first, then the capability inventory.

The capability acceptance rule mirrors aik_profiles.execution (accepted, with
provider_observed or configured_catalog evidence, request provenance
'requested', matching the requested combination). It is restated here because
that helper is private and verify_execution cannot evaluate a fallback while
the primary is still capability-accepted (for example after an auth failure).
"""

from __future__ import annotations

import re

from aik_capability import CapabilityError, Provenance, ValidationResult
from aik_director.models import DirectorBinding
from aik_profiles import FallbackApproval

from .models import AUTH_REASONS, CandidateCheck


_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_REASON = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:./-]{0,191}\Z")
_EVIDENCE = (Provenance.PROVIDER_OBSERVED, Provenance.CONFIGURED_CATALOG)


def _reason(value: object) -> str:
    return value if type(value) is str and _REASON.fullmatch(value) is not None else "UNCLASSIFIED"


def profile_positions(profile) -> tuple:
    """The user's order: primary, then fallbacks in their configured order."""
    return ("primary", *(f"fallback:{index}" for index in range(len(profile.fallbacks))))


def choice_at(profile, position: str):
    if position == "primary":
        return profile.primary
    index = int(position.split(":", 1)[1])
    return profile.fallbacks[index] if index < len(profile.fallbacks) else None


def make_binding(profile, revision: int, position: str) -> DirectorBinding:
    choice = choice_at(profile, position)
    return DirectorBinding(profile.id, revision, choice.connection_id, choice.model, choice.effort,
                           choice.tools, position)


def approval_dict(profile_id: str, revision: int, position: str, policy) -> dict | None:
    """The user policy is the approval of a fallback position; primary needs none."""
    if position == "primary":
        return None
    approval = FallbackApproval(profile_id, revision, int(position.split(":", 1)[1]), policy.approval_ref)
    return {"profile_id": approval.profile_id, "profile_revision": approval.profile_revision,
            "fallback_index": approval.fallback_index, "approval_ref": approval.approval_ref}


def check_capability(inventory, position: str, choice, min_context: int | None) -> CandidateCheck:
    """Inventory support of (connection, model, effort, tools[, context]); fail-closed."""
    try:
        result = inventory.validate(choice.connection_id, choice.model, choice.effort, choice.tools,
                                    min_context=min_context)
    except CapabilityError as error:
        code = error.args[0] if error.args and type(error.args[0]) is str else ""
        code = code if _CODE.fullmatch(code) else "UNCLASSIFIED"
        return CandidateCheck(position, False, ("CAPABILITY_ERROR:" + code,))
    except Exception:
        return CandidateCheck(position, False, ("INVENTORY_UNAVAILABLE",))
    if type(result) is not ValidationResult or (
        result.connection_id, result.model, result.effort, tuple(result.tools),
    ) != (choice.connection_id, choice.model, choice.effort, choice.tools):
        return CandidateCheck(position, False, ("VALIDATION_RESULT_MISMATCH",))
    evidence = result.supported_by in _EVIDENCE
    if result.accepted is True and evidence and result.request_provenance is Provenance.REQUESTED:
        return CandidateCheck(position, True, (), result.supported_by.value, result.evidence_digest)
    reasons = ["CAPABILITY_REJECTED", *("CAPABILITY:" + _reason(item) for item in result.reasons)]
    if result.accepted is True:
        reasons.append("NO_SUPPORT_EVIDENCE")
    return CandidateCheck(position, False, tuple(reasons))


def evaluate(position: str, *, profile, revision: int, policy, inventory, current: DirectorBinding,
             reason: str, budget_remaining: int | None) -> CandidateCheck:
    """All user limits must hold before the inventory is consulted."""
    if policy is None:
        return CandidateCheck(position, False, ("NO_FALLBACK_POLICY",))
    if policy.profile_revision != revision:
        return CandidateCheck(position, False, ("POLICY_STALE",))
    rule = policy.rule(position)
    if rule is None:
        return CandidateCheck(position, False, ("NOT_ALLOWED_BY_POLICY",))
    choice = choice_at(profile, position)
    if choice is None:
        return CandidateCheck(position, False, ("POSITION_OUT_OF_RANGE",))
    reasons = []
    if position == current.position and current.profile_revision == revision:
        reasons.append("SAME_AS_CURRENT")
    if reason in AUTH_REASONS and choice.connection_id == current.connection_id:
        reasons.append("SAME_CONNECTION_AUTH_FAILED")
    if rule.quality < policy.min_quality:
        reasons.append("QUALITY_BELOW_FLOOR")
    configured = {tool.casefold() for tool in choice.tools}
    for tool in policy.required_tools:
        if tool.casefold() not in configured:
            reasons.append("TOOL_NOT_CONFIGURED:" + tool)
    if policy.data_scope not in rule.data_scopes:
        reasons.append("DATA_SCOPE_NOT_ALLOWED")
    if policy.max_cost_per_call is not None and rule.estimated_cost > policy.max_cost_per_call:
        reasons.append("OVER_COST_LIMIT")
    if rule.estimated_cost > 0:
        if budget_remaining is None:
            reasons.append("BUDGET_UNKNOWN")
        elif rule.estimated_cost > budget_remaining:
            reasons.append("BUDGET_EXCEEDED")
    if reasons:
        return CandidateCheck(position, False, tuple(reasons))
    return check_capability(inventory, position, choice, policy.min_context)
