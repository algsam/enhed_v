"""The partition assertion (SPEC §5.1), as a standalone, testable function.

Invariant: the set of DispatchableEntitys induces a partition of the *online*
PhysicalUnits. Every online physical unit is dispatched by exactly one entity
— itself, or its block. Offline units belong to none.

Decoupled from the concrete DispatchableEntity/PhysicalUnit classes so it can
be tested (and called from entities/build_entities, a later build-order step)
without constructing full model objects: entities are described by the set of
physical unit ids they dispatch.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


class PartitionError(ValueError):
    """Raised when DispatchableEntitys do not exactly partition the online units."""


def assert_partition(
    online_unit_ids: Iterable[str],
    entity_members: Mapping[str, frozenset[str]],
) -> None:
    """Assert entity_members partitions online_unit_ids.

    entity_members maps entity_id -> the set of physical unit ids it
    dispatches (a singleton set for a standalone unit, multiple for a CC
    block).

    Raises PartitionError if:
    - any unit is claimed by more than one entity (member sets not disjoint), or
    - the union of all members does not exactly equal the online set (a unit
      is missing, or an offline/unknown unit is claimed).
    """
    online = frozenset(online_unit_ids)
    claimed_by: dict[str, str] = {}
    for entity_id, members in entity_members.items():
        for unit_id in members:
            prior = claimed_by.get(unit_id)
            if prior is not None:
                raise PartitionError(
                    f"unit {unit_id!r} is claimed by both entity {prior!r} and "
                    f"entity {entity_id!r} — partition invariant violated"
                )
            claimed_by[unit_id] = entity_id

    covered = frozenset(claimed_by.keys())
    if covered != online:
        missing = online - covered
        extra = covered - online
        parts = []
        if missing:
            parts.append(f"online units dispatched by no entity: {sorted(missing)}")
        if extra:
            parts.append(f"entities claim units not in the online set: {sorted(extra)}")
        raise PartitionError("; ".join(parts))
