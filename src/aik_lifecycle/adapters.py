"""Effect port adapter over the hosted-runtime binding store and the effect journal.

Only public methods are used: ExecutionBindingStore.inflight_effects(),
unknown_attempts, unknown_threads, route_for_thread() and
AikEffectJournal.read_effect(). Uncertain attribution is reported as
unresolved (fail-closed).
"""

from __future__ import annotations

from .errors import LifecycleError


_OPEN_EFFECT_STATES = ("intent_recorded", "unknown")


class BindingEffectAdapter:
    """unresolved(vm_attempt_id) -> tuple of (code, subject) for in-flight/unknown effects.

    ``effect_ids(vm_attempt_id)`` (optional) yields the tool effect IDs known for
    the Attempt; each is checked in ``effect_journal``. An intent without a
    receipt (late receipt pending) or an ``unknown`` receipt is unresolved.
    """

    def __init__(self, binding_store, *, effect_journal=None, effect_ids=None):
        if not callable(getattr(binding_store, "inflight_effects", None)):
            raise LifecycleError("INVALID_BINDING_STORE")
        if (effect_journal is None) != (effect_ids is None):
            raise LifecycleError("INVALID_EFFECT_JOURNAL")
        if effect_journal is not None and (not callable(getattr(effect_journal, "read_effect", None))
                                           or not callable(effect_ids)):
            raise LifecycleError("INVALID_EFFECT_JOURNAL")
        self._binding = binding_store
        self._journal = effect_journal
        self._effect_ids = effect_ids

    def _thread_belongs(self, thread_id, vm_attempt_id: str) -> bool:
        route = self._binding.route_for_thread(thread_id)
        # No route means the owner cannot be proven: treat it as related.
        return route is None or route.vm_attempt_id in (None, vm_attempt_id)

    def unresolved(self, vm_attempt_id: str) -> tuple:
        items = []
        if vm_attempt_id in self._binding.unknown_attempts:
            items.append(("ATTEMPT_UNKNOWN", vm_attempt_id))
        for thread_id in sorted(self._binding.unknown_threads):
            if self._thread_belongs(thread_id, vm_attempt_id):
                items.append(("THREAD_UNKNOWN", thread_id))
        for row in self._binding.inflight_effects():
            attempt = row.get("vm_attempt_id")
            thread_id = row.get("thread_id")
            if attempt == vm_attempt_id or (attempt is None and (
                    thread_id is None or self._thread_belongs(thread_id, vm_attempt_id))):
                items.append(("EFFECT_IN_FLIGHT", str(row.get("id"))))
        if self._journal is not None:
            for effect_id in self._effect_ids(vm_attempt_id):
                record = self._journal.read_effect(effect_id)
                if record is None:
                    items.append(("EFFECT_NOT_JOURNALED", effect_id))
                elif record.get("state") in _OPEN_EFFECT_STATES:
                    items.append(("EFFECT_" + str(record.get("state")).upper(), effect_id))
        return tuple(items)
