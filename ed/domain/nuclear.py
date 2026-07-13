"""Nuclear — must-run, pinned to schedule (SPEC §5.4, §7 constraint 5;
CLAUDE.md "Domain rules"; build order step 8).

`P = scheduled` is a **hard bound** (`lower == upper == scheduled_mw`), not a
soft preference: a single variable pinned to one value, contributing
`+1 * scheduled_mw` of injection unconditionally. Excluded from regulation
and reserve — `reserve_eligible` is fixed `False` here (never a constructor
argument), matching SPEC §8 ("Nuclear: false") as a hard rule of this class,
not a per-instance choice the way it is for ties/DR.
"""

from __future__ import annotations

from ed.domain.enums import ResourceType
from ed.model.context import BuildContext
from ed.solver import SolveResult, VarHandle


class Nuclear:
    """A must-run resource: `resource_type=NUCLEAR`, pinned to
    `scheduled_mw`, never reserve-eligible."""

    resource_type = ResourceType.NUCLEAR
    is_system_generated = False
    emits_setpoint = True
    reserve_eligible = False

    def __init__(self, bus: str, scheduled_mw: float) -> None:
        if scheduled_mw < 0.0:
            raise ValueError(f"scheduled_mw={scheduled_mw} must be non-negative")
        self.bus = bus
        self.scheduled = scheduled_mw
        self._var: VarHandle | None = None

    def scheduled_mw(self, t: int) -> float:
        """`MustRun` capability. v1 carries only the singleton interval `t=0`."""
        if t != 0:
            raise ValueError(f"t={t}: v1 carries only the singleton interval t=0")
        return self.scheduled

    def contribute_variables(self, ctx: BuildContext) -> tuple[VarHandle, ...]:
        var = ctx.adapter.add_var(cost=0.0, lower=self.scheduled, upper=self.scheduled)
        ctx.add_injection(self.bus, var, coefficient=1.0)
        self._var = var
        return (var,)

    def contribute_constraints(self, ctx: BuildContext) -> None:
        return None

    def contribute_cost(self, ctx: BuildContext) -> None:
        return None

    def dispatch_mw(self, result: SolveResult) -> float:
        if self._var is None:
            raise RuntimeError("contribute_variables() must run before dispatch_mw()")
        return result.primal[self._var]
