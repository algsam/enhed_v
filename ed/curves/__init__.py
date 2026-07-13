"""Canonical cost-curve representation, ingest, and validation (SPEC §4)."""

from __future__ import annotations

from ed.curves.curve import CostCurve, LPStep, NonPSDSegmentError, QPSegment, Segment
from ed.curves.ingest import (
    ExactStaircaseStrategy,
    FuelToIncrementalStrategy,
    HeatRateCurve,
    MidpointInterpolationStrategy,
    from_fuel_cost,
    from_incremental,
    from_total_cost,
)
from ed.curves.validators import (
    CurveConcavityError,
    CurveConvexityError,
    PriceOrderingError,
    validate_demand_concavity,
    validate_price_ordering,
    validate_supply_convexity,
)

__all__ = [
    "CostCurve",
    "Segment",
    "QPSegment",
    "LPStep",
    "NonPSDSegmentError",
    "HeatRateCurve",
    "FuelToIncrementalStrategy",
    "ExactStaircaseStrategy",
    "MidpointInterpolationStrategy",
    "from_incremental",
    "from_total_cost",
    "from_fuel_cost",
    "validate_supply_convexity",
    "validate_demand_concavity",
    "validate_price_ordering",
    "CurveConvexityError",
    "CurveConcavityError",
    "PriceOrderingError",
]
