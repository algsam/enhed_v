"""Tests for PhysicalUnit and its mode-keyed characteristics (SPEC §5.1-5.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ed.domain.enums import Mode, ResourceType, SteamSource
from ed.domain.physical_unit import PhysicalUnit, UnitCharacteristics
from ed.domain.ramp import RampRateCurve


def _characteristics(pmin_mw: float, pmax_mw: float, ramp_mw_per_min: float) -> UnitCharacteristics:
    return UnitCharacteristics(
        pmin_mw=pmin_mw,
        pmax_mw=pmax_mw,
        ramp_up=RampRateCurve.constant(ramp_mw_per_min, pmin_mw),
        ramp_down=RampRateCurve.constant(ramp_mw_per_min, pmin_mw),
    )


def test_mode_keyed_characteristics_differ_by_mode() -> None:
    ct1 = PhysicalUnit(
        unit_id="CT1",
        bus="BUS1",
        resource_type=ResourceType.THERMAL,
        characteristics={
            Mode.SIMPLE_CYCLE: _characteristics(20, 100, 5),
            Mode.COMBINED_CYCLE: _characteristics(30, 120, 3),
        },
        active_mode=Mode.SIMPLE_CYCLE,
        p0_mw=50,
        online=True,
        hrv=False,
    )
    assert ct1.active_characteristics.pmax_mw == 100
    assert ct1.active_characteristics.ramp_up.rate_at_mw(50) == 5


def test_active_mode_must_have_characteristics_entry() -> None:
    with pytest.raises(ValidationError, match="active_mode"):
        PhysicalUnit(
            unit_id="CT1",
            bus="BUS1",
            resource_type=ResourceType.THERMAL,
            characteristics={Mode.SIMPLE_CYCLE: _characteristics(20, 100, 5)},
            active_mode=Mode.COMBINED_CYCLE,
            p0_mw=50,
            online=True,
        )


def test_online_steam_turbine_with_no_steam_source_is_invalid() -> None:
    with pytest.raises(ValidationError, match="steam_source"):
        PhysicalUnit(
            unit_id="ST1",
            bus="BUS1",
            resource_type=ResourceType.STEAM,
            characteristics={Mode.COMBINED_CYCLE: _characteristics(10, 80, 2)},
            active_mode=Mode.COMBINED_CYCLE,
            p0_mw=40,
            online=True,
            steam_source=SteamSource.NONE,
        )


def test_offline_steam_turbine_with_no_steam_source_is_valid() -> None:
    st1 = PhysicalUnit(
        unit_id="ST1",
        bus="BUS1",
        resource_type=ResourceType.STEAM,
        characteristics={Mode.COMBINED_CYCLE: _characteristics(10, 80, 2)},
        active_mode=Mode.COMBINED_CYCLE,
        p0_mw=0,
        online=False,
        steam_source=SteamSource.NONE,
    )
    assert not st1.emits_setpoint


def test_hrsg_steam_turbine_is_valid() -> None:
    st1 = PhysicalUnit(
        unit_id="ST1",
        bus="BUS1",
        resource_type=ResourceType.STEAM,
        characteristics={Mode.COMBINED_CYCLE: _characteristics(10, 80, 2)},
        active_mode=Mode.COMBINED_CYCLE,
        p0_mw=40,
        online=True,
        steam_source=SteamSource.HRSG,
    )
    assert st1.emits_setpoint


def test_physical_unit_is_never_system_generated() -> None:
    ct1 = PhysicalUnit(
        unit_id="CT1",
        bus="BUS1",
        resource_type=ResourceType.THERMAL,
        characteristics={Mode.SIMPLE_CYCLE: _characteristics(20, 100, 5)},
        active_mode=Mode.SIMPLE_CYCLE,
        p0_mw=50,
        online=True,
    )
    assert ct1.is_system_generated is False


def test_offline_unit_does_not_emit_a_setpoint() -> None:
    ct1 = PhysicalUnit(
        unit_id="CT1",
        bus="BUS1",
        resource_type=ResourceType.THERMAL,
        characteristics={Mode.SIMPLE_CYCLE: _characteristics(20, 100, 5)},
        active_mode=Mode.SIMPLE_CYCLE,
        p0_mw=0,
        online=False,
    )
    assert ct1.emits_setpoint is False


def test_pmax_below_pmin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="pmax_mw"):
        _characteristics(pmin_mw=50, pmax_mw=10, ramp_mw_per_min=5)
