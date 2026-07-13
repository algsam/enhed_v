"""BESS — bidirectional storage (SPEC §5.5; CLAUDE.md "Domain rules"; build
order step 8).

Two non-negative variables (`P_chg`, `P_dis`), never one variable on a
symmetric interval — same reasoning as `TieLine`. Net injection is
`P_dis - P_chg`.

**Single-snapshot SoE choice (SPEC §5.5 — "pick one of the two documented
options and say which"): this class is energy-budget-limited from a
passed-in current `soe_mwh`.** Each variable's own power rating is
additionally capped by how much energy the battery actually has available
this interval:

  - discharge is capped by the energy actually in the battery:
    `P_dis * dt_hr / discharge_efficiency <= soe_mwh`
  - charge is capped by remaining headroom to `capacity_mwh`:
    `P_chg * dt_hr * charge_efficiency <= capacity_mwh - soe_mwh`

This is *not* `not_dispatchable_until_multi_interval` — it does dispatch,
bounded by what one interval's worth of stored/spare energy can actually
deliver, rather than degenerating into "a free unit with a MW range that
discharges at full power every cycle" (SPEC §5.5's warning).

No binary no-simultaneous-charge/discharge constraint is added (that would
break the continuous QP) — `charge_cost >= discharge_revenue` (validated,
same guard class as `TieLine`'s price ordering) means simultaneity is never
economic; `assert_no_simultaneous_charge_discharge` checks it **post-solve**
instead (SPEC §5.5).
"""

from __future__ import annotations

from ed.curves.validators import validate_price_ordering
from ed.domain.enums import ResourceType
from ed.model.context import BuildContext
from ed.solver import SolveResult, VarHandle


class SimultaneousChargeDischargeError(ValueError):
    """Post-solve check found nonzero charge and discharge together (SPEC §5.5)."""


class BESS:
    """A battery: `resource_type=BESS`, injection `P_dis - P_chg`."""

    resource_type = ResourceType.BESS
    is_system_generated = False
    emits_setpoint = True

    def __init__(
        self,
        bus: str,
        power_rating_mw: float,
        capacity_mwh: float,
        soe_mwh: float,
        charge_efficiency: float,
        discharge_efficiency: float,
        charge_cost: float,
        discharge_revenue: float,
        dt_min: float,
        *,
        reserve_eligible: bool = False,
        ramp_up_mw_per_min: float | None = None,
    ) -> None:
        validate_price_ordering(charge_cost, discharge_revenue)
        if power_rating_mw < 0.0:
            raise ValueError(f"power_rating_mw={power_rating_mw} must be non-negative")
        if capacity_mwh < 0.0:
            raise ValueError(f"capacity_mwh={capacity_mwh} must be non-negative")
        if not (0.0 <= soe_mwh <= capacity_mwh):
            raise ValueError(f"soe_mwh={soe_mwh} must be within [0, capacity_mwh={capacity_mwh}]")
        if not (0.0 < charge_efficiency <= 1.0 and 0.0 < discharge_efficiency <= 1.0):
            raise ValueError("charge_efficiency and discharge_efficiency must be in (0, 1]")
        if dt_min <= 0.0:
            raise ValueError(f"dt_min={dt_min} must be strictly positive")
        if reserve_eligible and ramp_up_mw_per_min is None:
            raise ValueError(
                "reserve_eligible=True requires ramp_up_mw_per_min: the deliverability "
                "cap is mandatory (CLAUDE.md 'Domain rules')"
            )

        self.bus = bus
        self.power_rating_mw = power_rating_mw
        self.capacity_mwh = capacity_mwh
        self.soe_mwh = soe_mwh
        self.charge_efficiency = charge_efficiency
        self.discharge_efficiency = discharge_efficiency
        self.charge_cost = charge_cost
        self.discharge_revenue = discharge_revenue
        self.dt_min = dt_min
        self.reserve_eligible = reserve_eligible
        self.ramp_up_mw_per_min = ramp_up_mw_per_min
        self._chg_var: VarHandle | None = None
        self._dis_var: VarHandle | None = None

    def _energy_limited_bounds_mw(self) -> tuple[float, float]:
        """`(charge_upper_mw, discharge_upper_mw)`, each the tighter of the
        power rating and this interval's energy budget (see module
        docstring)."""
        dt_hr = self.dt_min / 60.0
        charge_headroom_mwh = self.capacity_mwh - self.soe_mwh
        charge_upper_mw = min(
            self.power_rating_mw, charge_headroom_mwh / (self.charge_efficiency * dt_hr)
        )
        discharge_upper_mw = min(
            self.power_rating_mw, self.soe_mwh * self.discharge_efficiency / dt_hr
        )
        return max(charge_upper_mw, 0.0), max(discharge_upper_mw, 0.0)

    def contribute_variables(self, ctx: BuildContext) -> tuple[VarHandle, ...]:
        charge_upper_mw, discharge_upper_mw = self._energy_limited_bounds_mw()
        chg_var = ctx.adapter.add_var(cost=self.charge_cost, lower=0.0, upper=charge_upper_mw)
        dis_var = ctx.adapter.add_var(
            cost=-self.discharge_revenue, lower=0.0, upper=discharge_upper_mw
        )
        ctx.add_injection(self.bus, dis_var, coefficient=1.0)
        ctx.add_injection(self.bus, chg_var, coefficient=-1.0)
        self._chg_var = chg_var
        self._dis_var = dis_var
        return (chg_var, dis_var)

    def contribute_constraints(self, ctx: BuildContext) -> None:
        return None

    def contribute_cost(self, ctx: BuildContext) -> None:
        return None

    def charge_mw(self, result: SolveResult) -> float:
        if self._chg_var is None:
            raise RuntimeError("contribute_variables() must run before charge_mw()")
        return result.primal[self._chg_var]

    def discharge_mw(self, result: SolveResult) -> float:
        if self._dis_var is None:
            raise RuntimeError("contribute_variables() must run before discharge_mw()")
        return result.primal[self._dis_var]

    def net_injection_mw(self, result: SolveResult) -> float:
        return self.discharge_mw(result) - self.charge_mw(result)

    def assert_no_simultaneous_charge_discharge(
        self, result: SolveResult, tol: float = 1e-6
    ) -> None:
        """Post-solve check (SPEC §5.5): never a binary in the model, so this
        must be verified after the fact rather than constrained during."""
        chg, dis = self.charge_mw(result), self.discharge_mw(result)
        if chg > tol and dis > tol:
            raise SimultaneousChargeDischargeError(
                f"simultaneous charge ({chg} MW) and discharge ({dis} MW): expected "
                "charge_cost >= discharge_revenue to preclude this economically"
            )
