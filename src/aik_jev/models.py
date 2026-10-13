"""RAW, common REQ, classification and AGENT selection records.

RAW text is never placed in the REQ; the REQ binds to it by SHA-256 only.
JEV output is advisory metadata. The AGENT selection is a separate record
with ``model_reported`` provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Mapping

from .errors import JevError


MAX_RAW_BYTES = 1024 * 1024
MAX_SKILLS = 64
MAX_CONSTRAINTS = 64
MAX_PROBABILITIES = 256
MAX_INTEGER = 2**63 - 1
PROVIDER_OBSERVED = "provider_observed"
LOCAL_DERIVED = "local_derived"
MODEL_REPORTED = "model_reported"
STATUSES = ("classified", "off", "unavailable", "failed", "rejected_response")
SOURCES = ("jev", "fallback_candidate", "none")
MODES = ("on", "off")
# Metadata confirmed by code or entered explicitly by the user; never LLM-normalized.
METADATA_PROVENANCES = ("code_verified", "user_supplied")

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_SOURCE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


def check_identifier(value: object, code: str) -> None:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise JevError(code)


def check_model(value: object, code: str = "INVALID_MODEL") -> None:
    if type(value) is not str or _MODEL.fullmatch(value) is None:
        raise JevError(code)


def check_digest(value: object, code: str) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise JevError(code)


def check_text(value: object, code: str, limit: int, *, multiline: bool = False) -> None:
    if type(value) is not str or not 1 <= len(value) <= limit:
        raise JevError(code)
    if multiline:
        if "\0" in value:
            raise JevError(code)
    elif not value.isprintable():
        raise JevError(code)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise JevError(code) from None


def check_timestamp(value: object, code: str = "INVALID_RECORDED_AT") -> None:
    if type(value) is not str:
        raise JevError(code)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise JevError(code) from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise JevError(code)


def utc_timestamp(moment: object) -> str:
    if type(moment) is not datetime or moment.tzinfo is None or moment.utcoffset() is None:
        raise JevError("INVALID_CLOCK")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def unit_interval(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def normalize_probabilities(value: object, code: str, allowed=None) -> tuple:
    if type(value) is list:
        value = tuple(value)
    if type(value) is not tuple or len(value) > MAX_PROBABILITIES:
        raise JevError(code)
    result = {}
    for item in value:
        if type(item) not in (tuple, list) or len(item) != 2:
            raise JevError(code)
        name, probability = item
        check_text(name, code, 256)
        if not unit_interval(probability) or name in result:
            raise JevError(code)
        if allowed is not None and name not in allowed:
            raise JevError(code)
        result[name] = float(probability)
    return tuple(sorted(result.items(), key=lambda pair: pair[0]))


@dataclass(frozen=True, slots=True, repr=False)
class RawPrompt:
    """The user's RAW prompt, kept byte-for-byte. repr never shows the text."""

    text: str

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text:
            raise JevError("INVALID_RAW")
        try:
            size = len(self.text.encode("utf-8"))
        except UnicodeEncodeError:
            raise JevError("INVALID_RAW") from None
        if size > MAX_RAW_BYTES:
            raise JevError("RAW_TOO_LARGE")

    @property
    def byte_length(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return f"RawPrompt(sha256={self.sha256}, byte_length={self.byte_length})"


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    skill_id: str
    description: str | None = None
    source: str = "agent_manifest"

    def __post_init__(self) -> None:
        check_identifier(self.skill_id, "INVALID_SKILL_ID")
        if self.description is not None:
            check_text(self.description, "INVALID_SKILL_DESCRIPTION", 4096, multiline=True)
        if type(self.source) is not str or _SOURCE.fullmatch(self.source) is None:
            raise JevError("INVALID_SKILL_SOURCE")

    def canonical_dict(self) -> dict:
        return {"skill_id": self.skill_id, "description": self.description, "source": self.source}


@dataclass(frozen=True, slots=True)
class CommonReq:
    """Code-verified metadata only. RAW text is excluded and bound by raw_sha256."""

    raw_sha256: str
    work_mode: str
    target: str | None
    skills: tuple
    constraints: tuple = ()
    metadata_provenance: str = "code_verified"

    def __post_init__(self) -> None:
        check_digest(self.raw_sha256, "INVALID_RAW_SHA256")
        check_identifier(self.work_mode, "INVALID_WORK_MODE")
        if self.target is not None:
            check_text(self.target, "INVALID_TARGET", 1024)
        skills = tuple(self.skills) if type(self.skills) is list else self.skills
        if type(skills) is not tuple or not 1 <= len(skills) <= MAX_SKILLS:
            raise JevError("INVALID_SKILLS")
        rebuilt, seen = [], set()
        for skill in skills:
            if type(skill) is not SkillCandidate:
                raise JevError("INVALID_SKILLS")
            skill = SkillCandidate(skill.skill_id, skill.description, skill.source)
            if skill.skill_id in seen:
                raise JevError("DUPLICATE_SKILL")
            seen.add(skill.skill_id)
            rebuilt.append(skill)
        object.__setattr__(self, "skills", tuple(rebuilt))
        constraints = tuple(self.constraints) if type(self.constraints) is list else self.constraints
        if type(constraints) is not tuple or len(constraints) > MAX_CONSTRAINTS:
            raise JevError("INVALID_CONSTRAINTS")
        for item in constraints:
            check_text(item, "INVALID_CONSTRAINTS", 512)
        object.__setattr__(self, "constraints", constraints)
        if type(self.metadata_provenance) is not str or self.metadata_provenance not in METADATA_PROVENANCES:
            raise JevError("INVALID_METADATA_PROVENANCE")

    @classmethod
    def for_raw(cls, raw: RawPrompt, *, work_mode: str, skills, target: str | None = None,
                constraints=(), metadata_provenance: str = "code_verified") -> "CommonReq":
        if type(raw) is not RawPrompt:
            raise JevError("INVALID_RAW")
        return cls(raw.sha256, work_mode, target, tuple(skills), tuple(constraints), metadata_provenance)

    @property
    def skill_ids(self) -> tuple:
        return tuple(skill.skill_id for skill in self.skills)

    def canonical_dict(self) -> dict:
        return {
            "raw_sha256": self.raw_sha256,
            "work_mode": self.work_mode,
            "target": self.target,
            "skills": [skill.canonical_dict() for skill in self.skills],
            "constraints": list(self.constraints),
            "metadata_provenance": self.metadata_provenance,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.canonical_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class JevAnswer:
    """A JEV choice answer reduced to choice, confidence and probabilities."""

    choice: str | None
    confidence: float | None
    probabilities: tuple = ()

    def __post_init__(self) -> None:
        if self.choice is not None:
            check_text(self.choice, "INVALID_RESPONSE", 256)
        if self.confidence is not None:
            if not unit_interval(self.confidence):
                raise JevError("INVALID_RESPONSE")
            object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "probabilities", normalize_probabilities(self.probabilities, "INVALID_RESPONSE"))

    @classmethod
    def from_choice_answer(cls, answer: object) -> "JevAnswer":
        if not isinstance(answer, Mapping) or answer.get("type") != "choice":
            raise JevError("INVALID_RESPONSE")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, Mapping):
            raise JevError("INVALID_RESPONSE")
        return cls(answer.get("choice"), answer.get("confidence"), tuple(probabilities.items()))


@dataclass(frozen=True, slots=True)
class FallbackCandidate:
    """A classification route that passed (or failed) the vendor quality bar."""

    vendor: str
    route_id: str
    model: str
    quality_passed: bool
    cost_rank: int

    def __post_init__(self) -> None:
        check_identifier(self.vendor, "INVALID_FALLBACK")
        check_identifier(self.route_id, "INVALID_FALLBACK")
        check_model(self.model, "INVALID_FALLBACK")
        if type(self.quality_passed) is not bool:
            raise JevError("INVALID_FALLBACK")
        if type(self.cost_rank) is not int or not 0 <= self.cost_rank <= MAX_INTEGER:
            raise JevError("INVALID_FALLBACK")

    def canonical_dict(self) -> dict:
        return {"vendor": self.vendor, "route_id": self.route_id, "model": self.model,
                "quality_passed": self.quality_passed, "cost_rank": self.cost_rank}


def select_fallback(candidates, vendor: str) -> FallbackCandidate | None:
    """Cheapest quality-passed candidate of the same vendor; ties keep input order."""
    check_identifier(vendor, "INVALID_VENDOR")
    best = None
    for candidate in candidates:
        if type(candidate) is not FallbackCandidate:
            raise JevError("INVALID_FALLBACK")
        candidate = FallbackCandidate(candidate.vendor, candidate.route_id, candidate.model,
                                      candidate.quality_passed, candidate.cost_rank)
        if candidate.vendor != vendor or not candidate.quality_passed:
            continue
        if best is None or candidate.cost_rank < best.cost_rank:
            best = candidate
    return best


@dataclass(frozen=True, slots=True)
class Classification:
    """One JEV (or OFF/failure) classification outcome. Advisory only."""

    classification_id: str
    request_id: str
    raw_sha256: str
    raw_bytes: int
    req_sha256: str
    candidate_skill_ids: tuple
    mode: str
    status: str
    source: str
    requested_model: str | None
    observed_model: str | None
    recommended_skill: str | None
    confidence: float | None
    probabilities: tuple
    usage_input_tokens: int | None
    usage_output_tokens: int | None
    failure_code: str | None
    fallback: FallbackCandidate | None
    recorded_at: str

    def __post_init__(self) -> None:
        code = "INVALID_CLASSIFICATION"
        check_identifier(self.classification_id, code)
        check_identifier(self.request_id, "INVALID_REQUEST_ID")
        check_digest(self.raw_sha256, code)
        if type(self.raw_bytes) is not int or not 1 <= self.raw_bytes <= MAX_RAW_BYTES:
            raise JevError(code)
        check_digest(self.req_sha256, code)
        ids = tuple(self.candidate_skill_ids) if type(self.candidate_skill_ids) is list else self.candidate_skill_ids
        if type(ids) is not tuple or not 1 <= len(ids) <= MAX_SKILLS or len(set(ids)) != len(ids):
            raise JevError(code)
        for skill_id in ids:
            check_identifier(skill_id, code)
        object.__setattr__(self, "candidate_skill_ids", ids)
        for value, allowed in ((self.mode, MODES), (self.status, STATUSES), (self.source, SOURCES)):
            if type(value) is not str or value not in allowed:
                raise JevError(code)
        if self.requested_model is not None:
            check_model(self.requested_model, code)
        if self.observed_model is not None:
            check_text(self.observed_model, code, 256)
        if self.recommended_skill is not None and self.recommended_skill not in ids:
            raise JevError(code)
        if self.confidence is not None:
            if not unit_interval(self.confidence):
                raise JevError(code)
            object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "probabilities", normalize_probabilities(self.probabilities, code, set(ids)))
        for tokens in (self.usage_input_tokens, self.usage_output_tokens):
            if tokens is not None and (type(tokens) is not int or not 0 <= tokens <= MAX_INTEGER):
                raise JevError(code)
        if self.failure_code is not None and (type(self.failure_code) is not str
                                              or _CODE.fullmatch(self.failure_code) is None):
            raise JevError(code)
        if self.fallback is not None:
            fallback = self.fallback
            if type(fallback) is not FallbackCandidate:
                raise JevError(code)
            object.__setattr__(self, "fallback", FallbackCandidate(
                fallback.vendor, fallback.route_id, fallback.model, fallback.quality_passed, fallback.cost_rank))
        check_timestamp(self.recorded_at)
        observed = (self.observed_model, self.recommended_skill, self.confidence,
                    self.usage_input_tokens, self.usage_output_tokens)
        if self.status == "classified":
            if (self.source != "jev" or self.mode != "on" or self.requested_model is None
                    or any(value is None for value in observed)
                    or self.failure_code is not None or self.fallback is not None):
                raise JevError(code)
        else:
            if self.source == "jev" or any(value is not None for value in observed) or self.probabilities:
                raise JevError(code)
            if (self.source == "fallback_candidate") != (self.fallback is not None):
                raise JevError(code)
            if self.status == "off":
                if self.failure_code is not None or self.requested_model is not None:
                    raise JevError(code)
            elif self.failure_code is None or self.mode != "on" or self.requested_model is None:
                raise JevError(code)

    @property
    def provenance(self) -> Mapping[str, str]:
        """Field provenance: provider observations versus local derivations."""
        result = {name: LOCAL_DERIVED for name in (
            "status", "source", "mode", "requested_model", "failure_code", "fallback", "candidate_skill_ids",
        )}
        if self.status == "classified":
            for name in ("observed_model", "recommended_skill", "confidence", "probabilities",
                         "usage_input_tokens", "usage_output_tokens"):
                result[name] = PROVIDER_OBSERVED
        return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class AgentSelection:
    """The AGENT's own final skill choice. ``None`` means no skill was used."""

    agent_id: str
    selected_skill_id: str | None
    provenance: str = MODEL_REPORTED

    def __post_init__(self) -> None:
        check_identifier(self.agent_id, "INVALID_SELECTION")
        if self.selected_skill_id is not None:
            check_identifier(self.selected_skill_id, "INVALID_SELECTION")
        if self.provenance != MODEL_REPORTED:
            raise JevError("INVALID_SELECTION")


def selection_flags(classification: Classification, selected_skill_id: str | None) -> tuple:
    """(matches_recommendation, outside_candidates); matches is None without a recommendation."""
    recommended = classification.recommended_skill
    matches = None if recommended is None else selected_skill_id == recommended
    outside = selected_skill_id is not None and selected_skill_id not in classification.candidate_skill_ids
    return matches, outside


@dataclass(frozen=True, slots=True)
class AgentSelectionRecord:
    selection_id: str
    classification_id: str
    agent_id: str
    selected_skill_id: str | None
    provenance: str
    matches_recommendation: bool | None
    outside_candidates: bool
    recorded_at: str

    def __post_init__(self) -> None:
        check_identifier(self.selection_id, "INVALID_SELECTION")
        check_identifier(self.classification_id, "INVALID_SELECTION")
        AgentSelection(self.agent_id, self.selected_skill_id, self.provenance)
        if self.matches_recommendation is not None and type(self.matches_recommendation) is not bool:
            raise JevError("INVALID_SELECTION")
        if type(self.outside_candidates) is not bool:
            raise JevError("INVALID_SELECTION")
        check_timestamp(self.recorded_at)
