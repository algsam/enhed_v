"""Tests for CostCurve (CLAUDE.md "Cost curves"; SPEC §4, §11 acceptance criteria)."""

from __future__ import annotations

import pytest

from ed.curves import (
    CurveConcavityError,
    CurveConvexityError,
    HeatRateCurve,
    NonPSDSegmentError,
    PriceOrderingError,
    from_fuel_cost,
    from_incremental,
    from_total_cost,
    validate_demand_concavity,
    validate_price_ordering,
    validate_supply_convexity,
)
from ed.curves.curve import CostCurve, Segment

# --- construction / contiguity ---


def test_segment_rejects_nonpositive_width() -> None:
    with pytest.raises(ValueError, match="right_mw must exceed left_mw"):
        Segment(left_mw=10, right_mw=10, left_ic=5, right_ic=5)


def test_costcurve_requires_contiguous_segments() -> None:
    with pytest.raises(ValueError, match="not contiguous"):
        CostCurve(
            segments=(
                Segment(left_mw=0, right_mw=10, left_ic=5, right_ic=5),
                Segment(left_mw=11, right_mw=20, left_ic=5, right_ic=5),
            )
        )


def test_costcurve_requires_at_least_one_segment() -> None:
    with pytest.raises(ValueError, match="at least one segment"):
        CostCurve(segments=())


# --- from_incremental ---


def test_from_incremental_builds_interpolating_segments() -> None:
    curve = from_incremental((0, 10, 20), (10, 14, 16))
    assert curve.x0 == 0
    assert curve.x_n == 20
    assert curve.segments[0].right_ic == curve.segments[1].left_ic == 14


def test_non_monotonic_slopes_with_non_decreasing_ic_is_accepted() -> None:
    # slope of segment 0 is 4 ($/MWh per MW), slope of segment 1 is 2 —
    # slopes decrease, but IC itself (10, 14, 16) never decreases. SPEC §4.4:
    # sufficient that IC is non-decreasing; slopes need not increase.
    curve = from_incremental((0, 10, 20), (10, 14, 16))
    validate_supply_convexity(curve)  # must not raise


def test_downward_segment_is_rejected() -> None:
    with pytest.raises(CurveConvexityError, match="segment 0"):
        from_incremental((0, 10), (10, 5))


def test_downward_breakpoint_jump_is_rejected() -> None:
    # Two internally-flat (staircase) segments, but the second's IC is lower
    # than the first's — a downward jump at the shared breakpoint.
    curve = CostCurve(
        segments=(
            Segment(left_mw=0, right_mw=10, left_ic=5, right_ic=5),
            Segment(left_mw=10, right_mw=20, left_ic=3, right_ic=3),
        )
    )
    with pytest.raises(CurveConvexityError, match="boundary at 10"):
        validate_supply_convexity(curve)


def test_valid_demand_curve_accepted_by_demand_rejected_by_supply() -> None:
    curve = from_incremental((0, 10, 20), (20, 15, 10), validate_as="demand")
    validate_demand_concavity(curve)  # accepted
    with pytest.raises(CurveConvexityError):
        validate_supply_convexity(curve)


def test_from_incremental_rejects_invalid_demand_curve_at_ingest() -> None:
    with pytest.raises(CurveConcavityError):
        from_incremental((0, 10, 20), (10, 14, 16), validate_as="demand")


def test_from_incremental_mismatched_lengths_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        from_incremental((0, 10, 20), (10, 14))


def test_from_incremental_no_load_cost_preserved() -> None:
    curve = from_incremental((0, 10), (5, 5), no_load_cost=100.0)
    assert curve.has_absolute_cost
    assert curve.no_load_cost == 100.0


def test_from_incremental_without_no_load_cost_not_absolute() -> None:
    curve = from_incremental((0, 10), (5, 5))
    assert not curve.has_absolute_cost


# --- QP segments ---


def test_to_qp_segments_reads_a_and_q_off_the_curve() -> None:
    curve = from_incremental((0, 10, 30), (10, 14, 16))
    qp = curve.to_qp_segments()
    assert qp[0].a == 10
    assert qp[0].width_mw == 10
    assert qp[0].q == pytest.approx((14 - 10) / 10)
    assert qp[1].a == 14
    assert qp[1].width_mw == 20
    assert qp[1].q == pytest.approx((16 - 14) / 20)


def test_staircase_segment_has_zero_q() -> None:
    curve = CostCurve(segments=(Segment(left_mw=0, right_mw=10, left_ic=7, right_ic=7),))
    assert curve.to_qp_segments()[0].q == 0.0


# --- QP sign convention for demand/WTP ("value") curves ---


def test_valid_demand_curve_survives_to_qp_segments_with_nonneg_q() -> None:
    # DispatchableLoad / DR WTP curve: 20 -> 15 -> 10 $/MWh, non-increasing.
    curve = from_incremental((0, 10, 20), (20, 15, 10), validate_as="demand")
    assert curve.curve_role == "value"
    qp = curve.to_qp_segments()
    assert all(seg.q >= 0 for seg in qp)


def test_value_curve_qp_coefficients_are_the_negation_of_the_raw_wtp() -> None:
    # seg0: WTP 20 -> 15 over 10 MW, slope_wtp = -0.5.
    # cost_j(p) = -integral WTP = -20*p + 0.25*p**2  =>  a = -20, q = 0.5.
    curve = from_incremental((0, 10, 20), (20, 15, 10), validate_as="demand")
    qp = curve.to_qp_segments()
    assert qp[0].a == pytest.approx(-20.0)
    assert qp[0].q == pytest.approx(0.5)
    assert qp[1].a == pytest.approx(-15.0)
    assert qp[1].q == pytest.approx(0.5)


def test_cost_role_curve_is_unaffected_by_sign_flip() -> None:
    curve = from_incremental((0, 10, 20), (10, 14, 16))  # default validate_as="supply"
    assert curve.curve_role == "cost"
    qp = curve.to_qp_segments()
    assert qp[0].a == 10
    assert qp[0].q == pytest.approx(0.4)


def test_mistagged_value_curve_trips_the_nonpsd_assertion() -> None:
    # Built directly (bypassing ingest), tagged "value" but shaped like a
    # supply curve (IC non-decreasing => slope >= 0). Negating a
    # non-negative slope for the "value" sign convention produces Q_jj < 0
    # — exactly the mistake the assertion in to_qp_segments exists to catch
    # before it reaches HiGHS.
    curve = CostCurve(
        segments=(Segment(left_mw=0, right_mw=10, left_ic=5, right_ic=10),),
        curve_role="value",
    )
    with pytest.raises(NonPSDSegmentError, match="Q_jj"):
        curve.to_qp_segments()


# --- total_cost integration ---


def test_total_cost_integrates_a_single_flat_segment() -> None:
    curve = CostCurve(
        segments=(Segment(left_mw=0, right_mw=10, left_ic=5, right_ic=5),), no_load_cost=0.0
    )
    assert curve.total_cost(10) == pytest.approx(50.0)
    assert curve.total_cost(4) == pytest.approx(20.0)


def test_total_cost_integrates_a_sloped_segment_as_a_parabola() -> None:
    # IC rises linearly 0 -> 10 $/MWh over 0 -> 100 MW: cost(P) = P^2/20.
    curve = CostCurve(
        segments=(Segment(left_mw=0, right_mw=100, left_ic=0, right_ic=10),), no_load_cost=0.0
    )
    assert curve.total_cost(100) == pytest.approx(500.0)
    assert curve.total_cost(50) == pytest.approx(125.0)


def test_total_cost_anchors_no_load_cost_at_x0() -> None:
    curve = from_incremental((0, 10), (5, 5), no_load_cost=1000.0)
    assert curve.total_cost(0) == pytest.approx(1000.0)
    assert curve.total_cost(10) == pytest.approx(1050.0)


def test_total_cost_sums_across_multiple_segments() -> None:
    curve = from_incremental((0, 10, 20), (10, 10, 20), no_load_cost=0.0)
    # segment 0: flat 10 $/MWh over 10 MW = 100
    # segment 1: 10 -> 20 $/MWh over 10 MW, filled to 5 MW: 10*5 + 1/2*5*5 = 62.5
    assert curve.total_cost(15) == pytest.approx(100 + 62.5)


def test_total_cost_rejects_out_of_domain() -> None:
    curve = from_incremental((0, 10), (5, 5))
    with pytest.raises(ValueError, match="outside curve domain"):
        curve.total_cost(11)


# --- LP staircase ---


def test_lp_staircase_of_flat_segment_is_a_single_exact_step() -> None:
    curve = CostCurve(segments=(Segment(left_mw=0, right_mw=10, left_ic=5, right_ic=5),))
    steps = curve.to_lp_staircase(samples_per_segment=4)
    assert len(steps) == 1
    assert steps[0].ic == 5


def test_lp_staircase_midpoint_sampling_preserves_total_cost() -> None:
    curve = CostCurve(
        segments=(Segment(left_mw=0, right_mw=100, left_ic=0, right_ic=10),), no_load_cost=0.0
    )
    exact = curve.total_cost(100)
    for n in (1, 2, 5, 10):
        steps = curve.to_lp_staircase(samples_per_segment=n)
        sampled_cost = sum(s.ic * (s.right_mw - s.left_mw) for s in steps)
        assert sampled_cost == pytest.approx(exact)


# --- fuel-cost / total-cost ingest ---


def test_from_total_cost_default_is_exact_staircase_derivative() -> None:
    # total cost: 0 @ 0 MW, 100 @ 10 MW, 300 @ 20 MW -> slopes 10, 20 $/MWh.
    curve = from_total_cost((0, 10, 20), (0, 100, 300))
    assert curve.segments[0].left_ic == curve.segments[0].right_ic == 10
    assert curve.segments[1].left_ic == curve.segments[1].right_ic == 20


def test_from_total_cost_staircase_is_exactly_cost_preserving() -> None:
    curve = from_total_cost((0, 10, 20), (0, 100, 300))
    assert curve.total_cost(10) == pytest.approx(100)
    assert curve.total_cost(20) == pytest.approx(300)


def test_from_total_cost_midpoint_interpolation_is_explicit_opt_in() -> None:
    staircase = from_total_cost((0, 10, 20), (0, 100, 300))
    interpolated = from_total_cost((0, 10, 20), (0, 100, 300), interpolate_at_midpoints=True)
    assert all(seg.is_flat for seg in staircase.segments)
    # breakpoints move to the midpoints (5, 15); the middle segment interpolates
    # between the two segment slopes instead of jumping.
    assert [s.left_mw for s in interpolated.segments] == [0, 5, 15]
    assert not interpolated.segments[1].is_flat
    assert interpolated.segments[1].left_ic == 10
    assert interpolated.segments[1].right_ic == 20
    # at the shared breakpoint (10 MW), the staircase jumps discontinuously
    # from 10 to 20 while the interpolated curve passes continuously through 15.
    assert staircase.segments[0].right_ic == 10
    assert staircase.segments[1].left_ic == 20


def test_from_fuel_cost_multiplies_heat_rate_by_price() -> None:
    heat_rate = HeatRateCurve(breakpoints_mw=(0, 10, 20), heat_input_mmbtu_per_h=(0, 100, 300))
    curve = from_fuel_cost(heat_rate, fuel_price_per_mmbtu=2.0)
    assert curve.segments[0].left_ic == pytest.approx(20.0)
    assert curve.segments[1].left_ic == pytest.approx(40.0)


def test_from_fuel_cost_price_update_does_not_require_curve_reentry() -> None:
    heat_rate = HeatRateCurve(breakpoints_mw=(0, 10), heat_input_mmbtu_per_h=(0, 100))
    cheap = from_fuel_cost(heat_rate, fuel_price_per_mmbtu=1.0)
    expensive = from_fuel_cost(heat_rate, fuel_price_per_mmbtu=3.0)
    assert expensive.segments[0].left_ic == pytest.approx(3 * cheap.segments[0].left_ic)


def test_from_total_cost_rejects_non_convex_result() -> None:
    # total cost 0 -> 100 -> 150 over 0->10->20 MW: slopes 10 then 5 (decreasing IC).
    with pytest.raises(CurveConvexityError):
        from_total_cost((0, 10, 20), (0, 100, 150))


def test_heat_rate_curve_rejects_unsorted_breakpoints() -> None:
    with pytest.raises(ValueError, match="ascending"):
        HeatRateCurve(breakpoints_mw=(10, 0), heat_input_mmbtu_per_h=(100, 0))


def test_heat_rate_curve_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        HeatRateCurve(breakpoints_mw=(0, 10, 20), heat_input_mmbtu_per_h=(0, 100))


# --- bidirectional price ordering ---


def test_price_ordering_accepts_import_at_or_above_export() -> None:
    validate_price_ordering(import_price=40.0, export_price=30.0)
    validate_price_ordering(import_price=30.0, export_price=30.0)


def test_price_ordering_rejects_import_below_export() -> None:
    with pytest.raises(PriceOrderingError):
        validate_price_ordering(import_price=20.0, export_price=30.0)


# --- zero-width / duplicate breakpoints are rejected at ingest ---


def test_from_incremental_rejects_duplicate_breakpoint() -> None:
    with pytest.raises(ValueError, match="strictly ascending"):
        from_incremental((0, 10, 10, 20), (5, 6, 6, 7))


def test_from_incremental_rejects_descending_breakpoint() -> None:
    with pytest.raises(ValueError, match="strictly ascending"):
        from_incremental((0, 10, 5), (5, 6, 7))


def test_from_total_cost_rejects_duplicate_breakpoint_before_dividing_by_zero() -> None:
    # Without the ascending check this would raise a bare ZeroDivisionError
    # from `slope = delta_cost / width` instead of a clear domain error.
    with pytest.raises(ValueError, match="strictly ascending"):
        from_total_cost((0, 10, 10, 20), (0, 100, 100, 300))


def test_heat_rate_curve_rejects_duplicate_breakpoint() -> None:
    # A duplicate breakpoint sorts identically to itself, so a naive
    # `sorted() == breakpoints` check would silently accept it.
    with pytest.raises(ValueError, match="strictly ascending"):
        HeatRateCurve(breakpoints_mw=(0, 10, 10), heat_input_mmbtu_per_h=(0, 100, 100))


def test_from_fuel_cost_rejects_duplicate_breakpoint() -> None:
    heat_rate = HeatRateCurve.model_construct(
        breakpoints_mw=(0, 10, 10), heat_input_mmbtu_per_h=(0, 100, 100)
    )
    with pytest.raises(ValueError, match="strictly ascending"):
        from_fuel_cost(heat_rate, fuel_price_per_mmbtu=2.0)


# --- midpoint interpolation covers the full input domain ---


def test_midpoint_interpolation_spans_the_full_input_domain_two_segments() -> None:
    curve = from_total_cost((0, 10, 20), (0, 100, 300), interpolate_at_midpoints=True)
    assert curve.x0 == 0
    assert curve.x_n == 20
    # flat extrapolation below the first midpoint (5) and above the last (15)
    assert curve.segments[0].left_mw == 0
    assert curve.segments[0].right_mw == 5
    assert curve.segments[0].is_flat
    assert curve.segments[-1].left_mw == 15
    assert curve.segments[-1].right_mw == 20
    assert curve.segments[-1].is_flat
    # the curve is usable across its whole domain, including both endpoints
    curve.total_cost(curve.x0)
    curve.total_cost(curve.x_n)


def test_midpoint_interpolation_spans_the_full_input_domain_three_segments() -> None:
    # slopes: 10, 20, 30 $/MWh over [0,10], [10,20], [20,30]; midpoints 5, 15, 25.
    curve = from_total_cost((0, 10, 20, 30), (0, 100, 300, 600), interpolate_at_midpoints=True)
    assert curve.x0 == 0
    assert curve.x_n == 30
    assert [round(s.left_mw, 6) for s in curve.segments] == [0, 5, 15, 25]
    assert [round(s.right_mw, 6) for s in curve.segments] == [5, 15, 25, 30]
    assert curve.segments[0].is_flat and curve.segments[0].left_ic == 10
    assert curve.segments[-1].is_flat and curve.segments[-1].left_ic == 30
    # no gap: contiguous segments already assert this at construction, but
    # confirm explicitly that evaluating cost at the domain edges succeeds.
    curve.total_cost(curve.x0)
    curve.total_cost(curve.x_n)


def test_midpoint_interpolation_single_segment_still_spans_full_domain() -> None:
    curve = from_total_cost((0, 10), (0, 100), interpolate_at_midpoints=True)
    assert curve.x0 == 0
    assert curve.x_n == 10


# --- QP and LP paths agree in sign, for both curve roles ---


def _objective_via_lp_staircase(
    curve: CostCurve, p_mw: float, samples_per_segment: int = 20
) -> float:
    """Sum of LP-staircase step costs up to p_mw, for parity comparison with
    to_qp_segments / objective_cost."""
    total = 0.0
    remaining = p_mw - curve.x0
    for step in curve.to_lp_staircase(samples_per_segment=samples_per_segment):
        width = step.right_mw - step.left_mw
        take = max(0.0, min(width, remaining))
        total += step.ic * take
        remaining -= take
    return total


def test_qp_and_lp_agree_in_sign_for_cost_role() -> None:
    curve = from_incremental((0, 10, 20), (10, 14, 16))
    assert curve.curve_role == "cost"
    qp_total = sum(
        seg.a * seg.width_mw + seg.q / 2 * seg.width_mw**2 for seg in curve.to_qp_segments()
    )
    lp_total = _objective_via_lp_staircase(curve, curve.x_n)
    assert qp_total == pytest.approx(lp_total)
    assert qp_total == pytest.approx(curve.objective_cost(curve.x_n) - (curve.no_load_cost or 0.0))
    assert qp_total > 0  # a real production cost


def test_qp_and_lp_agree_in_sign_for_value_role() -> None:
    curve = from_incremental((0, 10, 20), (20, 15, 10), validate_as="demand")
    assert curve.curve_role == "value"
    qp_total = sum(
        seg.a * seg.width_mw + seg.q / 2 * seg.width_mw**2 for seg in curve.to_qp_segments()
    )
    lp_total = _objective_via_lp_staircase(curve, curve.x_n)
    assert qp_total == pytest.approx(lp_total)
    assert qp_total < 0  # dispatching this load offsets system cost, not adds to it
    assert qp_total == pytest.approx(curve.objective_cost(curve.x_n) - (curve.no_load_cost or 0.0))


def test_lp_staircase_negates_flat_value_segment() -> None:
    curve = CostCurve(
        segments=(Segment(left_mw=0, right_mw=10, left_ic=7, right_ic=7),),
        curve_role="value",
    )
    steps = curve.to_lp_staircase()
    assert steps[0].ic == -7


# --- total_cost / objective_cost sign convention ---


def test_total_cost_on_value_curve_is_positive_consumer_value() -> None:
    curve = from_incremental((0, 10, 20), (20, 15, 10), validate_as="demand", no_load_cost=0.0)
    assert curve.total_cost(20) > 0


def test_objective_cost_negates_total_cost_for_value_curves_only() -> None:
    supply = from_incremental((0, 10), (5, 5), no_load_cost=0.0)
    demand = from_incremental((0, 10), (5, 5), validate_as="demand", no_load_cost=0.0)
    assert supply.objective_cost(10) == pytest.approx(supply.total_cost(10))
    assert demand.objective_cost(10) == pytest.approx(-demand.total_cost(10))


# --- DispatchableLoad vs DemandResponse: opposite roles ---


def test_dispatchable_load_is_value_role_and_offsets_system_cost() -> None:
    # Consumes: negative injection, non-increasing WTP -> "value".
    dispatchable_load = from_incremental((0, 10), (30, 20), validate_as="demand")
    assert dispatchable_load.curve_role == "value"
    assert dispatchable_load.objective_cost(10) < 0


def test_demand_response_is_cost_role_and_increases_objective_with_dispatch() -> None:
    # DR curtails load: positive injection, compensation rises with MW
    # curtailed -> a supply-shaped curve, ingested with validate_as="supply",
    # exactly like a generator's cost curve.
    dr_compensation = from_incremental((0, 10, 20), (15, 25, 35))
    assert dr_compensation.curve_role == "cost"
    low = dr_compensation.objective_cost(5)
    high = dr_compensation.objective_cost(20)
    assert 0 < low < high  # more DR dispatched -> strictly more objective cost

    # QP segments confirm this isn't free capacity the solver would max out:
    # a >= 0 and q >= 0 for every segment, so marginal DR is never cheaper
    # than not calling it.
    for seg in dr_compensation.to_qp_segments():
        assert seg.a >= 0
        assert seg.q >= 0


def test_mistagging_dr_as_value_would_make_it_look_like_free_revenue() -> None:
    # Demonstrates the hazard named in the review: the *same* compensation
    # polyline, wrongly tagged "value", is negated and starts looking like
    # revenue instead of cost. This is why ingest ties curve_role to
    # validate_as instead of leaving it to the caller to set by hand.
    dr_compensation = from_incremental((0, 10, 20), (15, 25, 35))
    mistagged = dr_compensation.model_copy(update={"curve_role": "value"})
    assert dr_compensation.objective_cost(20) > 0
    assert mistagged.objective_cost(20) < 0
