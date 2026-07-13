"""SolverAdapter over HiGHS (SPEC §3, §9). The only package that imports
`highspy` — see `ed.solver.adapter` module docstring.

Importing this package performs no solve. `assert_qp_duals_supported()` is
exported but **not** called here — merely importing `ed.solver` must be
side-effect-free, or every test collection and every `mypy`/static-analysis
pass would silently solve a QP, and a real solver regression would present
as an opaque import-time error nobody could skip or catch. Call it
explicitly once from application startup (the FastAPI service, or any CLI
entry point) instead.
"""

from __future__ import annotations

from ed.solver.adapter import (
    RowHandle,
    SolveError,
    SolverAdapter,
    SolverCapabilityError,
    SolveResult,
    VarHandle,
    assert_qp_duals_supported,
)

__all__ = [
    "SolverAdapter",
    "SolveResult",
    "VarHandle",
    "RowHandle",
    "SolveError",
    "SolverCapabilityError",
    "assert_qp_duals_supported",
]
