from ed.curves import from_incremental
from ed.domain.enums import Mode, ResourceType, SteamSource
from ed.domain.physical_unit import PhysicalUnit, UnitCharacteristics
from ed.domain.ramp import RampRateCurve
from ed.domain.cc_block import CCBlock, CCBlockConfig, CCBlockRoster
from ed.domain.generator import Generator
from ed.entities import build_entities

ct1 = PhysicalUnit(
    unit_id="CT1", bus="bus1", resource_type=ResourceType.THERMAL,
    characteristics={Mode.SIMPLE_CYCLE: UnitCharacteristics(
        pmin_mw=20, pmax_mw=100,
        ramp_up=RampRateCurve.constant(5, 20), ramp_down=RampRateCurve.constant(5, 20),
        cost_curve=from_incremental(breakpoints_mw=(20.0, 100.0), ic_values=(15.0, 25.0)),
    )},
    active_mode=Mode.SIMPLE_CYCLE, p0_mw=50, online=True, hrv=False,
)

ct2 = PhysicalUnit(
    unit_id="CT2", bus="bus1", resource_type=ResourceType.THERMAL,
    characteristics={Mode.COMBINED_CYCLE: UnitCharacteristics(
        pmin_mw=20, pmax_mw=100,
        ramp_up=RampRateCurve.constant(5, 20), ramp_down=RampRateCurve.constant(5, 20),
    )},
    active_mode=Mode.COMBINED_CYCLE, p0_mw=60, online=True, hrv=True,
)

st1 = PhysicalUnit(
    unit_id="ST1", bus="bus1", resource_type=ResourceType.STEAM,
    characteristics={Mode.COMBINED_CYCLE: UnitCharacteristics(
        pmin_mw=30, pmax_mw=80,
        ramp_up=RampRateCurve.constant(1, 30), ramp_down=RampRateCurve.constant(1, 30),
    )},
    active_mode=Mode.COMBINED_CYCLE, p0_mw=40, online=True, steam_source=SteamSource.HRSG,
)

agg_curve = from_incremental(breakpoints_mw=(50.0, 150.0), ic_values=(10.0, 30.0))
config = CCBlockConfig(config_id="CT2_ST1", active_members=frozenset({"CT2", "ST1"}), cost_curve=agg_curve)
block = CCBlockRoster(block_id="BLOCK1", bus="bus1", member_unit_ids=frozenset({"CT2", "ST1"}), configs=(config,))

result = build_entities(units=[ct1, ct2, st1], blocks=[block])


def label(e):
    return e.block_id if isinstance(e, CCBlock) else "standalone"


print([label(e) for e in result.entities])   # expect exactly 2 entries
print(len(result.entities))                  # 2, not 3, not 1
print(result.entity_members)                 # {'CT1': {'CT1'}, 'BLOCK1': {'CT2', 'ST1'}}

# confirm CT2/ST1 do not also appear as standalone Generators
standalone_units = {e.bus for e in result.entities if isinstance(e, Generator)}
assert not any(
    isinstance(e, CCBlock) and {u.unit_id for u in e.member_units} & {"CT1"}
    for e in result.entities
)
print("CC block members:", {u.unit_id for e in result.entities if isinstance(e, CCBlock) for u in e.member_units})
