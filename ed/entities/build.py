"""`build_entities(units, blocks)` — config resolution from telemetry, the
partition assertion, and standalone-entity construction (SPEC §5.1, §5.2,
§13 build order step 6).

This is a **validator**, not a constraint generator (SPEC §5.2): v1 has no
commitment decision, so a block's active configuration is derived from its
members' `(online, hrv, steam_source)` vector and checked against the
block's enumerated legal configurations, once, before the solve — never
encoded as constraints the optimizer could choose to violate.

Resolution, per block roster:

1. Among the roster's *online* members, the *engaged* subset is derived
   from telemetry (SPEC §5.2): a thermal unit is engaged iff `hrv=True`; a
   steam turbine is engaged iff `steam_source=HRSG`. This is exactly the
   ground-truth vector SPEC §5.2 says must drive resolution, never a stored
   config flag.
2. An empty engaged set means no active configuration this cycle — every
   online roster member runs standalone (the all-simple-cycle case).
3. A non-empty engaged set must exactly match one declared
   `CCBlockConfig.active_members`. No match is an inconsistent
   HRV/steam_source vector (SPEC §5.2's "surface it, do not dispatch"
   case) and raises `BlockConfigError` rather than guessing.
4. On a match, the engaged members become one `CCBlock` entity; every
   *other* online roster member (e.g. a CT with its damper closed) runs
   standalone — this is exactly the CT1-simple-cycle case (SPEC §5.2,
   §11): roster `{CT1, CT2, ST1}` with `CT1.hrv=False` yields two entities,
   standalone CT1 and the `CT2+ST1` block.

Every online unit outside any block roster is dispatched standalone
directly. The resulting entity set is asserted to partition the online
units (SPEC §5.1) before being returned — this is the module's own
correctness gate, run every call, not just in tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ed.disagg import Disaggregator, RangeProRata
from ed.domain.cc_block import CCBlock, CCBlockConfig, CCBlockRoster
from ed.domain.entity import DispatchableEntity
from ed.domain.enums import Mode, ResourceType, SteamSource
from ed.domain.generator import Generator
from ed.domain.partition import assert_partition
from ed.domain.physical_unit import PhysicalUnit


class BlockConfigError(ValueError):
    """A CC block roster references an unknown unit, a unit claimed by two
    rosters, or the members' telemetry does not resolve to any declared
    legal configuration (SPEC §5.2: surfaced, never silently dispatched).
    """


@dataclass
class BuildEntitiesResult:
    """The resolved, partition-checked entity set for one dispatch cycle."""

    entities: list[DispatchableEntity]
    entity_members: dict[str, frozenset[str]] = field(default_factory=dict)
    active_configs: dict[str, str] = field(default_factory=dict)
    """block_id -> resolved config_id, for audit logging (CLAUDE.md
    "the active selection is part of the saved case and is recorded in
    every result")."""


def _is_engaged_in_block(unit: PhysicalUnit) -> bool:
    """Telemetry-derived participation in an active CC config (SPEC §5.2):
    a steam turbine is engaged iff it draws HRSG steam; any other roster
    member (a CT) is engaged iff its diverter (`hrv`) is open.
    """
    if unit.resource_type is ResourceType.STEAM:
        return unit.steam_source is SteamSource.HRSG
    return unit.hrv


def _standalone_entity(unit: PhysicalUnit) -> Generator:
    """The simple-cycle `DispatchableEntity` for a unit running on its own
    (SPEC §5.3: `characteristics[SIMPLE_CYCLE]` is used "when it is a
    standalone entity").

    `AUX_BOILER` is present in `SteamSource` but deliberately unreachable in
    v1 (SPEC §5.4): a steam turbine firing its own boiler would need its own
    standalone entity with its own fuel cost, which v1 does not build.
    """
    if unit.resource_type is ResourceType.STEAM and unit.steam_source is SteamSource.AUX_BOILER:
        raise NotImplementedError(
            f"unit {unit.unit_id!r}: AUX_BOILER-sourced steam turbines are not dispatchable "
            "in v1 (SPEC §5.4: AUX_BOILER is present in SteamSource but unreachable)"
        )
    mode = Mode.SIMPLE_CYCLE if Mode.SIMPLE_CYCLE in unit.characteristics else unit.active_mode
    chars = unit.characteristics.get(mode)
    if chars is None or chars.cost_curve is None:
        raise BlockConfigError(
            f"unit {unit.unit_id!r}: no cost curve available to dispatch it standalone "
            f"(mode {mode})"
        )
    return Generator(bus=unit.bus, cost_curve=chars.cost_curve)


def _validate_aggregate_limits(
    block_id: str, config: CCBlockConfig, engaged: Sequence[PhysicalUnit]
) -> None:
    """CLAUDE.md "Domain rules": `sum(Pmin_units) <= x_0` and `x_n <=
    sum(Pmax_units)`, checked against the *engaged* members' own
    `COMBINED_CYCLE` limits, or the disaggregator could be handed a base
    point no split of the members can actually deliver.
    """
    sum_pmin = sum(u.characteristics[Mode.COMBINED_CYCLE].pmin_mw for u in engaged)
    sum_pmax = sum(u.characteristics[Mode.COMBINED_CYCLE].pmax_mw for u in engaged)
    if sum_pmin > config.pmin_mw + 1e-9:
        raise BlockConfigError(
            f"block {block_id!r} config {config.config_id!r}: sum(Pmin_units)={sum_pmin} "
            f"exceeds the config's own Pmin={config.pmin_mw} — the disaggregator could be "
            "handed a base point below what its members can jointly reach"
        )
    if config.pmax_mw > sum_pmax + 1e-9:
        raise BlockConfigError(
            f"block {block_id!r} config {config.config_id!r}: the config's own "
            f"Pmax={config.pmax_mw} exceeds sum(Pmax_units)={sum_pmax} — the disaggregator "
            "could be handed a base point above what its members can jointly deliver"
        )


def build_entities(
    units: Sequence[PhysicalUnit],
    blocks: Sequence[CCBlockRoster],
    disaggregator: Disaggregator = RangeProRata(),
) -> BuildEntitiesResult:
    """Resolve `units`/`blocks` telemetry into the entity set the
    `ModelBuilder` will iterate this cycle (SPEC §13 build order step 6).

    `disaggregator` is shared by every `CCBlock` entity this call produces
    — it is a per-cycle config choice (CLAUDE.md "every user choice... is a
    strategy swap"), not per-block state.
    """
    units_by_id = {u.unit_id: u for u in units}

    claimed_by_roster: dict[str, str] = {}
    for roster in blocks:
        for unit_id in roster.member_unit_ids:
            if unit_id not in units_by_id:
                raise BlockConfigError(
                    f"block {roster.block_id!r}: unknown member unit {unit_id!r}"
                )
            prior = claimed_by_roster.get(unit_id)
            if prior is not None:
                raise BlockConfigError(
                    f"unit {unit_id!r} is a member of both block {prior!r} and "
                    f"{roster.block_id!r} — a unit may belong to at most one CC block"
                )
            claimed_by_roster[unit_id] = roster.block_id

    entities: list[DispatchableEntity] = []
    entity_members: dict[str, frozenset[str]] = {}
    active_configs: dict[str, str] = {}

    for roster in blocks:
        member_units = [units_by_id[uid] for uid in sorted(roster.member_unit_ids)]
        online_members = [u for u in member_units if u.online]
        engaged = [u for u in online_members if _is_engaged_in_block(u)]
        engaged_ids = frozenset(u.unit_id for u in engaged)

        if engaged_ids:
            config = roster.config_for(engaged_ids)
            if config is None:
                legal = [sorted(c.active_members) for c in roster.configs]
                raise BlockConfigError(
                    f"block {roster.block_id!r}: engaged members {sorted(engaged_ids)} "
                    f"(from online/hrv/steam_source telemetry) match none of the declared "
                    f"legal configurations {legal} — HRV vector is inconsistent with every "
                    "declared config"
                )
            _validate_aggregate_limits(roster.block_id, config, engaged)
            entities.append(
                CCBlock(
                    block_id=roster.block_id,
                    bus=roster.bus,
                    cost_curve=config.cost_curve,
                    config_id=config.config_id,
                    member_units=engaged,
                    disaggregator=disaggregator,
                )
            )
            entity_members[roster.block_id] = engaged_ids
            active_configs[roster.block_id] = config.config_id

        for unit in online_members:
            if unit.unit_id in engaged_ids:
                continue
            entities.append(_standalone_entity(unit))
            entity_members[unit.unit_id] = frozenset({unit.unit_id})

    rostered_unit_ids = frozenset(claimed_by_roster)
    for unit in units:
        if unit.unit_id in rostered_unit_ids or not unit.online:
            continue
        entities.append(_standalone_entity(unit))
        entity_members[unit.unit_id] = frozenset({unit.unit_id})

    online_ids = [u.unit_id for u in units if u.online]
    assert_partition(online_ids, entity_members)

    return BuildEntitiesResult(
        entities=entities, entity_members=entity_members, active_configs=active_configs
    )
