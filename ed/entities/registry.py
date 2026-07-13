"""Operator-facing resource listing (SPEC §5.6, §11 "Lifecycle separation";
CLAUDE.md "Domain rules"; build order step 8).

"Virtual"/"system-generated" is recovered as a **property**, never a class
hierarchy (CLAUDE.md): the listing filters on `is_system_generated`, and
`remove_resource` refuses to touch anything flagged `True` — an operator
must never be able to delete the slack that keeps the solve feasible.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class _Listable(Protocol):
    is_system_generated: bool


class SystemGeneratedResourceError(ValueError):
    """Attempted to remove a system-generated (engine-created) resource."""


def user_editable_resources[T: _Listable](entities: Sequence[T]) -> list[T]:
    """The operator-facing resource list: excludes anything
    `is_system_generated` (e.g. `Slack`); a `TieLine` (`is_system_generated
    =False`) does appear."""
    return [e for e in entities if not e.is_system_generated]


def remove_resource[T: _Listable](entities: list[T], target: T) -> None:
    """Remove `target` from `entities`; raises rather than removing anything
    `is_system_generated`."""
    if target.is_system_generated:
        raise SystemGeneratedResourceError(
            f"{target!r} is system-generated and cannot be deleted by an operator"
        )
    entities.remove(target)
