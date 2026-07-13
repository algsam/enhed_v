"""Tests for the `Disaggregator` protocol + `RangeProRata`/`PmaxProRata`
(CLAUDE.md "Domain rules"; SPEC §6, §11 "Disaggregation & ramp — the
highest-risk area"; build order step 5).
"""

from __future__ import annotations

import pytest

from ed.disagg import (
    AggregateRamp,
    PmaxProRata,
    RangeProRata,
    SplitValidationError,
    validate_split_result,
)
from ed.domain.enums import Mode, ResourceType, SteamSource
from ed.domain.physical_unit import PhysicalUnit, UnitCharacteristics
from ed.domain.ramp import RampRateCurve


def _unit(
    unit_id: str,
    pmin: float,
    pmax: float,
    ramp_up: float,
    ramp_down: float,
    p0_mw: float = 0.0,
    resource_type: ResourceType = ResourceType.THERMAL,
    steam_source: SteamSource = SteamSource.NONE,
) -> PhysicalUnit:
    """A CC-block member, characterised for `Mode.COMBINED_CYCLE` (SPEC
    §5.3): its Pmin/Pmax/ramp are its contribution *within the block*, not
    its standalone simple-cycle characteristics.
    """
    chars = UnitCharacteristics(
        pmin_mw=pmin,
        pmax_mw=pmax,
        ramp_up=RampRateCurve.constant(ramp_up, pmin),
        ramp_down=RampRateCurve.constant(ramp_down, pmin),
    )
    return PhysicalUnit(
        unit_id=unit_id,
        bus="bus1",
        resource_type=resource_type,
        characteristics={Mode.COMBINED_CYCLE: chars},
        active_mode=Mode.COMBINED_CYCLE,
        p0_mw=p0_mw,
        online=True,
        hrv=True,
        steam_source=steam_source,
    )


# --- split(): sums exactly, respects every unit's [Pmin, Pmax] ---


def test_range_pro_rata_split_sums_to_entity_and_respects_bounds() -> None:
    ct = _unit("CT1", pmin=20.0, pmax=100.0, ramp_up=5.0, ramp_down=5.0)
    st = _unit(
        "ST1",
        pmin=30.0,
        pmax=80.0,
        ramp_up=1.0,
        ramp_down=1.0,
        resource_type=ResourceType.STEAM,
        steam_source=SteamSource.HRSG,
    )
    units = [ct, st]
    strategy = RangeProRata()
    pmin_e, pmax_e = strategy.aggregate_limits(units)

    for entity_mw in (pmin_e, 70.0, 90.0, 130.0, pmax_e):
        result = strategy.split(entity_mw, units)
        assert result.keys() == {"CT1", "ST1"}
        assert sum(result.values()) == pytest.approx(entity_mw)
        for unit in units:
            p_i = result[unit.unit_id]
            assert unit.active_characteristics.pmin_mw - 1e-9 <= p_i
            assert p_i <= unit.active_characteristics.pmax_mw + 1e-9
        validate_split_result(entity_mw, units, result)  # must not raise


def test_range_pro_rata_rejects_entity_mw_outside_aggregate_range() -> None:
    units = [_unit("CT1", pmin=20.0, pmax=100.0, ramp_up=5.0, ramp_down=5.0)]
    strategy = RangeProRata()
    with pytest.raises(SplitValidationError):
        strategy.split(500.0, units)


# --- Pmax pro-rata violates Pmin at low block output; range-based does not ---


def test_pmax_pro_rata_violates_pmin_range_based_does_not() -> None:
    unit_a = _unit("A", pmin=0.0, pmax=100.0, ramp_up=10.0, ramp_down=10.0)
    unit_b = _unit("B", pmin=40.0, pmax=50.0, ramp_up=10.0, ramp_down=10.0)
    units = [unit_a, unit_b]
    entity_mw = 45.0  # just above sum(Pmin) = 40, near the low end of the block's range

    range_based = RangeProRata().split(entity_mw, units)
    assert range_based["B"] >= 40.0 - 1e-9
    validate_split_result(entity_mw, units, range_based)  # accepted

    pmax_based = PmaxProRata().split(entity_mw, units)
    assert sum(pmax_based.values()) == pytest.approx(entity_mw)
    assert pmax_based["B"] < 40.0  # violates B's own Pmin
    with pytest.raises(SplitValidationError):
        validate_split_result(entity_mw, units, pmax_based)  # caught, if checked


# --- RU_e is a min over units, not a sum: a slow ST caps the whole block ---


def _zero_drift_telemetry(
    strategy: RangeProRata, units: list[PhysicalUnit], entity_p0_mw: float
) -> dict[str, float]:
    """Telemetry exactly on the disaggregator's manifold (drift d_i == 0 for
    every unit), so aggregate_ramp's drift term drops out and the plain
    min-over-units rate comparison is isolated.
    """
    return strategy.split(entity_p0_mw, units)


def test_ru_e_is_min_over_units_not_sum_for_a_slow_steam_turbine() -> None:
    ct = _unit("CT1", pmin=20.0, pmax=100.0, ramp_up=5.0, ramp_down=5.0)
    st = _unit(
        "ST1",
        pmin=30.0,
        pmax=80.0,
        ramp_up=1.0,
        ramp_down=1.0,
        resource_type=ResourceType.STEAM,
        steam_source=SteamSource.HRSG,
    )
    units = [ct, st]
    strategy = RangeProRata()
    telemetry = _zero_drift_telemetry(strategy, units, entity_p0_mw=90.0)
    dt_min = 5.0

    result = strategy.aggregate_ramp(units, telemetry, dt_min)

    assert isinstance(result, AggregateRamp)
    assert not result.clamped_up
    assert result.ru_mw_per_min == pytest.approx(2.6)
    sum_ru_i = 5.0 + 1.0
    assert result.ru_mw_per_min < sum_ru_i


def test_asymmetric_up_down_rates_produce_asymmetric_aggregate_ramp() -> None:
    ct = _unit("CT1", pmin=20.0, pmax=100.0, ramp_up=5.0, ramp_down=2.0)
    st = _unit(
        "ST1",
        pmin=30.0,
        pmax=80.0,
        ramp_up=1.0,
        ramp_down=1.5,
        resource_type=ResourceType.STEAM,
        steam_source=SteamSource.HRSG,
    )
    units = [ct, st]
    strategy = RangeProRata()
    telemetry = _zero_drift_telemetry(strategy, units, entity_p0_mw=90.0)

    result = strategy.aggregate_ramp(units, telemetry, dt_min=5.0)

    assert result.ru_mw_per_min == pytest.approx(2.6)
    assert result.rd_mw_per_min == pytest.approx(3.25)
    assert result.ru_mw_per_min != pytest.approx(result.rd_mw_per_min)


# --- drift formula, asserted exactly ---


def test_drift_formula_matches_hand_computation_exactly() -> None:
    ct = _unit("CT1", pmin=20.0, pmax=100.0, ramp_up=5.0, ramp_down=5.0)
    st = _unit(
        "ST1",
        pmin=30.0,
        pmax=80.0,
        ramp_up=1.0,
        ramp_down=1.0,
        resource_type=ResourceType.STEAM,
        steam_source=SteamSource.HRSG,
    )
    units = [ct, st]
    dt_min = 5.0

    # Off-manifold telemetry: P_e0 = 90 MW (same total as the zero-drift
    # fixture above), but split 40/50 between CT/ST instead of the
    # manifold's ~44.615/45.385 — chosen so the drift terms (d_CT = +60/13,
    # d_ST = -60/13) land on exact eighths/fifths by hand:
    #   RU_e candidate CT = (5*5 - 60/13) / (8/13) = 265/8 = 33.125
    #   RU_e candidate ST = (1*5 + 60/13) / (5/13) = 125/5 = 25.0   <- binding
    #   RD_e candidate CT = (5*5 + 60/13) / (8/13) = 385/8 = 48.125
    #   RD_e candidate ST = (1*5 - 60/13) / (5/13) = 5/5   = 1.0    <- binding
    telemetry = {"CT1": 40.0, "ST1": 50.0}

    result = RangeProRata().aggregate_ramp(units, telemetry, dt_min)

    assert not result.clamped_up
    assert not result.clamped_down
    assert result.ru_mw_per_min == pytest.approx(25.0 / dt_min)
    assert result.rd_mw_per_min == pytest.approx(1.0 / dt_min)


# --- over-drift clamps to zero, with a diagnostic, never negative ---


def test_over_drift_clamps_aggregate_ramp_to_zero_with_diagnostic() -> None:
    ct = _unit("CT1", pmin=20.0, pmax=100.0, ramp_up=5.0, ramp_down=5.0)
    st = _unit(
        "ST1",
        pmin=30.0,
        pmax=80.0,
        ramp_up=1.0,
        ramp_down=1.0,
        resource_type=ResourceType.STEAM,
        steam_source=SteamSource.HRSG,
    )
    units = [ct, st]
    dt_min = 5.0

    # ST has drifted 10 MW off the zero-drift manifold value (45.3846...),
    # ten times its own ramp budget (RD_ST*dt = 1*5 = 5 MW) in the direction
    # that eats the ramp-up budget: d_ST = +10 > RU_ST*dt = 5.
    telemetry = {"CT1": 54.6153846154, "ST1": 35.3846153846}

    result = RangeProRata().aggregate_ramp(units, telemetry, dt_min)

    assert result.clamped_up
    assert result.ru_mw_per_min == 0.0
    assert result.ru_mw_per_min >= 0.0  # never negative -> never infeasible
    assert len(result.diagnostics) >= 1
    assert "clamped" in result.diagnostics[0]

    # This is the disaggregation-layer half of "clamp, don't infeasible":
    # aggregate_ramp itself always returns a valid (non-negative) result
    # rather than raising, so a downstream ramp constraint built from
    # ru_mw_per_min=0 constrains the block to hold, not blocks the solve.
