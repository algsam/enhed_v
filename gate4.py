from ed.curves.ingest import from_incremental
from ed.domain.generator import Generator
from ed.model import ModelBuilder
from ed.modules import BalanceModule

curve = from_incremental(breakpoints_mw=(0, 50, 100), ic_values=(20, 25, 40))  # kink at 50

for load in [40, 48, 49, 50, 51, 52, 60]:
    g = Generator(bus="B", cost_curve=curve)
    balance = BalanceModule(load_mw=load, voll=10_000.0, overgen_penalty=10_000.0)
    built = ModelBuilder(entities=[g], modules=[balance]).build()
    result = built.adapter.solve()
    print(f"load={load:3}  P={g.dispatch_mw(result):6.2f}  lambda={balance.extract_price(result):7.3f}")