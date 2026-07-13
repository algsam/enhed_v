"""Generator — the standalone-thermal case of `DispatchableEntity` (SPEC
§5.1: "A standalone thermal unit is both PhysicalUnit and
DispatchableEntity").

This is the minimal concrete resource needed to drive the first
end-to-end dispatch (build order step 4): a pure signed-injection
generator backed directly by a canonical `CostCurve`, decomposed into QP
segments exactly per SPEC §4.2. It carries no ramp-*constraint* behavior
yet (that is a later build-order step), but does carry the reserve-facing
data `ReserveModule` needs (build order step 7): `reserve_eligible`
(explicit per CLAUDE.md "Domain rules", never inferred from
resource_type) and a resolved `ramp_up_mw_per_min` scalar for the
deliverability cap (SPEC §6.3 amendment: resolved once per cycle from
measured P0 — this class takes the already-resolved scalar, it does not
resolve a `RampRateCurve` itself).

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

    def __init__(
        self,
        bus: str,
        cost_curve: CostCurve,
        *,
        reserve_eligible: bool = False,
        ramp_up_mw_per_min: float | None = None,
    ) -> None:
        if reserve_eligible and ramp_up_mw_per_min is None:
            raise ValueError(
                "reserve_eligible=True requires ramp_up_mw_per_min: the deliverability "
                "cap is mandatory (CLAUDE.md 'Domain rules') — without it a reserve "
                "module would reserve MW this unit cannot actually reach in time"
            )
        self.bus = bus
        self.cost_curve = cost_curve
        self.reserve_eligible = reserve_eligible
        self.ramp_up_mw_per_min = ramp_up_mw_per_min
        self._segment_vars: tuple[VarHandle, ...] = ()

    @property
    def capacity_mw(self) -> float:
        """Total dispatchable range (`Pmax - Pmin`), i.e. the curve's own
        width — the same relative scale `energy_vars()` sums to (SPEC §8's
        `Pmax_e` headroom bound, expressed relative to `Pmin_e` since that
        is the frame every segment variable already uses).
        """
        return self.cost_curve.x_n - self.cost_curve.x0

    def energy_vars(self) -> tuple[VarHandle, ...]:
        """This generator's own energy (segment-fill) variable handles, for
        a `ReserveModule` to couple into a `P_e + R_up_e <= Pmax_e` row
        (SPEC §8 Mode B) or an aggregate headroom row (Mode A). Populated
        by `contribute_variables`; call only after that has run.
        """
        return self._segment_vars

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
