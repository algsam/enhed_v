"""DemandResponse — curtailed load, injected as `+P` (SPEC §5.6; CLAUDE.md
"Domain rules"; build order step 8).

DR is a demand-side resource that nonetheless carries a **`"cost"`** curve,
not a `"value"` curve (see `CostCurve.to_qp_segments`'s docstring for the
full argument): curtailing load is compensated, and compensation *rises*
with MW curtailed, exactly the non-decreasing-IC shape a generator has.
Tagging it `"value"` would negate that compensation into apparent revenue
and the QP would want to call the maximum available DR on every solve.
Ingest with `validate_as="supply"` (`ed.curves.ingest`); this class rejects
a curve whose `curve_role` is `"value"` at construction so that mistake can
never reach the solver.

Structurally identical to `Generator`'s contribution shape (signed injection
`+1`, diagonal-Hessian QP segments) — the two classes are kept separate
files/types per CLAUDE.md ("no VirtualUnit base class... each resource gets
a real concrete class") rather than sharing a base, since nothing else about
them is shared: DR is virtual (`emits_setpoint=False`) and user-configured,
where a `Generator` may or may not be either.
"""

from __future__ import annotations

from ed.curves.curve import CostCurve
from ed.domain.enums import ResourceType
from ed.model.context import BuildContext
from ed.solver import SolveResult, VarHandle


class DemandResponse:
    """A DR contract: `resource_type=DEMAND_RESPONSE`, injection `+P`, cost =
    DR compensation. `reserve_eligible` is an explicit per-contract fact
    (CLAUDE.md "Domain rules": "Ties/DR: do not infer from type").
    """

    resource_type = ResourceType.DEMAND_RESPONSE
    is_system_generated = False
    emits_setpoint = False

    def __init__(
        self,
        bus: str,
        cost_curve: CostCurve,
        *,
        reserve_eligible: bool = False,
        ramp_up_mw_per_min: float | None = None,
    ) -> None:
        if cost_curve.curve_role != "cost":
            raise ValueError(
                f"DemandResponse requires a 'cost' curve (compensation rises with MW "
                f"curtailed), got curve_role={cost_curve.curve_role!r}; ingest with "
                "validate_as='supply'"
            )
        if reserve_eligible and ramp_up_mw_per_min is None:
            raise ValueError(
                "reserve_eligible=True requires ramp_up_mw_per_min: the deliverability "
                "cap is mandatory (CLAUDE.md 'Domain rules')"
            )
        self.bus = bus
        self.cost_curve = cost_curve
        self.reserve_eligible = reserve_eligible
        self.ramp_up_mw_per_min = ramp_up_mw_per_min
        self._segment_vars: tuple[VarHandle, ...] = ()

    @property
    def capacity_mw(self) -> float:
        return self.cost_curve.x_n - self.cost_curve.x0

    def energy_vars(self) -> tuple[VarHandle, ...]:
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
        return None

    def contribute_cost(self, ctx: BuildContext) -> None:
        return None

    def dispatch_mw(self, result: SolveResult) -> float:
        return sum(result.primal[v] for v in self._segment_vars)
