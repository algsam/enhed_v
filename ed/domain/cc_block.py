"""CCBlockConfig, CCBlockRoster, and CCBlock — the configuration-based
combined-cycle model (SPEC §5.3, §5.1; CLAUDE.md "Domain rules"; build order
step 6).

Three levels, matching SPEC §5.1's spine:

- `CCBlockConfig` — one *legal configuration* (a "pseudo-unit", SPEC §5.3):
  the subset of a block's member units that participate when it is active,
  plus that configuration's own measured aggregate IC curve. The curve's
  domain is the single source of truth for the block's aggregate limits
  (CLAUDE.md "Domain rules": `x_0 = Pmin_config`, `x_n = Pmax_config`,
  never stored twice).
- `CCBlockRoster` — a block's fixed member roster plus every config it can
  legally resolve to. Which config is *active* is not stored here: it is
  derived every run from telemetry (`ed.entities.build_entities`, SPEC
  §5.2), never dispatched off a stored flag that can drift.
- `CCBlock` — the `DispatchableEntity` for one *resolved* active
  configuration. Structurally identical to `Generator` (SPEC §5.1: "the
  optimizer iterates over DispatchableEntity and never knows CC blocks
  exist") — its cost is the config's curve, decomposed into QP segments
  exactly as any other signed-injection resource (SPEC §5.6 admission
  test). Disaggregation back to per-unit setpoints is a downstream concern
  it merely holds the seam for (`split`), never something the model
  builder or solver needs to know about.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, model_validator

from ed.curves.curve import CostCurve
from ed.domain.physical_unit import PhysicalUnit
from ed.model.context import BuildContext
from ed.solver import SolveResult, VarHandle

if TYPE_CHECKING:
    # Deferred to a TYPE_CHECKING-only import: `ed.disagg.protocol` itself
    # imports `ed.domain.physical_unit`, which (via `ed/domain/__init__.py`
    # re-exporting this module) would otherwise close a circular import at
    # runtime. `Disaggregator` is a structural `Protocol` used here purely
    # as a type hint, so this costs nothing at runtime.
    from ed.disagg.protocol import Disaggregator, UnitId
else:
    UnitId = str


class CCBlockConfig(BaseModel):
    """One legal configuration of a CC block (SPEC §5.3): a pseudo-unit with
    its own measured aggregate IC curve and its own `(Pmin, Pmax)`, read off
    the curve's own domain rather than stored twice (CLAUDE.md "Domain
    rules").
    """

    model_config = ConfigDict(frozen=True)

    config_id: str
    active_members: frozenset[UnitId]
    cost_curve: CostCurve

    @model_validator(mode="after")
    def _check_nonempty(self) -> CCBlockConfig:
        if not self.active_members:
            raise ValueError(f"config {self.config_id!r}: active_members must be non-empty")
        return self

    @property
    def pmin_mw(self) -> float:
        return self.cost_curve.x0

    @property
    def pmax_mw(self) -> float:
        return self.cost_curve.x_n


class CCBlockRoster(BaseModel):
    """A CC block's fixed member roster and its enumerated legal
    configurations (SPEC §5.2). The active configuration is *derived* from
    telemetry every run (`ed.entities.build_entities`), never stored here.
    """

    model_config = ConfigDict(frozen=True)

    block_id: str
    bus: str
    member_unit_ids: frozenset[UnitId]
    configs: tuple[CCBlockConfig, ...]

    @model_validator(mode="after")
    def _check_configs(self) -> CCBlockRoster:
        if len(self.configs) == 0:
            raise ValueError(f"block {self.block_id!r}: at least one legal configuration required")
        seen_ids: set[str] = set()
        seen_active_members: set[frozenset[UnitId]] = set()
        for cfg in self.configs:
            if cfg.config_id in seen_ids:
                raise ValueError(f"block {self.block_id!r}: duplicate config_id {cfg.config_id!r}")
            seen_ids.add(cfg.config_id)
            if cfg.active_members in seen_active_members:
                raise ValueError(
                    f"block {self.block_id!r}: two configs share active_members "
                    f"{sorted(cfg.active_members)}"
                )
            seen_active_members.add(cfg.active_members)
            if not cfg.active_members <= self.member_unit_ids:
                raise ValueError(
                    f"block {self.block_id!r} config {cfg.config_id!r}: active_members "
                    f"{sorted(cfg.active_members)} is not a subset of roster members "
                    f"{sorted(self.member_unit_ids)}"
                )
        return self

    def config_for(self, engaged_members: frozenset[UnitId]) -> CCBlockConfig | None:
        """The legal config whose `active_members` exactly matches
        `engaged_members`, or `None` if no declared config matches (SPEC
        §5.2: an inconsistent HRV/steam_source vector must be surfaced, not
        silently dispatched under the nearest config).
        """
        for cfg in self.configs:
            if cfg.active_members == engaged_members:
                return cfg
        return None


class CCBlock:
    """The `DispatchableEntity` for one resolved active CC configuration
    (SPEC §5.1, §5.3). Structurally the same signed-injection + convex-cost
    shape as `Generator`; see that class's docstring for why this satisfies
    the `DispatchableEntity` contract with no branch in the model builder.
    """

    def __init__(
        self,
        block_id: str,
        bus: str,
        cost_curve: CostCurve,
        config_id: str,
        member_units: Sequence[PhysicalUnit],
        disaggregator: Disaggregator,
        *,
        reserve_eligible: bool = False,
        ramp_up_mw_per_min: float | None = None,
    ) -> None:
        if reserve_eligible and ramp_up_mw_per_min is None:
            raise ValueError(
                "reserve_eligible=True requires ramp_up_mw_per_min: the deliverability "
                "cap is mandatory (CLAUDE.md 'Domain rules') — see Generator's "
                "docstring for the same rule"
            )
        self.block_id = block_id
        self.bus = bus
        self.cost_curve = cost_curve
        self.config_id = config_id
        self.member_units = tuple(member_units)
        self.disaggregator = disaggregator
        self.reserve_eligible = reserve_eligible
        self.ramp_up_mw_per_min = ramp_up_mw_per_min
        self._segment_vars: tuple[VarHandle, ...] = ()

    @property
    def capacity_mw(self) -> float:
        """See `Generator.capacity_mw` — same relative-width convention."""
        return self.cost_curve.x_n - self.cost_curve.x0

    def energy_vars(self) -> tuple[VarHandle, ...]:
        """See `Generator.energy_vars` — the config's own segment-fill vars."""
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
        """No constraints of its own yet: ramp/limit rows are a later
        build-order step, exactly as for `Generator`."""
        return None

    def contribute_cost(self, ctx: BuildContext) -> None:
        """No-op — see `Generator.contribute_cost` for why this method
        exists at all despite doing nothing here."""
        return None

    def dispatch_mw(self, result: SolveResult) -> float:
        """Total MW dispatched by the config: the sum of its segment fills."""
        return sum(result.primal[v] for v in self._segment_vars)

    def split(self, result: SolveResult) -> dict[UnitId, float]:
        """Per-member setpoints for this cycle's base point (SPEC §6.1):
        setpoint allocation, never cost decomposition — the members' own
        cost curves play no part here."""
        return self.disaggregator.split(self.dispatch_mw(result), list(self.member_units))
