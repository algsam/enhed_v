"""ReserveModule — SPEC §8, CLAUDE.md "Domain rules"/"Operational"; build
order step 7: two reserve modes behind one interface, toggled by a config
flag (`ReserveMode`).

Reserve variables and requirements are **keyed by product** (`[entity,
product]`) from day one (CLAUDE.md "Domain rules"), with exactly one
product populated in v1 — `product` is carried on every module here purely
as that key, never branched on. Product substitution/cascading is
explicitly out of scope (SPEC §2, §8) and not built.

Only entities with `reserve_eligible=True` participate — always an explicit
per-entity fact, never inferred from `resource_type` (CLAUDE.md "Domain
rules", SPEC §8: "Nuclear: false. Ties/DR: do not infer from type").

**Mode A `AGGREGATE_HEADROOM` (stub).** One system-wide row per product:
`sum_e headroom_e >= Requirement`. Each entity's `headroom_e` is bounded by
both `Pmax_e - P_e` *and* `RampRate_e * T_reserve` — the deliverability cap
is mandatory (CLAUDE.md: "without it the stub reserves MW no unit can reach
in time"). Both bounds are wired as plain linear bounds on a free
(zero-cost) headroom variable, which is the standard LP encoding of
`min(a, b)` for a quantity that only ever needs to be *at least* some
requirement: nothing in the objective rewards pushing headroom_e higher
than necessary, so the solver only claims as much as the row needs, up to
whichever bound binds first. This mode does not decide who holds the
reserve or price it (SPEC §8): no reserve variable enters the objective, no
price is exposed, and (deliberately, matching the given build-order
instructions exactly) no shortfall slack is injected here — unlike Mode B,
an infeasible Mode A requirement stays infeasible. Scoped to a single
product for the same reason SPEC §8 gives: "the pure aggregate form cannot
cleanly express multiple reserve products."

**Mode B `PER_UNIT_COOPTIMIZATION` (full).** A reserve variable `R_up_e`
per eligible entity, solved *jointly* with energy via the same two
mechanisms as Mode A's headroom variable — `R_up_e <= RU_e * T_reserve` as
a direct variable bound, and `P_e + R_up_e <= Pmax_e` as a row coupling
`R_up_e` to that entity's own energy (segment) variables — except here
`R_up_e` is not free: it is what the shortfall-avoidance incentive backs
energy off of. `sum_e R_up_e + shortfall >= Requirement`, with shortfall
slack auto-injected at a high policy penalty, same discipline as
`BalanceModule`'s unserved/overgen slack (never "infeasible"). Because
`R_up_e` costs nothing directly but competes with energy for the same
`Pmax_e` headroom, and paying the shortfall penalty is expensive, the joint
solve backs the *cheapest* marginal energy off first wherever that frees
enough reserve headroom to avoid shortfall — the requirement row's own
dual is therefore a genuine reserve marginal price, extracted the same way
`BalanceModule.extract_price` extracts lambda (CLAUDE.md "power-balance
dual = lambda, reserve-requirement dual = reserve price — never compute
prices any other way").
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from ed.model.context import BuildContext, ConstraintModule
from ed.solver import RowHandle, SolveResult, VarHandle


class ReserveMode(Enum):
    AGGREGATE_HEADROOM = "aggregate_headroom"
    PER_UNIT_COOPTIMIZATION = "per_unit_cooptimization"


@runtime_checkable
class ReserveParticipant(Protocol):
    """What `ReserveModule` needs from a `DispatchableEntity` to couple
    reserve into its own energy variables. `reserve_eligible` is always an
    explicit per-entity fact (CLAUDE.md "Domain rules"); `ramp_up_mw_per_min`
    is the already-resolved conservative scalar (SPEC §6.3 amendment) — this
    protocol never touches a `RampRateCurve` directly.
    """

    reserve_eligible: bool
    ramp_up_mw_per_min: float | None

    @property
    def capacity_mw(self) -> float: ...

    def energy_vars(self) -> tuple[VarHandle, ...]: ...


@runtime_checkable
class ReserveModule(ConstraintModule, Protocol):
    """The `ReserveModule` interface: a `ConstraintModule` (SPEC §9) plus
    the `product` key every reserve row is indexed by. `AggregateHeadroomReserve`
    and `PerUnitCoOptimizationReserve` are its two implementations, selected
    behind `ReserveMode` — never a branch inside the solver or the builder.
    """

    product: str


def _eligible(participants: list[ReserveParticipant]) -> list[ReserveParticipant]:
    eligible = [p for p in participants if p.reserve_eligible]
    for p in eligible:
        if p.ramp_up_mw_per_min is None:
            raise ValueError(
                "reserve_eligible participant is missing ramp_up_mw_per_min: the "
                "deliverability cap is mandatory (CLAUDE.md 'Domain rules')"
            )
    return eligible


@dataclass
class AggregateHeadroomReserve:
    """Mode A — see module docstring. `t_reserve_min` is the reserve
    delivery window (CLAUDE.md units convention: minutes)."""

    participants: list[ReserveParticipant]
    product: str
    requirement_mw: float
    t_reserve_min: float

    _row: RowHandle | None = field(default=None, init=False, repr=False)
    _headroom_vars: tuple[VarHandle, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        self._eligible = _eligible(self.participants)

    @property
    def eligible_participants(self) -> tuple[ReserveParticipant, ...]:
        return tuple(self._eligible)

    def contribute(self, ctx: BuildContext) -> None:
        coefficients: dict[VarHandle, float] = {}
        headroom_vars: list[VarHandle] = []
        for p in self._eligible:
            assert p.ramp_up_mw_per_min is not None  # checked by _eligible
            ramp_cap_mw = p.ramp_up_mw_per_min * self.t_reserve_min
            headroom_var = ctx.adapter.add_var(cost=0.0, lower=0.0, upper=ramp_cap_mw)
            coupling_coeffs: dict[VarHandle, float] = {headroom_var: 1.0}
            for ev in p.energy_vars():
                coupling_coeffs[ev] = coupling_coeffs.get(ev, 0.0) + 1.0
            ctx.adapter.add_row(lower=-math.inf, upper=p.capacity_mw, coefficients=coupling_coeffs)

            coefficients[headroom_var] = coefficients.get(headroom_var, 0.0) + 1.0
            headroom_vars.append(headroom_var)

        self._headroom_vars = tuple(headroom_vars)
        self._row = ctx.adapter.add_row(
            lower=self.requirement_mw, upper=math.inf, coefficients=coefficients
        )

    def headroom_mw(self, result: SolveResult) -> float:
        """Total deliverable headroom actually claimed across eligible
        entities (a feasibility quantity, not a price — Mode A exposes no
        price, SPEC §8)."""
        if self._row is None:
            raise RuntimeError("contribute() must run before headroom_mw()")
        return sum(result.primal[v] for v in self._headroom_vars)


@dataclass
class PerUnitCoOptimizationReserve:
    """Mode B — see module docstring."""

    participants: list[ReserveParticipant]
    product: str
    requirement_mw: float
    t_reserve_min: float
    shortfall_penalty: float

    _row: RowHandle | None = field(default=None, init=False, repr=False)
    _reserve_vars: dict[int, VarHandle] = field(default_factory=dict, init=False, repr=False)
    _shortfall_var: VarHandle | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.shortfall_penalty <= 0.0:
            raise ValueError(
                f"shortfall_penalty={self.shortfall_penalty} must be strictly positive "
                "(CLAUDE.md: policy penalties must be strictly positive so shortfall "
                "is never economic ahead of real reserve)"
            )
        self._eligible = _eligible(self.participants)

    @property
    def eligible_participants(self) -> tuple[ReserveParticipant, ...]:
        return tuple(self._eligible)

    def contribute(self, ctx: BuildContext) -> None:
        coefficients: dict[VarHandle, float] = {}
        reserve_vars: dict[int, VarHandle] = {}
        for i, p in enumerate(self._eligible):
            assert p.ramp_up_mw_per_min is not None  # checked by _eligible
            ramp_cap_mw = p.ramp_up_mw_per_min * self.t_reserve_min
            r_var = ctx.adapter.add_var(cost=0.0, lower=0.0, upper=ramp_cap_mw)
            coupling_coeffs: dict[VarHandle, float] = {r_var: 1.0}
            for ev in p.energy_vars():
                coupling_coeffs[ev] = coupling_coeffs.get(ev, 0.0) + 1.0
            ctx.adapter.add_row(lower=-math.inf, upper=p.capacity_mw, coefficients=coupling_coeffs)

            coefficients[r_var] = coefficients.get(r_var, 0.0) + 1.0
            reserve_vars[i] = r_var

        shortfall_var = ctx.adapter.add_var(
            cost=self.shortfall_penalty, lower=0.0, upper=math.inf
        )
        coefficients[shortfall_var] = coefficients.get(shortfall_var, 0.0) + 1.0

        self._reserve_vars = reserve_vars
        self._shortfall_var = shortfall_var
        self._row = ctx.adapter.add_row(
            lower=self.requirement_mw, upper=math.inf, coefficients=coefficients
        )

    def extract_price(self, result: SolveResult) -> float:
        """Reserve marginal price: the requirement row's own dual (CLAUDE.md
        "reserve-requirement dual = reserve price"), exposed alongside
        `BalanceModule.extract_price`'s lambda, never derived any other way.
        """
        if self._row is None:
            raise RuntimeError("contribute() must run before extract_price()")
        return result.row_duals[self._row]

    def reserve_mw(self, result: SolveResult, entity: ReserveParticipant) -> float:
        """Reserve committed by a specific eligible entity this cycle."""
        index = self._eligible.index(entity)
        return result.primal[self._reserve_vars[index]]

    def total_reserve_mw(self, result: SolveResult) -> float:
        return sum(result.primal[v] for v in self._reserve_vars.values())

    def shortfall_mw(self, result: SolveResult) -> float:
        if self._shortfall_var is None:
            raise RuntimeError("contribute() must run before shortfall_mw()")
        return result.primal[self._shortfall_var]

    def is_scarce(self, result: SolveResult) -> bool:
        return self.shortfall_mw(result) > 0.0


def build_reserve_module(
    mode: ReserveMode,
    participants: list[ReserveParticipant],
    product: str,
    requirement_mw: float,
    t_reserve_min: float,
    shortfall_penalty: float | None = None,
) -> ReserveModule:
    """The config-flag seam (CLAUDE.md "every user choice... is a strategy
    swap behind a stable interface, i.e. a config flag feeding the same
    build"): `mode` selects the implementation, never a branch inside the
    solver or `ModelBuilder`.
    """
    if mode is ReserveMode.AGGREGATE_HEADROOM:
        return AggregateHeadroomReserve(
            participants=participants,
            product=product,
            requirement_mw=requirement_mw,
            t_reserve_min=t_reserve_min,
        )
    if mode is ReserveMode.PER_UNIT_COOPTIMIZATION:
        if shortfall_penalty is None:
            raise ValueError("shortfall_penalty is required for PER_UNIT_COOPTIMIZATION")
        return PerUnitCoOptimizationReserve(
            participants=participants,
            product=product,
            requirement_mw=requirement_mw,
            t_reserve_min=t_reserve_min,
            shortfall_penalty=shortfall_penalty,
        )
    raise ValueError(f"unknown reserve mode: {mode}")
