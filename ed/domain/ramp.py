"""Ramp rate as a curve over output MW (CLAUDE.md "Domain rules").

Ramp rate is stored in MW/min. It is a curve vs MW — a constant rate is the
one-segment special case — because deliverable ramp capability can vary with
output level (used by both ramp constraints and reserve deliverability).
Conversion to MW/interval happens only at constraint-build time (RampRate * dt),
never stored, so the dispatch cycle length is never baked into the data.

SPEC §6.3's aggregate_ramp derivation is written in terms of scalar RU_i/RD_i.
`resolve_ramp_up_mw_per_min` / `resolve_ramp_down_mw_per_min` are the amendment
that reconciles this file with that section: they resolve a curve to a single
conservative scalar per cycle, from the unit's measured P0, before §6.3's
algebra runs — the algebra itself is unchanged. See SPEC §6.3 for the full
argument for conservatism (point evaluation can overstate capability across a
breakpoint).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator


class RampRateCurve(BaseModel):
    """Ramp rate (MW/min) as a step function of output MW.

    `breakpoints_mw` are ascending MW levels; `rates_mw_per_min[i]` is the rate
    applicable at/above `breakpoints_mw[i]`. A single breakpoint is the constant
    (one-segment) case.
    """

    model_config = ConfigDict(frozen=True)

    breakpoints_mw: tuple[float, ...]
    rates_mw_per_min: tuple[float, ...]

    @model_validator(mode="after")
    def _check_shape(self) -> RampRateCurve:
        if len(self.breakpoints_mw) == 0:
            raise ValueError("RampRateCurve requires at least one breakpoint")
        if len(self.breakpoints_mw) != len(self.rates_mw_per_min):
            raise ValueError("breakpoints_mw and rates_mw_per_min must be the same length")
        if any(r < 0 for r in self.rates_mw_per_min):
            raise ValueError("ramp rates must be non-negative")
        if list(self.breakpoints_mw) != sorted(self.breakpoints_mw):
            raise ValueError("breakpoints_mw must be strictly ascending")
        return self

    @classmethod
    def constant(cls, rate_mw_per_min: float, pmin_mw: float) -> RampRateCurve:
        """The one-segment case: a single rate applicable across the whole range."""
        return cls(breakpoints_mw=(pmin_mw,), rates_mw_per_min=(rate_mw_per_min,))

    def rate_at_mw(self, output_mw: float) -> float:
        """Ramp rate (MW/min) applicable at the given output level (point evaluation).

        Point evaluation alone is not safe for resolving a per-cycle scalar
        ramp limit — see `resolve_ramp_up_mw_per_min` / `resolve_ramp_down_mw_per_min`.
        """
        applicable = self.rates_mw_per_min[0]
        for bp, rate in zip(self.breakpoints_mw, self.rates_mw_per_min, strict=True):
            if output_mw >= bp:
                applicable = rate
            else:
                break
        return applicable

    def min_rate_over_range(self, lo_mw: float, hi_mw: float) -> float:
        """Minimum rate over every segment that overlaps [lo_mw, hi_mw].

        Used to conservatively resolve a curve to a single scalar over a
        reachable band, rather than trusting the rate at one point.
        """
        lo, hi = (lo_mw, hi_mw) if lo_mw <= hi_mw else (hi_mw, lo_mw)
        if lo == hi:
            return self.rate_at_mw(lo)

        n = len(self.breakpoints_mw)
        matched: list[float] = []
        for i in range(n):
            seg_start = self.breakpoints_mw[i]
            seg_end = self.breakpoints_mw[i + 1] if i + 1 < n else float("inf")
            if seg_end > lo and seg_start < hi:
                matched.append(self.rates_mw_per_min[i])
        if not matched:
            # [lo, hi] lies entirely below the first breakpoint: the curve is
            # flat-extrapolated there (rate_at_mw's default), same as above.
            matched = [self.rate_at_mw(lo)]
        return min(matched)


def resolve_ramp_up_mw_per_min(curve: RampRateCurve, p0_mw: float, dt_min: float) -> float:
    """Conservative per-cycle scalar resolution of a ramp-up curve (SPEC §6.3 amendment).

    Point evaluation at P0 can overstate capability when the reachable band
    crosses into a slower segment: e.g. 8 MW/min below 100 MW, 3 MW/min above,
    P0 = 90 MW, dt = 5 min — point evaluation reports 8 MW/min and commands 40
    MW of headroom, but the unit crosses 100 MW after 10 MW of movement and
    cannot sustain 8 MW/min past that. Commanding a base point the plant
    cannot follow is exactly the failure mode §6.3's min-not-sum rule exists
    to prevent for CC blocks; the same hazard exists for a single unit's own
    curve.

    Resolve conservatively instead: seed with the point rate, compute the
    reachable band it implies, then take the minimum rate over every segment
    that band touches. A single pass is sufficient — shrinking the rate only
    ever shrinks the band, never grows it back out to a slower segment.
    """
    seed = curve.rate_at_mw(p0_mw)
    band_upper = p0_mw + seed * dt_min
    return curve.min_rate_over_range(p0_mw, band_upper)


def resolve_ramp_down_mw_per_min(curve: RampRateCurve, p0_mw: float, dt_min: float) -> float:
    """Conservative per-cycle scalar resolution of a ramp-down curve. See
    `resolve_ramp_up_mw_per_min`; symmetric, over the band below P0."""
    seed = curve.rate_at_mw(p0_mw)
    band_lower = p0_mw - seed * dt_min
    return curve.min_rate_over_range(band_lower, p0_mw)
