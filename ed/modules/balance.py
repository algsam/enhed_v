"""BalanceModule — the copperplate power-balance constraint module (SPEC §7
constraint 1; CLAUDE.md "Solver", "Architecture", "Operational"; build order
step 4).

`Sum injection = load`, with injections read out of `ctx.bus_injection_terms`
— **bus-indexed from day one** (SPEC §5.8) even though v1 sums every bus into
one system-wide row. Swapping this row for a per-bus balance later only
touches this module, never entity code or `ModelBuilder`.

**This module owns lambda extraction.** No other module computes a price:
`extract_price` reads the balance row's own dual straight off the
`SolveResult`, per CLAUDE.md ("power-balance dual = system marginal price
lambda. Never compute prices any other way.").

It also **auto-injects** unserved-energy (deficit) and over-generation
(surplus) slack pseudo-units, `is_system_generated=True` and never
user-configurable (CLAUDE.md "Operational"; SPEC §5.6): the engine always
returns an actionable dispatch, so the balance row can never itself be
infeasible. Separate up-slack and down-slack variables with independent
penalties (never one symmetric variable) — over-generation is not priced
like unserved load. Each penalty is a policy constant (VoLL, over-generation
penalty), set strictly above any real resource's cost so slack is never
economic and is dispatched only in true scarcity; validating
`VOLL > max IC across all active curves` is a case-level ingest concern
(deferred to a later build-order step), not this module's.
"""

from __future__ import annotations

import math

from ed.model.context import BuildContext
from ed.solver import RowHandle, SolveResult, VarHandle


class BalanceModule:
    """Copperplate power balance for a single dispatch interval.

    `load_mw` is the (fixed) system load for this interval. `voll` and
    `overgen_penalty` are policy constants for the auto-injected slack
    pseudo-units; both must be strictly positive so slack is never
    economic ahead of real generation.
    """

    def __init__(self, load_mw: float, voll: float, overgen_penalty: float) -> None:
        if voll <= 0.0:
            raise ValueError(f"voll={voll} must be strictly positive")
        if overgen_penalty <= 0.0:
            raise ValueError(f"overgen_penalty={overgen_penalty} must be strictly positive")
        self.load_mw = load_mw
        self.voll = voll
        self.overgen_penalty = overgen_penalty
        self._row: RowHandle | None = None
        self._unserved_var: VarHandle | None = None
        self._overgen_var: VarHandle | None = None

    def contribute(self, ctx: BuildContext) -> None:
        coefficients: dict[VarHandle, float] = {}
        for terms in ctx.bus_injection_terms.values():
            for var, coefficient in terms:
                coefficients[var] = coefficients.get(var, 0.0) + coefficient

        unserved_var = ctx.adapter.add_var(cost=self.voll, lower=0.0, upper=math.inf)
        overgen_var = ctx.adapter.add_var(cost=self.overgen_penalty, lower=0.0, upper=math.inf)
        coefficients[unserved_var] = coefficients.get(unserved_var, 0.0) + 1.0
        coefficients[overgen_var] = coefficients.get(overgen_var, 0.0) - 1.0

        self._unserved_var = unserved_var
        self._overgen_var = overgen_var
        self._row = ctx.adapter.add_row(
            lower=self.load_mw, upper=self.load_mw, coefficients=coefficients
        )

    def extract_price(self, result: SolveResult) -> float:
        """System marginal price lambda: the balance row's own dual.

        This is the only place in the engine that reads this row's dual —
        CLAUDE.md's "power-balance dual = lambda" invariant, enforced by
        `BalanceModule` being the sole owner of `self._row`.
        """
        if self._row is None:
            raise RuntimeError("contribute() must run before extract_price()")
        return result.row_duals[self._row]

    def unserved_mw(self, result: SolveResult) -> float:
        if self._unserved_var is None:
            raise RuntimeError("contribute() must run before unserved_mw()")
        return result.primal[self._unserved_var]

    def overgen_mw(self, result: SolveResult) -> float:
        if self._overgen_var is None:
            raise RuntimeError("contribute() must run before overgen_mw()")
        return result.primal[self._overgen_var]

    def is_scarce(self, result: SolveResult) -> bool:
        """Scarcity flag: unserved energy or over-generation was dispatched.

        An operator must never miss this — nonzero slack is the dispatch's
        scarcity signal (CLAUDE.md/SPEC §11).
        """
        return self.unserved_mw(result) > 0.0 or self.overgen_mw(result) > 0.0
