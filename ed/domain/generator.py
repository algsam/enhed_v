"""Generator — the standalone-thermal case of `DispatchableEntity` (SPEC
§5.1: "A standalone thermal unit is both PhysicalUnit and
DispatchableEntity").

This is the minimal concrete resource needed to drive the first
end-to-end dispatch (build order step 4): a pure signed-injection
generator backed directly by a canonical `CostCurve`, decomposed into QP
segments exactly per SPEC §4.2. It carries no ramp/reserve behavior yet —
those are later build-order steps (§13 steps 5, 7) layered on via the same
capability protocols, not by editing this class's contribution contract.

Deliberately *not* wired to `PhysicalUnit`/`ResourceType` here: associating
a Generator with its owning `PhysicalUnit` and identity metadata is
`entities/build_entities()`'s job (build order step 6). This class only
needs to satisfy `DispatchableEntity` so `ModelBuilder` can dispatch it —
type must never determine math (CLAUDE.md Architecture), and this class
has none to determine: it is the pure signed-injection + convex-cost shape
every resource ultimately reduces to (SPEC §5.6 admission test).
"""

from __future__ import annotations

from ed.curves.curve import CostCurve
from ed.model.context import BuildContext
from ed.solver import SolveResult, VarHandle


class Generator:
    """A signed-injection resource whose cost is a `CostCurve`, segment-decomposed
    into the diagonal-Hessian QP form (SPEC §4.2). Injection sign is `+1`
    (a generator is the special case `lower >= 0`, SPEC §5.6).
    """

    def __init__(self, bus: str, cost_curve: CostCurve) -> None:
        self.bus = bus
        self.cost_curve = cost_curve
        self._segment_vars: tuple[VarHandle, ...] = ()

    def contribute_variables(self, ctx: BuildContext) -> tuple[VarHandle, ...]:
        segment_vars = []
        for qp in self.cost_curve.to_qp_segments():
            var = ctx.adapter.add_var(
                cost=qp.a, lower=0.0, upper=qp.width_mw, hessian_diag=qp.q
            )
            ctx.add_injection(self.bus, var, coefficient=1.0)
            segment_vars.append(var)
        self._segment_vars = tuple(segment_vars)
        return self._segment_vars

    def contribute_constraints(self, ctx: BuildContext) -> None:
        """No constraints of its own yet: ramp/limit rows are §13 step 5/7."""
        return None

    def contribute_cost(self, ctx: BuildContext) -> None:
        """No-op: cost and Hessian are already attached per-segment at
        `contribute_variables` time, since HiGHS's `add_var` takes both
        together. This method exists so the uniform three-call contract
        (SPEC §5.4) holds for every entity, including future ones whose
        cost is not fully known until after all variables exist.
        """
        return None

    def dispatch_mw(self, result: SolveResult) -> float:
        """Total MW dispatched: the sum of this generator's segment fills."""
        return sum(result.primal[v] for v in self._segment_vars)
