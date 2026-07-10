"""Capability protocols (SPEC §5.4).

Dispatch behavior is expressed as composable capabilities rather than deep
inheritance. A concrete entity implements whichever of these it needs; the
model builder consumes them structurally (via isinstance-against-Protocol or
hasattr checks made available to modules, never as a type switch).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RampLimited(Protocol):
    """Contributes ramp constraints anchored to measured telemetry (SPEC §7.3)."""

    def ramp_up_limit_mw(self, dt_min: float) -> float:
        """Maximum MW increase over an interval of length dt_min, from P0."""
        ...

    def ramp_down_limit_mw(self, dt_min: float) -> float:
        """Maximum MW decrease over an interval of length dt_min, from P0."""
        ...


@runtime_checkable
class ReserveCapable(Protocol):
    """May contribute reserve variables and headroom coupling (SPEC §8).

    `reserve_eligible` is always an explicit, per-entity fact — never inferred
    from resource_type (CLAUDE.md, SPEC §8).
    """

    reserve_eligible: bool

    def reserve_headroom_mw(self, product: str, t_reserve_min: float) -> float:
        """Deliverable headroom for `product` within the reserve window.

        Aggregate-headroom reserve caps contribution by deliverability:
        min(Pmax - P, RampRate * T_reserve).
        """
        ...


@runtime_checkable
class ForecastLimited(Protocol):
    """Renewables: Pmax is a forecast *upper bound*, not an equality (curtailable)."""

    def forecast_mw(self, t: int) -> float: ...


@runtime_checkable
class MustRun(Protocol):
    """Nuclear: bounds pinned to a fixed schedule, excluded from regulation/reserve."""

    def scheduled_mw(self, t: int) -> float: ...


@runtime_checkable
class StorageCapable(Protocol):
    """BESS: bidirectional, inter-temporal state of energy (SPEC §5.5)."""

    charge_efficiency: float
    discharge_efficiency: float

    def state_of_energy_mwh(self) -> float: ...


@runtime_checkable
class SignedInjection(Protocol):
    """An entity whose injection may go negative (SPEC §5.6): ties, DR, loads, slack."""

    def injection_bounds_mw(self, t: int) -> tuple[float, float]:
        """(lower, upper) bound on signed injection at interval t.

        A generator is the special case lower >= 0.
        """
        ...
