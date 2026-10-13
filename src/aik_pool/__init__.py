"""AIK-019 ResourcePool registry, health probes and TTL-based freshness.

Advertised values are probe observations. Adapter implementation, a fresh
probe and live qualification are separate levels. Only externally verified
live qualification can make a route eligible for automatic placement.
"""

from .registry import (
    Freshness, Governance, PoolRegistryError, QualificationEvidence,
    QualificationLevel, QualificationRejected, ResourcePoolRegistry,
    RouteStatus, UnknownRoute,
)

__all__ = [
    "Freshness", "Governance", "PoolRegistryError", "QualificationEvidence",
    "QualificationLevel", "QualificationRejected", "ResourcePoolRegistry",
    "RouteStatus", "UnknownRoute",
]
