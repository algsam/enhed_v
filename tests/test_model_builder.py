"""Tests for ModelBuilder + BalanceModule (CLAUDE.md "Architecture";
SPEC §7, §9, §11; build order step 4: first end-to-end copperplate dispatch).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from ed.curves import from_incremental
from ed.domain.generator import Generator
from ed.model import ModelBuilder
from ed.modules import BalanceModule

# --- structural invariant: no type-dispatch in the builder (SPEC §9 rule 3,
# §11 "structural invariants") ---


def test_builder_module_has_no_type_dispatch() -> None:
    """Static check: `ed/model/builder.py` contains no `isinstance`, no
    `ResourceType.` switch, and no `is_virtual` branch. The ModelBuilder
    iterates entities via the uniform contribution contract and a registry
    of modules only — types exist everywhere except here.

    Parses the module's AST rather than grepping raw text so that a mention
    of "isinstance" inside a comment/docstring (as this very file's own
    docstrings do, describing the rule) can never trip the check; only an
    actual `isinstance(...)` call or `ResourceType.`/`is_virtual` attribute
    access in code counts as a hit.
    """
    source = (
        pathlib.Path(__file__).parent.parent / "ed" / "model" / "builder.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    hits: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
        ):
            hits.append("isinstance(...) call")
        if isinstance(node, ast.Attribute) and node.attr == "is_virtual":
            hits.append("is_virtual attribute access")
        if isinstance(node, ast.Name) and node.id == "ResourceType":
            hits.append("ResourceType reference")

    assert hits == [], f"type-dispatch found in ModelBuilder: {hits}"


# --- first end-to-end dispatch: two thermal units, copperplate, lambda out ---


def _flat_generator(bus: str, ic: float, cap_mw: float) -> Generator:
    """A staircase (constant-IC) generator: `Q_jj = 0`, pure LP segment."""
    curve = from_incremental(breakpoints_mw=(0.0, cap_mw), ic_values=(ic, ic))
    return Generator(bus=bus, cost_curve=curve)


def test_merit_order_cheap_unit_loads_first() -> None:
    """Cheap unit (IC=10, cap 50) fills to its cap before the expensive unit
    (IC=20, cap 50) contributes anything, for a load that requires both.
    Lambda equals the marginal (expensive) unit's own IC, since it is the
    one left interior at the optimum.
    """
    cheap = _flat_generator("bus1", ic=10.0, cap_mw=50.0)
    expensive = _flat_generator("bus1", ic=20.0, cap_mw=50.0)
    balance = BalanceModule(load_mw=70.0, voll=10_000.0, overgen_penalty=10_000.0)

    built = ModelBuilder(entities=[cheap, expensive], modules=[balance]).build()
    result = built.adapter.solve()

    assert cheap.dispatch_mw(result) == pytest.approx(50.0)
    assert expensive.dispatch_mw(result) == pytest.approx(20.0)
    assert balance.extract_price(result) == pytest.approx(20.0)
    assert not balance.is_scarce(result)


def test_two_identical_units_symmetric_load_split_equally() -> None:
    """Two identical interpolating-IC units split a load they can each
    partially serve equally, at a common lambda (SPEC §11 correctness
    acceptance criterion)."""
    curve = from_incremental(breakpoints_mw=(0.0, 100.0), ic_values=(10.0, 30.0))
    unit1 = Generator(bus="bus1", cost_curve=curve)
    unit2 = Generator(bus="bus1", cost_curve=curve)
    balance = BalanceModule(load_mw=100.0, voll=10_000.0, overgen_penalty=10_000.0)

    built = ModelBuilder(entities=[unit1, unit2], modules=[balance]).build()
    result = built.adapter.solve()

    assert unit1.dispatch_mw(result) == pytest.approx(50.0)
    assert unit2.dispatch_mw(result) == pytest.approx(50.0)
    expected_ic = 10.0 + 0.2 * 50.0
    assert balance.extract_price(result) == pytest.approx(expected_ic)


# --- lambda interpolates across a breakpoint (the whole point of PWL-IC) ---


def test_lambda_interpolates_continuously_across_a_breakpoint() -> None:
    """A single generator with an interpolating IC curve whose slope changes
    at MW=50 (IC: 10 -> 20 over [0,50], 20 -> 50 over [50,100]). Sweeping
    load across that breakpoint must show lambda varying *continuously*
    with load, matching the curve's own IC(P) exactly on both sides and
    landing on the same value at the breakpoint itself — no jump, unlike
    the LP staircase path where the marginal unit landing on a breakpoint
    is a genuine price discontinuity.
    """
    curve = from_incremental(breakpoints_mw=(0.0, 50.0, 100.0), ic_values=(10.0, 20.0, 50.0))

    def lambda_at_load(load_mw: float) -> float:
        generator = Generator(bus="bus1", cost_curve=curve)
        balance = BalanceModule(load_mw=load_mw, voll=10_000.0, overgen_penalty=10_000.0)
        built = ModelBuilder(entities=[generator], modules=[balance]).build()
        result = built.adapter.solve()
        assert not balance.is_scarce(result)
        return balance.extract_price(result)

    just_below = lambda_at_load(49.0)
    at_breakpoint = lambda_at_load(50.0)
    just_above = lambda_at_load(51.0)

    assert just_below == pytest.approx(10.0 + 0.2 * 49.0)
    assert at_breakpoint == pytest.approx(20.0)
    assert just_above == pytest.approx(20.0 + 0.6 * 1.0)

    # continuity: the jump either side of the breakpoint is bounded by the
    # steeper segment's own slope over that 1 MW step, not a discontinuous
    # price cliff.
    assert abs(at_breakpoint - just_below) < 0.2 + 1e-9
    assert abs(just_above - at_breakpoint) < 0.6 + 1e-9


# --- feasibility / control-room: never "infeasible" ---


def test_load_above_total_capacity_solves_with_scarcity() -> None:
    """Load exceeding total online capacity must still solve: unserved
    energy makes up the shortfall, lambda equals VoLL, and the scarcity
    flag is raised. Never "infeasible" (CLAUDE.md "Operational"; SPEC §11).
    """
    unit1 = _flat_generator("bus1", ic=10.0, cap_mw=50.0)
    unit2 = _flat_generator("bus1", ic=20.0, cap_mw=50.0)
    voll = 10_000.0
    balance = BalanceModule(load_mw=150.0, voll=voll, overgen_penalty=5_000.0)

    built = ModelBuilder(entities=[unit1, unit2], modules=[balance]).build()
    result = built.adapter.solve()

    assert unit1.dispatch_mw(result) == pytest.approx(50.0)
    assert unit2.dispatch_mw(result) == pytest.approx(50.0)
    assert balance.unserved_mw(result) == pytest.approx(50.0)
    assert balance.overgen_mw(result) == pytest.approx(0.0)
    assert balance.extract_price(result) == pytest.approx(voll)
    assert balance.is_scarce(result)


def test_load_below_zero_solves_with_overgeneration_scarcity() -> None:
    """Mirror of the above: over-generation, the other slack.

    Pmin/must-run floors are deferred to a later build-order step (Generator
    carries no floor yet — see its docstring), so this drives the same
    surplus condition directly through net load rather than waiting on that
    feature: a negative `load_mw` is net demand after a must-run floor, i.e.
    demand below the floor by 30 MW. Over-generation slack must absorb the
    surplus, unserved stays at zero, and — the other half of the sign
    convention that the undersupply case above doesn't exercise — lambda
    must go *negative*: during oversupply, an extra MW of demand is valuable
    (it soaks up surplus), not costly, so the marginal price is the negative
    of the over-generation penalty, not its magnitude. Never "infeasible"
    (CLAUDE.md "Operational"; SPEC §11).
    """
    unit = _flat_generator("bus1", ic=10.0, cap_mw=50.0)
    voll = 10_000.0
    overgen_penalty = 5_000.0
    balance = BalanceModule(load_mw=-30.0, voll=voll, overgen_penalty=overgen_penalty)

    built = ModelBuilder(entities=[unit], modules=[balance]).build()
    result = built.adapter.solve()

    assert unit.dispatch_mw(result) == pytest.approx(0.0)
    assert balance.overgen_mw(result) == pytest.approx(30.0)
    assert balance.unserved_mw(result) == pytest.approx(0.0)
    assert balance.extract_price(result) == pytest.approx(-overgen_penalty)
    assert balance.extract_price(result) < 0.0
    assert balance.is_scarce(result)
