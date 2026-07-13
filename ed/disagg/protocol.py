"""Disaggregator protocol — CC-block setpoint splitting (SPEC §6, build order
step 5).

A `Disaggregator` is a pluggable post-processor, fully decoupled from the
solve: the optimizer only ever sees one `DispatchableEntity` per CC block
(SPEC §5.1 — "The optimizer iterates over DispatchableEntity and never knows
CC blocks exist"). This module owns the seam that turns that single base
point back into per-`PhysicalUnit` setpoints, plus the aggregate envelope
(`Pmin_e`, `Pmax_e`, `RU_e`, `RD_e`) the entity's own constraints are built
from.

All three responsibilities — `split`, `aggregate_limits`, `aggregate_ramp` —
live on one interface deliberately (SPEC §6.1): the rule that splits MW is
the same rule that determines how fast the aggregate can move. Separating
them into different classes would let someone swap the splitter and
silently invalidate the ramp limits derived from it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ed.domain.physical_unit import PhysicalUnit

UnitId = str


class SplitValidationError(ValueError):
    """`split()` output does not sum to the entity base point, or assigns a
    member unit a setpoint outside its own `[Pmin, Pmax]` (SPEC §6.1: "The
    interface must validate that split() outputs sum to the entity base
    point and lie within each unit's limits, so a future custom splitter
    cannot silently produce an infeasible setpoint vector.").
    """


def validate_split_result(
    entity_mw: float,
    units: Sequence[PhysicalUnit],
    split_mw: Mapping[UnitId, float],
    tol: float = 1e-6,
) -> None:
    """SPEC §6.1's split() validation, as a standalone function so it can be
    called both by a strategy that means to guarantee it (`RangeProRata`,
    every call) and directly by a test demonstrating a strategy that does
    *not* (`PmaxProRata`, SPEC §6.2).

    Checks the two properties SPEC §6.1 names: the split sums exactly to
    `entity_mw`, and every unit's assigned MW lies within its own
    `[Pmin, Pmax]`.
    """
    total = sum(split_mw.values())
    if abs(total - entity_mw) > tol:
        raise SplitValidationError(f"split sums to {total}, not entity_mw={entity_mw}")
    for unit in units:
        p_i = split_mw[unit.unit_id]
        pmin = unit.active_characteristics.pmin_mw
        pmax = unit.active_characteristics.pmax_mw
        if not (pmin - tol <= p_i <= pmax + tol):
            raise SplitValidationError(
                f"unit {unit.unit_id}: split assigns {p_i} MW, outside [{pmin}, {pmax}]"
            )


@dataclass(frozen=True)
class AggregateRamp:
    """Result of `Disaggregator.aggregate_ramp` (SPEC §6.3).

    SPEC §6.1 sketches this method's return type as a bare
    `tuple[float, float]`; that sketch omits the operator diagnostic SPEC
    §6.3 and CLAUDE.md both require whenever the drift-aware clamp bites
    ("Surface this as an operator diagnostic — do not let it go negative
    and make the QP infeasible."). This dataclass carries the two rate
    scalars *and* that diagnostic together, so the clamp can never silently
    vanish at a call site the way it could if `diagnostics` were an
    easily-ignored third return value.
    """

    ru_mw_per_min: float
    rd_mw_per_min: float
    clamped_up: bool
    clamped_down: bool
    diagnostics: tuple[str, ...] = ()


@runtime_checkable
class Disaggregator(Protocol):
    """Pluggable CC-block setpoint splitter (SPEC §6.1)."""

    def split(self, entity_mw: float, units: Sequence[PhysicalUnit]) -> dict[UnitId, float]:
        """Allocate the entity's base point across its member units."""
        ...

    def aggregate_limits(self, units: Sequence[PhysicalUnit]) -> tuple[float, float]:
        """`(Pmin_e, Pmax_e)`: the aggregate envelope implied by the members'
        own limits, for validating against the config curve's domain
        (CLAUDE.md domain rules: `sum(Pmin_units) <= x_0` and
        `x_n <= sum(Pmax_units)`)."""
        ...

    def aggregate_ramp(
        self,
        units: Sequence[PhysicalUnit],
        telemetry: Mapping[UnitId, float],
        dt_min: float,
    ) -> AggregateRamp:
        """`(RU_e, RD_e)`, drift-aware (SPEC §6.3).

        `telemetry` is each member's *measured* current output `P_i^0`,
        supplied fresh every cycle — kept separate from `units` (which
        carry the static per-mode `Pmin`/`Pmax`/ramp-curve configuration)
        because ramp feasibility must be checked from what the field
        reports this cycle, never a remembered or scheduled value
        (CLAUDE.md domain rules; SPEC §6.4).
        """
        ...
