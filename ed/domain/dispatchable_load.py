"""DispatchableLoad — a consuming resource, injected as `-P` (SPEC §5.6;
CLAUDE.md "Domain rules"; build order step 8).

Carries a `"value"` (willingness-to-pay) curve, validated non-increasing
(`validate_demand_concavity`) — the mirror of a generator's non-decreasing
IC. `CostCurve.to_qp_segments` applies the `"value"` sign flip so that
consuming *earns* the objective (a negative cost coefficient), not costs it;
this class only needs to register the injection with coefficient `-1.0` and
let the curve's own role drive the sign of the QP segments.
"""

from __future__ import annotations

from ed.curves.curve import CostCurve
from ed.domain.enums import ResourceType
from ed.model.context import BuildContext
from ed.solver import SolveResult, VarHandle


class DispatchableLoad:
    """A price-responsive load: `resource_type=DISPATCHABLE_LOAD`, injection
    `-P`, cost = negative consumer value (earns/saves load value).
    """

    resource_type = ResourceType.DISPATCHABLE_LOAD
    is_system_generated = False
    emits_setpoint = False

    def __init__(
        self,
        bus: str,
        value_curve: CostCurve,
        *,
        reserve_eligible: bool = False,
        ramp_up_mw_per_min: float | None = None,
    ) -> None:
        if value_curve.curve_role != "value":
            raise ValueError(
                f"DispatchableLoad requires a 'value' curve (willingness-to-pay, "
                f"non-increasing), got curve_role={value_curve.curve_role!r}; ingest "
                "with validate_as='demand'"
            )
        if reserve_eligible and ramp_up_mw_per_min is None:
            raise ValueError(
                "reserve_eligible=True requires ramp_up_mw_per_min: the deliverability "
                "cap is mandatory (CLAUDE.md 'Domain rules')"
            )
        self.bus = bus
        self.value_curve = value_curve
        self.reserve_eligible = reserve_eligible
        self.ramp_up_mw_per_min = ramp_up_mw_per_min
        self._segment_vars: tuple[VarHandle, ...] = ()

    @property
    def capacity_mw(self) -> float:
        return self.value_curve.x_n - self.value_curve.x0

    def energy_vars(self) -> tuple[VarHandle, ...]:
        return self._segment_vars

    def contribute_variables(self, ctx: BuildContext) -> tuple[VarHandle, ...]:
        segment_vars = []
        for qp in self.value_curve.to_qp_segments():
            var = ctx.adapter.add_var(
                cost=qp.a, lower=0.0, upper=qp.width_mw, hessian_diag=qp.q
            )
            ctx.add_injection(self.bus, var, coefficient=-1.0)
            segment_vars.append(var)
        self._segment_vars = tuple(segment_vars)
        return self._segment_vars

    def contribute_constraints(self, ctx: BuildContext) -> None:
        return None

    def contribute_cost(self, ctx: BuildContext) -> None:
        return None

    def consumption_mw(self, result: SolveResult) -> float:
        return sum(result.primal[v] for v in self._segment_vars)
