"""Ingest paths that convert user input into the canonical `CostCurve` form
(CLAUDE.md "Cost curves"; SPEC §4.3).

Two modes:

- `from_incremental` — user supplies IC breakpoints directly. Interpolating
  by construction (consecutive breakpoints share a value), plus an optional
  `no_load_cost`.
- `from_fuel_cost` / `from_total_cost` — user supplies a PWL total-cost
  polyline (or a heat-rate curve x a fuel price, multiplied internally so a
  fuel-price update never forces curve re-entry). Differentiating a PWL
  total-cost polyline yields a *staircase* IC (constant per segment, non
  -interpolating) — this is the **exact derivative** and is the default.

  Midpoint-knot interpolation (placing each segment's slope at its midpoint
  `m_j = (x_{j-1}+x_j)/2` and connecting linearly, flat outside the outer
  midpoints) produces an interpolating IC curve instead, at the cost of
  being only approximately cost-preserving. It is an **explicit opt-in flag**
  (`interpolate_at_midpoints=True`), never a hidden ingest transform.

  Cubic/PCHIP interpolation is forbidden: it would make IC non-piecewise
  -linear, total cost non-piecewise-quadratic, and would break the
  diagonal-Hessian QP form HiGHS accepts (SPEC §4.2) — there is no cubic
  segment decomposition with a diagonal Q.

Every ingest path validates on construction (`validate_as="supply"` by
default; pass `"demand"` for demand-side curves) — never returns an
unvalidated curve.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from ed.curves.curve import CostCurve, Segment
from ed.curves.validators import validate_demand_concavity, validate_supply_convexity

ValidateAs = Literal["supply", "demand"]


def _check_strictly_ascending(breakpoints_mw: tuple[float, ...]) -> None:
    """Reject duplicate or descending breakpoints before any segment is built.

    A duplicate breakpoint (`right_mw == left_mw`) makes `L_j == 0`, and
    `Q_jj = (b_j - a_j) / L_j` (and the fuel-cost strategies' `slope =
    delta_cost / width`) divide by it. `Segment`'s own width check would
    eventually catch a duplicate too, but only *after* a bare
    `ZeroDivisionError` from the slope computation in the FUEL_COST ingest
    path — checking strict ascending order up front, before any division,
    gives one clear error instead. A simple `sorted() == breakpoints_mw`
    check is not sufficient here: a list with a duplicate is already sorted,
    so it would pass silently.
    """
    for i in range(len(breakpoints_mw) - 1):
        if breakpoints_mw[i + 1] <= breakpoints_mw[i]:
            raise ValueError(
                f"breakpoints_mw must be strictly ascending (no duplicate or descending "
                f"breakpoints); got {breakpoints_mw[i]} at index {i} followed by "
                f"{breakpoints_mw[i + 1]}"
            )


def _validate_and_tag(curve: CostCurve, validate_as: ValidateAs) -> CostCurve:
    """Run the matching validator, then stamp `curve_role` from the same
    `validate_as` so the role `to_qp_segments` uses for its sign convention
    can never drift from the validator that actually ran.
    """
    if validate_as == "supply":
        validate_supply_convexity(curve)
        role = "cost"
    else:
        validate_demand_concavity(curve)
        role = "value"
    return curve.model_copy(update={"curve_role": role})


def from_incremental(
    breakpoints_mw: tuple[float, ...],
    ic_values: tuple[float, ...],
    *,
    no_load_cost: float | None = None,
    validate_as: ValidateAs = "supply",
) -> CostCurve:
    """`INCREMENTAL` mode: IC breakpoints supplied directly, used as-is.

    Consecutive breakpoints share their IC value at the shared boundary, so
    the resulting curve is interpolating by construction. IC input loses the
    constant term (base point is invariant to it); pass `no_load_cost` to
    preserve a meaningful `total_cost()`.
    """
    if len(breakpoints_mw) != len(ic_values):
        raise ValueError("breakpoints_mw and ic_values must be the same length")
    if len(breakpoints_mw) < 2:
        raise ValueError("at least two breakpoints (one segment) are required")
    _check_strictly_ascending(breakpoints_mw)
    segments = tuple(
        Segment(
            left_mw=breakpoints_mw[i],
            right_mw=breakpoints_mw[i + 1],
            left_ic=ic_values[i],
            right_ic=ic_values[i + 1],
        )
        for i in range(len(breakpoints_mw) - 1)
    )
    curve = CostCurve(segments=segments, no_load_cost=no_load_cost)
    return _validate_and_tag(curve, validate_as)


class HeatRateCurve(BaseModel):
    """Total heat input (MMBtu/h) as a PWL function of output MW.

    Kept separate from fuel price so a price update never forces curve
    re-entry (SPEC §4.3) — `from_fuel_cost` multiplies the two at ingest
    time.
    """

    model_config = ConfigDict(frozen=True)

    breakpoints_mw: tuple[float, ...]
    heat_input_mmbtu_per_h: tuple[float, ...]

    @model_validator(mode="after")
    def _check_shape(self) -> HeatRateCurve:
        if len(self.breakpoints_mw) != len(self.heat_input_mmbtu_per_h):
            raise ValueError(
                "breakpoints_mw and heat_input_mmbtu_per_h must be the same length"
            )
        if len(self.breakpoints_mw) < 2:
            raise ValueError("at least two breakpoints (one segment) are required")
        _check_strictly_ascending(self.breakpoints_mw)
        return self


class FuelToIncrementalStrategy(Protocol):
    """Named, swappable strategy converting a PWL total-cost polyline into a
    canonical IC `CostCurve` (SPEC §4.3). Never invoked implicitly — the
    caller (`from_total_cost`/`from_fuel_cost`) selects one explicitly.
    """

    def build(
        self, breakpoints_mw: tuple[float, ...], cost_values: tuple[float, ...]
    ) -> CostCurve: ...


class ExactStaircaseStrategy:
    """Default: differentiate the PWL total-cost polyline exactly.

    Each segment's IC is the constant slope of that segment of total cost —
    a staircase (`left_ic == right_ic`), non-interpolating, and exactly
    cost-preserving by construction (it *is* the derivative of the input).
    """

    def build(
        self, breakpoints_mw: tuple[float, ...], cost_values: tuple[float, ...]
    ) -> CostCurve:
        segments = []
        for i in range(len(breakpoints_mw) - 1):
            width = breakpoints_mw[i + 1] - breakpoints_mw[i]
            slope = (cost_values[i + 1] - cost_values[i]) / width
            segments.append(
                Segment(
                    left_mw=breakpoints_mw[i],
                    right_mw=breakpoints_mw[i + 1],
                    left_ic=slope,
                    right_ic=slope,
                )
            )
        return CostCurve(segments=tuple(segments))


class MidpointInterpolationStrategy:
    """Explicit opt-in: place each segment's exact slope at its midpoint
    `m_j = (x_{j-1}+x_j)/2`, connect midpoints linearly, and extrapolate
    flat outside the outermost midpoints (SPEC §4.3).

    This yields an interpolating IC curve, at the cost of no longer being
    exactly cost-preserving relative to the original input polyline — that
    tradeoff is why `CostCurve.total_cost()` always integrates the
    *canonical* curve rather than falling back to the input, so reported
    cost has one source of truth regardless of which strategy built it.

    With a single input segment there is no interior structure to
    interpolate between two midpoints, so this degenerates to the same flat
    single segment `ExactStaircaseStrategy` would produce.
    """

    def build(
        self, breakpoints_mw: tuple[float, ...], cost_values: tuple[float, ...]
    ) -> CostCurve:
        n = len(breakpoints_mw) - 1
        slopes = [
            (cost_values[i + 1] - cost_values[i]) / (breakpoints_mw[i + 1] - breakpoints_mw[i])
            for i in range(n)
        ]
        if n == 1:
            return CostCurve(
                segments=(
                    Segment(
                        left_mw=breakpoints_mw[0],
                        right_mw=breakpoints_mw[-1],
                        left_ic=slopes[0],
                        right_ic=slopes[0],
                    ),
                )
            )
        midpoints = [(breakpoints_mw[i] + breakpoints_mw[i + 1]) / 2 for i in range(n)]
        segments = [
            Segment(
                left_mw=breakpoints_mw[0],
                right_mw=midpoints[0],
                left_ic=slopes[0],
                right_ic=slopes[0],
            )
        ]
        for i in range(n - 1):
            segments.append(
                Segment(
                    left_mw=midpoints[i],
                    right_mw=midpoints[i + 1],
                    left_ic=slopes[i],
                    right_ic=slopes[i + 1],
                )
            )
        segments.append(
            Segment(
                left_mw=midpoints[-1],
                right_mw=breakpoints_mw[-1],
                left_ic=slopes[-1],
                right_ic=slopes[-1],
            )
        )
        return CostCurve(segments=tuple(segments))


def from_total_cost(
    breakpoints_mw: tuple[float, ...],
    total_cost_values: tuple[float, ...],
    *,
    no_load_cost: float | None = None,
    interpolate_at_midpoints: bool = False,
    validate_as: ValidateAs = "supply",
) -> CostCurve:
    """`FUEL_COST` mode, direct total-cost form: user supplies total cost
    ($/h) as a PWL function of MW.

    `no_load_cost` defaults to the curve's own value at `x0`
    (`total_cost_values[0]`) when not given, since a total-cost input
    already carries an absolute anchor — unlike `INCREMENTAL` mode, this
    constant is not lost.
    """
    if len(breakpoints_mw) != len(total_cost_values):
        raise ValueError("breakpoints_mw and total_cost_values must be the same length")
    if len(breakpoints_mw) < 2:
        raise ValueError("at least two breakpoints (one segment) are required")
    _check_strictly_ascending(breakpoints_mw)
    strategy: FuelToIncrementalStrategy = (
        MidpointInterpolationStrategy() if interpolate_at_midpoints else ExactStaircaseStrategy()
    )
    curve = strategy.build(breakpoints_mw, total_cost_values)
    anchor = no_load_cost if no_load_cost is not None else total_cost_values[0]
    curve = curve.model_copy(update={"no_load_cost": anchor})
    return _validate_and_tag(curve, validate_as)


def from_fuel_cost(
    heat_rate: HeatRateCurve,
    fuel_price_per_mmbtu: float,
    *,
    no_load_cost: float | None = None,
    interpolate_at_midpoints: bool = False,
    validate_as: ValidateAs = "supply",
) -> CostCurve:
    """`FUEL_COST` mode, preferred separated form: heat-rate curve x fuel
    price, multiplied internally (SPEC §4.3) so a fuel-price update never
    forces curve re-entry.
    """
    total_cost_values = tuple(h * fuel_price_per_mmbtu for h in heat_rate.heat_input_mmbtu_per_h)
    return from_total_cost(
        heat_rate.breakpoints_mw,
        total_cost_values,
        no_load_cost=no_load_cost,
        interpolate_at_midpoints=interpolate_at_midpoints,
        validate_as=validate_as,
    )
