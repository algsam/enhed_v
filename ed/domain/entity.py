"""The DispatchableEntity protocol — what the optimizer sees (SPEC §5.1).

Distinct from PhysicalUnit ("what the field sees"). A DispatchableEntity owns
variables, bounds, a convex signed cost curve, a bus, and optional reserve
terms, and gets a base point. It contributes to the model uniformly, through
this contract — type must never determine math (CLAUDE.md, SPEC §5.4/§9).

The variable/constraint/cost contribution types are intentionally left generic
here: the ModelBuilder and SolverAdapter that define concrete context/handle
types are later build-order steps (SPEC §13). No solver code is imported here.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DispatchableEntity(Protocol):
    """Uniform contract every dispatchable resource implements.

    The ModelBuilder iterates entities calling these three methods and never
    branches on resource_type or isinstance (CLAUDE.md Architecture).
    """

    @property
    def bus(self) -> str: ...

    def contribute_variables(self, ctx: Any) -> Any: ...

    def contribute_constraints(self, ctx: Any) -> Any: ...

    def contribute_cost(self, ctx: Any) -> Any: ...
