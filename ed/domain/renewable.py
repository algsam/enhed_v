"""Renewable — a curtailable, near-zero-marginal-cost signed injection (SPEC
§5.4, §7 constraint 4; CLAUDE.md "Domain rules"; build order step 8).

`0 <= P <= forecast` is an upper **bound**, never an equality — that is what
permits curtailment (forecast can exceed load, or a must-run unit can leave
no room, and the solver must be free to dispatch less than forecast rather
than go infeasible). The optional curtailment penalty is wired as a negative
linear cost coefficient — i.e. a bonus for producing — rather than a separate
objective term keyed off `forecast - P`: minimizing `-penalty * P` over
`[0, forecast]` is exactly minimizing curtailment, with no extra variable.

Structurally the same signed-injection + convex-cost shape as `Generator`
(SPEC §5.6 admission test): a single non-negative variable, coefficient
`+1`. `curtailment_mw` is a reporting concern computed post-solve from
`forecast_mw - dispatch_mw`, not a solver quantity.
"""

from __future__ import annotations

from ed.domain.enums import ResourceType
from ed.model.context import BuildContext
from ed.solver import SolveResult, VarHandle


class Renewable:
    """A wind/solar resource: `resource_type=RENEWABLE`, dispatchable-down
    only. Not reserve-eligible in v1 (no ramp/deliverability data is modeled
    for it here); excluded from regulation like any curtailable, forecast-
    driven resource.
    """

    resource_type = ResourceType.RENEWABLE
    is_system_generated = False
    emits_setpoint = True
    reserve_eligible = False

    def __init__(
        self,
        bus: str,
        forecast_mw: float,
        *,
        curtailment_penalty_per_mwh: float = 0.0,
    ) -> None:
        if forecast_mw < 0.0:
            raise ValueError(f"forecast_mw={forecast_mw} must be non-negative")
        if curtailment_penalty_per_mwh < 0.0:
            raise ValueError(
                f"curtailment_penalty_per_mwh={curtailment_penalty_per_mwh} must be "
                "non-negative: a negative penalty would reward curtailment"
            )
        self.bus = bus
        self.forecast = forecast_mw
        self.curtailment_penalty_per_mwh = curtailment_penalty_per_mwh
        self._var: VarHandle | None = None

    def forecast_mw(self, t: int) -> float:
        """`ForecastLimited` capability. v1 runs a single interval (`t=0`)."""
        if t != 0:
            raise ValueError(f"t={t}: v1 carries only the singleton interval t=0")
        return self.forecast

    def contribute_variables(self, ctx: BuildContext) -> tuple[VarHandle, ...]:
        var = ctx.adapter.add_var(
            cost=-self.curtailment_penalty_per_mwh, lower=0.0, upper=self.forecast
        )
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

    def curtailment_mw(self, result: SolveResult) -> float:
        """Forecast minus actual dispatch — the reporting quantity SPEC §10
        requires ("curtailment MW per renewable"), never a solver variable."""
        return self.forecast - self.dispatch_mw(result)
