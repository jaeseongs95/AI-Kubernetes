"""Connects RAW + common REQ to JEV and records AGENT selections separately.

JEV output is advisory. The bridge never chooses or overrides the AGENT's
skill, never calls a fallback LLM itself and never retries JEV.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from .client import JevClient
from .errors import JevError
from .journal import JevJournal
from .models import (
    AgentSelection, AgentSelectionRecord, Classification, CommonReq, FallbackCandidate, JevAnswer,
    RawPrompt, check_identifier, check_model, select_fallback, selection_flags, utc_timestamp,
)


SKILL_QUESTION = "skill"
SKILL_INSTRUCTIONS = (
    "Which skill candidate best fits the raw request? This is advisory; the agent makes the final choice."
)
_FAILED_CODES = frozenset({"REQUEST_REJECTED", "INVALID_REQUEST"})
_REJECTED_CODES = frozenset({"INVALID_RESPONSE", "RESPONSE_TOO_LARGE"})


def _failure_status(code: str) -> str:
    if code in _FAILED_CODES:
        return "failed"
    if code in _REJECTED_CODES:
        return "rejected_response"
    return "unavailable"


class JevBridge:
    def __init__(self, journal: JevJournal, *, client: JevClient | None = None, mode: str, vendor: str,
                 model: str = "jev-latest", fallback_candidates=(), clock=None):
        if type(journal) is not JevJournal:
            raise JevError("INVALID_JOURNAL")
        if client is not None and type(client) is not JevClient:
            raise JevError("INVALID_CLIENT")
        if mode not in ("on", "off"):
            raise JevError("INVALID_MODE")
        check_identifier(vendor, "INVALID_VENDOR")
        check_model(model)
        candidates = tuple(fallback_candidates)
        if any(type(candidate) is not FallbackCandidate for candidate in candidates):
            raise JevError("INVALID_FALLBACK")
        if clock is not None and not callable(clock):
            raise JevError("INVALID_CLOCK")
        self._journal = journal
        self._client = client
        self._mode = mode
        self._vendor = vendor
        self._model = model
        self._candidates = candidates
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def mode(self) -> str:
        return self._mode

    def _now(self) -> str:
        return utc_timestamp(self._clock())

    def _without_jev(self, base: dict, status: str, failure_code: str | None,
                     requested_model: str | None) -> Classification:
        fallback = select_fallback(self._candidates, self._vendor)
        return Classification(
            **base, status=status, source="none" if fallback is None else "fallback_candidate",
            requested_model=requested_model, observed_model=None, recommended_skill=None, confidence=None,
            probabilities=(), usage_input_tokens=None, usage_output_tokens=None,
            failure_code=failure_code, fallback=fallback, recorded_at=self._now(),
        )

    def classify(self, request_id: str, raw: RawPrompt, req: CommonReq) -> Classification:
        check_identifier(request_id, "INVALID_REQUEST_ID")
        if type(raw) is not RawPrompt:
            raise JevError("INVALID_RAW")
        if type(req) is not CommonReq:
            raise JevError("INVALID_REQ")
        raw = RawPrompt(raw.text)
        req = CommonReq(req.raw_sha256, req.work_mode, req.target, req.skills, req.constraints,
                        req.metadata_provenance)
        if raw.sha256 != req.raw_sha256:
            raise JevError("RAW_REQ_MISMATCH")
        req_json, req_sha256 = req.canonical_json(), req.sha256
        existing = self._journal.find_by_request(request_id)
        if existing is not None:
            if (existing.raw_sha256, existing.raw_bytes, existing.req_sha256) == (
                    raw.sha256, raw.byte_length, req_sha256):
                return existing
            raise JevError("REQUEST_CONFLICT")
        base = {
            "classification_id": f"jevc-{uuid.uuid4().hex}", "request_id": request_id,
            "raw_sha256": raw.sha256, "raw_bytes": raw.byte_length, "req_sha256": req_sha256,
            "candidate_skill_ids": req.skill_ids, "mode": self._mode,
        }
        if self._mode == "off" or self._client is None:
            record = self._without_jev(base, "off", None, None)
        else:
            question = {
                "type": "choice", "instructions": SKILL_INSTRUCTIONS,
                "criteria": {skill.skill_id: skill.description for skill in req.skills},
            }
            try:
                result = self._client.evaluate(
                    self._model, {"raw": raw.text, "req": req.canonical_dict()}, {SKILL_QUESTION: question},
                )
            except JevError as error:
                record = self._without_jev(base, _failure_status(error.code), error.code, self._model)
            else:
                answer = JevAnswer.from_choice_answer(result.answers[SKILL_QUESTION])
                record = Classification(
                    **base, status="classified", source="jev", requested_model=self._model,
                    observed_model=result.observed_model, recommended_skill=answer.choice,
                    confidence=answer.confidence, probabilities=answer.probabilities,
                    usage_input_tokens=result.input_tokens, usage_output_tokens=result.output_tokens,
                    failure_code=None, fallback=None, recorded_at=self._now(),
                )
        return self._journal.append_classification(record, req_json)

    def record_agent_selection(self, classification_id: str, selection: AgentSelection) -> AgentSelectionRecord:
        """Record the AGENT's final choice as reported; it is never rejected or overridden."""
        check_identifier(classification_id, "INVALID_CLASSIFICATION")
        if type(selection) is not AgentSelection:
            raise JevError("INVALID_SELECTION")
        selection = AgentSelection(selection.agent_id, selection.selected_skill_id, selection.provenance)
        classification = self._journal.get_classification(classification_id)
        if classification is None:
            raise JevError("UNKNOWN_CLASSIFICATION")
        matches, outside = selection_flags(classification, selection.selected_skill_id)
        record = AgentSelectionRecord(
            f"jevs-{uuid.uuid4().hex}", classification_id, selection.agent_id, selection.selected_skill_id,
            selection.provenance, matches, outside, self._now(),
        )
        return self._journal.append_selection(record)

    def view(self, classification_id: str) -> tuple:
        """(Classification, AGENT selection records) as separate values."""
        classification = self._journal.get_classification(classification_id)
        if classification is None:
            raise JevError("UNKNOWN_CLASSIFICATION")
        return classification, self._journal.selections(classification_id)
