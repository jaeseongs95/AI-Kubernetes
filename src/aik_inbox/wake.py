"""Wake the director only when leasable work exists; never on an idle queue.

This module produces a wake signal through an injected callback. It does not
call any model itself. A signal is emitted at most once per queue state, so a
director that does not consume items is not woken repeatedly. min_interval
applies to every callback attempt, including attempts that raised.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading

from .errors import InboxError, InvalidMessage
from .store import InboxStore


@dataclass(frozen=True, slots=True)
class WakeSignal:
    available: int
    kinds: tuple


class WakePolicy:
    def __init__(self, store: InboxStore, wake, *, clock=None, min_interval: float = 0.0):
        if not isinstance(store, InboxStore):
            raise InvalidMessage("The wake policy requires an InboxStore.")
        if not callable(wake):
            raise InvalidMessage("The wake callback must be callable.")
        if clock is not None and not callable(clock):
            raise InvalidMessage("The clock must be callable.")
        if (isinstance(min_interval, bool) or not isinstance(min_interval, (int, float))
                or not math.isfinite(min_interval) or min_interval < 0):
            raise InvalidMessage("min_interval must be a finite nonnegative number.")
        self._store = store
        self._wake = wake
        self._clock = clock if clock is not None else store.clock
        self._min_interval = float(min_interval)
        self._last_key = None
        self._last_wake_at = None
        self._mutex = threading.Lock()

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise InboxError("The injected clock returned an invalid time.")
        return float(value)

    def poll(self) -> WakeSignal | None:
        """Return the emitted signal, or None when nothing new is leasable."""
        with self._mutex:
            snapshot = self._store.wake_snapshot()
            if snapshot.available == 0 or snapshot.key == self._last_key:
                return None
            now = self._now()
            if self._last_wake_at is not None and now - self._last_wake_at < self._min_interval:
                return None
            signal = WakeSignal(snapshot.available, snapshot.kinds)
            # Record the attempt before calling: a raising callback is retried
            # by a later poll, but no sooner than min_interval.
            self._last_wake_at = now
            self._wake(signal)
            self._last_key = snapshot.key
            return signal

    def reset(self) -> None:
        """Forget the last signalled state, e.g. after the director restarted."""
        with self._mutex:
            self._last_key = None
            self._last_wake_at = None
