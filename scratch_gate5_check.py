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


units = [
    make_unit("CT", pmin=0.0, pmax=100.0, ru=10.0, rd=10.0, p0=50.0),
    make_unit("ST", pmin=0.0, pmax=100.0, ru=2.0, rd=2.0, p0=50.0),
]
d = RangeProRata()
dt = 5.0

print("=" * 70)
print("Case 1: zero drift, both measured at 50")
telemetry = {"CT": 50.0, "ST": 50.0}
agg = d.aggregate_ramp(units, telemetry, dt)
alpha_ct, alpha_st = 0.5, 0.5
print(f"alpha_CT = {alpha_ct}, alpha_ST = {alpha_st}")
print(f"candidate CT: RU_CT/alpha_CT = {10.0/alpha_ct}")
print(f"candidate ST: RU_ST/alpha_ST = {2.0/alpha_st}")
print(f"RU_e = {agg.ru_mw_per_min} MW/min   (RU_e*dt = {agg.ru_mw_per_min*dt} MW)")
print(f"clamped_up={agg.clamped_up} diagnostics={agg.diagnostics}")
binder = "ST" if 2.0/alpha_st < 10.0/alpha_ct else "CT"
print(f"binding unit: {binder}")

print("=" * 70)
print("Case 2: ST measured at 42 (drifted below its manifold value)")
telemetry2 = {"CT": 50.0, "ST": 42.0}
p_e0 = sum(telemetry2.values())
manifold_st = alpha_st * p_e0 + 0.0
d_st = manifold_st - telemetry2["ST"]
print(f"P_e0 = {p_e0}, manifold ST (alpha*P_e0) = {manifold_st}, drift d_ST = {d_st}")
agg2 = d.aggregate_ramp(units, telemetry2, dt)
candidate_st_2 = (2.0 * dt - d_st) / alpha_st
manifold_ct = alpha_ct * p_e0 + 0.0
d_ct = manifold_ct - telemetry2["CT"]
candidate_ct_2 = (10.0 * dt - d_ct) / alpha_ct
print(f"candidate CT (raw MW over dt) = {candidate_ct_2}")
print(f"candidate ST (raw MW over dt) = {candidate_st_2}")
print(f"RU_e = {agg2.ru_mw_per_min} MW/min  vs zero-drift RU_e = {agg.ru_mw_per_min} MW/min")
print(f"clamped_up={agg2.clamped_up} diagnostics={agg2.diagnostics}")

print("=" * 70)
print("Case 3: ST measured at 20 (drifted beyond one interval's recovery)")
telemetry3 = {"CT": 50.0, "ST": 20.0}
p_e0_3 = sum(telemetry3.values())
manifold_st_3 = alpha_st * p_e0_3
d_st_3 = manifold_st_3 - telemetry3["ST"]
candidate_st_3 = (2.0 * dt - d_st_3) / alpha_st
print(f"P_e0 = {p_e0_3}, manifold ST = {manifold_st_3}, drift d_ST = {d_st_3}")
print(f"raw candidate ST (would-be MW over dt) = {candidate_st_3}")
agg3 = d.aggregate_ramp(units, telemetry3, dt)
print(f"RU_e = {agg3.ru_mw_per_min} MW/min")
print(f"clamped_up = {agg3.clamped_up}")
print(f"diagnostics = {agg3.diagnostics}")
assert agg3.ru_mw_per_min == 0.0
assert agg3.ru_mw_per_min >= 0.0
assert agg3.clamped_up
assert len(agg3.diagnostics) >= 1
print("Confirmed: clamps to exactly 0.0, does not go negative, diagnostic present, no exception raised.")
