"""Disaggregator protocol + default strategies (SPEC §6, build order step 5)."""

from __future__ import annotations

from ed.disagg.protocol import (
    AggregateRamp,
    Disaggregator,
    SplitValidationError,
    UnitId,
    validate_split_result,
)
from ed.disagg.range_pro_rata import PmaxProRata, RangeProRata

__all__ = [
    "AggregateRamp",
    "Disaggregator",
    "PmaxProRata",
    "RangeProRata",
    "SplitValidationError",
    "UnitId",
    "validate_split_result",
]
