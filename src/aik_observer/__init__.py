"""AIK-036 worker execution observer, event cursor and usage normalizer.

The observer watches one lease (AIK-034 lease ID, which equals the aik_budget
reservation ID) and its provider job. It reaches the provider only through
injected ports (JobPort, EventSource). It never reads credentials and never
grants execution rights.

- Requested settings (client_requested) are stored apart from provider
  observations (provider_observed) and model self-reports (model_reported).
- Gaps and duplicates in the event cursor are detected. Missing ranges are
  recovered by re-reading the source.
- Missing usage stays unknown. An explicit 0 is a confirmed 0.
- Only a terminal provider job state counts as completion. Partial output or
  an accepted request does not.
"""

from .models import (
    EVENT_KINDS, EVENT_PROVENANCES, NOT_PROVIDED, REQUESTED_PROVENANCE, EventConflict,
    EventSource, GapRecord, IngestResult, InvalidEvent, JobPort, ObservationView,
    ObservedSetting, ObservedSettings, ObserverError, ProviderEvent, RequestedSettings,
    SettlementSubmission, UnknownObservation, merge_usage, parse_usage,
)
from .observer import APPLICATION_ID, SCHEMA_VERSION, WorkerObserver

__all__ = [
    "WorkerObserver", "APPLICATION_ID", "SCHEMA_VERSION",
    "ProviderEvent", "EventSource", "JobPort",
    "IngestResult", "ObservationView", "RequestedSettings", "ObservedSetting", "ObservedSettings",
    "GapRecord", "SettlementSubmission",
    "EVENT_KINDS", "EVENT_PROVENANCES", "NOT_PROVIDED", "REQUESTED_PROVENANCE",
    "ObserverError", "InvalidEvent", "EventConflict", "UnknownObservation",
    "parse_usage", "merge_usage",
]
