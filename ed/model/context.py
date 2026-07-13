"""BuildContext — the shared object threaded through every entity's
`contribute_*` call and every constraint module's `contribute` call (SPEC
§9, build order step 4).

It is the *only* channel through which entities and modules reach the
solver: they never touch `SolverAdapter` directly except via
`ctx.adapter`, and they never assemble the balance row themselves — they
only declare signed injections at a bus via `add_injection`, and
`BalanceModule` (the sole owner of the balance row and its dual, per
CLAUDE.md "BalanceModule... owns lambda extraction") reads them back.

Injections are bus-indexed from day one (SPEC §5.8) even though v1 sums
them into a single copperplate row, so that swapping in a per-bus balance
later is additive here, not a rewrite.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from ed.solver import SolverAdapter, VarHandle


class BuildContext:
    """Accumulates bus-indexed injection terms while entities and modules
    contribute variables/constraints, for a single `ModelBuilder.build()` call.
    """

    def __init__(self, adapter: SolverAdapter) -> None:
        self.adapter = adapter
        self.bus_injection_terms: dict[str, list[tuple[VarHandle, float]]] = defaultdict(list)

    def add_injection(self, bus: str, var: VarHandle, coefficient: float = 1.0) -> None:
        """Register that `var` contributes `coefficient * var` MW of signed
        injection at `bus` (SPEC §5.6: a generator is `coefficient=+1`; a
        consuming/negative-injection resource may register `-1`).
        """
        self.bus_injection_terms[bus].append((var, coefficient))


class ConstraintModule(Protocol):
    """A toggleable constraint module in the `ModelBuilder`'s registry (SPEC
    §9 architecture rule 3: modules, not branches, are how the builder grows).

    Called once per `build()`, after every entity has contributed its
    variables. A module reads `ctx.bus_injection_terms` to assemble rows
    that couple entities together (the balance row, a future reserve
    requirement row, a future network row) — content no single entity owns.
    """

    def contribute(self, ctx: BuildContext) -> None: ...
