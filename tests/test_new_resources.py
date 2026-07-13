"""Tests for Renewable, Nuclear, TieLine, DemandResponse, DispatchableLoad,
BESS, and Slack/registry (SPEC §5.4-§5.6, §7, §11; CLAUDE.md "Domain rules";
build order step 8).
"""

from __future__ import annotations

import pytest

from ed.curves import from_incremental
from ed.disagg import RangeProRata
from ed.domain.bess import BESS, SimultaneousChargeDischargeError
from ed.domain.cc_block import CCBlock
from ed.domain.demand_response import DemandResponse
from ed.domain.dispatchable_load import DispatchableLoad
from ed.domain.enums import Mode, ResourceType
from ed.domain.generator import Generator
from ed.domain.nuclear import Nuclear
from ed.domain.physical_unit import PhysicalUnit, UnitCharacteristics
from ed.domain.ramp import RampRateCurve
from ed.domain.renewable import Renewable
from ed.domain.slack import Slack
from ed.domain.tie_line import TieLine
from ed.entities import SystemGeneratedResourceError, remove_resource, user_editable_resources
from ed.model import ModelBuilder
from ed.modules import BalanceModule


def _flat_generator(bus: str, ic: float, cap_mw: float) -> Generator:
    curve = from_incremental(breakpoints_mw=(0.0, cap_mw), ic_values=(ic, ic))
    return Generator(bus=bus, cost_curve=curve)


def _cc_member(unit_id: str, pmin: float, pmax: float) -> PhysicalUnit:
    chars = UnitCharacteristics(
        pmin_mw=pmin,
        pmax_mw=pmax,
        ramp_up=RampRateCurve.constant(5.0, pmin),
        ramp_down=RampRateCurve.constant(5.0, pmin),
    )
    return PhysicalUnit(
        unit_id=unit_id,
        bus="bus1",
        resource_type=ResourceType.THERMAL,
        characteristics={Mode.COMBINED_CYCLE: chars},
        active_mode=Mode.COMBINED_CYCLE,
        p0_mw=pmin,
        online=True,
    )


# --- mixed case: THERMAL + CC_BLOCK + RENEWABLE + TIE_LINE + DEMAND_RESPONSE
# + SLACK solves and Sum injection == load (SPEC §11) ---


def test_mixed_resource_case_solves_and_balances() -> None:
    thermal = _flat_generator("bus1", ic=10.0, cap_mw=50.0)

    members = [_cc_member("CT2", 20.0, 100.0), _cc_member("ST1", 30.0, 80.0)]
    cc_curve = from_incremental(breakpoints_mw=(50.0, 180.0), ic_values=(12.0, 25.0))
    cc_block = CCBlock(
        block_id="BLOCK1",
        bus="bus1",
        cost_curve=cc_curve,
        config_id="CT2_ST1",
        member_units=members,
        disaggregator=RangeProRata(),
    )

    renewable = Renewable("bus1", forecast_mw=30.0, curtailment_penalty_per_mwh=1.0)
    tie = TieLine("bus1", schedule_mw=10.0, import_price=5.0, export_price=2.0)
    dr_curve = from_incremental(breakpoints_mw=(0.0, 20.0), ic_values=(50.0, 60.0))
    dr = DemandResponse("bus1", cost_curve=dr_curve)

    load_mw = 200.0
    balance = BalanceModule(load_mw=load_mw, voll=10_000.0, overgen_penalty=10_000.0)

    built = ModelBuilder(
        entities=[thermal, cc_block, renewable, tie, dr], modules=[balance]
    ).build()
    result = built.adapter.solve()

    assert not balance.is_scarce(result)

    total_injection = (
        thermal.dispatch_mw(result)
        + cc_block.dispatch_mw(result)
        + renewable.dispatch_mw(result)
        + tie.net_injection_mw(result)
        + dr.dispatch_mw(result)
        + balance.unserved_mw(result)
        - balance.overgen_mw(result)
    )
    assert total_injection == pytest.approx(load_mw)


# --- adding a throwaway ResourceType requires zero edits to model_builder.py ---


class _ThrowawayResource:
    """A brand-new resource type invented purely by this test, whose
    `resource_type` isn't even in `ed.domain.enums.ResourceType` — if the
    builder needed editing to support a new type, this would fail to build.
    """

    resource_type = "THROWAWAY_TYPE_NOT_IN_ENUM"
    is_system_generated = False
    emits_setpoint = True

    def __init__(self, bus: str, fixed_mw: float) -> None:
        self.bus = bus
        self.fixed_mw = fixed_mw
        self._var = None

    def contribute_variables(self, ctx):  # type: ignore[no-untyped-def]
        var = ctx.adapter.add_var(cost=0.0, lower=self.fixed_mw, upper=self.fixed_mw)
        ctx.add_injection(self.bus, var, coefficient=1.0)
        self._var = var
        return (var,)

    def contribute_constraints(self, ctx):  # type: ignore[no-untyped-def]
        return None

    def contribute_cost(self, ctx):  # type: ignore[no-untyped-def]
        return None

    def dispatch_mw(self, result: object) -> float:
        return float(result.primal[self._var])  # type: ignore[attr-defined]


def test_throwaway_resource_type_requires_no_model_builder_edits() -> None:
    thermal = _flat_generator("bus1", ic=10.0, cap_mw=50.0)
    throwaway = _ThrowawayResource("bus1", fixed_mw=15.0)
    balance = BalanceModule(load_mw=40.0, voll=10_000.0, overgen_penalty=10_000.0)

    built = ModelBuilder(entities=[thermal, throwaway], modules=[balance]).build()
    result = built.adapter.solve()

    assert not balance.is_scarce(result)
    assert throwaway.dispatch_mw(result) == pytest.approx(15.0)
    assert thermal.dispatch_mw(result) == pytest.approx(25.0)


# --- curtailment occurs when forced ---


def test_curtailment_occurs_when_forced() -> None:
    """Nuclear pinned at 80 MW plus renewable forecast of 50 MW would exceed
    a 100 MW load if the renewable ran to forecast — the equality balance row
    forces it down to exactly 20 MW, curtailing 30 MW, even though a
    curtailment penalty rewards producing more.
    """
    nuclear = Nuclear("bus1", scheduled_mw=80.0)
    renewable = Renewable("bus1", forecast_mw=50.0, curtailment_penalty_per_mwh=1.0)
    balance = BalanceModule(load_mw=100.0, voll=10_000.0, overgen_penalty=10_000.0)

    built = ModelBuilder(entities=[nuclear, renewable], modules=[balance]).build()
    result = built.adapter.solve()

    assert not balance.is_scarce(result)
    assert nuclear.dispatch_mw(result) == pytest.approx(80.0)
    assert renewable.dispatch_mw(result) == pytest.approx(20.0)
    assert renewable.curtailment_mw(result) == pytest.approx(30.0)


def test_nuclear_is_pinned_to_schedule_hard_bound() -> None:
    nuclear = Nuclear("bus1", scheduled_mw=42.0)
    balance = BalanceModule(load_mw=42.0, voll=10_000.0, overgen_penalty=10_000.0)
    built = ModelBuilder(entities=[nuclear], modules=[balance]).build()
    result = built.adapter.solve()
    assert nuclear.dispatch_mw(result) == pytest.approx(42.0)
    assert nuclear.reserve_eligible is False


# --- Slack cannot be deleted from an operator-facing resource list ---


def test_slack_cannot_be_deleted_from_operator_facing_resource_list() -> None:
    unserved_slack = Slack("unserved", "bus1", "unserved")
    overgen_slack = Slack("overgen", "bus1", "overgen")
    tie = TieLine("bus1", schedule_mw=5.0, import_price=5.0, export_price=1.0)

    resources: list[Slack | TieLine] = [unserved_slack, overgen_slack, tie]

    editable = user_editable_resources(resources)
    assert unserved_slack not in editable
    assert overgen_slack not in editable
    assert tie in editable

    with pytest.raises(SystemGeneratedResourceError):
        remove_resource(resources, unserved_slack)

    # a non-system-generated resource *can* be removed
    remove_resource(resources, tie)
    assert tie not in resources


def test_slack_is_system_generated_tie_line_is_not() -> None:
    slack = Slack("unserved", "bus1", "unserved")
    tie = TieLine("bus1", schedule_mw=0.0, import_price=5.0, export_price=1.0)
    assert slack.is_system_generated is True
    assert tie.is_system_generated is False
    assert slack.emits_setpoint is False
    assert tie.emits_setpoint is False


# --- TieLine / BESS price-ordering and simultaneity guards ---


def test_tie_line_rejects_import_price_below_export_price() -> None:
    from ed.curves.validators import PriceOrderingError

    with pytest.raises(PriceOrderingError):
        TieLine("bus1", schedule_mw=10.0, import_price=1.0, export_price=5.0)


def test_bess_rejects_charge_cost_below_discharge_revenue() -> None:
    from ed.curves.validators import PriceOrderingError

    with pytest.raises(PriceOrderingError):
        BESS(
            bus="bus1",
            power_rating_mw=10.0,
            capacity_mwh=40.0,
            soe_mwh=20.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=1.0,
            discharge_revenue=5.0,
            dt_min=5.0,
        )


def test_bess_discharge_capped_by_energy_budget_not_just_power_rating() -> None:
    """Small SoE relative to power rating: the energy-limited bound binds
    below the power rating (SPEC §5.5's single-snapshot energy-budget
    option)."""
    bess = BESS(
        bus="bus1",
        power_rating_mw=100.0,
        capacity_mwh=200.0,
        soe_mwh=5.0,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        charge_cost=10.0,
        discharge_revenue=5.0,
        dt_min=60.0,
    )
    balance = BalanceModule(load_mw=1.0, voll=10_000.0, overgen_penalty=10_000.0)
    built = ModelBuilder(entities=[bess], modules=[balance]).build()
    result = built.adapter.solve()

    # discharge bound = soe_mwh * discharge_efficiency / dt_hr = 5 * 0.9 / 1 = 4.5 MW,
    # strictly below the 100 MW power rating.
    assert bess.discharge_mw(result) == pytest.approx(1.0)
    bess.assert_no_simultaneous_charge_discharge(result)


def test_bess_post_solve_assert_raises_on_simultaneous_charge_and_discharge() -> None:
    """Direct unit test of the guard function itself (constructing a
    SolveResult-like fake is unnecessary — the assert only reads two
    primal values off whatever handles were captured at contribute time)."""
    bess = BESS(
        bus="bus1",
        power_rating_mw=10.0,
        capacity_mwh=40.0,
        soe_mwh=20.0,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        charge_cost=10.0,
        discharge_revenue=5.0,
        dt_min=5.0,
    )
    balance = BalanceModule(load_mw=0.0, voll=10_000.0, overgen_penalty=10_000.0)
    built = ModelBuilder(entities=[bess], modules=[balance]).build()
    result = built.adapter.solve()

    from dataclasses import replace

    assert bess._chg_var is not None and bess._dis_var is not None
    fake_result = replace(
        result,
        primal={**result.primal, bess._chg_var: 3.0, bess._dis_var: 2.0},
    )
    with pytest.raises(SimultaneousChargeDischargeError):
        bess.assert_no_simultaneous_charge_discharge(fake_result)


# --- DemandResponse / DispatchableLoad curve-role guards ---


def test_demand_response_rejects_a_value_curve() -> None:
    from ed.curves import from_incremental as _fi

    value_curve = _fi(
        breakpoints_mw=(0.0, 20.0), ic_values=(60.0, 50.0), validate_as="demand"
    )
    with pytest.raises(ValueError, match="cost"):
        DemandResponse("bus1", cost_curve=value_curve)


def test_dispatchable_load_rejects_a_cost_curve() -> None:
    cost_curve = from_incremental(breakpoints_mw=(0.0, 20.0), ic_values=(10.0, 20.0))
    with pytest.raises(ValueError, match="value"):
        DispatchableLoad("bus1", value_curve=cost_curve)


def test_dispatchable_load_consumes_as_negative_injection() -> None:
    value_curve = from_incremental(
        breakpoints_mw=(0.0, 30.0), ic_values=(40.0, 20.0), validate_as="demand"
    )
    load = DispatchableLoad("bus1", value_curve=value_curve)
    generator = _flat_generator("bus1", ic=10.0, cap_mw=100.0)
    balance = BalanceModule(load_mw=0.0, voll=10_000.0, overgen_penalty=10_000.0)

    built = ModelBuilder(entities=[generator, load], modules=[balance]).build()
    result = built.adapter.solve()

    assert not balance.is_scarce(result)
    assert load.consumption_mw(result) > 0.0
    assert generator.dispatch_mw(result) == pytest.approx(load.consumption_mw(result))
