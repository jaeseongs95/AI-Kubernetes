"""DirectorProfile / RoleProfile schema. No field exists for secret material."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from aik_config.errors import InvalidSetting
from aik_config.models import BUDGET_UNITS, AgentProfile, check_identifier, check_positive_integer

from .errors import InvalidProfile


_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
ROLE_NAMES = ("director", "worker", "reviewer")
MAX_TOOLS = 64
MAX_FALLBACKS = 8
PROFILE_FORMAT = "aik.role-profile"
PROFILE_FORMAT_VERSION = 1
_CHOICE_KEYS = frozenset({"connection_id", "model", "effort", "tools"})
_PROFILE_KEYS = frozenset({
    "id", "workspace_id", "role", "primary", "fallbacks", "max_calls", "budget_limit", "budget_unit",
})
_DOCUMENT_KEYS = frozenset({"format", "format_version", "profile"})


def check_profile_identifier(value: object, field: str = "id") -> None:
    try:
        check_identifier(value, field)
    except InvalidSetting:
        raise InvalidProfile(f"Invalid identifier field: {field}.") from None


def _positive(value: object, field: str) -> None:
    try:
        check_positive_integer(value, field)
    except InvalidSetting:
        raise InvalidProfile(f"Invalid positive integer field: {field}.") from None


@dataclass(frozen=True, slots=True)
class ExecutionChoice:
    """One requested (connection, model, effort, tools) combination."""

    connection_id: str
    model: str
    effort: str | None = None
    tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_profile_identifier(self.connection_id, "connection_id")
        if type(self.model) is not str or _MODEL.fullmatch(self.model) is None:
            raise InvalidProfile("A model must be a bounded model identifier.")
        if self.effort is not None:
            check_profile_identifier(self.effort, "effort")
        if type(self.tools) is not tuple or len(self.tools) > MAX_TOOLS:
            raise InvalidProfile("Tools must be a bounded tuple of tool identifiers.")
        for tool in self.tools:
            check_profile_identifier(tool, "tool")
        if len({tool.casefold() for tool in self.tools}) != len(self.tools):
            raise InvalidProfile("A tool is repeated.")


def _identity(choice: ExecutionChoice) -> tuple:
    return (choice.connection_id, choice.model, choice.effort,
            tuple(sorted(tool.casefold() for tool in choice.tools)))


def _validate_profile(profile) -> None:
    check_profile_identifier(profile.id)
    check_profile_identifier(profile.workspace_id, "workspace_id")
    if type(profile.primary) is not ExecutionChoice:
        raise InvalidProfile("A profile requires a primary execution choice.")
    if type(profile.fallbacks) is not tuple or len(profile.fallbacks) > MAX_FALLBACKS:
        raise InvalidProfile("Fallbacks must be a bounded ordered tuple of execution choices.")
    seen = {_identity(profile.primary)}
    for fallback in profile.fallbacks:
        if type(fallback) is not ExecutionChoice:
            raise InvalidProfile("Fallbacks must be execution choices.")
        identity = _identity(fallback)
        if identity in seen:
            raise InvalidProfile("A fallback repeats the primary choice or another fallback.")
        seen.add(identity)
    _positive(profile.max_calls, "max_calls")
    if profile.budget_limit is not None:
        _positive(profile.budget_limit, "budget_limit")
    if type(profile.budget_unit) is not str or profile.budget_unit not in BUDGET_UNITS:
        raise InvalidProfile("Unknown value for field: budget_unit.")


@dataclass(frozen=True, slots=True)
class DirectorProfile:
    """Director execution resource, model, effort, tools, limits and allowed fallbacks."""

    id: str
    workspace_id: str
    primary: ExecutionChoice
    max_calls: int
    fallbacks: tuple[ExecutionChoice, ...] = ()
    budget_limit: int | None = None
    budget_unit: str = "usd_cents"

    def __post_init__(self) -> None:
        _validate_profile(self)

    @property
    def role(self) -> str:
        return "director"


@dataclass(frozen=True, slots=True)
class RoleProfile:
    """Worker or reviewer profile, stored independently of the director."""

    id: str
    workspace_id: str
    role: str
    primary: ExecutionChoice
    max_calls: int
    fallbacks: tuple[ExecutionChoice, ...] = ()
    budget_limit: int | None = None
    budget_unit: str = "usd_cents"

    def __post_init__(self) -> None:
        if type(self.role) is not str or self.role not in ("worker", "reviewer"):
            raise InvalidProfile("A role profile must be a worker or reviewer profile.")
        _validate_profile(self)


# ------------------------------------------------------------------ documents

def _exact(value: object, keys: frozenset, what: str) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise InvalidProfile(f"The {what} has missing or unsupported fields.")
    return value


def _unique_pairs(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise InvalidProfile("The document repeats a key.")
    return dict(pairs)


def _reject_constant(_name):
    raise InvalidProfile("The document contains a non-finite number.")


def parse_json(text: object):
    """Strict JSON: duplicate keys and NaN/Infinity are rejected."""
    if type(text) is not str:
        raise InvalidProfile("The document must be JSON text.")
    try:
        return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except InvalidProfile:
        raise
    except (ValueError, RecursionError):
        raise InvalidProfile("The document is not valid JSON.") from None


def choice_to_dict(choice) -> dict:
    if type(choice) is not ExecutionChoice or type(choice.tools) is not tuple:
        raise InvalidProfile("An execution choice is malformed.")
    return {"connection_id": choice.connection_id, "model": choice.model, "effort": choice.effort,
            "tools": list(choice.tools)}


def choice_from_dict(value: object) -> ExecutionChoice:
    item = _exact(value, _CHOICE_KEYS, "execution choice")
    if type(item["tools"]) is not list:
        raise InvalidProfile("Execution choice tools must be a list.")
    return ExecutionChoice(item["connection_id"], item["model"], item["effort"], tuple(item["tools"]))


def profile_to_dict(profile) -> dict:
    if type(profile) is DirectorProfile:
        role = "director"
    elif type(profile) is RoleProfile:
        role = profile.role
    else:
        raise InvalidProfile("Only DirectorProfile and RoleProfile are execution profiles.")
    if type(profile.fallbacks) is not tuple:
        raise InvalidProfile("Fallbacks must be an ordered tuple.")
    return {
        "id": profile.id,
        "workspace_id": profile.workspace_id,
        "role": role,
        "primary": choice_to_dict(profile.primary),
        "fallbacks": [choice_to_dict(choice) for choice in profile.fallbacks],
        "max_calls": profile.max_calls,
        "budget_limit": profile.budget_limit,
        "budget_unit": profile.budget_unit,
    }


def profile_from_dict(value: object):
    item = _exact(value, _PROFILE_KEYS, "profile")
    if type(item["fallbacks"]) is not list:
        raise InvalidProfile("Profile fallbacks must be a list.")
    primary = choice_from_dict(item["primary"])
    fallbacks = tuple(choice_from_dict(choice) for choice in item["fallbacks"])
    if type(item["role"]) is str and item["role"] == "director":
        return DirectorProfile(item["id"], item["workspace_id"], primary, item["max_calls"], fallbacks,
                               item["budget_limit"], item["budget_unit"])
    return RoleProfile(item["id"], item["workspace_id"], item["role"], primary, item["max_calls"], fallbacks,
                       item["budget_limit"], item["budget_unit"])


def rebuild_profile(profile):
    """Reconstruct, so object.__setattr__ forgeries are validated again."""
    try:
        return profile_from_dict(profile_to_dict(profile))
    except InvalidProfile:
        raise
    except (AttributeError, TypeError, ValueError):
        raise InvalidProfile("The execution profile is malformed.") from None


def profile_document(profile) -> dict:
    return {"format": PROFILE_FORMAT, "format_version": PROFILE_FORMAT_VERSION,
            "profile": profile_to_dict(rebuild_profile(profile))}


def load_profile_document(document: object):
    top = _exact(document, _DOCUMENT_KEYS, "profile document")
    if top["format"] != PROFILE_FORMAT or type(top["format_version"]) is not int \
            or top["format_version"] != PROFILE_FORMAT_VERSION:
        raise InvalidProfile("The document is not a supported AIK role profile.")
    return profile_from_dict(top["profile"])


# ------------------------------------------------- aik_config AgentProfile view

def to_agent_profile(profile) -> AgentProfile | None:
    """Read-only AgentProfile view; reviewers have no AgentProfile role."""
    profile = rebuild_profile(profile)
    if profile.role == "reviewer":
        return None
    return AgentProfile(profile.id, profile.workspace_id, profile.role, profile.primary.connection_id,
                        profile.primary.model, effort=profile.primary.effort)


def from_agent_profile(agent, *, max_calls: int, tools: tuple = (), fallbacks: tuple = (),
                       budget_limit: int | None = None, budget_unit: str = "usd_cents"):
    """Build an execution profile from an existing settings AgentProfile."""
    if type(agent) is not AgentProfile:
        raise InvalidProfile("Only aik_config AgentProfile records can be adapted.")
    agent = AgentProfile(agent.id, agent.workspace_id, agent.role, agent.connection_id, agent.model, agent.effort)
    primary = ExecutionChoice(agent.connection_id, agent.model, agent.effort, tools)
    if agent.role == "director":
        return DirectorProfile(agent.id, agent.workspace_id, primary, max_calls, fallbacks, budget_limit, budget_unit)
    return RoleProfile(agent.id, agent.workspace_id, agent.role, primary, max_calls, fallbacks, budget_limit,
                       budget_unit)
