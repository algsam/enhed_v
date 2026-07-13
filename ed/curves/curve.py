"""CostCurve — the canonical piecewise-linear incremental-cost form (CLAUDE.md
"Cost curves", SPEC §4).

The canonical internal representation is a list of segments carrying
`(left_value, right_value)` — the IC ($/MWh) at the left and right edge of
each MW-width segment. A staircase is the special case `left == right` (a
pure LP/constant-IC segment, `Q_jj = 0`); interpolating is `left != right`.
There is exactly one code path for both — nothing here branches on curve
"type". Whether a curve is a staircase is a per-segment fact you can read off
`Segment.is_flat`, not a mode stored on `CostCurve`.

Two representations are derivable from the same `CostCurve` object, never
duplicated by hand:
  - `to_qp_segments()` — the diagonal-Hessian QP decomposition (SPEC §4.2).
  - `to_lp_staircase()` — a sampled constant-IC staircase, for the pure-LP
    fallback path (CLAUDE.md "Segment variables need no ordering
    constraints"; SPEC §9 "LP fallback path").

`total_cost()` always integrates *this* canonical curve, never an original
input curve — see `ed.curves.ingest` for why that matters for FUEL_COST
ingest (midpoint interpolation is not exactly cost-preserving).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class NonPSDSegmentError(ValueError):
    """`to_qp_segments` produced a segment with `Q_jj < 0`.

    HiGHS's QP path requires a PSD (here diagonal, so elementwise
    non-negative) Hessian; a negative entry would be silently rejected or
    produce a non-convex solve. This should be unreachable for any curve
    that was actually validated against the `curve_role` it claims — see
    `CostCurve.to_qp_segments`.
    """


class Segment(BaseModel):
    """One MW-width span of the canonical IC polyline.

    `left_ic`/`right_ic` are $/MWh at `left_mw`/`right_mw`. `left_ic ==
    right_ic` is the staircase (constant-IC) special case; a validator
    downstream (supply convexity / demand concavity) decides whether the
    *direction* of `right_ic - left_ic` is acceptable — this class only
    enforces that the segment has positive width.
    """

    model_config = ConfigDict(frozen=True)

    left_mw: float
    right_mw: float
    left_ic: float
    right_ic: float

    @model_validator(mode="after")
    def _check_width(self) -> Segment:
        if self.right_mw <= self.left_mw:
            raise ValueError(
                f"segment [{self.left_mw}, {self.right_mw}]: right_mw must exceed left_mw"
            )
        return self

    @property
    def width_mw(self) -> float:
        return self.right_mw - self.left_mw

    @property
    def is_flat(self) -> bool:
        """The staircase special case: left == right, Q_jj == 0."""
        return self.left_ic == self.right_ic

    @property
    def slope(self) -> float:
        return (self.right_ic - self.left_ic) / self.width_mw

    def cost_of_fill(self, width_mw: float) -> float:
        """Cost of filling `width_mw` MW from this segment's left edge.

        `cost_j(p_j) = a_j * p_j + (b_j - a_j) / (2 * L_j) * p_j**2` (SPEC
        §4.2) — the exact integral of the linear IC over [left_mw, left_mw +
        width_mw], valid for any 0 <= width_mw <= width_mw of the segment.
        """
        return self.left_ic * width_mw + self.slope / 2.0 * width_mw * width_mw


class CostCurve(BaseModel):
    """The canonical PWL incremental-cost curve: an ordered, contiguous list
    of `Segment`s, plus an optional no-load cost anchored at `x_0`.

    `no_load_cost` is optional (CLAUDE.md: incremental-cost input loses the
    constant term). When `None`, `total_cost()` still returns a value, but it
    is only correct up to an additive constant — callers doing settlement
    must check `has_absolute_cost` rather than silently trusting the number.

    `curve_role` records which of the two mirror-image validators (SPEC
    §4.4) this curve's polyline was shaped for, and is what `to_qp_segments`
    uses to decide the QP sign convention — see that method's docstring.
    `"cost"` (default) is a supply-side IC curve (non-decreasing); `"value"`
    is a demand-side willingness-to-pay curve (non-increasing). Ingest
    (`ed.curves.ingest`) sets this from `validate_as` so the role and the
    validator that actually ran can never drift apart.
    """

    model_config = ConfigDict(frozen=True)

    segments: tuple[Segment, ...]
    no_load_cost: float | None = None
    curve_role: Literal["cost", "value"] = "cost"

    @model_validator(mode="after")
    def _check_contiguous(self) -> CostCurve:
        if len(self.segments) == 0:
            raise ValueError("CostCurve requires at least one segment")
        for i in range(len(self.segments) - 1):
            if self.segments[i].right_mw != self.segments[i + 1].left_mw:
                raise ValueError(
                    f"segments {i} and {i + 1} are not contiguous: "
                    f"{self.segments[i].right_mw} != {self.segments[i + 1].left_mw}"
                )
        return self

    @property
    def x0(self) -> float:
        return self.segments[0].left_mw

    @property
    def x_n(self) -> float:
        return self.segments[-1].right_mw

    @property
    def has_absolute_cost(self) -> bool:
        return self.no_load_cost is not None

    def to_qp_segments(self) -> tuple[QPSegment, ...]:
        """The diagonal-Hessian QP decomposition (SPEC §4.2).

        Sign convention — this is decided by `curve_role`, *not* by the
        entity's injection sign. The segment-fill variable `p_j` the solver
        sees is always `p_j >= 0` (MW filled within the segment), exactly as
        for a generator, regardless of whether the owning entity's injection
        into the balance row is `+p_j` or `-p_j` (SPEC §5.6). That external
        sign is applied only when the model builder couples `sum(p_j)` into
        the balance row — never here.

        - `curve_role="cost"` (supply IC curves, validated non-decreasing):
          used as-is. `c_j = a_j`, `Q_jj = (b_j - a_j) / L_j`, literally the
          segment's IC slope. Non-decreasing IC guarantees `Q_jj >= 0`.
        - `curve_role="value"` (demand/WTP curves, validated non-increasing —
          e.g. `DispatchableLoad`, which *consumes* and whose injection is
          negative): the objective is `min sum(cost)`, and consuming has
          *value*, not cost, so this segment's cost is the negative of
          consumer value —
              cost_j(p_j) = -integral_0^p_j WTP(left + x) dx
                          = (-a_j) * p_j + (-(b_j - a_j) / L_j) / 2 * p_j**2
          giving `c_j = -a_j`, `Q_jj = -(b_j - a_j) / L_j` — the *negation*
          of the raw polyline's left value and slope. Non-increasing WTP
          means `(b_j - a_j) / L_j <= 0`, so the negated `Q_jj >= 0`.

        `DemandResponse` is **not** a `"value"` curve, despite also being a
        demand-side resource — it *curtails* load (SPEC §5.6: injection
        `+P`, "pay DR compensation"), and compensation rises with MW
        curtailed. That is a `"cost"` curve like any generator's, ingested
        with `validate_as="supply"`. Tagging DR `"value"` would negate its
        compensation cost into apparent revenue, and the QP would then want
        to call the maximum available DR on every solve — this is exactly
        the class of mistake `curve_role` and the assertion below exist to
        catch before it reaches the objective.

        Either branch lands on `Q_jj >= 0`; a curve built with a role that
        doesn't match the shape it was actually validated against (e.g. a
        "value" curve whose WTP was never checked non-increasing) is exactly
        the mistake that would hand HiGHS a non-PSD diagonal and get the QP
        rejected. The assertion below is the last line of defense against
        that mistake reaching the solver.
        """
        sign = -1.0 if self.curve_role == "value" else 1.0
        segments = tuple(
            QPSegment(
                index=i,
                left_mw=seg.left_mw,
                width_mw=seg.width_mw,
                a=sign * seg.left_ic,
                q=sign * seg.slope,
            )
            for i, seg in enumerate(self.segments)
        )
        for qp in segments:
            if qp.q < 0:
                raise NonPSDSegmentError(
                    f"segment {qp.index} [{qp.left_mw}, {qp.left_mw + qp.width_mw}] MW: "
                    f"Q_jj={qp.q} < 0 for curve_role={self.curve_role!r}; the Hessian "
                    "would be non-PSD and HiGHS would reject this QP"
                )
        return segments

    def to_lp_staircase(self, samples_per_segment: int = 1) -> tuple[LPStep, ...]:
        """A constant-IC staircase sampled from this curve, for the pure-LP
        fallback path (SPEC §9).

        A segment that is already flat (`is_flat`) is emitted as a single
        exact step regardless of `samples_per_segment` — subdividing a
        constant would add rows for no benefit. An interpolating segment is
        split into `samples_per_segment` equal-width steps, each priced at
        its sub-step's *midpoint* IC: since IC is linear within a segment,
        width * midpoint-IC exactly equals that sub-step's true integral, so
        the staircase's total cost matches the canonical curve's exactly
        regardless of resolution — only the price *signal* within a segment
        is approximated, not the total cost.

        Applies the **same `curve_role` sign convention as `to_qp_segments`**
        (each step's `ic` is negated when `curve_role == "value"`). Both
        methods come off the same `CostCurve` object and Stage 4's QP/LP
        parity gate compares them directly — a sign disagreement here would
        not fail loudly, it would silently present as a balance-module
        pricing bug (the LP fallback path calling a "value" curve to its
        full extent for apparent profit while the QP path correctly refuses
        to).
        """
        if samples_per_segment < 1:
            raise ValueError("samples_per_segment must be >= 1")
        sign = -1.0 if self.curve_role == "value" else 1.0
        steps: list[LPStep] = []
        for seg in self.segments:
            if seg.is_flat:
                steps.append(
                    LPStep(left_mw=seg.left_mw, right_mw=seg.right_mw, ic=sign * seg.left_ic)
                )
                continue
            step_width = seg.width_mw / samples_per_segment
            for k in range(samples_per_segment):
                left = seg.left_mw + k * step_width
                right = left + step_width
                mid_ic = seg.left_ic + seg.slope * ((left + right) / 2.0 - seg.left_mw)
                steps.append(LPStep(left_mw=left, right_mw=right, ic=sign * mid_ic))
        return tuple(steps)

    def incremental_cost_area(self, p_mw: float) -> float:
        """Integral of the canonical IC curve from `x0` to `p_mw` — the pure
        variable-cost area, with no no-load anchor added.

        **Sign convention: this is the raw integral of the curve's own
        values, unsigned by `curve_role`.** For a `"cost"` curve that is a
        production cost, as the name says. For a `"value"` curve it is
        *consumer value* (the WTP integral, positive when the load derives
        benefit) — not a system cost, and not negated. This is deliberate:
        it is the number an operator display should show for "value of load
        served," positive. It is *not* the number to add into a system-cost
        total — use `objective_cost` for that.
        """
        if not (self.x0 <= p_mw <= self.x_n):
            raise ValueError(f"p_mw={p_mw} is outside curve domain [{self.x0}, {self.x_n}]")
        area = 0.0
        for seg in self.segments:
            if p_mw >= seg.right_mw:
                area += seg.cost_of_fill(seg.width_mw)
            elif p_mw > seg.left_mw:
                area += seg.cost_of_fill(p_mw - seg.left_mw)
                break
            else:
                break
        return area

    def total_cost(self, p_mw: float) -> float:
        """Reported production/value at `p_mw`, always by integrating *this*
        canonical curve (CLAUDE.md: never the original input curve).

        Anchored at `x0` via `no_load_cost` when present (else the constant
        term is simply 0, and the result is only meaningful up to an
        additive constant — see `has_absolute_cost`).

        Same sign convention as `incremental_cost_area`: unsigned by
        `curve_role`. A `"value"` curve's `total_cost()` is positive
        consumer value, not a negative system cost — see `objective_cost`.
        """
        return self.incremental_cost_area(p_mw) + (self.no_load_cost or 0.0)

    def objective_cost(self, p_mw: float) -> float:
        """The signed contribution to the solver's objective at `p_mw` —
        i.e. `total_cost(p_mw)` with the same `curve_role` sign flip
        `to_qp_segments`/`to_lp_staircase` apply.

        This is the number any system-cost aggregation must use, never
        `total_cost()` directly: for a `"value"` (demand/WTP) curve,
        `total_cost()` is positive consumer value, but that value *offsets*
        system cost rather than adding to it (a dispatched load is revenue
        to the system, matching `to_qp_segments`' `cost_j = -integral(WTP)`)
        — so `objective_cost` is `-total_cost()` for `"value"` curves, and
        exactly `total_cost()` for `"cost"` curves. Summing `objective_cost`
        across every entity is what should equal the solver's objective
        value; summing raw `total_cost()` would silently inflate it by
        double-counting every dispatched load as a cost instead of a
        cost offset.
        """
        sign = -1.0 if self.curve_role == "value" else 1.0
        return sign * self.total_cost(p_mw)


@dataclass(frozen=True)
class QPSegment:
    """One segment's contribution to the diagonal-Hessian QP objective."""

    index: int
    left_mw: float
    width_mw: float
    a: float
    q: float


@dataclass(frozen=True)
class LPStep:
    """One constant-IC step of a sampled LP staircase."""

    left_mw: float
    right_mw: float
    ic: float
