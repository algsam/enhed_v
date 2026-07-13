"""`RangeProRata` (default) and `PmaxProRata` (demonstration-only) — the two
concrete `Disaggregator` strategies for build order step 5 (SPEC §6.2, §6.3).

Both strategies are linear disaggregation rules `P_i = alpha_i * P_e + beta_i`
with `sum(alpha_i) == 1` (SPEC §6.3), which is exactly the shape the
drift-aware `aggregate_ramp` derivation is written for — so both strategies
share `_drift_aware_aggregate_ramp` and differ only in how they compute
`alpha_i`/`beta_i`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ed.disagg.protocol import AggregateRamp, SplitValidationError, UnitId, validate_split_result
from ed.domain.physical_unit import PhysicalUnit
from ed.domain.ramp import resolve_ramp_down_mw_per_min, resolve_ramp_up_mw_per_min


def _drift_aware_aggregate_ramp(
    units: Sequence[PhysicalUnit],
    telemetry: Mapping[UnitId, float],
    dt_min: float,
    alpha: Mapping[UnitId, float],
    beta: Mapping[UnitId, float],
) -> AggregateRamp:
    """The exact derivation in SPEC §6.3, for any linear split rule.

    `P_e^0 = sum(P_i^0)` is the entity's own previous output (measured, not
    remembered). For each unit, the drift `d_i = (alpha_i * P_e^0 + beta_i)
    - P_i^0` is how far AGC has moved it off the disaggregator's manifold
    since the last cycle. If the entity moves by `Delta`, unit `i` must move
    by `d_i + alpha_i * Delta`; imposing `-RD_i*dt <= d_i + alpha_i*Delta <=
    RU_i*dt` and solving for `Delta` gives one candidate `RU_e`/`RD_e` per
    unit. The aggregate is the **min** over units — the block ramps at the
    pace of its slowest member relative to its own range, never the sum
    (SPEC §6.3's central point).

    `RU_i`/`RD_i` are resolved from each unit's ramp-rate *curve* to a single
    conservative scalar from its own measured `P_i^0` (SPEC §6.3 amendment;
    `ed.domain.ramp`) before this algebra runs.

    A unit with `alpha_i == 0` (a zero-range member: `Pmax_i == Pmin_i` under
    range-based splitting) takes none of the entity's move by construction —
    dividing by it would be undefined, and physically it imposes no bound on
    how fast the *entity* can move (its own position is pinned regardless of
    `Delta`), so it is simply excluded from the min rather than treated as a
    candidate.
    """
    p_e0 = sum(telemetry[u.unit_id] for u in units)

    ru_candidates: list[float] = []
    rd_candidates: list[float] = []
    for unit in units:
        a_i = alpha[unit.unit_id]
        if a_i == 0.0:
            continue
        b_i = beta[unit.unit_id]
        p_i0 = telemetry[unit.unit_id]
        chars = unit.active_characteristics

        ru_i = resolve_ramp_up_mw_per_min(chars.ramp_up, p_i0, dt_min)
        rd_i = resolve_ramp_down_mw_per_min(chars.ramp_down, p_i0, dt_min)
        d_i = (a_i * p_e0 + b_i) - p_i0

        ru_candidates.append((ru_i * dt_min - d_i) / a_i)
        rd_candidates.append((rd_i * dt_min + d_i) / a_i)

    raw_ru_mw = min(ru_candidates) if ru_candidates else 0.0
    raw_rd_mw = min(rd_candidates) if rd_candidates else 0.0

    clamped_up = raw_ru_mw < 0.0
    clamped_down = raw_rd_mw < 0.0
    ru_mw = max(0.0, raw_ru_mw)
    rd_mw = max(0.0, raw_rd_mw)

    diagnostics: list[str] = []
    if clamped_up:
        diagnostics.append(
            f"aggregate ramp-up clamped to 0 (raw={raw_ru_mw:.4f} MW over {dt_min} min): "
            "at least one member has drifted further from its split-implied position "
            "than its own ramp budget can recover this cycle"
        )
    if clamped_down:
        diagnostics.append(
            f"aggregate ramp-down clamped to 0 (raw={raw_rd_mw:.4f} MW over {dt_min} min): "
            "at least one member has drifted further from its split-implied position "
            "than its own ramp budget can recover this cycle"
        )

    return AggregateRamp(
        ru_mw_per_min=ru_mw / dt_min,
        rd_mw_per_min=rd_mw / dt_min,
        clamped_up=clamped_up,
        clamped_down=clamped_down,
        diagnostics=tuple(diagnostics),
    )


class RangeProRata:
    """Default disaggregation strategy (SPEC §6.2 [DECISION]): range-based
    pro-rata.

    ```
    f   = (P_e - sum(Pmin_i)) / (sum(Pmax_i) - sum(Pmin_i))
    P_i = Pmin_i + f * (Pmax_i - Pmin_i)
    ```

    Respects every unit's `[Pmin, Pmax]` by construction — `f` maps
    linearly onto each unit's *own* range, so it can never push a member
    below its own floor the way pure Pmax pro-rata can (`PmaxProRata`,
    kept only to demonstrate that failure).
    """

    def split(self, entity_mw: float, units: Sequence[PhysicalUnit]) -> dict[UnitId, float]:
        pmin_e, pmax_e = self.aggregate_limits(units)
        total_range = pmax_e - pmin_e
        if total_range == 0.0:
            if abs(entity_mw - pmin_e) > 1e-9:
                raise SplitValidationError(
                    f"units have zero aggregate range [{pmin_e}, {pmax_e}] but "
                    f"entity_mw={entity_mw} != {pmin_e}"
                )
            result = {u.unit_id: u.active_characteristics.pmin_mw for u in units}
        else:
            fill_fraction = (entity_mw - pmin_e) / total_range
            result = {
                u.unit_id: u.active_characteristics.pmin_mw
                + fill_fraction
                * (u.active_characteristics.pmax_mw - u.active_characteristics.pmin_mw)
                for u in units
            }
        validate_split_result(entity_mw, units, result)
        return result

    def aggregate_limits(self, units: Sequence[PhysicalUnit]) -> tuple[float, float]:
        return (
            sum(u.active_characteristics.pmin_mw for u in units),
            sum(u.active_characteristics.pmax_mw for u in units),
        )

    def aggregate_ramp(
        self,
        units: Sequence[PhysicalUnit],
        telemetry: Mapping[UnitId, float],
        dt_min: float,
    ) -> AggregateRamp:
        alpha, beta = self._alpha_beta(units)
        return _drift_aware_aggregate_ramp(units, telemetry, dt_min, alpha, beta)

    @staticmethod
    def _alpha_beta(
        units: Sequence[PhysicalUnit],
    ) -> tuple[dict[UnitId, float], dict[UnitId, float]]:
        """SPEC §6.3: for range-based splitting, `alpha_i = (Pmax_i -
        Pmin_i) / sum_j(Pmax_j - Pmin_j)`, `beta_i = Pmin_i - alpha_i *
        sum_j(Pmin_j)`."""
        sum_pmin = sum(u.active_characteristics.pmin_mw for u in units)
        total_range = sum(
            u.active_characteristics.pmax_mw - u.active_characteristics.pmin_mw for u in units
        )
        alpha: dict[UnitId, float] = {}
        beta: dict[UnitId, float] = {}
        for u in units:
            unit_range = u.active_characteristics.pmax_mw - u.active_characteristics.pmin_mw
            a_i = unit_range / total_range if total_range != 0.0 else 0.0
            alpha[u.unit_id] = a_i
            beta[u.unit_id] = u.active_characteristics.pmin_mw - a_i * sum_pmin
        return alpha, beta


class PmaxProRata:
    """Pure Pmax pro-rata (SPEC §6.2) — kept **solely** so a test can
    demonstrate the failure mode `RangeProRata` exists to avoid: at low
    block output it can assign a member a setpoint below its own `Pmin`,
    producing a per-unit setpoint vector the plant cannot actually deliver.

    Deliberately does **not** call `validate_split_result` from `split()`:
    that would turn the demonstration into an exception instead of letting
    a test observe the actual (invalid) numbers, then hand them to
    `validate_split_result` directly to show it catches them. Not the
    default disaggregator; do not wire this into `build_entities()`.
    """

    def split(self, entity_mw: float, units: Sequence[PhysicalUnit]) -> dict[UnitId, float]:
        sum_pmax = sum(u.active_characteristics.pmax_mw for u in units)
        if sum_pmax == 0.0:
            raise SplitValidationError("units have zero aggregate Pmax")
        return {
            u.unit_id: entity_mw * u.active_characteristics.pmax_mw / sum_pmax for u in units
        }

    def aggregate_limits(self, units: Sequence[PhysicalUnit]) -> tuple[float, float]:
        return (
            sum(u.active_characteristics.pmin_mw for u in units),
            sum(u.active_characteristics.pmax_mw for u in units),
        )

    def aggregate_ramp(
        self,
        units: Sequence[PhysicalUnit],
        telemetry: Mapping[UnitId, float],
        dt_min: float,
    ) -> AggregateRamp:
        sum_pmax = sum(u.active_characteristics.pmax_mw for u in units)
        alpha = {
            u.unit_id: (u.active_characteristics.pmax_mw / sum_pmax if sum_pmax != 0.0 else 0.0)
            for u in units
        }
        beta = {u.unit_id: 0.0 for u in units}
        return _drift_aware_aggregate_ramp(units, telemetry, dt_min, alpha, beta)
