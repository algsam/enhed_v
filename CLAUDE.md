# CLAUDE.md — Project Invariants

Economic dispatch / AGC base-point engine. The full task specification is in `SPEC.md` — read it before starting new work.

This file holds **invariants that must hold on every edit**, for the life of the project. If a requested change would violate one of these, stop and tell me rather than proceeding.

**Precedence:** `SPEC.md` governs *formulations* (the exact math, the exact algebra, what a `[DECISION]` section says to implement as written). This file governs *invariants* (properties that must hold regardless of formulation). When a line in this file and a formulation in `SPEC.md` appear to conflict — e.g. a stored representation here implies math that a SPEC formula doesn't accommodate as written — **stop and ask**, rather than silently choosing one document over the other. Prefer resolving the conflict as an explicit, documented amendment to the SPEC formulation (see SPEC §6.3 for a worked example) over quietly reverting either document.

---

## Solver

- The solver is **HiGHS** (via `highspy`). Access it only through the solver-adapter layer; no other module touches solver specifics.
- **HiGHS solves LP, MILP, and convex QP — but NOT mixed-integer QP.** Never introduce a formulation that needs integers alongside a quadratic objective.
- **v1 is continuous-only.** No binaries, no commitment decisions, no MILP.
- Prices come from constraint duals: **power-balance dual = system marginal price λ**; **reserve-requirement dual = reserve price**. Never compute prices any other way.

## Cost curves

- The **canonical internal form is piecewise-linear incremental cost** ($/MWh vs MW). All input modes (fuel/total cost, incremental, heat-rate × fuel price) convert to it on ingest.
- A canonical curve is a list of segments carrying `(left_value, right_value)`. **A staircase is the special case `left == right`** (`Q_jj = 0`, pure LP segment); interpolating is `left != right`. The builder handles both with one code path — no branching on curve "type".
- **Fuel-cost input defaults to the exact staircase derivative.** Midpoint-knot interpolation (IC knots at segment midpoints `m_j = (x_{j-1}+x_j)/2` with value `s_j`, linear between, flat extrapolation outside) is an **explicit opt-in flag on the curve**, never a hidden ingest transform. Cubic/PCHIP interpolation is **forbidden**: it makes IC non-piecewise-linear, total cost non-piecewise-quadratic, and leaves the diagonal-Hessian QP form HiGHS accepts.
- **All reported production cost is obtained by integrating the canonical curve, never the original input curve.** Midpoint interpolation is not exactly cost-preserving; one source of truth is what prevents silent divergence. Anchor the no-load cost at `x_0`.
- Segment variables need **no ordering constraints** — non-decreasing IC guarantees merit-order fill. This redundancy holds *only* because convexity is enforced at ingest; if binaries or SOS2 are ever introduced, ordering constraints become mandatory.
- **Convexity is validated on every ingest and every edit: incremental cost must be non-decreasing.** A non-convex curve loads out of merit order and yields a wrong, non-physical dispatch and price. Reject it and identify the offending segment. Never bypass this check.
- Slopes need **not** increase across segments; non-decreasing IC is sufficient.
- PWL incremental → piecewise-**quadratic** total cost, modeled by segment-variable decomposition with a **diagonal, PSD** Hessian. Keep an LP staircase path generated from the *same* curve object as a swappable alternative.
- A PWL *total* cost curve differentiates to a *staircase* incremental; a PWL *incremental* curve is interpolating. Any conversion between them is **explicit**, never silent — the two modes must not quietly produce different pricing behavior.
- Incremental-cost input loses the constant term. It does not change the base point but **does** change reported cost. Preserve optional no-load cost.

## Architecture

- **All validation lives once, in the domain/validation layer**, and is called by both engine and GUI. **Never reimplement validation in the frontend.**
- Every selector — cost input mode, disaggregation strategy, reserve mode, copperplate-vs-network — is a **config flag feeding one model builder**. Never a branch inside the solver.
- Resources implement a common interface (`contribute_variables()`, `contribute_constraints()`, `contribute_cost()`). Adding a resource type = adding a class, not editing the builder.
- **Identity and math are orthogonal axes.** Every resource has a first-class `resource_type` (`THERMAL, STEAM, NUCLEAR, RENEWABLE, BESS, CC_BLOCK, TIE_LINE, DEMAND_RESPONSE, DISPATCHABLE_LOAD, SLACK`) and a concrete class carrying its own fields, validators, and reporting semantics — needed for filtering, analysis, settlement, and UI. **Type must never determine math:** no `isinstance`, no `ResourceType` switch, no `is_virtual` branch inside the model builder. Types exist everywhere except there.
- **There is no `VirtualUnit` base class and no `virtual` type.** "Virtual" is a negation, not a type — a tie-line, a DR contract, and an unserved-energy slack share no analytical content. Use the leaf types for filtering, and recover virtuality as the property `not emits_setpoint`, which is the question the output layer actually asks.
- Three orthogonal properties, never a hierarchy, do the filtering: `emits_setpoint` (does it reach a governor?), `is_system_generated` (engine-created slack vs user-configured resource — an operator must never be able to delete a slack), `reserve_eligible` (contractual, never inferred from type).
- **Admission test for any new resource:** can it be expressed as a signed injection with a convex cost, with no branch in the model builder? Yes → a resource (new type + class). No — it needs a binary, or couples two buses' flows → it is a **module**, and belongs in the constraint registry beside network and reserve.
- Bidirectional resources (BESS, tie-lines) declare **two non-negative variables**, never one variable on a symmetric interval — a single variable with asymmetric import/export prices is a non-convex kink at zero. Validate `import_price ≥ export_price`; the same guard class covers simultaneous charge/discharge.
- **Demand-side convexity is the mirror condition:** willingness-to-pay must be **non-increasing** in consumption. It requires its own validator — the supply-side non-decreasing-IC validator would reject every valid demand curve.
- **Everything is bus-indexed**, even though v1 runs a single copperplate balance constraint. All price extraction routes through the balance module's duals.
- Solver rows and columns are referenced through opaque `VarHandle` / `RowHandle` wrapper types, **never bare ints outside `solver_adapter/`**. This makes "only the adapter touches solver specifics" a type-checkable invariant rather than a convention. The handle→name mapping lives in the adapter so results can be labeled without leaking indices.
- The GUI is a **thin client over the domain**, talking to the API. No solver logic, no validation logic, no modeling logic in the client.

## Units — one convention, globally

- **Power: MW. Cost: $/MWh. Ramp rate: MW/min. Time (`T_reserve`, interval length `Δt`): minutes.**
- Ramp rates are **stored** in MW/min and converted to MW/interval **only at constraint-build time** (`RampRate × Δt`). Never store MW/interval — that bakes the dispatch cycle length into the data and silently breaks the day the cycle changes from 5 to 15 minutes.
- Every field name carries its unit suffix (`ramp_rate_mw_per_min`, `t_reserve_min`), or uses typed quantities. No bare numbers crossing layer boundaries.

## Domain rules

- **CC base-point disaggregation defaults to range-based, not Pmax pro-rata.** Pure `Pmax_i/ΣPmax` splitting can push a unit below its own Pmin and produce infeasible setpoints. Use:
  `P_i = Pmin_i + (P_config − ΣPmin)/(ΣPmax − ΣPmin)·(Pmax_i − Pmin_i)`
  The `Disaggregator` interface must validate that outputs sum to the base point and respect each unit's limits.
- A combined-cycle block's marginal price is the **configuration's** incremental cost, not that of its composing units.
- For a CC config, the **curve's domain is the single source of truth for the block's aggregate limits**: `x_0 = Pmin_config`, `x_n = Pmax_config`, validated at ingest, never stored twice. The **composing units'** individual Pmin/Pmax are separate attributes the curve does not encode; the disaggregator needs them. Validate `ΣPmin_units ≤ x_0` and `x_n ≤ ΣPmax_units`, or the disaggregator can be handed a base point it cannot feasibly split.
- Reserve requirements and reserve variables are **keyed by product name** (`[unit, product]`) from day one, with exactly one product populated in v1. Same argument as bus-indexing: the key costs nothing now, re-indexing later is the painful path.
- A generator may hold **many cost curves but exactly one active**. `set_active` is atomic (never zero, never two). The active selection is part of the saved case and is **recorded in every result** — reproducibility is a hard control-room requirement.
- For CC blocks, the active curve is bound to the active configuration; the two selectors must never disagree.
- **Ramp rate is a curve** vs MW (a scalar is the one-segment case), used by both ramp constraints and reserve deliverability. Curve-valued rates are resolved to a scalar **conservatively**, once per cycle, from measured `P0` — see SPEC §6.3's amendment for the exact resolution and why point evaluation at `P0` is unsafe (it can overstate capability across a breakpoint). Every consumer of a ramp rate (ramp constraints, `aggregate_ramp`, reserve headroom) takes the resolved scalar, never the curve itself.
- Ramp limits are measured from **actual current output `P0`**, never from a schedule.
- Renewables are curtailable: forecast is an **upper bound**, not an equality. Nuclear is must-run at a fixed setpoint and excluded from regulation.
- Aggregate-headroom reserve caps each unit's contribution by deliverability: `min(Pmax_i − P_i, RampRate_i · T_reserve)`.

## Deferred — additive, not foreclosed

Do **not** implement these in v1; do **not** make them harder to add later: unit commitment / binaries / CC config *selection*; transmission security constraints (PTDF, N-1) and LMP decomposition; storage with inter-temporal state of energy; reserve product substitution/cascading.

## Operational

- The engine **always returns an actionable dispatch**: soft-constrain power balance with a high-penalty slack rather than returning "infeasible." Use **separate up-slack (deficit) and down-slack (surplus)** with independent penalties — over-generation is not priced like unserved load.
- **The slack penalty is VOLL: a configurable policy constant** (e.g. $10,000/MWh), set per market/regulator. **Never derive it from the case's own cost curves.** When slack is nonzero, its penalty *becomes* λ and sets the scarcity price; deriving it from the online fleet makes prices non-reproducible and non-comparable across cycles, which is unacceptable in a control room.
- **Validate at ingest that `VOLL > max IC across all active curves`**, so slack is never economic and never dispatched ahead of real generation. That validation — not a derivation — is what guarantees the safety property.
- Warm-start within a case across cycles, but key the basis cache on **`(case_id, structural_hash)`** and drop the basis on mismatch. A basis is valid only if rows and columns are unchanged; changing the active curve, adding a unit, or switching reserve mode invalidates it. Silently reusing a stale basis is a correctness bug, not a performance one. Warm-starting principally benefits the **LP path**.
- Log every dispatch for audit.

## Working agreement

- **Never silently change a modeling assumption.** Surface it and ask.
- Tests accompany every module. Before building on the cost layer, pin λ against a hand-computed dispatch with a known analytic answer.
- Work in the phases set out at the end of `SPEC.md`: domain + validation → builder + HiGHS adapter → reserves → API → GUI. Stop for review at each phase boundary.
