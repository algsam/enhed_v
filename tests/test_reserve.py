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
    dispatch — *and* lambda — untouched to the last digit: 5 MW of reserve
    (load=150, both units reserve-eligible with ample ramp) is trivially
    covered by the expensive unit's own spare capacity, so nothing needs to
    back down. Asserted as exact equality (not `pytest.approx` tolerance
    slop) between the two modes' full result tuples: if Mode B's
    co-optimization were leaking into the energy dispatch even a little
    when reserve isn't binding, this would catch it.
    """

    def _solve(mode: ReserveMode) -> tuple[float, float, float]:
        cheap = _flat_generator(
            "bus1", 10.0, 100.0, reserve_eligible=True, ramp_up_mw_per_min=1000.0
        )
        expensive = _flat_generator(
            "bus1", 20.0, 100.0, reserve_eligible=True, ramp_up_mw_per_min=1000.0
        )
        balance = BalanceModule(load_mw=150.0, voll=10_000.0, overgen_penalty=10_000.0)
        reserve = build_reserve_module(
            mode=mode,
            participants=[cheap, expensive],
            product="spin",
            requirement_mw=5.0,
            t_reserve_min=10.0,
            shortfall_penalty=10_000.0 if mode is ReserveMode.PER_UNIT_COOPTIMIZATION else None,
        )
        built = ModelBuilder(entities=[cheap, expensive], modules=[balance, reserve]).build()
        result = built.adapter.solve()
        return (
            round(cheap.dispatch_mw(result), 3),
            round(expensive.dispatch_mw(result), 3),
            round(balance.extract_price(result), 3),
        )

    mode_a = _solve(ReserveMode.AGGREGATE_HEADROOM)
    mode_b = _solve(ReserveMode.PER_UNIT_COOPTIMIZATION)

    # merit order alone (ignoring reserve): cheap fills to its 100 MW cap,
    # expensive covers the rest (150-100=50), lambda = expensive's own IC.
    assert mode_a == (100.0, 50.0, 20.0)
    assert mode_b == (100.0, 50.0, 20.0)
    assert mode_a == mode_b


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

    # the "cheapest unit backs off energy" acceptance criterion: cheap no
    # longer fills to its 100 MW cap the way merit order alone would.
    assert cheap.dispatch_mw(result) < 100.0

    # The reserve price is not just positive — it equals the exact
    # opportunity cost of holding cheap back: cheap is the unit forced off
    # its cost-minimizing position to free reserve headroom, so the
    # marginal $/MW cost of the next unit of reserve is what one more MW of
    # backdown costs in foregone cheap-for-expensive energy substitution:
    # lambda - IC_cheap = 20.0 - 10.0 = 10.0. It does *not* equal
    # lambda - IC_expensive (0.0) — expensive is the marginal *energy* unit
    # (lambda = its own IC = 20.0) but cheap is the marginal *reserve* unit.
    lam = balance.extract_price(result)
    assert lam == pytest.approx(20.0)
    assert reserve.extract_price(result) == pytest.approx(10.0)
    assert reserve.extract_price(result) == pytest.approx(lam - 10.0)  # lambda - IC_cheap
    assert reserve.extract_price(result) != pytest.approx(lam - 20.0)  # NOT lambda - IC_expensive


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


def test_mode_a_headroom_is_capped_by_ramp_not_just_by_capacity() -> None:
    """The deliverability cap is the entire reason Mode A exists (CLAUDE.md
    "Domain rules": "without it the stub reserves MW no unit can reach in
    time"). Construct a unit with large *capacity* headroom but a slow
    ramp, so `RampRate_e * T_reserve` binds strictly below `Pmax_e - P_e`,
    and confirm the claimed headroom tracks the smaller (ramp) number, not
    the larger (capacity) one.

    One unit, cap=100 MW, dispatched to 20 MW (load=20) -> capacity
    headroom `Pmax - P = 80` MW. Ramp = 2 MW/min, `T_reserve=10` min ->
    ramp cap = 20 MW, strictly below the 80 MW capacity headroom.
    """
    slow = _flat_generator("bus1", 10.0, 100.0, reserve_eligible=True, ramp_up_mw_per_min=2.0)
    balance = BalanceModule(load_mw=20.0, voll=10_000.0, overgen_penalty=10_000.0)
    t_reserve_min = 10.0
    ramp_cap_mw = slow.ramp_up_mw_per_min * t_reserve_min
    capacity_headroom_mw = slow.capacity_mw - 20.0
    assert ramp_cap_mw == pytest.approx(20.0)
    assert capacity_headroom_mw == pytest.approx(80.0)
    assert ramp_cap_mw < capacity_headroom_mw  # the case worth testing

    # (a) a requirement between the two caps (20 < req < 80) is satisfiable
    # up to *exactly* the ramp cap, never beyond it, even though capacity
    # headroom alone would allow far more.
    reserve_at_cap = build_reserve_module(
        mode=ReserveMode.AGGREGATE_HEADROOM,
        participants=[slow],
        product="spin",
        requirement_mw=ramp_cap_mw,
        t_reserve_min=t_reserve_min,
    )
    built = ModelBuilder(entities=[slow], modules=[balance, reserve_at_cap]).build()
    result = built.adapter.solve()
    assert slow.dispatch_mw(result) == pytest.approx(20.0)
    assert reserve_at_cap.headroom_mw(result) == pytest.approx(ramp_cap_mw)
    assert reserve_at_cap.headroom_mw(result) < capacity_headroom_mw

    # (b) a requirement one MW above the ramp cap — but still far below the
    # 80 MW capacity headroom — cannot be met at all: Mode A has no
    # shortfall slack (by design, see module docstring), so the model goes
    # infeasible. If the ramp cap were *not* enforced (a bug that let the
    # module fall back to `Pmax - P` alone), this would solve instead.
    from ed.solver import SolveError

    balance2 = BalanceModule(load_mw=20.0, voll=10_000.0, overgen_penalty=10_000.0)
    slow2 = _flat_generator("bus1", 10.0, 100.0, reserve_eligible=True, ramp_up_mw_per_min=2.0)
    reserve_over_cap = build_reserve_module(
        mode=ReserveMode.AGGREGATE_HEADROOM,
        participants=[slow2],
        product="spin",
        requirement_mw=ramp_cap_mw + 1.0,
        t_reserve_min=t_reserve_min,
    )
    built2 = ModelBuilder(entities=[slow2], modules=[balance2, reserve_over_cap]).build()
    with pytest.raises(SolveError):
        built2.adapter.solve()


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
