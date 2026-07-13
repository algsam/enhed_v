"""TieLine — a fixed-schedule interchange (SPEC §5.6, §5.8; CLAUDE.md
"Domain rules"; build order step 8).

**Fixed schedule in v1** (SPEC §5.6 [DECISION]): `lower == upper == schedule`
on the *net* injection. Bidirectional entities get **two non-negative
variables** (SPEC §5.5/§5.6), never one variable on a symmetric interval — a
single variable on `[-cap, +cap]` with import@price / export@price is a
non-convex kink at zero. Here each of the two variables is itself pinned to
the schedule's own sign: whichever direction the fixed schedule doesn't use
is pinned to zero. This is deliberately just a bounds choice, not a
constraint row — flipping a tie-line to dispatchable later means widening
each variable's bounds (e.g. `[0, import_cap]` / `[0, export_cap]`), never
touching this class's `contribute_*` methods.

Virtual (SPEC §5.6): `emits_setpoint=False`, `is_system_generated=False` —
user-configured, and does appear in an operator-facing resource list (unlike
`Slack`).
"""

from __future__ import annotations

from ed.curves.validators import validate_price_ordering
from ed.domain.enums import ResourceType
from ed.model.context import BuildContext
from ed.solver import SolveResult, VarHandle


class TieLine:
    """A fixed-schedule interchange: `resource_type=TIE_LINE`.

    `schedule_mw > 0` is import (injection `+P`); `< 0` is export
    (injection `-P`, earning `export_price`). `import_price >= export_price`
    is validated on construction (SPEC §4.4) — otherwise the model could
    import and export simultaneously for a risk-free profit.
    """

    resource_type = ResourceType.TIE_LINE
    is_system_generated = False
    emits_setpoint = False

    def __init__(
        self,
        bus: str,
        schedule_mw: float,
        import_price: float,
        export_price: float,
        *,
        reserve_eligible: bool = False,
    ) -> None:
        validate_price_ordering(import_price, export_price)
        self.bus = bus
        self.schedule_mw = schedule_mw
        self.import_price = import_price
        self.export_price = export_price
        self.reserve_eligible = reserve_eligible
        self._import_pinned_mw = max(schedule_mw, 0.0)
        self._export_pinned_mw = max(-schedule_mw, 0.0)
        self._imp_var: VarHandle | None = None
        self._exp_var: VarHandle | None = None

    def contribute_variables(self, ctx: BuildContext) -> tuple[VarHandle, ...]:
        imp_var = ctx.adapter.add_var(
            cost=self.import_price, lower=self._import_pinned_mw, upper=self._import_pinned_mw
        )
        exp_var = ctx.adapter.add_var(
            cost=-self.export_price, lower=self._export_pinned_mw, upper=self._export_pinned_mw
        )
        ctx.add_injection(self.bus, imp_var, coefficient=1.0)
        ctx.add_injection(self.bus, exp_var, coefficient=-1.0)
        self._imp_var = imp_var
        self._exp_var = exp_var
        return (imp_var, exp_var)

    def contribute_constraints(self, ctx: BuildContext) -> None:
        return None

    def contribute_cost(self, ctx: BuildContext) -> None:
        return None

    def net_injection_mw(self, result: SolveResult) -> float:
        if self._imp_var is None or self._exp_var is None:
            raise RuntimeError("contribute_variables() must run before net_injection_mw()")
        return result.primal[self._imp_var] - result.primal[self._exp_var]
