"""AIK-040 admission guard, usage reservation/settlement and quota status.

The data lives in its own SQLite file (application ID AIKB). It holds the
following records:
- stored scope policies;
- reservations;
- append-only usage records;
- provider observations (subscription quota remaining, 429 rate limits);
- every admission decision.

Path rules and the cooperating process lock are reused from aik_runtime.paths.
Each admission reads its counters and inserts its reservation inside one
locked IMMEDIATE transaction. Contending callers therefore cannot both take
the last concurrency slot or the last API cost budget. Remaining subscription
quota is guarded conservatively: every reservation counts as at least one
unit. No provider is called and no credential is read.

Each usage report states the total usage of its call. For each field, the
latest confirmed report wins. When no confirmed report exists, the latest
estimated report is used. When neither exists, the call's cost is unknown.
An unknown cost is never counted as zero.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta
import errno
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import NamedTuple

from aik_config.models import Revisioned
from aik_pool import RouteStatus
from aik_profiles import rebuild_profile
from aik_profiles.errors import InvalidProfile
from aik_runtime.errors import InvalidDatabase, RevisionConflict, StorageBusy, StorageError
from aik_runtime.models import MAX_INTEGER
from aik_runtime.paths import database_lock, default_database_path, validate_database_path

from .models import (
    MAX_SECONDS, AdmissionDecision, AdmissionRequest, BudgetError, BudgetPolicy, CostTotals,
    PoolStatus, QuotaObservation, QuotaUsage, Reservation, ScopeStatus, SettlementRecord,
    StoredPolicy, TokenTotals, Usage, check_count, check_id, format_timestamp, normalize_timestamp,
    parse_timestamp, policy_from_dict, rebuild, usage_from_dict,
)


APPLICATION_ID = 0x41494B42  # AIKB; distinct from runtime (AIKR), profiles (AIKP), settings (AIKC).
SCHEMA_VERSION = 1


def _append_only(table: str) -> tuple[str, str]:
    return (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    )


_STATEMENTS = (
    "CREATE TABLE policies (scope_id TEXT PRIMARY KEY NOT NULL, document TEXT NOT NULL, "
    "revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 1))",
    "CREATE TABLE reservations (id TEXT PRIMARY KEY NOT NULL, pool_id TEXT NOT NULL, profile_id TEXT NOT NULL, "
    "role TEXT NOT NULL CHECK (role IN ('director', 'worker', 'reviewer')), "
    "state TEXT NOT NULL CHECK (state IN ('reserved', 'settled', 'released')), "
    "created_at TEXT NOT NULL, document TEXT NOT NULL)",
    "CREATE INDEX reservations_pool_state ON reservations (pool_id, state)",
    "CREATE INDEX reservations_profile ON reservations (profile_id)",
    "CREATE TABLE reservation_scopes (reservation_id TEXT NOT NULL REFERENCES reservations (id), "
    "scope_id TEXT NOT NULL, PRIMARY KEY (reservation_id, scope_id))",
    "CREATE INDEX reservation_scopes_scope ON reservation_scopes (scope_id)",
    "CREATE TABLE usage_records (seq INTEGER PRIMARY KEY, "
    "reservation_id TEXT NOT NULL REFERENCES reservations (id), "
    "kind TEXT NOT NULL CHECK (kind IN ('settlement', 'release', 'late')), "
    "recorded_at TEXT NOT NULL, document TEXT NOT NULL)",
    "CREATE INDEX usage_records_reservation ON usage_records (reservation_id, seq)",
    "CREATE TABLE observations (seq INTEGER PRIMARY KEY, pool_id TEXT NOT NULL, "
    "kind TEXT NOT NULL CHECK (kind IN ('quota', 'rate_limited', 'rate_cleared')), "
    "observed_at TEXT NOT NULL, document TEXT NOT NULL)",
    "CREATE INDEX observations_pool ON observations (pool_id, kind, seq)",
    "CREATE TABLE decisions (seq INTEGER PRIMARY KEY, "
    "status TEXT NOT NULL CHECK (status IN ('admitted', 'wait', 'rejected')), "
    "decided_at TEXT NOT NULL, document TEXT NOT NULL)",
    *_append_only("reservation_scopes"),
    *_append_only("usage_records"),
    *_append_only("observations"),
    *_append_only("decisions"),
    "CREATE TRIGGER reservations_no_delete BEFORE DELETE ON reservations "
    "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    "CREATE TRIGGER reservations_single_transition BEFORE UPDATE ON reservations WHEN NOT ("
    "OLD.state = 'reserved' AND NEW.state IN ('settled', 'released') AND NEW.id = OLD.id "
    "AND NEW.pool_id = OLD.pool_id AND NEW.profile_id = OLD.profile_id AND NEW.role = OLD.role "
    "AND NEW.created_at = OLD.created_at AND NEW.document = OLD.document) "
    "BEGIN SELECT RAISE(ABORT, 'single state transition'); END",
)

# A profile budget unit is enforced only when its quantity is measurable here.
_PROFILE_BUDGET_UNITS = {"usd_cents": "max_api_cost_cents", "tokens": "token_stop"}
# Stored resource policies with these prefixes apply to every matching call.
_RESOURCE_SCOPE_COLUMNS = (("pool:", "pool_id"), ("profile:", "profile_id"))
_QUOTA_KEYS = {"unit", "remaining", "limit", "resets_at"}


def default_budget_path() -> Path:
    """Next to the runtime database, as its own file."""
    return default_database_path().with_name("budget.sqlite3")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _STATEMENTS:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=1)
def _expected_schema() -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        _create_schema(connection)
        return _schema_rows(connection)


def _validate_schema(connection: sqlite3.Connection, *, full: bool) -> None:
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID \
                or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise InvalidDatabase("This is not a supported AIK budget database.")
        if _schema_rows(connection) != _expected_schema():
            raise InvalidDatabase("The budget schema differs from its declared version.")
        if full and tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
            raise InvalidDatabase("Budget database integrity validation failed.")
    except sqlite3.Error:
        raise InvalidDatabase("Budget database validation could not be completed.") from None


def _json(document: dict) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load(text: object) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        raise InvalidDatabase("A stored budget document is not valid JSON.") from None
    if type(value) is not dict:
        raise InvalidDatabase("A stored budget document is malformed.")
    return value


def _sqlite_failure(error: sqlite3.Error) -> None:
    if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise StorageBusy("SQLite could not acquire the budget database lock.") from None
    raise StorageError("The budget database operation could not be completed.") from None


def _sync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # No portable directory fsync on Windows; rename durability is OS-owned.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


def _seconds(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 < value <= MAX_SECONDS:
        raise BudgetError(code)
    return float(value)


def _profile(value):
    if type(value) is Revisioned:
        value = value.value
    try:
        return rebuild_profile(value)
    except InvalidProfile:
        raise BudgetError("INVALID_PROFILE") from None


# ------------------------------------------------------------------ accounting

class _Limits(NamedTuple):
    max_api_cost_cents: int | None = None
    max_calls: int | None = None
    deadline: datetime | None = None
    max_concurrency: int | None = None
    token_stop: int | None = None


def _policy_limits(policy: BudgetPolicy) -> _Limits:
    deadline = None if policy.deadline is None else parse_timestamp(policy.deadline, "deadline")
    return _Limits(policy.max_api_cost_cents, policy.max_calls, deadline, policy.max_concurrency,
                   policy.token_stop)


def _profile_limits(profile) -> tuple[_Limits, list]:
    limits = _Limits(max_calls=profile.max_calls)
    problems = []
    if profile.budget_limit is not None:
        target = _PROFILE_BUDGET_UNITS.get(profile.budget_unit)
        if target is None:
            problems.append(("reject", "BUDGET_UNIT_UNSUPPORTED", None))
        else:
            limits = limits._replace(**{target: profile.budget_limit})
    return limits, problems


class _Tally:
    __slots__ = ("calls", "active", "cost", "cost_reserved", "cost_unknown_held", "cost_unknown_records",
                 "cost_unknown_unheld", "cost_unheld_active", "tokens_observed", "token_unknown_records",
                 "quota", "quota_unreported")

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.cost = {"confirmed": 0, "estimated": 0}
        self.cost_reserved = 0
        self.cost_unknown_held = 0
        self.cost_unknown_records = 0
        self.cost_unknown_unheld = 0
        self.cost_unheld_active = 0
        self.tokens_observed = 0
        self.token_unknown_records = 0
        self.quota: dict[str, list[int]] = {}
        self.quota_unreported = 0

    @property
    def committed(self) -> int:
        return self.cost["confirmed"] + self.cost["estimated"] + self.cost_reserved + self.cost_unknown_held


def _latest_known(items) -> tuple | None:
    """Pick the effective report: latest confirmed, otherwise latest estimated, otherwise None."""
    chosen = {}
    for amount, extra in items:
        if amount.known:
            chosen[amount.basis] = (amount, extra)
    return chosen.get("confirmed", chosen.get("estimated"))


def _tally(connection: sqlite3.Connection, rows: list) -> _Tally:
    tally = _Tally()
    try:
        for reservation_id, state, document in rows:
            hold = _load(document)["api_cost_estimate_cents"]
            if hold is not None and (type(hold) is not int or hold < 0):
                raise ValueError
            if state not in ("reserved", "settled", "released"):
                raise ValueError
            tally.calls += 1
            records = connection.execute(
                "SELECT kind, document FROM usage_records WHERE reservation_id = ? ORDER BY seq",
                (reservation_id,),
            ).fetchall()
            reports = [usage_from_dict(_load(record)["usage"]) for kind, record in records if kind != "release"]
            if state == "reserved":
                if records:
                    raise ValueError  # Settlement and release always end the reservation.
                tally.active += 1
                if hold is None:
                    tally.cost_unheld_active += 1
                else:
                    tally.cost_reserved += hold
                continue
            if not reports:
                if state == "settled":
                    raise ValueError
                continue  # Released before dispatch and never reported.
            # Each report states the call's total, so the latest one replaces
            # earlier ones instead of being added (retransmission, late bills).
            cost = _latest_known((usage.api_cost_cents, None) for usage in reports)
            if cost is not None:
                tally.cost[cost[0].basis] += cost[0].value
            else:
                tally.cost_unknown_records += 1
                if state == "settled" and hold is not None:
                    # The actual cost is unknown: keep the hold, never assume zero.
                    tally.cost_unknown_held += hold
                else:
                    tally.cost_unknown_unheld += 1
            token_unknown = False
            for name in ("input_tokens", "output_tokens"):
                token = _latest_known((getattr(usage, name), None) for usage in reports)
                if token is None:
                    token_unknown = True
                else:
                    tally.tokens_observed += token[0].value
            tally.token_unknown_records += int(token_unknown)
            quota = _latest_known((usage.quota_used, usage.quota_unit) for usage in reports)
            if quota is None:
                tally.quota_unreported += 1
            else:
                entry = tally.quota.setdefault(quota[1], [0, 0])
                entry[0 if quota[0].basis == "confirmed" else 1] += quota[0].value
    except (KeyError, TypeError, ValueError):
        raise InvalidDatabase("A stored reservation or usage record violates its contract.") from None
    return tally


def _limit_verdicts(limits: _Limits, tally: _Tally, now: datetime, request: AdmissionRequest | None) -> list:
    out = []
    if limits.deadline is not None:
        if now >= limits.deadline:
            out.append(("reject", "DEADLINE_PASSED", None))
        elif request is not None and now + timedelta(seconds=request.max_seconds) > limits.deadline:
            out.append(("reject", "DEADLINE_INSUFFICIENT", None))
    if limits.max_calls is not None and tally.calls >= limits.max_calls:
        out.append(("reject", "CALL_LIMIT_REACHED", None))
    if limits.max_concurrency is not None and tally.active >= limits.max_concurrency:
        out.append(("wait", "CONCURRENCY_LIMIT", None))
    limit = limits.max_api_cost_cents
    if limit is not None:
        if tally.cost_unknown_unheld:
            # A dispatched call with no reported cost and no hold is not zero.
            out.append(("reject", "API_COST_UNKNOWN", None))
        if tally.cost_unheld_active:
            out.append(("wait", "API_COST_UNHELD_ACTIVE", None))
        committed = tally.committed
        settled = committed - tally.cost_reserved
        if settled >= limit:
            out.append(("reject", "API_COST_LIMIT_REACHED", None))
        elif request is None:
            if committed >= limit:
                out.append(("wait", "API_COST_HELD_BY_ACTIVE", None))
        else:
            estimate = request.api_cost_estimate_cents
            if estimate is None:
                out.append(("reject", "API_COST_ESTIMATE_REQUIRED", None))
            elif committed + estimate > limit:
                if settled + estimate <= limit:
                    out.append(("wait", "API_COST_HELD_BY_ACTIVE", None))
                else:
                    out.append(("reject", "API_COST_INSUFFICIENT", None))
    if limits.token_stop is not None and tally.tokens_observed >= limits.token_stop:
        out.append(("reject", "TOKEN_STOP_REACHED", None))
    return out


def _label(verdicts: list, label: str) -> list:
    return [(severity, f"{code}@{label}", hint) for severity, code, hint in verdicts]


class BudgetStore:
    """Admission guard, reservations, append-only settlement and quota status."""

    def __init__(self, path: Path | str | None = None, *, clock=None, timeout: float = 5.0,
                 route_max_age_seconds: float = 300.0, quota_ttl_seconds: float = 300.0,
                 require_subscription_quota: bool = True):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise BudgetError("INVALID_TIMEOUT")
        if clock is not None and not callable(clock):
            raise BudgetError("INVALID_CLOCK")
        if type(require_subscription_quota) is not bool:
            raise BudgetError("INVALID_QUOTA_REQUIREMENT")
        self.timeout = float(timeout)
        self._clock = clock
        self._route_max_age = timedelta(seconds=_seconds(route_max_age_seconds, "INVALID_ROUTE_MAX_AGE"))
        self._quota_ttl = timedelta(seconds=_seconds(quota_ttl_seconds, "INVALID_QUOTA_TTL"))
        self._require_quota = require_subscription_quota
        self.path = validate_database_path(default_budget_path() if path is None else path, create_parent=True)
        with database_lock(self.path, self.timeout):
            self._initialize()

    # ---------------------------------------------------------------- clock
    def _now(self) -> datetime:
        if self._clock is None:
            from datetime import timezone
            value = datetime.now(timezone.utc)
        else:
            try:
                value = self._clock()
            except Exception:
                raise BudgetError("CLOCK_UNAVAILABLE") from None
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise BudgetError("INVALID_CLOCK")
        return parse_timestamp(format_timestamp(value), "clock")

    # ---------------------------------------------------------------- files
    def _connect(self, path: Path, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self.timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidDatabase("Budget storage requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _check_files(self) -> bool:
        validate_database_path(self.path)
        for suffix in ("-wal", "-shm"):
            companion = self.path.with_name(self.path.name + suffix)
            validate_database_path(companion)
            if companion.exists():
                raise InvalidDatabase("Budget storage requires a DELETE-mode database without WAL companions.")
        journal = self.path.with_name(self.path.name + "-journal")
        validate_database_path(journal)
        if not self.path.exists():
            if journal.exists():
                raise InvalidDatabase("An orphan rollback journal cannot initialize budget storage.")
            return False
        with self.path.open("rb") as stream:
            header = stream.read(100)
        if len(header) < 100 or header[:16] != b"SQLite format 3\x00" \
                or int.from_bytes(header[68:72], "big") != APPLICATION_ID:
            raise InvalidDatabase("This file is not an AIK budget database.")
        return True

    def _initialize(self) -> None:
        try:
            if self._check_files():
                with closing(self._connect(self.path)) as connection:
                    _validate_schema(connection, full=True)
                return
            temporary = Path(tempfile.mkdtemp(prefix=".aik-budget-init-", dir=self.path.parent))
            try:
                created = temporary / "budget.sqlite3"
                with closing(self._connect(created, create=True)) as connection:
                    _create_schema(connection)
                    _validate_schema(connection, full=True)
                os.chmod(created, 0o600)
                _sync_file(created)
                if self._check_files():
                    raise StorageError("The budget destination changed during initialization.")
                os.replace(created, self.path)
                _sync_directory(self.path.parent)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        except StorageError:
            raise
        except sqlite3.Error as error:
            _sqlite_failure(error)
        except OSError:
            raise StorageError("The budget database files could not be processed safely.") from None

    @contextmanager
    def _transaction(self):
        with database_lock(self.path, self.timeout):
            try:
                exists = self._check_files()
            except OSError:
                raise StorageError("The budget database files could not be inspected.") from None
            if not exists:
                raise InvalidDatabase("The budget database is missing; it is not recreated implicitly.")
            connection = None
            try:
                connection = self._connect(self.path)
                connection.execute("BEGIN IMMEDIATE")
                _validate_schema(connection, full=False)
                yield connection
                connection.commit()
            except sqlite3.Error as error:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                _sqlite_failure(error)
            except BaseException:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                if connection is not None:
                    connection.close()

    # ------------------------------------------------------------- policies
    @staticmethod
    def _fetch_policy(connection, scope_id: str) -> StoredPolicy | None:
        row = connection.execute("SELECT document, revision FROM policies WHERE scope_id = ?",
                                 (scope_id,)).fetchone()
        if row is None:
            return None
        try:
            policy = policy_from_dict(_load(row[0]))
            if policy.scope_id != scope_id or type(row[1]) is not int or row[1] < 1:
                raise ValueError
        except ValueError:
            raise InvalidDatabase("A stored budget policy violates its contract.") from None
        return StoredPolicy(policy, row[1])

    def set_policy(self, policy: BudgetPolicy, expected_revision: int = 0) -> StoredPolicy:
        policy = rebuild(policy, BudgetPolicy)
        check_count(expected_revision, "expected_revision", minimum=0, optional=False)
        document = _json(policy.to_dict())
        with self._transaction() as connection:
            row = connection.execute("SELECT revision FROM policies WHERE scope_id = ?",
                                     (policy.scope_id,)).fetchone()
            if expected_revision == 0:
                if row is not None:
                    raise RevisionConflict("The budget policy already exists.")
                connection.execute("INSERT INTO policies (scope_id, document, revision) VALUES (?, ?, 1)",
                                   (policy.scope_id, document))
                revision = 1
            else:
                if row is None or row[0] != expected_revision or expected_revision == MAX_INTEGER:
                    raise RevisionConflict("The budget policy revision has changed or the policy is absent.")
                revision = expected_revision + 1
                connection.execute("UPDATE policies SET document = ?, revision = ? WHERE scope_id = ? AND revision = ?",
                                   (document, revision, policy.scope_id, expected_revision))
        return StoredPolicy(policy, revision)

    def policy(self, scope_id: str) -> StoredPolicy | None:
        check_id(scope_id, "scope_id")
        with self._transaction() as connection:
            return self._fetch_policy(connection, scope_id)

    # --------------------------------------------------------- observations
    def record_quota(self, pool_id: str, unit: str, remaining: int | None, *, limit: int | None = None,
                     resets_at: str | None = None) -> int:
        """Append a provider-reported subscription quota observation (remaining None = not reported)."""
        check_id(pool_id, "pool_id")
        check_id(unit, "quota_unit")
        check_count(remaining, "remaining", minimum=0)
        check_count(limit, "limit", minimum=0)
        if resets_at is not None:
            resets_at = normalize_timestamp(resets_at, "resets_at")
        document = {"unit": unit, "remaining": remaining, "limit": limit, "resets_at": resets_at}
        return self._observe(pool_id, "quota", document)

    def record_rate_limit(self, pool_id: str, retry_after_seconds: int | None = None) -> int:
        """Append a 429 observation; without retry_after, admission waits until clear_rate_limit."""
        check_id(pool_id, "pool_id")
        check_count(retry_after_seconds, "retry_after_seconds", minimum=0, maximum=MAX_SECONDS)
        return self._observe(pool_id, "rate_limited", {"retry_after_seconds": retry_after_seconds},
                             retry_after=retry_after_seconds)

    def clear_rate_limit(self, pool_id: str) -> int:
        check_id(pool_id, "pool_id")
        return self._observe(pool_id, "rate_cleared", {})

    def _observe(self, pool_id: str, kind: str, document: dict, *, retry_after: int | None = None) -> int:
        with self._transaction() as connection:
            now = self._now()
            if kind == "rate_limited":
                document = dict(document, retry_at=None if retry_after is None
                                else format_timestamp(now + timedelta(seconds=retry_after)))
            cursor = connection.execute(
                "INSERT INTO observations (pool_id, kind, observed_at, document) VALUES (?, ?, ?, ?)",
                (pool_id, kind, format_timestamp(now), _json(document)),
            )
            return cursor.lastrowid

    def _rate_limit(self, connection, pool_id: str, now: datetime) -> tuple[bool, str | None]:
        row = connection.execute(
            "SELECT kind, document FROM observations WHERE pool_id = ? AND kind IN ('rate_limited', 'rate_cleared') "
            "ORDER BY seq DESC LIMIT 1", (pool_id,),
        ).fetchone()
        if row is None or row[0] != "rate_limited":
            return False, None
        try:
            retry_at = _load(row[1])["retry_at"]
            if retry_at is None:
                return True, None
            return now < parse_timestamp(retry_at, "retry_at"), retry_at
        except (KeyError, TypeError, ValueError):
            raise InvalidDatabase("A stored rate limit observation is malformed.") from None

    def _latest_quota(self, connection, pool_id: str, now: datetime) -> dict[str, QuotaObservation]:
        latest: dict[str, QuotaObservation] = {}
        rows = connection.execute(
            "SELECT observed_at, document FROM observations WHERE pool_id = ? AND kind = 'quota' ORDER BY seq",
            (pool_id,),
        ).fetchall()
        try:
            for observed_at, document in rows:
                item = _load(document)
                # Revalidate stored documents: a damaged value must not read as quota remaining.
                if set(item) != _QUOTA_KEYS:
                    raise ValueError
                check_id(item["unit"], "quota_unit")
                check_count(item["remaining"], "remaining", minimum=0)
                check_count(item["limit"], "limit", minimum=0)
                if item["resets_at"] is not None:
                    normalize_timestamp(item["resets_at"], "resets_at")
                age = now - parse_timestamp(observed_at, "observed_at")
                # A clock that moves backwards is not proof of freshness.
                freshness = "fresh" if timedelta(0) <= age <= self._quota_ttl else "stale"
                latest[item["unit"]] = QuotaObservation(item["unit"], item["remaining"], item["limit"],
                                                        item["resets_at"], observed_at, freshness)
        except (KeyError, TypeError, ValueError):
            raise InvalidDatabase("A stored quota observation is malformed.") from None
        return latest

    @staticmethod
    def _quota_pending(connection, pool_id: str, observed_at: datetime) -> int:
        """Calls that may not be reflected in an observation: active or created since it."""
        rows = connection.execute("SELECT state, created_at FROM reservations WHERE pool_id = ?",
                                  (pool_id,)).fetchall()
        count = 0
        try:
            for state, created_at in rows:
                if state == "reserved" or parse_timestamp(created_at, "created_at") >= observed_at:
                    count += 1
        except ValueError:
            raise InvalidDatabase("A stored reservation violates its contract.") from None
        return count

    # ------------------------------------------------------------- admission
    def _route_verdicts(self, route: RouteStatus, active: int, now: datetime) -> list:
        out = []
        if route.pool_enabled is not True:
            out.append(("reject", "POOL_DISABLED", None))
        age_ok = False
        if route.last_probe_at is not None:
            try:
                age = now - parse_timestamp(route.last_probe_at, "last_probe_at")
                age_ok = timedelta(0) <= age <= self._route_max_age
            except BudgetError:
                age_ok = False
        if route.freshness != "fresh" or not age_ok:
            out.append(("wait", "ROUTE_STATUS_NOT_FRESH", None))
        elif route.capacity_state != "known" or type(route.available_capacity) is not int:
            out.append(("wait", "CAPACITY_UNKNOWN", None))
        elif route.available_capacity <= 0:
            out.append(("wait", "CAPACITY_EXHAUSTED", None))
        elif route.executable_now is not True:
            out.append(("wait", "ROUTE_NOT_EXECUTABLE", None))
        limit = route.configured_max_parallel
        if type(limit) is not int or limit < 1:
            out.append(("reject", "INVALID_POOL_LIMIT", None))
        elif active >= limit:
            out.append(("wait", "CONCURRENCY_LIMIT", None))
        return out

    def _observation_verdicts(self, connection, pool_id: str, auth_kind: str, now: datetime) -> list:
        out = []
        limited, retry_at = self._rate_limit(connection, pool_id, now)
        if limited:
            if retry_at is None:
                out.append(("wait", "RATE_LIMITED_RETRY_UNKNOWN", None))
            else:
                out.append(("wait", "RATE_LIMITED", retry_at))
        if auth_kind == "native_subscription" and self._require_quota:
            latest = self._latest_quota(connection, pool_id, now)
            if not latest:
                out.append(("wait", "QUOTA_REMAINING_UNKNOWN", None))
            for observation in latest.values():
                if observation.freshness != "fresh":
                    out.append(("wait", "QUOTA_OBSERVATION_STALE", None))
                elif observation.remaining is None:
                    out.append(("wait", "QUOTA_REMAINING_UNKNOWN", None))
                elif observation.remaining <= 0:
                    out.append(("wait", "QUOTA_EXHAUSTED", observation.resets_at))
                elif self._quota_pending(connection, pool_id, parse_timestamp(
                        observation.observed_at, "observed_at")) >= observation.remaining:
                    # Each call is counted as at least one unit; a new observation is required.
                    out.append(("wait", "QUOTA_REMAINING_UNCONFIRMED", None))
        return out

    @staticmethod
    def _profile_rows(connection, profile_id: str) -> list:
        return connection.execute("SELECT id, state, document FROM reservations WHERE profile_id = ?",
                                  (profile_id,)).fetchall()

    @staticmethod
    def _scope_rows(connection, scope_id: str) -> list:
        return connection.execute(
            "SELECT r.id, r.state, r.document FROM reservations r "
            "JOIN reservation_scopes s ON s.reservation_id = r.id WHERE s.scope_id = ?", (scope_id,),
        ).fetchall()

    @classmethod
    def _rows_for_scope(cls, connection, scope_id: str) -> list:
        """Resource scopes count every reservation on that pool/profile, plus explicitly tagged ones."""
        for prefix, column in _RESOURCE_SCOPE_COLUMNS:
            if scope_id.startswith(prefix):
                return connection.execute(
                    f"SELECT id, state, document FROM reservations WHERE {column} = ? OR id IN "
                    "(SELECT reservation_id FROM reservation_scopes WHERE scope_id = ?)",
                    (scope_id[len(prefix):], scope_id),
                ).fetchall()
        return cls._scope_rows(connection, scope_id)

    def _applied_scopes(self, connection, request: AdmissionRequest, profile) -> tuple[str, ...]:
        implicit = tuple(
            scope_id for scope_id in ("pool:" + request.pool_id, "profile:" + profile.id)
            if scope_id not in request.scopes and self._fetch_policy(connection, scope_id) is not None
        )
        return (*request.scopes, *implicit)

    def admit(self, request: AdmissionRequest, profile, route: RouteStatus) -> AdmissionDecision:
        """Check every limit and, only when nothing blocks, append one reservation."""
        request = rebuild(request, AdmissionRequest)
        profile = _profile(profile)
        if type(route) is not RouteStatus:
            raise BudgetError("INVALID_ROUTE")
        if (route.pool_id, route.model) != (request.pool_id, request.model):
            raise BudgetError("ROUTE_MISMATCH")
        with self._transaction() as connection:
            now = self._now()
            if connection.execute("SELECT 1 FROM reservations WHERE id = ?", (request.reservation_id,)).fetchone():
                raise BudgetError("DUPLICATE_RESERVATION")
            pool_label = "pool:" + request.pool_id
            active_on_pool = connection.execute(
                "SELECT COUNT(*) FROM reservations WHERE pool_id = ? AND state = 'reserved'", (request.pool_id,),
            ).fetchone()[0]
            verdicts = _label(self._route_verdicts(route, active_on_pool, now), pool_label)
            verdicts += _label(self._observation_verdicts(connection, request.pool_id, route.auth_kind, now),
                               pool_label)
            profile_label = "profile:" + profile.id
            limits, problems = _profile_limits(profile)
            verdicts += _label(problems, profile_label)
            verdicts += _label(_limit_verdicts(limits, _tally(connection, self._profile_rows(connection, profile.id)),
                                               now, request), profile_label)
            for scope_id in self._applied_scopes(connection, request, profile):
                stored = self._fetch_policy(connection, scope_id)
                if stored is None:
                    verdicts.append(("reject", f"SCOPE_POLICY_MISSING@{scope_id}", None))
                    continue
                tally = _tally(connection, self._rows_for_scope(connection, scope_id))
                verdicts += _label(_limit_verdicts(_policy_limits(stored.policy), tally, now, request), scope_id)

            if any(severity == "reject" for severity, _, _ in verdicts):
                status = "rejected"
            elif verdicts:
                status = "wait"
            else:
                status = "admitted"
            hints = [parse_timestamp(hint, "retry_at") for severity, _, hint in verdicts
                     if severity == "wait" and hint is not None]
            retry_at = format_timestamp(max(hints)) if status == "wait" and hints else None
            decided_at = format_timestamp(now)
            if status == "admitted":
                document = {
                    "request": request.to_dict(),
                    "api_cost_estimate_cents": request.api_cost_estimate_cents,
                    "run_deadline": format_timestamp(now + timedelta(seconds=request.max_seconds)),
                    "auth_kind": str(route.auth_kind),
                }
                connection.execute(
                    "INSERT INTO reservations (id, pool_id, profile_id, role, state, created_at, document) "
                    "VALUES (?, ?, ?, ?, 'reserved', ?, ?)",
                    (request.reservation_id, request.pool_id, profile.id, profile.role, decided_at, _json(document)),
                )
                for scope_id in request.scopes:
                    connection.execute("INSERT INTO reservation_scopes (reservation_id, scope_id) VALUES (?, ?)",
                                       (request.reservation_id, scope_id))
            decision = AdmissionDecision(
                status=status,
                reservation_id=request.reservation_id if status == "admitted" else None,
                request_id=request.reservation_id,
                profile_id=profile.id,
                role=profile.role,
                pool_id=request.pool_id,
                reasons=tuple(code for _, code, _ in verdicts),
                retry_at=retry_at,
                decided_at=decided_at,
            )
            connection.execute("INSERT INTO decisions (status, decided_at, document) VALUES (?, ?, ?)",
                               (status, decided_at, _json(decision.to_dict())))
        return decision

    # ------------------------------------------------------------ settlement
    def settle(self, reservation_id: str, usage: Usage) -> SettlementRecord:
        """Report the call's total usage.

        The first report settles an active reservation; later reports are
        appended as 'late'. Per field, the latest confirmed report supersedes
        earlier ones, so a resend or a corrected bill is not added twice.
        """
        check_id(reservation_id, "reservation_id")
        usage = rebuild(usage, Usage)
        with self._transaction() as connection:
            now = format_timestamp(self._now())
            row = connection.execute("SELECT state FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
            if row is None:
                raise BudgetError("UNKNOWN_RESERVATION")
            kind = "settlement" if row[0] == "reserved" else "late"
            if kind == "settlement":
                connection.execute("UPDATE reservations SET state = 'settled' WHERE id = ? AND state = 'reserved'",
                                   (reservation_id,))
            cursor = connection.execute(
                "INSERT INTO usage_records (reservation_id, kind, recorded_at, document) VALUES (?, ?, ?, ?)",
                (reservation_id, kind, now, _json({"usage": usage.to_dict()})),
            )
            return SettlementRecord(cursor.lastrowid, reservation_id, kind, now, usage)

    def release(self, reservation_id: str) -> SettlementRecord:
        """The caller confirms no provider call was dispatched; the hold and slot are freed."""
        check_id(reservation_id, "reservation_id")
        with self._transaction() as connection:
            now = format_timestamp(self._now())
            row = connection.execute("SELECT state FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
            if row is None:
                raise BudgetError("UNKNOWN_RESERVATION")
            if row[0] != "reserved":
                raise BudgetError("RESERVATION_NOT_ACTIVE")
            connection.execute("UPDATE reservations SET state = 'released' WHERE id = ? AND state = 'reserved'",
                               (reservation_id,))
            cursor = connection.execute(
                "INSERT INTO usage_records (reservation_id, kind, recorded_at, document) VALUES (?, 'release', ?, ?)",
                (reservation_id, now, _json({"usage": None})),
            )
            return SettlementRecord(cursor.lastrowid, reservation_id, "release", now, None)

    def usage_records(self, reservation_id: str) -> tuple[SettlementRecord, ...]:
        check_id(reservation_id, "reservation_id")
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT seq, kind, recorded_at, document FROM usage_records WHERE reservation_id = ? ORDER BY seq",
                (reservation_id,),
            ).fetchall()
        records = []
        try:
            for seq, kind, recorded_at, document in rows:
                usage = _load(document)["usage"]
                records.append(SettlementRecord(seq, reservation_id, kind, recorded_at,
                                                None if usage is None else usage_from_dict(usage)))
        except (KeyError, ValueError):
            raise InvalidDatabase("A stored usage record violates its contract.") from None
        return tuple(records)

    def reservation(self, reservation_id: str) -> Reservation | None:
        check_id(reservation_id, "reservation_id")
        with self._transaction() as connection:
            now = self._now()
            row = connection.execute(
                "SELECT id, pool_id, profile_id, role, state, created_at, document FROM reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            document = _load(row[6])
            run_deadline = document["run_deadline"]
            overdue = row[4] == "reserved" and now > parse_timestamp(run_deadline, "run_deadline")
            return Reservation(row[0], row[1], row[2], row[3], row[4], row[5], run_deadline,
                               document["api_cost_estimate_cents"], tuple(document["request"]["scopes"]), overdue)
        except (KeyError, TypeError, ValueError):
            raise InvalidDatabase("A stored reservation violates its contract.") from None

    def decisions(self, limit: int = 100, after_seq: int | None = None, *,
                  newest_first: bool = False) -> tuple[dict, ...]:
        """Decisions in seq order after after_seq, or the newest first; each dict carries its seq."""
        check_count(limit, "limit", maximum=1000, optional=False)
        check_count(after_seq, "after_seq", minimum=0)
        if type(newest_first) is not bool:
            raise BudgetError("INVALID_NEWEST_FIRST")
        if newest_first and after_seq is not None:
            raise BudgetError("INVALID_DECISION_QUERY")
        with self._transaction() as connection:
            if newest_first:
                rows = connection.execute("SELECT seq, document FROM decisions ORDER BY seq DESC LIMIT ?",
                                          (limit,)).fetchall()
            else:
                rows = connection.execute("SELECT seq, document FROM decisions WHERE seq > ? ORDER BY seq LIMIT ?",
                                          (0 if after_seq is None else after_seq, limit)).fetchall()
        return tuple({**_load(document), "seq": seq} for seq, document in rows)

    # ---------------------------------------------------------------- status
    @staticmethod
    def _status(label: str, limits: _Limits | None, tally: _Tally, now: datetime) -> ScopeStatus:
        verdicts = [] if limits is None else _limit_verdicts(limits, tally, now, None)
        if limits is None:
            state = "unconfigured"
        elif limits.max_api_cost_cents is not None and tally.committed - tally.cost_reserved > limits.max_api_cost_cents:
            state = "over_limit"
        elif any(severity == "reject" for severity, _, _ in verdicts):
            state = "limit_reached"
        else:
            state = "open"
        return ScopeStatus(
            scope_id=label,
            configured=limits is not None,
            calls=tally.calls,
            active=tally.active,
            api_cost=CostTotals(tally.cost["confirmed"], tally.cost["estimated"], tally.cost_reserved,
                                tally.cost_unknown_held, tally.cost_unknown_records,
                                tally.cost_unknown_unheld, tally.cost_unheld_active),
            tokens=TokenTotals(tally.tokens_observed, tally.token_unknown_records),
            quota_used=tuple(QuotaUsage(unit, values[0], values[1]) for unit, values in sorted(tally.quota.items())),
            quota_unreported_records=tally.quota_unreported,
            state=state,
            reasons=tuple(code for _, code, _ in verdicts),
            observed_at=format_timestamp(now),
        )

    def scope_status(self, scope_id: str) -> ScopeStatus:
        check_id(scope_id, "scope_id")
        with self._transaction() as connection:
            now = self._now()
            stored = self._fetch_policy(connection, scope_id)
            tally = _tally(connection, self._rows_for_scope(connection, scope_id))
        return self._status(scope_id, None if stored is None else _policy_limits(stored.policy), tally, now)

    def profile_status(self, profile) -> ScopeStatus:
        profile = _profile(profile)
        limits, problems = _profile_limits(profile)
        with self._transaction() as connection:
            now = self._now()
            tally = _tally(connection, self._profile_rows(connection, profile.id))
        status = self._status("profile:" + profile.id, limits, tally, now)
        if problems:
            return ScopeStatus(**{**{name: getattr(status, name) for name in ScopeStatus.__slots__},
                                  "state": "limit_reached",
                                  "reasons": status.reasons + tuple(code for _, code, _ in problems)})
        return status

    def pool_status(self, pool_id: str) -> PoolStatus:
        check_id(pool_id, "pool_id")
        with self._transaction() as connection:
            now = self._now()
            active = connection.execute(
                "SELECT COUNT(*) FROM reservations WHERE pool_id = ? AND state = 'reserved'", (pool_id,),
            ).fetchone()[0]
            limited, retry_at = self._rate_limit(connection, pool_id, now)
            quota = self._latest_quota(connection, pool_id, now)
        return PoolStatus(pool_id, active, limited, retry_at if limited else None,
                          tuple(quota[unit] for unit in sorted(quota)), format_timestamp(now))
