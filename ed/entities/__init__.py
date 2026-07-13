"""`build_entities()` — config resolution and the partition assertion (SPEC
§13 build order step 6)."""

from __future__ import annotations

from ed.entities.build import BlockConfigError, BuildEntitiesResult, build_entities
from ed.entities.registry import (
    SystemGeneratedResourceError,
    remove_resource,
    user_editable_resources,
)

__all__ = [
    "BlockConfigError",
    "BuildEntitiesResult",
    "SystemGeneratedResourceError",
    "build_entities",
    "remove_resource",
    "user_editable_resources",
]
