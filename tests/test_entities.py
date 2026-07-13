"""Tests for `build_entities()` (SPEC §5.2, §13 build order step 6; CLAUDE.md
"Domain rules").
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ed.curves import from_incremental
from ed.disagg import RangeProRata
from ed.domain.cc_block import CCBlock, CCBlockConfig, CCBlockRoster
from ed.domain.enums import Mode, ResourceType, SteamSource
from ed.domain.generator import Generator
from ed.domain.partition import PartitionError
from ed.domain.physical_unit import PhysicalUnit, UnitCharacteristics
from ed.domain.ramp import RampRateCurve
from ed.entities import BlockConfigError, build_entities


def _simple_cycle_ct(unit_id: str, hrv: bool, *, online: bool = True) -> PhysicalUnit:
    curve = from_incremental(breakpoints_mw=(20.0, 100.0), ic_values=(15.0, 25.0))
    chars = UnitCharacteristics(
        pmin_mw=20.0,
        pmax_mw=100.0,
        ramp_up=RampRateCurve.constant(5.0, 20.0),
        ramp_down=RampRateCurve.constant(5.0, 20.0),
        cost_curve=curve,
    )
    return PhysicalUnit(
        unit_id=unit_id,
        bus="bus1",
        resource_type=ResourceType.THERMAL,
        characteristics={Mode.SIMPLE_CYCLE: chars},
        active_mode=Mode.SIMPLE_CYCLE,
        p0_mw=50.0,
        online=online,
        hrv=hrv,
    )


def _cc_member_ct(unit_id: str, hrv: bool, *, online: bool = True) -> PhysicalUnit:
    chars = UnitCharacteristics(
        pmin_mw=20.0,
        pmax_mw=100.0,
        ramp_up=RampRateCurve.constant(5.0, 20.0),
        ramp_down=RampRateCurve.constant(5.0, 20.0),
    )
    return PhysicalUnit(
        unit_id=unit_id,
        bus="bus1",
        resource_type=ResourceType.THERMAL,
        characteristics={Mode.COMBINED_CYCLE: chars},
        active_mode=Mode.COMBINED_CYCLE,
        p0_mw=60.0,
        online=online,
        hrv=hrv,
    )


def _cc_member_st(
    unit_id: str,
    steam_source: SteamSource,
    *,
    online: bool = True,
) -> PhysicalUnit:
    chars = UnitCharacteristics(
        pmin_mw=30.0,
        pmax_mw=80.0,
        ramp_up=RampRateCurve.constant(1.0, 30.0),
        ramp_down=RampRateCurve.constant(1.0, 30.0),
    )
    return PhysicalUnit(
        unit_id=unit_id,
        bus="bus1",
        resource_type=ResourceType.STEAM,
        characteristics={Mode.COMBINED_CYCLE: chars},
        active_mode=Mode.COMBINED_CYCLE,
        p0_mw=40.0,
        online=online,
        steam_source=steam_source,
    )


def _cc2_st1_roster() -> CCBlockRoster:
    """Roster `{CT2, ST1}` with a single legal config: both engaged."""
    agg_curve = from_incremental(breakpoints_mw=(50.0, 150.0), ic_values=(10.0, 30.0))
    config = CCBlockConfig(
        config_id="CT2_ST1", active_members=frozenset({"CT2", "ST1"}), cost_curve=agg_curve
    )
    return CCBlockRoster(
        block_id="BLOCK1",
        bus="bus1",
        member_unit_ids=frozenset({"CT2", "ST1"}),
        configs=(config,),
    )


# --- the mandatory CT1-simple-cycle case (SPEC §5.2, §11) ---


def test_ct1_simple_cycle_case_produces_two_entities_no_phantom_no_double_count() -> None:
    """Roster {CT1, CT2, ST1}; CT1.hrv=False, CT2.hrv=True, ST1 on HRSG ->
    exactly two entities (standalone CT1, and the CT2+ST1 block), both
    dispatched, no phantom generation, no double-count, partition passes.
    """
    ct1 = _simple_cycle_ct("CT1", hrv=False)
    ct2 = _cc_member_ct("CT2", hrv=True)
    st1 = _cc_member_st("ST1", SteamSource.HRSG)
    roster = _cc2_st1_roster()

    result = build_entities([ct1, ct2, st1], [roster])

    assert len(result.entities) == 2
    assert result.entity_members == {
        "CT1": frozenset({"CT1"}),
        "BLOCK1": frozenset({"CT2", "ST1"}),
    }
    assert result.active_configs == {"BLOCK1": "CT2_ST1"}

    standalone = next(e for e in result.entities if isinstance(e, Generator))
    block = next(e for e in result.entities if isinstance(e, CCBlock))
    assert standalone.bus == "bus1"
    assert block.block_id == "BLOCK1"
    assert block.config_id == "CT2_ST1"
    assert {u.unit_id for u in block.member_units} == {"CT2", "ST1"}

    # no double-count: CT2/ST1 do not also appear as their own standalone
    # entities, and CT1 is not swept into the block.
    assert not any(
        isinstance(e, Generator) and e is not standalone for e in result.entities
    )


def test_offline_roster_member_is_excluded_from_both_block_and_standalone() -> None:
    """A block member that is offline belongs to neither the block nor a
    standalone entity, and is absent from the online partition."""
    ct2 = _cc_member_ct("CT2", hrv=True, online=False)
    st1 = _cc_member_st("ST1", SteamSource.NONE, online=False)
    roster = _cc2_st1_roster()

    result = build_entities([ct2, st1], [roster])

    assert result.entities == []
    assert result.entity_members == {}


# --- ST with steam_source=NONE and online=True raises (SPEC §11) ---


def test_online_steam_turbine_with_steam_source_none_raises() -> None:
    """This is enforced at `PhysicalUnit` construction itself (SPEC §5.4) —
    an invalid unit can never even be handed to `build_entities`."""
    with pytest.raises(ValidationError, match="steam_source"):
        _cc_member_st("ST1", SteamSource.NONE, online=True)


# --- HRV vector inconsistent with the declared active config: surfaced ---


def test_inconsistent_engaged_set_raises_block_config_error() -> None:
    """CT2 signals engagement (hrv=True) but its steam partner is offline,
    so the engaged set {"CT2"} matches no declared legal configuration —
    this must be surfaced, not silently dispatched under the nearest config.
    """
    ct2 = _cc_member_ct("CT2", hrv=True)
    st1 = _cc_member_st("ST1", SteamSource.NONE, online=False)
    roster = _cc2_st1_roster()

    with pytest.raises(BlockConfigError, match="inconsistent"):
        build_entities([ct2, st1], [roster])


# --- AUX_BOILER is present in the enum but unreachable (SPEC §5.4) ---


def test_aux_boiler_steam_turbine_is_unreachable() -> None:
    st1 = _cc_member_st("ST1", SteamSource.AUX_BOILER, online=True)

    with pytest.raises(NotImplementedError, match="AUX_BOILER"):
        build_entities([st1], [])


# --- partition assertion runs every call (SPEC §5.1) ---


def test_build_entities_asserts_the_partition_invariant() -> None:
    """A block roster claiming a unit id that is not passed in `units` is
    exactly the kind of configuration error the partition assertion (and
    this function's own pre-check) exists to catch."""
    ct1 = _simple_cycle_ct("CT1", hrv=False)
    agg_curve = from_incremental(breakpoints_mw=(0.0, 50.0), ic_values=(10.0, 10.0))
    config = CCBlockConfig(
        config_id="GHOST", active_members=frozenset({"GHOST_UNIT"}), cost_curve=agg_curve
    )
    roster = CCBlockRoster(
        block_id="BLOCK_GHOST",
        bus="bus1",
        member_unit_ids=frozenset({"GHOST_UNIT"}),
        configs=(config,),
    )

    with pytest.raises(BlockConfigError, match="unknown member unit"):
        build_entities([ct1], [roster])


def test_disaggregator_is_shared_across_cc_block_entities() -> None:
    ct2 = _cc_member_ct("CT2", hrv=True)
    st1 = _cc_member_st("ST1", SteamSource.HRSG)
    roster = _cc2_st1_roster()
    strategy = RangeProRata()

    result = build_entities([ct2, st1], [roster], disaggregator=strategy)

    block = next(e for e in result.entities if isinstance(e, CCBlock))
    assert block.disaggregator is strategy
