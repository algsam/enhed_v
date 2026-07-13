"""Cost-curve validators (CLAUDE.md "Cost curves"; SPEC §4.4).

Three separate validators, run on ingest and on every edit — never bypassed,
never merged into one function:

1. `validate_supply_convexity` — IC must be non-decreasing. A non-convex
   curve loads out of merit order and yields a wrong, non-physical dispatch.
2. `validate_demand_concavity` — willingness-to-pay must be non-increasing.
   The *mirror* condition. Reusing (1) here would reject every valid demand
   curve, so this is a genuinely separate function, not a wrapper.
3. `validate_price_ordering` — for bidirectional entities (BESS, tie-lines),
   `import_price >= export_price`, or the model has a free arbitrage.

All three reject by raising, identifying the offending segment where
applicable. None of them mutate the curve.
"""

from __future__ import annotations

from ed.curves.curve import CostCurve


class CurveConvexityError(ValueError):
    """Supply-side curve is not convex: IC decreases somewhere."""


class CurveConcavityError(ValueError):
    """Demand-side curve is not concave: willingness-to-pay increases somewhere."""


class PriceOrderingError(ValueError):
    """Bidirectional entity has import_price < export_price (free arbitrage)."""


def validate_supply_convexity(curve: CostCurve) -> None:
    """IC must be non-decreasing across the whole polyline (SPEC §4.4).

    Sufficient condition: `right_ic >= left_ic` within every segment (no
    downward segment), and `next.left_ic >= this.right_ic` at every
    boundary (no downward jump). Segment *slopes* need not increase across
    segments — only the IC value itself must never decrease.
    """
    for i, seg in enumerate(curve.segments):
        if seg.right_ic < seg.left_ic:
            raise CurveConvexityError(
                f"segment {i} [{seg.left_mw}, {seg.right_mw}] MW: IC decreases "
                f"within the segment ({seg.left_ic} -> {seg.right_ic}); supply IC "
                "must be non-decreasing"
            )
    for i in range(len(curve.segments) - 1):
        cur, nxt = curve.segments[i], curve.segments[i + 1]
        if nxt.left_ic < cur.right_ic:
            raise CurveConvexityError(
                f"segment {i}->{i + 1} boundary at {cur.right_mw} MW: IC drops "
                f"from {cur.right_ic} to {nxt.left_ic}; supply IC must be "
                "non-decreasing"
            )


def validate_demand_concavity(curve: CostCurve) -> None:
    """Willingness-to-pay must be non-increasing across the whole polyline.

    Mirror of `validate_supply_convexity`: `right_ic <= left_ic` within every
    segment, and `next.left_ic <= this.right_ic` at every boundary.
    """
    for i, seg in enumerate(curve.segments):
        if seg.right_ic > seg.left_ic:
            raise CurveConcavityError(
                f"segment {i} [{seg.left_mw}, {seg.right_mw}] MW: WTP increases "
                f"within the segment ({seg.left_ic} -> {seg.right_ic}); demand "
                "WTP must be non-increasing"
            )
    for i in range(len(curve.segments) - 1):
        cur, nxt = curve.segments[i], curve.segments[i + 1]
        if nxt.left_ic > cur.right_ic:
            raise CurveConcavityError(
                f"segment {i}->{i + 1} boundary at {cur.right_mw} MW: WTP rises "
                f"from {cur.right_ic} to {nxt.left_ic}; demand WTP must be "
                "non-increasing"
            )


def validate_price_ordering(import_price: float, export_price: float) -> None:
    """`import_price >= export_price`, or the entity can import and export
    simultaneously for a risk-free profit (SPEC §4.4). Covers BESS
    (charge_cost >= discharge_revenue) and tie-lines alike.
    """
    if import_price < export_price:
        raise PriceOrderingError(
            f"import_price ({import_price}) < export_price ({export_price}): "
            "this is a free arbitrage"
        )
