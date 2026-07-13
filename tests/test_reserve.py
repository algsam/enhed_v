"""Tests for ReserveModule (SPEC §8; CLAUDE.md "Domain rules"; build order
step 7): two modes behind one config flag.
"""

from __future__ import annotations

import pytest

from ed.curves import from_incremental
from ed.domain.generator import Generator
from ed.model import ModelBuilder
from ed.modules import BalanceModule
from ed.modules.reserve import (
    AggregateHeadroomReserve,
    PerUnitCoOptimizationReserve,
    ReserveMode,
    build_reserve_module,
)


def _flat_generator(
    bus: str,
    ic: float,
    cap_mw: float,
    *,
    reserve_eligible: bool = False,
    ramp_up_mw_per_min: float | None = None,
) -> Generator:
    """A staircase (constant-IC) generator, per the pattern in
    test_model_builder.py, extended with reserve-facing fields."""
    curve = from_incremental(breakpoints_mw=(0.0, cap_mw), ic_values=(ic, ic))
    return Generator(
        bus=bus,
        cost_curve=curve,
        reserve_eligible=reserve_eligible,
        ramp_up_mw_per_min=ramp_up_mw_per_min,
    )


def test_non_binding_requirement_gives_identical_energy_dispatch_both_modes() -> None:
    """With a non-binding requirement, both modes leave the energy-optimal
    dispatch untouched: 20 MW of reserve is trivially covered by the
    expensive unit's own spare capacity, so nothing needs to back down.
    """

    def _dispatch(mode: ReserveMode) -> tuple[float, float]:
        cheap = _flat_generator(
            "bus1", 10.0, 100.0, reserve_eligible=True, ramp_up_mw_per_min=1000.0
        )
        expensive = _flat_generator(
            "bus1", 20.0, 100.0, reserve_eligible=True, ramp_up_mw_per_min=1000.0
        )
        balance = BalanceModule(load_mw=100.0, voll=10_000.0, overgen_penalty=10_000.0)
        reserve = build_reserve_module(
            mode=mode,
            participants=[cheap, expensive],
            product="spin",
            requirement_mw=20.0,
            t_reserve_min=10.0,
            shortfall_penalty=10_000.0 if mode is ReserveMode.PER_UNIT_COOPTIMIZATION else None,
        )
        built = ModelBuilder(entities=[cheap, expensive], modules=[balance, reserve]).build()
        result = built.adapter.solve()
        return cheap.dispatch_mw(result), expensive.dispatch_mw(result)

    mode_a_dispatch = _dispatch(ReserveMode.AGGREGATE_HEADROOM)
    mode_b_dispatch = _dispatch(ReserveMode.PER_UNIT_COOPTIMIZATION)

    # merit order alone (ignoring reserve): cheap fills to its 100 MW cap.
    assert mode_a_dispatch == pytest.approx((100.0, 0.0))
    assert mode_b_dispatch == pytest.approx(mode_a_dispatch)


def test_binding_requirement_mode_b_backs_off_cheap_unit_and_prices_reserve() -> None:
    """A binding requirement that the merit-order dispatch cannot cover:
    the expensive unit's ramp caps its deliverable headroom at 30 MW, so 40
    MW of reserve is unreachable unless the cheap unit backs off energy to
    free its own (unramped) headroom.

    By hand: cheap (IC=10, cap=100, ramp effectively unlimited), expensive
    (IC=20, cap=100, ramp=3 MW/min * 10 min = 30 MW deliverable cap),
    load=100. Minimizing energy cost `10*a + 20*(100-a)` is strictly
    decreasing in `a` (cheap's dispatch), so the solver pushes `a` as high
    as feasible. Total deliverable headroom as a function of `a` is
    `(100-a) + min(a, 30)`; for `a >= 30` this is `130-a`, so `130-a >= 40`
    requires `a <= 90`. The optimum is therefore `a=90`, exactly freeing 40
    MW of headroom (10 from cheap's own capacity, 30 — its ramp cap — from
    expensive), with zero shortfall.
    """
    cheap = _flat_generator("bus1", 10.0, 100.0, reserve_eligible=True, ramp_up_mw_per_min=1000.0)
    expensive = _flat_generator(
        "bus1", 20.0, 100.0, reserve_eligible=True, ramp_up_mw_per_min=3.0
    )
    balance = BalanceModule(load_mw=100.0, voll=10_000.0, overgen_penalty=10_000.0)
    reserve = PerUnitCoOptimizationReserve(
        participants=[cheap, expensive],
        product="spin",
        requirement_mw=40.0,
        t_reserve_min=10.0,
        shortfall_penalty=10_000.0,
    )

    built = ModelBuilder(entities=[cheap, expensive], modules=[balance, reserve]).build()
    result = built.adapter.solve()

    assert cheap.dispatch_mw(result) == pytest.approx(90.0)
    assert expensive.dispatch_mw(result) == pytest.approx(10.0)
    assert reserve.total_reserve_mw(result) == pytest.approx(40.0)
    assert reserve.reserve_mw(result, cheap) == pytest.approx(10.0)
    assert reserve.reserve_mw(result, expensive) == pytest.approx(30.0)
    assert reserve.shortfall_mw(result) == pytest.approx(0.0)
    assert not reserve.is_scarce(result)
    assert reserve.extract_price(result) > 0.0

    # the "cheapest unit backs off energy" acceptance criterion: cheap no
    # longer fills to its 100 MW cap the way merit order alone would.
    assert cheap.dispatch_mw(result) < 100.0


def test_mode_b_shortfall_slack_keeps_an_unreachable_requirement_solvable() -> None:
    """A requirement beyond what any amount of redispatch can deliver still
    solves — never "infeasible" (CLAUDE.md "Operational") — via the
    auto-injected shortfall slack, at a nonzero, positive reserve price
    (the shortfall penalty itself, once shortfall is marginal).
    """
    only_unit = _flat_generator(
        "bus1", 10.0, 100.0, reserve_eligible=True, ramp_up_mw_per_min=1000.0
    )
    balance = BalanceModule(load_mw=50.0, voll=10_000.0, overgen_penalty=10_000.0)
    shortfall_penalty = 5_000.0
    reserve = PerUnitCoOptimizationReserve(
        participants=[only_unit],
        product="spin",
        requirement_mw=1_000.0,
        t_reserve_min=10.0,
        shortfall_penalty=shortfall_penalty,
    )

    built = ModelBuilder(entities=[only_unit], modules=[balance, reserve]).build()
    result = built.adapter.solve()

    assert reserve.shortfall_mw(result) > 0.0
    assert reserve.is_scarce(result)
    assert reserve.extract_price(result) == pytest.approx(shortfall_penalty)


def test_only_reserve_eligible_entities_participate() -> None:
    """`reserve_eligible=False` entities (e.g. a nuclear unit, SPEC §8)
    never enter the requirement row — filtered at construction, not by a
    branch inside contribute()."""
    ineligible = _flat_generator("bus1", 5.0, 50.0, reserve_eligible=False)
    eligible = _flat_generator(
        "bus1", 10.0, 100.0, reserve_eligible=True, ramp_up_mw_per_min=1000.0
    )
    reserve = build_reserve_module(
        mode=ReserveMode.AGGREGATE_HEADROOM,
        participants=[ineligible, eligible],
        product="spin",
        requirement_mw=10.0,
        t_reserve_min=10.0,
    )

    assert isinstance(reserve, AggregateHeadroomReserve)
    assert reserve.eligible_participants == (eligible,)


def test_reserve_eligible_without_ramp_rate_is_rejected() -> None:
    """CLAUDE.md "Domain rules": the deliverability cap is mandatory — a
    reserve-eligible entity with no resolved ramp rate must be rejected,
    not silently treated as unconstrained.
    """
    with pytest.raises(ValueError):
        Generator(
            bus="bus1",
            cost_curve=from_incremental(breakpoints_mw=(0.0, 100.0), ic_values=(10.0, 10.0)),
            reserve_eligible=True,
        )
