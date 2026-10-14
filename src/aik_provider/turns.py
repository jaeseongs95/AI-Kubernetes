"""AIK-093: vendor-neutral prompt-cache, opaque block and usage types.

Common code may ask for a cache, carry provider blocks it must not interpret,
and account usage by component. Every provider-specific word (cache_control,
TTL strings, thinking block shapes, usage field names) lives in an adapter.

- ``CachePolicy`` only says how long a stable prompt prefix is worth keeping.
- ``OpaqueBlock`` is a provider block kept byte for byte and sent back, at its
  original position, only to the provider that produced it.
- ``UsageBreakdown`` splits usage into components. A component the provider did
  not report is unknown (``None``), never 0, and each value names its origin.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import re

_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_KIND = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
MAX_OPAQUE_BYTES = 1024 * 1024


class CachePolicy(StrEnum):
    """How long the stable prefix (instructions, tools, earlier turns) should stay cached."""

    NONE = "none"
    SHORT = "short"            # minutes: consecutive turns of one task
    LONG = "long"              # about an hour: a task with long pauses between turns
    PROVIDER_DEFAULT = "provider_default"  # whatever the provider does on its own


class CacheMode(StrEnum):
    EXPLICIT = "explicit"                  # the caller marks cache boundaries
    AUTOMATIC_PREFIX = "automatic_prefix"  # the provider caches matching prefixes by itself
    NONE = "none"


@dataclass(frozen=True, slots=True)
class PromptCacheCapability:
    """What an adapter discloses about prompt caching. Not a measured hit rate."""

    mode: CacheMode
    policies: frozenset = frozenset()
    max_breakpoints: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, CacheMode):
            raise ValueError("INVALID_CACHE_MODE")
        if not isinstance(self.policies, frozenset) or not all(isinstance(p, CachePolicy) for p in self.policies):
            raise ValueError("INVALID_CACHE_POLICIES")
        if self.max_breakpoints is not None and (type(self.max_breakpoints) is not int or self.max_breakpoints < 1):
            raise ValueError("INVALID_MAX_BREAKPOINTS")
        if self.mode is CacheMode.NONE and self.policies - {CachePolicy.NONE}:
            raise ValueError("CACHE_POLICIES_WITHOUT_CACHE")

    def supports(self, policy: CachePolicy) -> bool:
        return policy is CachePolicy.NONE or policy in self.policies


@dataclass(frozen=True, slots=True)
class OpaqueBlock:
    """A provider response block that AIK keeps but never interprets.

    ``raw_json`` is the block exactly as the provider sent it (one JSON object).
    ``position`` is its index among the response's content blocks, so the
    adapter can rebuild the original order when sending it back.
    """

    provider: str
    kind: str
    position: int
    raw_json: str

    def __post_init__(self) -> None:
        if type(self.provider) is not str or _PROVIDER.fullmatch(self.provider) is None:
            raise ValueError("INVALID_OPAQUE_PROVIDER")
        if type(self.kind) is not str or _KIND.fullmatch(self.kind) is None:
            raise ValueError("INVALID_OPAQUE_KIND")
        if type(self.position) is not int or not 0 <= self.position < 4096:
            raise ValueError("INVALID_OPAQUE_POSITION")
        if type(self.raw_json) is not str or len(self.raw_json.encode("utf-8")) > MAX_OPAQUE_BYTES:
            raise ValueError("INVALID_OPAQUE_RAW")
        try:
            value = json.loads(self.raw_json)
        except ValueError:
            raise ValueError("INVALID_OPAQUE_RAW") from None
        if not isinstance(value, dict):
            raise ValueError("INVALID_OPAQUE_RAW")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.raw_json.encode("utf-8")).hexdigest()

    def to_document(self) -> dict:
        return {"provider": self.provider, "kind": self.kind, "position": self.position, "raw_json": self.raw_json}

    @classmethod
    def from_document(cls, value: object) -> "OpaqueBlock":
        if not isinstance(value, dict) or set(value) != {"provider", "kind", "position", "raw_json"}:
            raise ValueError("INVALID_OPAQUE_BLOCK")
        return cls(value["provider"], value["kind"], value["position"], value["raw_json"])

    def for_provider(self, provider: str) -> "OpaqueBlock":
        """Return self when it may be sent to ``provider``; refuse a cross-provider send."""
        if provider != self.provider:
            raise ValueError("OPAQUE_BLOCK_PROVIDER_MISMATCH")
        return self


class Provenance(StrEnum):
    PROVIDER_OBSERVED = "provider_observed"  # a number the provider reported
    LOCAL_DERIVED = "local_derived"          # computed by AIK from reported numbers


@dataclass(frozen=True, slots=True)
class Measured:
    """One usage component. ``value is None`` means unknown, not zero."""

    value: int | None = None
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        if self.value is None:
            if self.provenance is not None:
                raise ValueError("UNKNOWN_VALUE_HAS_NO_PROVENANCE")
            return
        if type(self.value) is not int or self.value < 0:
            raise ValueError("INVALID_USAGE_VALUE")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("USAGE_VALUE_NEEDS_PROVENANCE")

    @property
    def known(self) -> bool:
        return self.value is not None

    @classmethod
    def observed(cls, value: int | None) -> "Measured":
        return cls() if value is None else cls(value, Provenance.PROVIDER_OBSERVED)

    @classmethod
    def derived(cls, value: int | None) -> "Measured":
        return cls() if value is None else cls(value, Provenance.LOCAL_DERIVED)

    def to_document(self) -> dict:
        return {"value": self.value, "provenance": None if self.provenance is None else self.provenance.value}


UNKNOWN = Measured()
_COMPONENTS = ("input_uncached", "cache_write_short", "cache_write_long", "cache_write_unspecified",
               "cache_read", "output", "reasoning")


@dataclass(frozen=True, slots=True)
class UsageBreakdown:
    """Token usage of one model turn, split by component.

    - ``input_uncached``: input billed at the normal rate.
    - ``cache_write_*``: input written to a cache, by retention class when known.
    - ``cache_read``: input served from a cache.
    - ``output``: all generated tokens, reasoning included.
    - ``reasoning``: the part of ``output`` spent on reasoning. It is already
      inside ``output``; never add the two.
    """

    input_uncached: Measured = field(default=UNKNOWN)
    cache_write_short: Measured = field(default=UNKNOWN)
    cache_write_long: Measured = field(default=UNKNOWN)
    cache_write_unspecified: Measured = field(default=UNKNOWN)
    cache_read: Measured = field(default=UNKNOWN)
    output: Measured = field(default=UNKNOWN)
    reasoning: Measured = field(default=UNKNOWN)

    def __post_init__(self) -> None:
        for name in _COMPONENTS:
            if not isinstance(getattr(self, name), Measured):
                raise ValueError("INVALID_USAGE_COMPONENT")
        if self.reasoning.known and self.output.known and self.reasoning.value > self.output.value:
            raise ValueError("REASONING_EXCEEDS_OUTPUT")

    def cache_write(self) -> int | None:
        """All cache writes; None when no write component is known."""
        parts = [self.cache_write_short, self.cache_write_long, self.cache_write_unspecified]
        known = [part.value for part in parts if part.known]
        return sum(known) if known else None

    def total_input(self) -> int | None:
        """Every input token of the turn; None if a component is unknown."""
        write = self.cache_write()
        if not self.input_uncached.known or not self.cache_read.known or write is None:
            return None
        return self.input_uncached.value + self.cache_read.value + write

    def to_document(self) -> dict:
        return {name: getattr(self, name).to_document() for name in _COMPONENTS}


__all__ = ["CachePolicy", "CacheMode", "PromptCacheCapability", "OpaqueBlock", "Provenance", "Measured",
           "UNKNOWN", "UsageBreakdown", "MAX_OPAQUE_BYTES"]
