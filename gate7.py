# Non-binding reserve requirement: both modes must give IDENTICAL energy dispatch
# Two gens, load 150, plenty of headroom, tiny reserve req that binds nothing.
from ed.curves import from_incremental
from ed.domain.generator import Generator
from ed.model import ModelBuilder
from ed.modules import BalanceModule
from ed.modules.reserve import build_reserve_module, ReserveMode


def build_and_solve(mode: ReserveMode, req_mw: float):
    cheap = Generator(
        bus="bus1",
        cost_curve=from_incremental(breakpoints_mw=(0.0, 100.0), ic_values=(10.0, 10.0)),
        reserve_eligible=True,
        ramp_up_mw_per_min=1000.0,
    )
    expensive = Generator(
        bus="bus1",
        cost_curve=from_incremental(breakpoints_mw=(0.0, 100.0), ic_values=(20.0, 20.0)),
        reserve_eligible=True,
        ramp_up_mw_per_min=1000.0,
    )
    balance = BalanceModule(load_mw=150.0, voll=10_000.0, overgen_penalty=10_000.0)
    reserve = build_reserve_module(
        mode=mode,
        participants=[cheap, expensive],
        product="spin",
        requirement_mw=req_mw,
        t_reserve_min=10.0,
        shortfall_penalty=10_000.0 if mode is ReserveMode.PER_UNIT_COOPTIMIZATION else None,
    )
    built = ModelBuilder(entities=[cheap, expensive], modules=[balance, reserve]).build()
    result = built.adapter.solve()
    dispatch = {"cheap": cheap.dispatch_mw(result), "expensive": expensive.dispatch_mw(result)}
    lam = balance.extract_price(result)
    return dispatch, lam


for mode in [ReserveMode.AGGREGATE_HEADROOM, ReserveMode.PER_UNIT_COOPTIMIZATION]:
    dispatch, lam = build_and_solve(mode, req_mw=5.0)
    print(mode.name, {n: round(p, 3) for n, p in dispatch.items()}, "lambda", round(lam, 3))