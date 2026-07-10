"""Identity and state enums for the domain layer.

Identity (ResourceType) and model contribution are orthogonal axes (SPEC §5.4):
ResourceType drives filtering/reporting/UI; it must never determine math in the
model builder.
"""

from __future__ import annotations

from enum import Enum, auto


class ResourceType(Enum):
    """First-class identity of a resource. See SPEC §5.4."""

    THERMAL = auto()
    STEAM = auto()
    NUCLEAR = auto()
    RENEWABLE = auto()
    BESS = auto()
    CC_BLOCK = auto()
    TIE_LINE = auto()
    DEMAND_RESPONSE = auto()
    DISPATCHABLE_LOAD = auto()
    SLACK = auto()


class Mode(Enum):
    """Operating mode that selects a unit's mode-keyed characteristics (SPEC §5.3).

    A CT running standalone (SIMPLE_CYCLE) is economically a different unit than
    the same CT contributing to an active CC config (COMBINED_CYCLE): different
    heat rate, Pmax, ramp rates, emissions rate.
    """

    SIMPLE_CYCLE = auto()
    COMBINED_CYCLE = auto()


class SteamSource(Enum):
    """Three-valued steam source for a steam turbine (SPEC §5.4).

    Ship v1 with AUX_BOILER present but unreachable (can_run_standalone is a
    runtime predicate against context, never a constant on the class) — do not
    ship a boolean and widen it later.
    """

    NONE = auto()
    HRSG = auto()
    AUX_BOILER = auto()
