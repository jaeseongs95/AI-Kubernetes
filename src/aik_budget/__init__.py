"""AIK-040 budget, quota and concurrency admission with append-only settlement.

Before every new director, worker or reviewer call, BudgetStore.admit checks
the following and returns admitted / wait / rejected with reason codes:

- the profile limits (aik_profiles: max_calls, budget_limit/budget_unit);
- the stored scope policies (resource or goal: API cost, calls, absolute
  deadline, concurrency, an opt-in best-effort token stop);
- the pool state (aik_pool.RouteStatus: enabled, freshness, capacity,
  configured_max_parallel);
- provider observations (429 rate limits, subscription quota remaining).

Usage is recorded only as supplied. A missing value stays unknown and is never
filled with zero or an estimate. API cost, tokens and subscription quota are
separate units and are never converted into each other. Settlement is
append-only. Late usage is an additional accounting record and does not
change any existing state.
No provider is called and no credential is read. Admission is an accounting
guard. It does not grant execution rights and does not cap provider billing.
"""

from .models import (
    ADMISSION_STATUSES, BASES, UNKNOWN, AdmissionDecision, AdmissionRequest, Amount,
    BudgetError, BudgetPolicy, CostTotals, PoolStatus, QuotaObservation, QuotaUsage,
    Reservation, ScopeStatus, SettlementRecord, StoredPolicy, TokenTotals, Usage,
    confirmed, estimated,
)
from .storage import APPLICATION_ID, BudgetStore, default_budget_path

__all__ = [
    "BudgetStore", "default_budget_path", "APPLICATION_ID",
    "AdmissionRequest", "AdmissionDecision", "BudgetPolicy", "StoredPolicy",
    "Amount", "Usage", "UNKNOWN", "confirmed", "estimated", "BASES", "ADMISSION_STATUSES",
    "CostTotals", "TokenTotals", "QuotaUsage", "ScopeStatus", "QuotaObservation", "PoolStatus",
    "SettlementRecord", "Reservation", "BudgetError",
]
