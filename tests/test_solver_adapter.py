"""Tests for SolverAdapter (CLAUDE.md "Solver"; SPEC §3, build order step 3).

The hand-solvable two-unit QP: unit 1 has cost `10*P1 + 0.05*P1^2` (c=10,
Q_11=0.1), unit 2 has cost `12*P2 + 0.025*P2^2` (c=12, Q_22=0.05), balance
`P1 + P2 = 100`, bounds `[0, 100]` each.

By hand: at the optimum both units are marginal (interior, KKT stationarity
active on both), so `10 + 0.1*P1 = 12 + 0.05*P2 = lambda` with `P2 = 100 -
P1`. Substituting: `0.15*P1 = 2 + 0.05*100 = 7`, `P1 = 46.6666...`, `P2 =
53.3333...`, `lambda = 10 + 0.1*46.6666... = 14.6666...`. Both outputs land
inside `[0, 100]`, confirming the interior-solution assumption.
"""

from __future__ import annotations

import math

import pytest

from ed.solver import SolveError, SolverAdapter


def test_two_unit_qp_matches_hand_solution() -> None:
    adapter = SolverAdapter()
    p1 = adapter.add_var(cost=10.0, lower=0.0, upper=100.0, hessian_diag=0.1)
    p2 = adapter.add_var(cost=12.0, lower=0.0, upper=100.0, hessian_diag=0.05)
    balance = adapter.add_row(lower=100.0, upper=100.0, coefficients={p1: 1.0, p2: 1.0})

    result = adapter.solve()

    expected_p1 = 46.0 + 2.0 / 3.0
    expected_p2 = 53.0 + 1.0 / 3.0
    expected_lambda = 10.0 + 0.1 * expected_p1

    assert result.status == "Optimal"
    assert result.primal[p1] == pytest.approx(expected_p1)
    assert result.primal[p2] == pytest.approx(expected_p2)
    assert result.row_duals[balance] == pytest.approx(expected_lambda)

    # lambda must equal the marginal (interior) unit's own IC at its
    # optimal output for *both* units, since both are interior here.
    ic1 = 10.0 + 0.1 * result.primal[p1]
    ic2 = 12.0 + 0.05 * result.primal[p2]
    assert result.row_duals[balance] == pytest.approx(ic1)
    assert result.row_duals[balance] == pytest.approx(ic2)


def test_duals_are_finite() -> None:
    adapter = SolverAdapter()
    p1 = adapter.add_var(cost=10.0, lower=0.0, upper=100.0, hessian_diag=0.1)
    p2 = adapter.add_var(cost=12.0, lower=0.0, upper=100.0, hessian_diag=0.05)
    adapter.add_row(lower=100.0, upper=100.0, coefficients={p1: 1.0, p2: 1.0})

    result = adapter.solve()

    assert result.row_duals
    assert all(math.isfinite(d) for d in result.row_duals.values())
    assert math.isfinite(result.objective)
    assert result.iteration_count >= 0
    assert result.solve_time_s >= 0.0


def test_lp_path_zero_hessian_gives_corner_solution() -> None:
    """cost=0 hessian_diag (staircase / LP segment): merit order picks the
    cheaper unit fully before the pricier one, same as any LP dispatch.
    """
    adapter = SolverAdapter()
    p1 = adapter.add_var(cost=10.0, lower=0.0, upper=60.0, hessian_diag=0.0)
    p2 = adapter.add_var(cost=20.0, lower=0.0, upper=60.0, hessian_diag=0.0)
    balance = adapter.add_row(lower=80.0, upper=80.0, coefficients={p1: 1.0, p2: 1.0})

    result = adapter.solve()

    assert result.primal[p1] == pytest.approx(60.0)
    assert result.primal[p2] == pytest.approx(20.0)
    assert result.row_duals[balance] == pytest.approx(20.0)


def test_negative_hessian_diag_rejected() -> None:
    adapter = SolverAdapter()
    with pytest.raises(ValueError, match="Q must be PSD"):
        adapter.add_var(cost=10.0, lower=0.0, upper=100.0, hessian_diag=-0.1)


def test_infeasible_bounds_raise_solve_error() -> None:
    adapter = SolverAdapter()
    p1 = adapter.add_var(cost=10.0, lower=0.0, upper=10.0, hessian_diag=0.1)
    # balance row demands 100 but p1 can supply at most 10 -> infeasible.
    adapter.add_row(lower=100.0, upper=100.0, coefficients={p1: 1.0})

    with pytest.raises(SolveError):
        adapter.solve()


def test_var_and_row_handles_are_opaque_ints_but_distinct_types() -> None:
    from ed.solver import RowHandle, VarHandle

    adapter = SolverAdapter()
    v = adapter.add_var(cost=1.0, lower=0.0, upper=1.0)
    r = adapter.add_row(lower=0.0, upper=1.0, coefficients={v: 1.0})

    assert isinstance(v, int)  # NewType erases at runtime; still int-valued
    assert isinstance(r, int)
    assert VarHandle(0) == 0
    assert RowHandle(0) == 0


def test_importing_ed_solver_performs_no_solve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the package must be side-effect-free: no QP is solved by
    merely doing `import ed.solver` (fix requested after stage 3 review —
    the startup capability check must be an explicit call, not an
    import-time side effect that every test collection / mypy run pays for
    and that cannot be caught or skipped).
    """
    import importlib

    import ed.solver as solver_pkg
    import ed.solver.adapter as adapter_mod

    calls = []
    original_solve = adapter_mod.SolverAdapter.solve

    def spy_solve(self: adapter_mod.SolverAdapter) -> adapter_mod.SolveResult:
        calls.append(1)
        return original_solve(self)

    monkeypatch.setattr(adapter_mod.SolverAdapter, "solve", spy_solve)
    importlib.reload(solver_pkg)

    assert calls == []


# --- dual sign convention (CLAUDE.md/SPEC §3 "prices come from duals") ---
#
# Pinned per the adapter module docstring: a row dual is d(objective)/d(rhs)
# for the minimization HiGHS solves, passed through unchanged from HiGHS's
# own convention (no sign flip in this adapter). These tests assert the
# *signed* value, not `abs()`, so a HiGHS regression that flips a sign would
# fail loudly here rather than silently comparing equal after `abs()`.


def test_equality_row_dual_is_positive_when_marginal() -> None:
    """Restates the sign half of test_two_unit_qp_matches_hand_solution
    explicitly: the balance row's dual (lambda) must be +14.6666..., not
    merely abs(14.6666...).
    """
    adapter = SolverAdapter()
    p1 = adapter.add_var(cost=10.0, lower=0.0, upper=100.0, hessian_diag=0.1)
    p2 = adapter.add_var(cost=12.0, lower=0.0, upper=100.0, hessian_diag=0.05)
    balance = adapter.add_row(lower=100.0, upper=100.0, coefficients={p1: 1.0, p2: 1.0})

    result = adapter.solve()

    assert result.row_duals[balance] == pytest.approx(14.0 + 2.0 / 3.0)
    assert result.row_duals[balance] > 0.0


def test_binding_ge_row_dual_is_positive_reserve_shadow_price() -> None:
    """Toy reserve requirement: R1 in [0,10] costs 5/MW, R2 in [0,10] costs
    8/MW, `R1 + R2 >= 15`. By hand: fill the cheap unit first, R1=10 (its
    upper bound), R2=5. The binding requirement's shadow price is the cost
    of the next (marginal) MW of reserve, which must come from R2 since R1
    is already maxed: `d(obj)/d(Req) = 8.0`, positive.
    """
    adapter = SolverAdapter()
    r1 = adapter.add_var(cost=5.0, lower=0.0, upper=10.0)
    r2 = adapter.add_var(cost=8.0, lower=0.0, upper=10.0)
    req = adapter.add_row(lower=15.0, upper=math.inf, coefficients={r1: 1.0, r2: 1.0})

    result = adapter.solve()

    assert result.primal[r1] == pytest.approx(10.0)
    assert result.primal[r2] == pytest.approx(5.0)
    assert result.row_duals[req] == pytest.approx(8.0)
    assert result.row_duals[req] >= 0.0


def test_slack_ge_row_dual_is_zero() -> None:
    """Same requirement row shape, but R1's own lower bound (8) alone
    already exceeds the requirement (5) regardless of R2 — the row is slack
    (R1 + R2 = 8 > 5) and its dual must be exactly 0.
    """
    adapter = SolverAdapter()
    r1 = adapter.add_var(cost=5.0, lower=8.0, upper=10.0)
    r2 = adapter.add_var(cost=8.0, lower=0.0, upper=10.0)
    req = adapter.add_row(lower=5.0, upper=math.inf, coefficients={r1: 1.0, r2: 1.0})

    result = adapter.solve()

    assert result.primal[r1] == pytest.approx(8.0)
    assert result.primal[r2] == pytest.approx(0.0)
    assert result.row_duals[req] == pytest.approx(0.0)


def test_binding_le_row_dual_is_negative_capacity_shadow_price() -> None:
    """Toy stand-in for a future line-flow limit (SPEC §5.7/§9 network
    seam): a variable with *negative* cost (economically beneficial to
    maximize, like flow toward a cheaper region) capped by `x <= 20`. By
    hand: x=20 (the limit binds), objective=-5*20=-100. Relaxing the limit
    by 1 MW would lower the objective by another 5, i.e.
    `d(obj)/d(limit) = -5.0` — negative, per this adapter's pinned
    convention for a binding `<=` row.
    """
    adapter = SolverAdapter()
    x = adapter.add_var(cost=-5.0, lower=0.0, upper=100.0)
    limit = adapter.add_row(lower=-math.inf, upper=20.0, coefficients={x: 1.0})

    result = adapter.solve()

    assert result.primal[x] == pytest.approx(20.0)
    assert result.objective == pytest.approx(-100.0)
    assert result.row_duals[limit] == pytest.approx(-5.0)
    assert result.row_duals[limit] <= 0.0


def test_slack_le_row_dual_is_zero() -> None:
    adapter = SolverAdapter()
    x = adapter.add_var(cost=-5.0, lower=0.0, upper=10.0)
    limit = adapter.add_row(lower=-math.inf, upper=20.0, coefficients={x: 1.0})

    result = adapter.solve()

    assert result.primal[x] == pytest.approx(10.0)
    assert result.row_duals[limit] == pytest.approx(0.0)


def test_one_sided_row_bound_accepts_math_inf() -> None:
    """`add_row`/`add_var` accept `math.inf`/`-math.inf` directly for a
    one-sided bound (e.g. a reserve requirement's `upper=math.inf`) without
    the caller ever touching HiGHS's own infinity sentinel — `_bound()`
    handles that translation inside the adapter.
    """
    adapter = SolverAdapter()
    x = adapter.add_var(cost=1.0, lower=0.0, upper=math.inf)
    row = adapter.add_row(lower=5.0, upper=math.inf, coefficients={x: 1.0})

    result = adapter.solve()

    assert result.primal[x] == pytest.approx(5.0)
    assert result.row_duals[row] == pytest.approx(1.0)
