"""SolverAdapter — the only module that imports `highspy` (CLAUDE.md
"Solver"; SPEC §3, §9, build order step 3).

Wraps HiGHS behind opaque `VarHandle`/`RowHandle` wrappers so that "no other
module touches solver specifics" is a type-checkable invariant rather than a
convention: no bare HiGHS column/row index is ever handed back to a caller.

Builds and solves a convex QP with a diagonal Hessian. An LP is not a
separate code path here — it is the special case where no variable is given
a nonzero `hessian_diag` (CLAUDE.md "Cost curves": a staircase segment is
`left == right`, `Q_jj == 0`; the same `add_var` handles both).

Prices come from row duals (CLAUDE.md "Solver": power-balance dual = λ,
reserve-requirement dual = reserve price) — never computed any other way.
Call `assert_qp_duals_supported()` once from application startup (not from
this module or its package `__init__` — see `ed/solver/__init__.py`) to fail
loudly if a HiGHS regression ever silently drops duals on the QP path, since
this engine has no other source of prices.

**Dual sign convention — pinned here, leaks no further.** A row dual is
`d(objective)/d(rhs)`: how much the optimal objective would change per unit
of relaxing/tightening that row's bound, for the minimization HiGHS actually
solves. This is HiGHS's *native* convention already and is passed through
unchanged (verified empirically and pinned by `tests/test_solver_adapter.py`
rather than assumed) — no sign flip happens in this adapter:

- **Equality row** (`lower == upper`, e.g. power balance): dual is the
  marginal price, **positive** when a real (non-slack) resource is on the
  margin. This is λ, used as-is.
- **`>=` row** (`upper = +inf`, e.g. a reserve requirement `sum(R) >= Req`):
  dual is **>= 0 when binding** (tightening the requirement can only raise
  or hold the minimized cost) and **exactly 0 when slack**.
- **`<=` row** (`lower = -inf`, e.g. a future line-flow limit): dual is
  **<= 0 when binding** (relaxing the limit can only lower or hold the
  minimized cost) and **exactly 0 when slack**. Any caller reporting this
  as a "congestion price" negates it explicitly at the call site — this
  adapter reports the raw `d(obj)/d(rhs)` sensitivity, never a pre-negated
  number, so there is exactly one sign convention to remember.

If a future HiGHS version ever returns a different raw sign for any of
these row shapes, the fix belongs **inside this adapter** (normalise back to
the convention above), never at a call site — that is what "one convention,
never leaked" means in practice.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import NewType

import highspy
import numpy as np

logger = logging.getLogger(__name__)

VarHandle = NewType("VarHandle", int)
RowHandle = NewType("RowHandle", int)


class SolverCapabilityError(RuntimeError):
    """The installed HiGHS does not return finite row duals on the QP path.

    Those duals are the engine's only source of prices (CLAUDE.md "Solver");
    silently falling back to zero would produce a plausible-looking but
    fabricated λ. Raised by `assert_qp_duals_supported`, which application
    startup (not this package's import) calls explicitly, so the failure
    surfaces once at process startup, not on some later dispatch cycle in a
    control room, and not as an uncatchable import-time error either.
    """


class SolveError(RuntimeError):
    """`solve()` did not reach a status that carries a primal/dual solution."""


@dataclass(frozen=True)
class SolveResult:
    """Everything CLAUDE.md's "Solver" section requires be exposed."""

    status: str
    objective: float
    primal: dict[VarHandle, float]
    row_duals: dict[RowHandle, float]
    iteration_count: int
    solve_time_s: float


def _check(status: highspy.HighsStatus, op: str) -> None:
    if status == highspy.HighsStatus.kError:
        raise SolveError(f"HiGHS call {op!r} returned kError")
    if status == highspy.HighsStatus.kWarning:
        logger.warning("HiGHS call %r returned a warning", op)


class SolverAdapter:
    """One HiGHS model per instance. Build with `add_var`/`add_row`, then
    `solve()` once; not meant to be reused for a second, different model —
    callers that need a fresh model construct a fresh `SolverAdapter`.
    """

    def __init__(self) -> None:
        self._highs = highspy.Highs()  # type: ignore[no-untyped-call]
        _check(self._highs.setOptionValue("output_flag", False), "setOptionValue")
        self._num_vars = 0
        self._num_rows = 0
        # Column index -> diagonal Hessian entry. Only nonzero entries are
        # stored; a column absent here contributes Q_jj = 0 (LP segment).
        self._hessian_diag: dict[int, float] = {}

    def _bound(self, value: float) -> float:
        """Map a caller-supplied `+-math.inf` to HiGHS's own infinity
        sentinel (`Highs.getInfinity()`) before it reaches the solver.

        In the installed HiGHS/highspy build this sentinel *is* `math.inf`,
        so this is currently a no-op — but that equality is a HiGHS
        implementation detail (older builds use a large finite sentinel
        like `1e30`), not a contract. Routing every bound through here means
        a one-sided row (`lower=Req, upper=+inf`) never materialises an
        actual infinite coefficient or literal into the solver call, and
        stays correct if that sentinel ever changes.
        """
        if value == math.inf:
            return self._highs.getInfinity()
        if value == -math.inf:
            return -self._highs.getInfinity()
        return value

    def add_var(
        self, cost: float, lower: float, upper: float, hessian_diag: float = 0.0
    ) -> VarHandle:
        """Add one decision variable (e.g. one QP segment fill `p_j`).

        `cost` is the linear objective coefficient (`c_j`); `hessian_diag`
        is `Q_jj` (SPEC §4.2's diagonal Hessian entry — the segment's IC
        slope). Leaving it at 0.0 gives a pure LP column.
        """
        if hessian_diag < 0.0:
            raise ValueError(
                f"hessian_diag={hessian_diag} < 0: Q must be PSD for HiGHS's QP path "
                "(CLAUDE.md: 'HiGHS solves LP, MILP, and convex QP')"
            )
        _check(self._highs.addVar(self._bound(lower), self._bound(upper)), "addVar")
        col = self._num_vars
        self._num_vars += 1
        _check(self._highs.changeColCost(col, cost), "changeColCost")
        if hessian_diag != 0.0:
            self._hessian_diag[col] = hessian_diag
        return VarHandle(col)

    def add_row(
        self, lower: float, upper: float, coefficients: dict[VarHandle, float]
    ) -> RowHandle:
        """Add one constraint row, e.g. the power-balance row whose dual is
        λ, or a reserve-requirement row whose dual is the reserve price.

        `lower == upper` encodes an equality row. A one-sided row is a
        finite bound on one side and `math.inf`/`-math.inf` on the other
        (e.g. a reserve requirement is `lower=Req, upper=math.inf`) — see
        `_bound` for how that infinity is handled; the caller never needs to
        know or use HiGHS's own sentinel.
        """
        indices = np.array([int(v) for v in coefficients], dtype=np.int32)
        values = np.array(list(coefficients.values()), dtype=np.float64)
        _check(
            self._highs.addRow(
                self._bound(lower), self._bound(upper), len(indices), indices, values
            ),
            "addRow",
        )
        row = self._num_rows
        self._num_rows += 1
        return RowHandle(row)

    def solve(self) -> SolveResult:
        if self._hessian_diag:
            self._pass_hessian()

        start = time.perf_counter()
        _check(self._highs.run(), "run")
        solve_time_s = time.perf_counter() - start

        model_status = self._highs.getModelStatus()
        status_str = self._highs.modelStatusToString(model_status)
        if model_status != highspy.HighsModelStatus.kOptimal:
            raise SolveError(f"solve did not reach optimality: status={status_str}")

        solution = self._highs.getSolution()
        info = self._highs.getInfo()

        primal = {VarHandle(i): v for i, v in enumerate(solution.col_value)}
        row_duals = {RowHandle(i): d for i, d in enumerate(solution.row_dual)}

        iteration_count = (
            info.qp_iteration_count if self._hessian_diag else info.simplex_iteration_count
        )

        return SolveResult(
            status=status_str,
            objective=self._highs.getObjectiveValue(),
            primal=primal,
            row_duals=row_duals,
            iteration_count=int(iteration_count),
            solve_time_s=solve_time_s,
        )

    def _pass_hessian(self) -> None:
        """Emit the accumulated diagonal Hessian in HiGHS's sparse
        triangular format: `start` has one entry per column plus a
        trailing total-nonzero count, `index`/`value` list each column's
        single diagonal nonzero (columns with `Q_jj == 0` contribute none).
        """
        dim = self._num_vars
        start: list[int] = []
        index: list[int] = []
        value: list[float] = []
        nz = 0
        for col in range(dim):
            start.append(nz)
            if col in self._hessian_diag:
                index.append(col)
                value.append(self._hessian_diag[col])
                nz += 1
        start.append(nz)
        status = self._highs.passHessian(
            dim,
            nz,
            int(highspy.HessianFormat.kTriangular),
            np.array(start, dtype=np.int32),
            np.array(index, dtype=np.int32),
            np.array(value, dtype=np.float64),
        )
        _check(status, "passHessian")


def assert_qp_duals_supported() -> None:
    """Fail loudly if the installed HiGHS does not return finite row duals
    on the QP path (SPEC §3: "If a HiGHS version regression ever breaks QP
    duals, the engine must fail loudly, not silently return zeros.").

    Call this explicitly from application startup (FastAPI service startup,
    a CLI entry point) — deliberately **not** wired into `import ed.solver`,
    so that importing the package (as every test collection and static
    analysis pass does) never itself performs a solve.

    Builds the smallest possible QP with a nontrivial dual: one variable,
    one equality row, a positive Hessian diagonal entry.
    """
    adapter = SolverAdapter()
    v = adapter.add_var(cost=1.0, lower=0.0, upper=10.0, hessian_diag=1.0)
    adapter.add_row(lower=1.0, upper=1.0, coefficients={v: 1.0})
    result = adapter.solve()
    if not result.row_duals:
        raise SolverCapabilityError(
            "HiGHS returned no row duals on the QP path; this installation "
            "cannot supply the prices this engine depends on"
        )
    (dual,) = result.row_duals.values()
    if not math.isfinite(dual):
        raise SolverCapabilityError(
            f"HiGHS returned a non-finite row dual ({dual!r}) on the QP path"
        )
