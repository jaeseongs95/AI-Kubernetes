"""AIK-020 per-resource model, effort and tool capability inventory and validator.

Model names are data supplied per connection (resource). They are never fixed in
product code. Every inventory entry carries a mandatory provenance. Requested
values are never promoted to observed capability.
"""

from .inventory import (
    CapabilityError, CapabilityInventory, InventorySnapshot, ModelCapability,
    Provenance, UnknownResource, ValidationResult,
)

__all__ = [
    "CapabilityError", "CapabilityInventory", "InventorySnapshot", "ModelCapability",
    "Provenance", "UnknownResource", "ValidationResult",
]
