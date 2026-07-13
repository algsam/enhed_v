"""ModelBuilder — assembles a solvable model from entities + a module
registry (SPEC §9 architecture rule 3; CLAUDE.md "Architecture"; build
order step 4).

Iterates entities calling the uniform `contribute_variables /
contribute_constraints / contribute_cost` contract, then iterates a
registry of toggleable constraint modules (e.g. `BalanceModule`, and later
`ReserveModule`/a network module). Deliberately free of any type switch or
runtime type check on a resource's identity, and free of any branch on
virtuality — see
`tests/test_model_builder.py::test_builder_module_has_no_type_dispatch` for
the static check that enforces this as a structural invariant, not a
convention (CLAUDE.md/SPEC §11 "structural invariants").

Adding a new resource type therefore requires zero edits here: it only
needs to implement the `DispatchableEntity` contract (SPEC §11's
type-agnostic-balance acceptance test).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ed.domain.entity import DispatchableEntity
from ed.model.context import BuildContext, ConstraintModule
from ed.solver import SolverAdapter


@dataclass
class BuiltModel:
    """The assembled, unsolved model: an adapter ready for `.solve()`, plus
    the context and module registry needed to interpret the result
    afterwards (e.g. `BalanceModule.extract_price`).
    """

    adapter: SolverAdapter
    ctx: BuildContext
    modules: tuple[ConstraintModule, ...] = field(default_factory=tuple)


class ModelBuilder:
    """Builds one `BuiltModel` from a fixed list of entities and modules.

    Construct a fresh `ModelBuilder` (and a fresh `BuiltModel`/`SolverAdapter`)
    per dispatch cycle — this mirrors `SolverAdapter`'s own "one model per
    instance" contract.
    """

    def __init__(
        self,
        entities: list[DispatchableEntity],
        modules: list[ConstraintModule],
    ) -> None:
        self.entities = entities
        self.modules = modules

    def build(self) -> BuiltModel:
        adapter = SolverAdapter()
        ctx = BuildContext(adapter)

        for entity in self.entities:
            entity.contribute_variables(ctx)
        for entity in self.entities:
            entity.contribute_constraints(ctx)
        for entity in self.entities:
            entity.contribute_cost(ctx)

        for module in self.modules:
            module.contribute(ctx)

        return BuiltModel(adapter=adapter, ctx=ctx, modules=tuple(self.modules))
