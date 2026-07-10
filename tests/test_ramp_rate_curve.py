"""Tests for RampRateCurve (CLAUDE.md Domain rules: ramp rate is a curve vs MW)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ed.domain.ramp import (
    RampRateCurve,
    resolve_ramp_down_mw_per_min,
    resolve_ramp_up_mw_per_min,
)


def test_constant_rate_applies_everywhere() -> None:
    curve = RampRateCurve.constant(5.0, pmin_mw=0)
    assert curve.rate_at_mw(0) == 5.0
    assert curve.rate_at_mw(100) == 5.0


def test_multi_segment_curve_picks_the_applicable_segment() -> None:
    curve = RampRateCurve(breakpoints_mw=(0, 50, 100), rates_mw_per_min=(2, 5, 3))
    assert curve.rate_at_mw(10) == 2
    assert curve.rate_at_mw(50) == 5
    assert curve.rate_at_mw(75) == 5
    assert curve.rate_at_mw(150) == 3


def test_negative_rate_rejected() -> None:
    with pytest.raises(ValidationError):
        RampRateCurve(breakpoints_mw=(0,), rates_mw_per_min=(-1,))


def test_mismatched_lengths_rejected() -> None:
    with pytest.raises(ValidationError):
        RampRateCurve(breakpoints_mw=(0, 50), rates_mw_per_min=(1,))


def test_unsorted_breakpoints_rejected() -> None:
    with pytest.raises(ValidationError):
        RampRateCurve(breakpoints_mw=(50, 0), rates_mw_per_min=(1, 2))


# --- conservative-min resolution (SPEC §6.3 amendment) ---


def test_one_segment_curve_reproduces_the_scalar_case_exactly() -> None:
    curve = RampRateCurve.constant(5.0, pmin_mw=0)
    assert resolve_ramp_up_mw_per_min(curve, p0_mw=40, dt_min=5) == 5.0
    assert resolve_ramp_down_mw_per_min(curve, p0_mw=40, dt_min=5) == 5.0


def test_band_entirely_within_one_segment_uses_that_segments_rate() -> None:
    # P0=10, up to 8 MW/min: band is [10, 50], entirely inside the [0, 100) segment.
    curve = RampRateCurve(breakpoints_mw=(0, 100), rates_mw_per_min=(8, 3))
    assert resolve_ramp_up_mw_per_min(curve, p0_mw=10, dt_min=5) == 8.0


def test_band_crossing_into_a_slower_segment_gets_the_slower_rate() -> None:
    # P0=90, point rate 8 MW/min => naive band [90, 130] crosses the breakpoint
    # at 100 into the 3 MW/min segment. The unit cannot sustain 8 MW/min all
    # the way to 130; the resolved rate must be the conservative 3, not 8.
    curve = RampRateCurve(breakpoints_mw=(0, 100), rates_mw_per_min=(8, 3))
    assert resolve_ramp_up_mw_per_min(curve, p0_mw=90, dt_min=5) == 3.0


def test_ramp_down_band_crossing_into_a_slower_segment_gets_the_slower_rate() -> None:
    # Mirror of the up case: P0=10, down-rate 8 below 0 doesn't apply here —
    # use a curve where the slow segment is below P0's naive down-band.
    curve = RampRateCurve(breakpoints_mw=(0, 10), rates_mw_per_min=(2, 8))
    # point rate at P0=15 is 8 MW/min => naive down-band [15 - 40, 15] = [-25, 15]
    # crosses the breakpoint at 10 into the 2 MW/min segment below it.
    assert resolve_ramp_down_mw_per_min(curve, p0_mw=15, dt_min=5) == 2.0


def test_min_rate_over_range_matches_point_rate_for_zero_width_range() -> None:
    curve = RampRateCurve(breakpoints_mw=(0, 100), rates_mw_per_min=(8, 3))
    assert curve.min_rate_over_range(50, 50) == curve.rate_at_mw(50)
