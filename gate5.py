from ed.disagg.range_pro_rata import RangeProRata
from ed.domain.enums import Mode, ResourceType, SteamSource
from ed.domain.physical_unit import PhysicalUnit, UnitCharacteristics
from ed.domain.ramp import RampRateCurve


def make_unit(name, pmin, pmax, ru, rd, p0):
    chars = UnitCharacteristics(
        pmin_mw=pmin,
        pmax_mw=pmax,
        ramp_up=RampRateCurve.constant(ru, pmin),
        ramp_down=RampRateCurve.constant(rd, pmin),
    )
    return PhysicalUnit(
        unit_id=name,
        bus="bus1",
        resource_type=ResourceType.THERMAL,
        characteristics={Mode.COMBINED_CYCLE: chars},
        active_mode=Mode.COMBINED_CYCLE,
        p0_mw=p0,
        online=True,
        hrv=True,
        steam_source=SteamSource.NONE,
    )


# Two units. Same range so alpha is easy: alpha_i = 0.5 each.
# CT: fast, RU = 10 MW/min.  ST: slow, RU = 2 MW/min.
# Pmin 0, Pmax 100 both -> range 100 each, total range 200.
units = [
    make_unit("CT", pmin=0.0, pmax=100.0, ru=10.0, rd=10.0, p0=50.0),
    make_unit("ST", pmin=0.0, pmax=100.0, ru=2.0, rd=2.0, p0=50.0),
]

d = RangeProRata()  # stateless -- no constructor args
dt = 5.0  # minutes

# aggregate_ramp(units, telemetry, dt_min) -- telemetry maps unit_id -> measured P0
agg = d.aggregate_ramp(units, {"CT": 50.0, "ST": 50.0}, dt)

print("RU_e =", agg.ru_mw_per_min, "MW/min")
print("sum RU_i * dt =", (10 + 2) * dt)  # 60 -- the WRONG answer
print("RU_e * dt     =", agg.ru_mw_per_min * dt)  # 20 -- the correct, min-over-units answer
