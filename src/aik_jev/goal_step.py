"""Optional JEV step for the controller's goal preparation (``Controller(classifier=...)``).

``GoalClassifier(request_id, goal_text)`` wraps the user's goal text as RAW,
asks the caller for the code-verified common REQ and runs ``JevBridge.classify``
under the controller's VM request ID. The goal text sent to VM is never
changed, and the result is advisory: it does not pick the skill, the plan or
the worker. Whether JEV is called at all follows the bridge mode.
"""

from __future__ import annotations

from typing import Callable

from .bridge import JevBridge
from .errors import JevError
from .models import Classification, CommonReq, RawPrompt


class GoalClassifier:
    def __init__(self, bridge: JevBridge, req_for_raw: Callable[[RawPrompt], CommonReq]):
        if type(bridge) is not JevBridge:
            raise JevError("INVALID_BRIDGE")
        if not callable(req_for_raw):
            raise JevError("INVALID_REQ_BUILDER")
        self._bridge = bridge
        self._req_for_raw = req_for_raw

    @property
    def bridge(self) -> JevBridge:
        return self._bridge

    def __call__(self, request_id: str, goal_text: str) -> Classification:
        raw = RawPrompt(goal_text)
        req = self._req_for_raw(raw)
        if type(req) is not CommonReq:
            raise JevError("INVALID_REQ")
        return self._bridge.classify(request_id, raw, req)


__all__ = ["GoalClassifier"]
