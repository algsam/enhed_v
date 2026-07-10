"""PhysicalUnit — what the field sees (SPEC §5.1, §5.2, §5.3).

Owns telemetry (P0), physical limits, ramp rates, a bus, and mode-keyed
characteristics. Receives a setpoint (via direct dispatch or disaggregation).

HRV is real telemetry (diverter damper position), not block membership (SPEC
§5.2) — whether a unit is *actually* running standalone vs inside a CC block is
derived (in entities/build_entities, a later build-order step) from the
online/hrv/steam_source vector, never dispatched off a stored flag that can
drift.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from ed.domain.enums import Mode, ResourceType, SteamSource
from ed.domain.ramp import RampRateCurve


class UnitCharacteristics(BaseModel):
    """Physical characteristics for one operating Mode of a unit (SPEC §5.3).

    A CT's SIMPLE_CYCLE characteristics differ from its COMBINED_CYCLE
    characteristics: different Pmax, ramp rates, heat rate, emissions rate.
    """

    model_config = ConfigDict(frozen=True)

    pmin_mw: float
    pmax_mw: float
    ramp_up: RampRateCurve
    ramp_down: RampRateCurve

    @model_validator(mode="after")
    def _check_bounds(self) -> UnitCharacteristics:
        if self.pmin_mw < 0:
            raise ValueError("pmin_mw must be non-negative")
        if self.pmax_mw < self.pmin_mw:
            raise ValueError("pmax_mw must be >= pmin_mw")
        return self


class PhysicalUnit(BaseModel):
    """A real generating unit in the field. See module docstring."""

    model_config = ConfigDict(frozen=True)

    unit_id: str
    bus: str
    resource_type: ResourceType
    characteristics: dict[Mode, UnitCharacteristics]
    active_mode: Mode
    p0_mw: float
    online: bool
    hrv: bool = False
    steam_source: SteamSource = SteamSource.NONE
    reserve_eligible: bool = False

    @model_validator(mode="after")
    def _check_consistency(self) -> PhysicalUnit:
        if self.active_mode not in self.characteristics:
            raise ValueError(
                f"unit {self.unit_id}: active_mode {self.active_mode} has no "
                "entry in characteristics"
            )
        if (
            self.resource_type is ResourceType.STEAM
            and self.online
            and self.steam_source is SteamSource.NONE
        ):
            raise ValueError(
                f"unit {self.unit_id}: an online steam turbine must have a "
                "steam_source (HRSG or AUX_BOILER), not NONE"
            )
        return self

    @property
    def active_characteristics(self) -> UnitCharacteristics:
        return self.characteristics[self.active_mode]

    @property
    def emits_setpoint(self) -> bool:
        """Does this go to a governor? Every online physical unit eventually
        receives a setpoint (directly, or via disaggregation from its block)."""
        return self.online

    @property
    def is_system_generated(self) -> bool:
        """Physical units are always real-world/user-configured, never
        engine-created (that is what distinguishes them from Slack)."""
        return False
