# Claude Code Prompt — Real-Time Economic Dispatch Engine (AGC Base Points)

> **How to use this file:** paste it as the opening prompt in a fresh Claude Code session in an empty repo.
> Sections marked **[DECISION]** are settled — implement them as written, do not re-litigate.
> Sections marked **[OPEN]** are deliberate extension points — build the seam, not the feature.

---

## 1. Role and mission

You are building a production-grade **real-time economic dispatch (RTED / SCED) engine** for a power system control room. Its output is the **base point** (MW setpoint) for every physical generating unit, plus **participation factors** consumed downstream by AGC, plus the **system marginal price** λ.

This sits between SCUC (upstream, decides what is online — *not your problem*) and AGC (downstream, moves units off base point every 2–4 s to chase ACE — *also not your problem*).

**The single most important non-functional requirement is extensibility.** Every design choice below exists so that later additions (network security constraints, storage, multi-interval look-ahead, unit commitment) are *additive modules*, never rewrites. When you face a choice between "simplest thing that works" and "simplest thing that works and leaves the seam open," choose the latter — but do **not** build the future feature itself.

Second most important: this is control-room software. It must **always return an actionable answer**. "Infeasible" is not an acceptable output.

---

## 2. Scope

### In scope (v1)
- Continuous convex optimization only. **No binary/integer variables anywhere.**
- Unit commitment is an **input**. Combined-cycle configuration is an **input**, known at execution time.
- Resource types: Thermal (CT), Steam (ST), Nuclear, Renewable (wind/solar), BESS, Combined-Cycle Block, plus the signed-injection resources Tie-Line, Demand Response, Dispatchable Load, and Slack. Each is a **first-class type**; none is a special case in the model builder.
- Two user-selectable cost input formats; two user-selectable reserve modes.
- Copperplate (single system-wide) power balance — **but every injection is indexed by bus from day one.**
- A minimal UI to edit inputs, run, and inspect results.

### Explicitly out of scope (v1) — build the seam only
- Network / DC power flow / PTDF / N-1 security constraints
- Unit commitment or combined-cycle configuration *selection*
- Multi-interval look-ahead (but see §5.1 — the time index exists)
- Losses
- Reserve product substitution/cascading
- Any metaheuristic solver (GA, PSO, etc.) — these give no duals, no optimality guarantee, and no bounded runtime. Never propose them.

---

## 3. Solver: HiGHS

Use **HiGHS** via the `highspy` Python interface.

**[DECISION] The governing constraint:** HiGHS solves LP, MILP, and **convex QP**, but it **cannot solve QP with integer variables** (no MIQP). Since v1 is continuous, we exploit the QP path. This is also *why* v1 forbids binaries: the moment a binary appears, the quadratic objective must be abandoned.

**[DECISION] Dual availability is a hard requirement.** λ is the dual of the power-balance row; the reserve clearing price is the dual of the reserve requirement row. Extract row duals from the HiGHS solution object. Write a test that asserts duals are returned and are finite on the QP path. If a HiGHS version regression ever breaks QP duals, the engine must fail loudly, not silently return zeros.

**Wrap HiGHS behind a `SolverAdapter` interface.** No module outside `solver/` may import `highspy`. The rest of the code speaks in terms of variables, bounds, rows, and a diagonal Hessian.

---

## 4. Cost curves

### 4.1 Canonical internal form

**[DECISION]** The canonical internal representation is a **piecewise-linear incremental cost (IC) curve**: an ordered list of breakpoints with an IC value at each, connected by straight lines. IC therefore **interpolates continuously** between breakpoints.

Consequence: **total cost is piecewise-quadratic ⇒ the model is a convex QP, not an LP.** Integrating a sloped IC line yields a parabola. This is intentional — it makes λ a continuous function of dispatch, eliminating the price discontinuity/degeneracy you get when IC is a staircase and the marginal unit lands on a breakpoint.

### 4.2 Segment decomposition (the exact formulation)

You cannot attach a piecewise-quadratic cost to a single `P` variable. Decompose:

```
P_e = Σ_j p_j                 with  p_j ∈ [0, L_j]
```

where `L_j` is the MW width of segment `j`. On segment `j`, IC rises linearly from `a_j` (left breakpoint) to `b_j` (right breakpoint). That segment's cost is:

```
cost_j(p_j) = a_j · p_j + (b_j − a_j) / (2 · L_j) · p_j²
```

So in HiGHS's `min ½xᵀQx + cᵀx` form:
- linear coefficient: `c_j = a_j`
- Hessian diagonal:  `Q_jj = (b_j − a_j) / L_j`   ← this is literally the slope of the IC line on segment j

**Q is diagonal and separable.** Read its entries straight off the input curve. No off-diagonal terms.

**No ordering constraints are needed.** Because the IC polyline is non-decreasing (enforced by the validator below), segments fill cheapest-first automatically at the optimum. Do not add SOS2 or binaries.

### 4.3 Two input formats — [DECISION] user-selectable, converted to the canonical form on ingest

**`INCREMENTAL` mode:** user supplies IC breakpoints directly. Used as-is.
- IC input loses the constant term. Provide an **optional `no_load_cost`** field. It does not change the base point (argmin is invariant to a constant) but it **does** change any reported total-production-cost or settlement figure. If absent, report costs as "up to an additive constant" — do not silently print a wrong number.

**`FUEL_COST` mode:** user supplies total cost ($/h vs MW), or preferably **heat-rate curve + fuel price separately** (multiply internally — fuel prices change far more often than heat rates, and a price update should not force curve re-entry). Support the separated form.

- **Conversion subtlety, do not paper over it:** a piecewise-*linear* total-cost polyline differentiates to a *staircase* IC, which does not interpolate. To obtain an interpolatable IC curve, evaluate segment slopes and place them at **segment midpoints**, then connect. This is an explicit, documented modeling choice that slightly shifts prices relative to the staircase reading. Log it. Make it a named, swappable strategy (`FuelToIncrementalStrategy`) so it can be changed without touching the builder.

### 4.4 Validators — run on ingest, reject or flag; never dispatch on an invalid curve

- **Supply convexity:** IC must be **non-decreasing** across the whole polyline (`b_j ≥ a_j` for all j, and `a_{j+1} ≥ b_j`). If violated: `Q_jj < 0`, the QP is non-convex, merit order breaks, and the LP/QP will load segments out of order and produce a silently wrong, non-physical dispatch. **Reject.**
  - Note it is *sufficient* that IC is non-decreasing; the segment slopes need not increase.
- **Demand convexity (separate validator!):** for consuming/demand-side entities (dispatchable load, DR, export), willingness-to-pay must be **non-increasing** in consumption. Reusing the supply validator here will reject every valid demand curve. Write it separately.
- **Bidirectional price ordering:** for any entity with separate buy/sell legs (BESS, tie-line), `import_price ≥ export_price` (and analogously `charge_cost ≥ discharge_revenue` net of efficiency). If violated, the model has a free arbitrage and will charge and discharge simultaneously. **Reject on ingest.** One guard covers BESS and tie-lines both.

---

## 5. Domain model

### 5.1 The core abstraction — [DECISION] this is the spine of the design

Two distinct concepts. Do not merge them.

- **`PhysicalUnit`** — what the field sees. Owns telemetry (`P_actual`), physical limits (`Pmin`, `Pmax`), ramp rates (`RU`, `RD`), a `bus`, and mode-keyed characteristics. Receives a **setpoint**.
- **`DispatchableEntity`** — what the optimizer sees. Owns variables, bounds, a convex signed cost curve, a `bus`, optional reserve terms. Gets a **base point**.

Mappings:
- A standalone thermal unit is **both**.
- A CC block is a `DispatchableEntity` **composed of** `PhysicalUnit`s, and is **not itself** a `PhysicalUnit`.
- A CT running inside an active CC config is a `PhysicalUnit` that is **not** a `DispatchableEntity`.

**Invariant (assert it at build time, every run):** the set of `DispatchableEntity`s induces a **partition** of the *online* `PhysicalUnit`s. Every online physical unit is dispatched by exactly one entity — itself, or its block. Offline units belong to none. Assert: member sets pairwise disjoint; union of block members + standalone set = exactly the online set. This assertion catches real configuration errors before they reach an operator.

The optimizer iterates over `DispatchableEntity` and **never knows CC blocks exist.**

### 5.2 State model — [DECISION] HRV is the heat-recovery path position, not block membership

`HRV` on a CT = diverter damper position (open ⇒ exhaust to HRSG). It is **real telemetry**, ground truth from the field.

| `online` | `HRV` | State |
|---|---|---|
| false | — | Offline. Not dispatched. |
| true | false | Running **simple cycle** → its own `DispatchableEntity`. |
| true | true | Contributing to a CC block → dispatched *inside* the block entity. |

**Critical:** "member of a block but not in the active config" does **not** mean offline. A CT with its damper closed runs perfectly well in simple cycle. Example that must work: block roster `{CT1, CT2, ST1}`; CT1 runs simple-cycle standalone while `CT2+ST1` form the active config. This produces **two** entities from one block roster.

**Steam turbines get an enum, not a boolean** (see §5.4).

**Do not dispatch off a stored flag that can drift.** HRV is telemetry; the block's **active configuration** must be *derived* from the members' `online`/`HRV`/`steam_source` vector and **validated** against the block's enumerated legal configurations. If block B's config says `CT2+ST1` but CT1 reports `HRV=true`, that is an inconsistency — surface it, do not dispatch. Since v1 has no commitment decision, this is a **validator**, not a constraint generator. (It becomes the constraint generator if config selection is ever added.)

### 5.3 Mode-dependent characteristics — [DECISION]

**CT1 in simple cycle is not the same unit, economically, as CT1 in combined cycle.** Worse heat rate (no bottoming cycle recovering exhaust), different Pmax, different ramp rates, different emissions rate.

So a unit carries characteristics **keyed by mode**:
- `CT.characteristics[SIMPLE_CYCLE]` → used when it is a standalone entity
- `CT.characteristics[COMBINED_CYCLE]` → its contribution within the block

**[DECISION] The block's aggregate cost curve is a property of the *configuration*, not the sum of its members' curves.** The ST produces MW from recovered heat it does not burn fuel for; fuel cost cannot be attributed to it independently. Each legal configuration is a **pseudo-unit** with its own measured aggregate IC curve and its own `(Pmin, Pmax)`. (This is the standard configuration-based CC model — Chen & Wang 2017.)

Corollary: **disaggregation is setpoint allocation, not cost decomposition.** An individual ST's "incremental cost" is not economically meaningful. Only the configuration's is. The system marginal price is set by the *config's* IC, never by a member unit's.

### 5.4 Type hierarchy — [DECISION] capabilities over deep inheritance

**Two orthogonal axes. Do not collapse them.**

- **Identity** — what a resource *is*. Drives filtering, reporting, validation, UI grouping, settlement, telemetry mapping. **First-class and typed.**
- **Model contribution** — what it gives the optimizer. **Uniform:** signed injection, bounds, convex cost, bus.

So: every resource carries a flat `resource_type` enum and has a concrete class holding its type-specific fields and validators —

```python
class ResourceType(Enum):
    THERMAL; STEAM; NUCLEAR; RENEWABLE; BESS; CC_BLOCK
    TIE_LINE; DEMAND_RESPONSE; DISPATCHABLE_LOAD; SLACK
```

— **and** every one of them contributes to the model through the same `contribute_variables / contribute_constraints / contribute_cost` contract. **Type carries domain semantics; the contract carries the math.** `TieLine` may hold `import_price`, `export_price`, `schedule`, `interface_id`, counterparty, and its own price-ordering validator, and still be invisible to the model builder as anything other than a signed injection with a convex cost.

The rule is not "no types." The rule is **type must never determine math**: no `isinstance` and no `resource_type` switch inside `ModelBuilder`.

Three **orthogonal properties** — not a hierarchy — do the filtering work:

| Property | Question it answers | Used by |
|---|---|---|
| `emits_setpoint` | Does this go to a governor? | output layer, disaggregation |
| `is_system_generated` | Did the engine create it, or the user? | UI (never expose slack for editing) |
| `reserve_eligible` | May it sell reserve? | `ReserveModule` |

Nuclear and Thermal differ mainly in *data*, not *structure*. Keep the class hierarchy **shallow (one level)**, for identity/telemetry/UI only. Express dispatch behavior as **composable capabilities**:

- `RampLimited`
- `ReserveCapable` (contributes reserve vars + headroom coupling)
- `ForecastLimited` (renewables: `Pmax` from forecast, curtailable)
- `MustRun` (nuclear: bounds pinned to schedule, excluded from regulation)
- `StorageCapable`
- `SignedInjection` (may go negative)

If you ever find yourself writing `if isinstance(unit, Nuclear):` inside the model builder, the design has failed. The builder sees only the uniform contribution interface.

Per-type notes:
- **Nuclear:** must-run at a fixed setpoint. Pin `P = scheduled`, exclude from regulation, zero/narrow ramp band. Hard bounds, not a soft preference.
- **Renewable:** dispatchable-*down*. `0 ≤ P ≤ forecast` (upper **bound**, never an equality — that is what permits curtailment). Near-zero marginal cost. Optional curtailment penalty term.
- **Steam turbine:** `steam_source ∈ {HRSG, AUX_BOILER, NONE}` (three-valued, **not** a boolean). `AUX_BOILER` ⇒ standalone entity with its **own fuel cost** (the boiler burns fuel), its own heat rate, typically much lower `Pmax` and slower ramp. **Ship v1 with `AUX_BOILER` present in the enum but unreachable** — do not ship a boolean and widen it later.
  - `can_run_standalone` for an ST is a **runtime predicate against context** (does the aux boiler exist *and* is it available now?), **not** a constant on the class.
  - **[ASSUMPTION, assert it]** No simultaneous HRSG + aux-boiler firing of the same ST in v1. Supplemental firing would put the ST partly inside the block and partly standalone, **breaking the partition invariant**. If it is ever needed, the honest model is that the block entity absorbs the aux boiler as an additional fuel input — the ST does not split across entities.
- **[ASSUMPTION, assert it]** No ST shared across two CC blocks in v1 (this would break the many-to-one unit→block link).

### 5.5 BESS — [DECISION] it breaks two assumptions; that is why the seams exist

- **Bidirectional:** not one `P ≥ 0` variable. Needs `P_chg ≥ 0`, `P_dis ≥ 0`, net injection `P_dis − P_chg`, plus round-trip efficiency. The contribution interface must therefore let a resource declare **however many variables it needs** — never assume one variable per entity.
- **Inter-temporal:** `SoE[t] = SoE[t−1] + η_c·P_chg[t]·Δt − P_dis[t]·Δt/η_d`.
  **In a single-snapshot run SoE is meaningless**, and BESS degenerates into "a free unit with a MW range" that discharges at full power every cycle. Ship v1 with BESS present but **either** SoE-constrained across a singleton horizon (i.e. effectively energy-limited by a passed-in current SoE and a per-interval energy budget) **or** clearly flagged as `not_dispatchable_until_multi_interval`. Pick one, document it, do not pretend it works.
- Do **not** add a binary no-simultaneous-charge/discharge constraint (that would break the continuous QP). With `η < 1` and correct price ordering, simultaneity is precluded — **assert it in a post-solve check** instead.

### 5.6 Virtual units — [DECISION] typed leaves, no `VirtualUnit` base class

Virtual resources **are** resources, and you will need to filter, process, and analyse them by type. So give each one a **real concrete class and a `resource_type`**: `TieLine`, `DemandResponse`, `DispatchableLoad`, `Slack`. Each holds its own fields, its own validators, its own reporting semantics.

What must **not** exist is a `VirtualUnit` base class or a `virtual` type. Two reasons:

1. **"Virtual" is a negation, not a type.** Nothing is analytically shared by a tie-line, a DR contract, and an unserved-energy slack — different prices, different counterparties, one of them isn't even real. The analyses you will actually run are *"total interchange across all ties," "DR called this hour," "was VoLL set?"* — filters on the **leaf types**, never on virtualness.
2. **It would fuse two different lifecycles.** `Slack` is system-generated and must never appear in an operator's resource list. `TieLine` is user-configured and its schedule *is* sent downstream. A shared base means someone eventually iterates it and either exposes slack for editing or forgets to send tie schedules.

"Virtual" is therefore recovered as a **property**, not a class: `[e for e in entities if not e.emits_setpoint]`. That is the operationally meaningful question ("does this go to a governor?") — which is the one the output layer actually asks.

**Admission test for any new resource:** *can it be expressed as a signed injection with a convex cost, without a branch in the model builder?* If yes → it is a resource (give it a type, add the class). If no — it needs a binary, or couples two buses' flows — it is a **module**, and belongs in the constraint registry beside network and reserve.

Structurally, tie-lines, demand response, dispatchable load, and slack all contribute the **same shape**: a `DispatchableEntity` whose injection bound may go **negative**, carrying a convex signed cost.

| Entity | Injection | Cost semantics |
|---|---|---|
| Tie import | `+P` | pay import price |
| Tie export | `−P` | earn export price |
| DR / load curtailment | `+P` | pay DR compensation |
| Dispatchable load | `−P` | earn/save load value |
| Unserved energy (slack) | `+P` | VoLL penalty |
| Over-generation (slack) | `−P` | high penalty |

A generator is simply the case `lower ≥ 0`. The balance row `Σ_e injection_e = load` sums over generators, CC blocks, BESS, ties, and DR **without a single branch**.

Rules:
- **Bidirectional entities get two variables** (`P_imp`, `P_exp`), each with its own curve — a single variable on `[−Pmax, +Pmax]` with import@$40 / export@$30 is a **non-convex** downward kink at zero. Same structure as BESS.
- Keep the objective strictly `min Σ cost`. Revenue is a **negative coefficient**, not a separate term.
- Every entity carries: `resource_type`, `emits_setpoint` (bool or `setpoint_channel`), `is_system_generated`, `bus`, `reserve_eligible`. **`reserve_eligible` and `emits_setpoint` are never *inferred* from type** — a tie or DR resource may or may not be reserve-qualified, and that is a contractual fact, not a structural one. Set them explicitly per resource.
- Virtual resources participate in the model but must **never receive a field setpoint**. The output layer filters on `emits_setpoint`, not on type — so a future physical resource that happens to be signed-injection (e.g. a pumped-hydro unit) needs no special case.
- **`TieLine` validator:** `import_price ≥ export_price`. Otherwise the QP imports and exports simultaneously (arbitrage). Same class of guard as the BESS simultaneous charge/discharge check.
- **Demand-side convexity is the mirror condition:** willingness-to-pay must be **non-increasing** in consumption (a demand curve slopes down). Write a **separate** validator — reusing the supply-side non-decreasing-IC validator will reject every valid demand curve.

**[DECISION] Slack pseudo-units are system-defined, not user-configured.** Unserved energy and over-generation are injected automatically by the `BalanceModule`; reserve shortfall by the `ReserveModule`. Their prices are policy constants (VoLL, over-gen penalty) set **strictly above any real resource's cost**, so they are marginal only in true scarcity. When one is marginal, λ equals its penalty price — the desired price-cap behavior — and its nonzero dispatch is the operator's **scarcity flag**. An operator must not be able to delete the slack that keeps the solve feasible. This is the feasibility guarantee from §1.

**[DECISION] Tie-lines are a fixed schedule input in v1, not decision variables.** Interchange typically changes on schedule-block boundaries, not continuously. Model them as a known injection on the balance row. Build the `SignedInjection` seam so they can become dispatchable later by supplying a curve and bounds. *(Flip this if your control room dispatches interchange — the machinery already supports it.)*

### 5.7 Time index — [DECISION]

**Carry the time index `t` on every variable from day one, and run v1 with `|T| = 1`.**

Adding a time dimension later is a rewrite. Carrying a singleton dimension costs nothing. Multi-interval look-ahead and any meaningful BESS behavior both depend on it. Write ramp constraints in their general `t−1 → t` form with `P[·, 0]` anchored to measured telemetry.

### 5.8 Bus index — [DECISION]

**Give every entity and every load a `bus` attribute today**, even though v1 runs a single copperplate balance row.

Enabling network security later then becomes purely additive: a module appending `PTDF · injection ≤ limit` rows (plus N-1 rows). Route **all** price extraction through the `BalanceModule`'s duals, so swapping copperplate for a nodal balance yields LMPs (energy + congestion + loss components) automatically. Retro-fitting bus indexing is the painful path. Tie-lines are where this bites first — an unbussed tie is meaningless the moment the network is on.

---

## 6. Combined-cycle disaggregation

### 6.1 Interface

A **`Disaggregator`** is a pluggable post-processor, fully decoupled from the solve. It owns **three** responsibilities — and they must live together, because the rule that splits MW is the same rule that determines how fast the aggregate can move. Separating them lets someone swap the splitter and silently invalidate the ramp limits.

```python
class Disaggregator(Protocol):
    def split(self, entity_mw: float, units: list[PhysicalUnit]) -> dict[UnitId, float]: ...
    def aggregate_limits(self, units) -> tuple[float, float]:            # (Pmin_e, Pmax_e)
    def aggregate_ramp(self, units, telemetry, dt) -> tuple[float, float]:  # (RU_e, RD_e)
```

The interface must **validate** that `split()` outputs sum to the entity base point and lie within each unit's limits, so a future custom splitter cannot silently produce an infeasible setpoint vector.

Also decide/handle: how AGC regulation on a CC block is split. The same disaggregator should handle `base_point + AGC_delta`, or you will have a consistent base point and an ad-hoc regulation split.

### 6.2 Default strategy — [DECISION] range-based pro-rata, **not** pure Pmax pro-rata

Pure `P_i = P_e · Pmax_i / ΣPmax` **can violate an individual unit's Pmin** when the block base point is low, producing an infeasible per-unit setpoint. Use instead:

```
f   = (P_e − Σ Pmin_i) / (Σ Pmax_i − Σ Pmin_i)          # fill fraction ∈ [0,1]
P_i = Pmin_i + f · (Pmax_i − Pmin_i)
```

This respects every unit's limits, sums exactly to `P_e`, and collapses to pure Pmax-prorating when all `Pmin = 0`. Strictly better at no cost.

### 6.3 Ramp aggregation — **it is a `min`, not a `sum`.** This is the part that is easy to get wrong.

The entity's previous output is the measured sum `P_e⁰ = Σ_i P_i⁰`. But its **ramp capability is not `Σ_i RU_i`.** That sum would only be achievable if the optimizer could move each unit independently. It cannot — **the disaggregator decides the split**, so per-unit moves are a fixed function of the entity move.

For any **linear** disaggregation rule `P_i = α_i · P_e + β_i` with `Σ α_i = 1`:
- range-based: `α_i = (Pmax_i − Pmin_i) / Σ_j (Pmax_j − Pmin_j)`,  `β_i = Pmin_i − α_i · Σ_j Pmin_j`
- Pmax pro-rata: `α_i = Pmax_i / Σ_j Pmax_j`,  `β_i = 0`

**Drift-aware derivation (implement exactly this).** Between dispatch cycles AGC has moved units, so measured `P_i⁰` will generally **not** lie on the disaggregator's manifold. Define the drift:

```
d_i = (α_i · P_e⁰ + β_i) − P_i⁰
```

If the entity moves by `Δ`, unit `i` must move by `d_i + α_i · Δ`. Imposing each unit's physical ramp:

```
−RD_i · Δt  ≤  d_i + α_i · Δ  ≤  RU_i · Δt
```

Therefore:

```
RU_e · Δt = max( 0, min_i ( RU_i · Δt − d_i ) / α_i )
RD_e · Δt = max( 0, min_i ( RD_i · Δt + d_i ) / α_i )
```

Consequences to internalize:
- The block ramps at the pace of its **slowest member relative to its own range** — usually the ST. This matches physical reality: a CC block ramps at the pace the steam turbine's thermal stress allows, not the sum of the gas turbines' capability. A naive `Σ RU_i` would command a ramp the plant cannot follow, and AGC would chase a base point it never reaches.
- **Ramp is asymmetric in general**: `RU_e ≠ RD_e` whenever the members' up/down rates differ in proportion.
- **The drift term eats into each unit's ramp budget before any new movement.** The zero-drift case is the special case `d_i = 0`.
- **Clamp at zero.** If a unit has drifted further than one interval's ramp from its implied split, the block simply cannot ramp that direction this cycle. **Surface this as an operator diagnostic** — do not let it go negative and make the QP infeasible. (Together with the slack pseudo-units, a drifted plant then *degrades* the dispatch rather than killing the solve.)

Have `aggregate_limits` and `aggregate_ramp` be **disaggregator methods** so a future non-linear splitter (e.g. one that loads the ST last) can report its own correct envelope.

**[AMENDMENT] `RU_i`/`RD_i` resolution when a unit's ramp rate is a curve, not a scalar.** CLAUDE.md's Domain rules store ramp rate as a curve over output MW (a scalar is the one-segment case) — this section's algebra is unaffected, but `RU_i`/`RD_i` must be *resolved to a scalar before entering it*, once per dispatch cycle, from each unit's measured `P_i⁰`:

```
RU_i := min{ rate_up(P) : P ∈ [P_i⁰, P_i⁰ + RU_i·Δt] }
RD_i := min{ rate_dn(P) : P ∈ [P_i⁰ − RD_i·Δt, P_i⁰] }
```

This is self-referential (the band depends on the rate being resolved); a single conservative pass is sufficient — seed with the point rate `rate_up(P_i⁰)`, compute the band it implies, then take the minimum rate over every segment that band touches. Shrinking the rate only ever shrinks the band, so one pass cannot overshoot back into a faster segment it already excluded.

**Point evaluation at `P_i⁰` alone is unsafe and must not be used.** It can overstate capability whenever the reachable band crosses into a slower segment — e.g. 8 MW/min below 100 MW, 3 MW/min above, `P_i⁰ = 90`, `Δt = 5 min`: point evaluation reports 8 MW/min and 40 MW of headroom, but the unit crosses 100 MW after only 10 MW of movement and cannot sustain 8 MW/min past that. Commanding a base point the plant cannot follow is exactly the failure mode this section's min-not-sum rule exists to prevent at the block level — the same hazard exists for a single unit's own curve, and the fix must be conservative (never over-command), not merely convenient.

Once `RU_i`/`RD_i` are resolved, the rest of §6.3 — drift, the `min`-over-units formula, asymmetry, clamping at zero — is used exactly as written, unchanged.

### 6.4 State ownership — [DECISION]

- `PhysicalUnit` owns `RU / RD / Pmin / Pmax / P⁰` as **physical truth**.
- `Disaggregator` owns the entity↔unit map **and** the induced aggregate `(Pmin_e, Pmax_e, RU_e, RD_e)` **given current telemetry**.
- `DispatchableEntity` stores **no historical state** and is **rebuilt every run**.

Ramp feasibility is checked from **measured** `P_i⁰`, never from the last commanded base point or the last commanded split. Nothing is remembered between cycles except what the field reports. This is exactly the property you want in control-room software — and it is what makes a changing HRV vector (a CT joining/leaving the block mid-shift) safe, since the entity set may change shape between cycles.

---

## 7. Mathematical formulation

### Objective

```
min  Σ_t Σ_e  Cost_e(P[e,t])            # PWL-IC ⇒ piecewise-quadratic; diagonal Q
   + Σ_t Σ_e  reserve_cost_e(R[e,t,r])  # full mode only
   + Σ_t Σ_w  curtail_penalty · (forecast[w,t] − P[w,t])   # optional
   + Σ_t       VoLL · unserved[t] + overgen_penalty · overgen[t]
```

Every term is a **pluggable cost component**. This is central to extensibility.

### Constraints

1. **Power balance** (per interval; per bus once network is enabled):
   `Σ_e injection[e,t] + unserved[t] − overgen[t] = load[t] − fixed_tie_schedule[t]`
   → **its dual is λ, the system marginal price.**
2. **Entity limits:** `Pmin_e ≤ P[e,t] ≤ Pmax_e` (for CC blocks, from `aggregate_limits`).
3. **Ramp limits, anchored to measured telemetry:**
   `P[e,t] − P[e,t−1] ≤ RU_e · Δt`, and the down analog, with `P[e,0]` anchored to `P⁰`.
   For CC blocks, `RU_e / RD_e` come from `aggregate_ramp(...)` per §6.3.
4. **Renewable:** `0 ≤ P[w,t] ≤ forecast[w,t]`.
5. **Must-run (nuclear):** `P = scheduled`, excluded from reserve.
6. **Reserve:** see §8.
7. **Network:** [OPEN] `PTDF · injection ≤ limit` — module seam only, not implemented.

---

## 8. Reserve — [DECISION] two modes behind one `ReserveModule` interface, toggled by config

### Mode A — `AGGREGATE_HEADROOM` (stub)

One system-wide row: total committed capacity minus total dispatched energy ≥ requirement. Guarantees spare MW; does **not** decide who holds it or price it.

**Required refinement (nearly free, and it makes the stub honest):** cap each entity's contribution by what it can actually **ramp within the reserve delivery window**:

```
headroom_e = min( Pmax_e − P_e,  RampRate_e · T_reserve )
```

Without this, the stub "reserves" MW that no unit can reach in time.

**Scope it as single-product.** The pure aggregate form cannot cleanly express multiple reserve products.

### Mode B — `PER_UNIT_COOPTIMIZATION` (full)

A reserve variable per entity per product. Capacity shared between energy and reserve, plus ramp feasibility, solved **jointly**:

```
P[e,t] + R_up[e,t]   ≤ Pmax_e         # cannot sell reserve you have no room to deliver
P[e,t] − R_dn[e,t]   ≥ Pmin_e
R_up[e,t] ≤ RU_e · T_reserve          # deliverability
Σ_e R_up[e,t] + shortfall[t] ≥ Req_up[t]
```

Two consequences to **surface in the outputs**:
- **Energy dispatch changes** — units back down energy to sell reserve. This is correct, and operators must see it.
- **The reserve requirement row has its own dual = the reserve clearing/marginal price.** Expose it alongside λ.

Reserve product substitution/cascading (reg counts as spin, etc.) is **[OPEN]** — do not build it.

Only entities with `reserve_eligible = true` participate. Nuclear: false. Ties/DR: **do not infer from type** — it is a per-entity attribute (DR is a common real reserve resource; contracts vary).

---

## 9. Architecture

```
ed/
  domain/        PhysicalUnit, DispatchableEntity, CCBlock, capabilities, enums
  curves/        CostCurve (canonical PWL-IC), FuelToIncrementalStrategy, validators
  entities/      build_entities() — config resolution, partition assertion
  disagg/        Disaggregator protocol, RangeProRata (default)
  modules/       BalanceModule, ReserveModule (A|B), [seam] NetworkModule
  model/         ModelBuilder — assembles vars/rows/Q from entities + module registry
  solver/        SolverAdapter (the ONLY place highspy is imported)
  results/       base points, participation factors, λ, reserve price, curtailment,
                 scarcity flags, diagnostics, feasibility report
  api/           FastAPI service (thin)
  ui/            Streamlit app (thin)
tests/
```

### Hard architectural rules
1. **`ed/` core imports no UI and no web framework.** The engine is a pure library.
2. **Only `solver/` imports `highspy`.**
3. **The `ModelBuilder` contains no `isinstance` checks, no `resource_type` switches, and no `if entity.is_virtual`.** It iterates entities calling `contribute_variables / contribute_constraints / contribute_cost`, then iterates a **registry of toggleable constraint modules**. Types exist everywhere *except* here. Enforce with a test that greps the builder module for `isinstance` and `ResourceType.` and fails on a hit.
4. **Every user choice** — fuel vs incremental, disaggregation strategy, reserve mode, copperplate vs network — is a **strategy swap behind a stable interface**, i.e. a config flag feeding the same build. Never a branch in the solver.
5. **The extensibility test for any new resource:** *can it be expressed as a signed injection with a convex cost, without a branch in the builder?* If yes → it is a **resource**. If no (needs a binary, or couples two buses' flows) → it is a **module**, and belongs in the constraint registry.

### Control-room concerns to build in from day one
- **Warm-starting.** HiGHS simplex hot-starts an LP from the previous basis very effectively; **QP active-set warm-starting is less mature.** Design the adapter to accept a warm start, and **benchmark it at the real cycle time (5 min)**.
- **LP fallback path.** Have the `CostCurve` object emit **either** QP quadratic segments **or** a sampled fine-grained LP staircase from the *same* curve. This gives a pure-LP fallback (better warm-starting, more robust) at zero duplication. Selectable by config.
- **Feasibility fallback.** Slack pseudo-units (§5.6). The engine returns an answer, always.
- **Audit logging.** Every dispatch — inputs, resolved entities, solve status, duals, outputs — must be reconstructible.

---

## 10. Outputs

- **Base point per `DispatchableEntity`**, and **setpoint per `PhysicalUnit`** (via disaggregation, filtered by `emits_setpoint`).
- **Participation factors** for AGC.
- **λ** = balance-row dual. **Reserve clearing price** = reserve-row dual.
- Reserve committed by entity and product.
- Curtailment MW per renewable.
- **Scarcity flags:** nonzero unserved / overgen / reserve shortfall.
- **Diagnostics:** clamped block ramps (§6.3), drift magnitudes, curve validation warnings, config-consistency warnings, solve status, solve time, iteration count.

---

## 11. Testing — treat these as acceptance criteria, not afterthoughts

Write these as you go, not at the end.

**Correctness / math**
- Two identical units, symmetric load → equal dispatch, λ = common IC.
- Merit order: cheap unit loads first; λ = marginal unit's IC at `P*`.
- **λ interpolates between breakpoints** (the whole point of PWL-IC). Sweep load across a breakpoint and assert λ is continuous — no jump.
- Non-convex IC curve → **rejected on ingest**, never dispatched.
- Demand curve with non-increasing WTP → **accepted** (guards against reusing the supply validator).
- `import_price < export_price` → rejected.
- Duals returned, finite, correct sign on the QP path.

**Structural invariants**
- **No type-dispatch in the builder:** static check that `model_builder.py` contains no `isinstance`, no `ResourceType.`, no `is_virtual`.
- **Type-agnostic balance:** a case mixing THERMAL, CC_BLOCK, RENEWABLE, TIE_LINE, DEMAND_RESPONSE and SLACK solves, and `Σ injection = load` holds. Adding a new `ResourceType` must require **zero** edits to `model_builder.py` — assert by adding a throwaway resource type in the test and building the model.
- **Virtuality is a property, not a class:** assert no `VirtualUnit` base exists; assert `[e for e in entities if not e.emits_setpoint]` returns exactly the ties/DR/loads/slack, and that the setpoint output contains none of them.
- **Lifecycle separation:** `Slack` entities have `is_system_generated=True` and are absent from the user-editable resource list; `TieLine` has `is_system_generated=False` and *does* appear. Assert an operator-facing resource listing cannot delete a slack.
- **Partition assertion:** every online unit dispatched exactly once. Construct a violating fixture and assert it raises.
- **The CT1-simple-cycle case (§5.2):** roster `{CT1, CT2, ST1}`, `CT1.HRV=false`, `CT2.HRV=true`, `ST1` on HRSG → **two entities**, both dispatched, no phantom generation, no double count.
- ST with `steam_source=NONE` and `online=true` → invalid state, raises (until aux boiler is enabled).
- HRV vector inconsistent with the declared active config → surfaced, not dispatched.

**Disaggregation & ramp — the highest-risk area**
- `split()` output sums exactly to the entity base point and respects every unit's `[Pmin, Pmax]`.
- **Pure Pmax pro-rata violates Pmin at low block output; range-based does not.** Assert both.
- `RU_e` is the **min-over-units** expression, **not** `Σ RU_i`. Assert `RU_e < Σ RU_i` for a fixture with one slow ST.
- **Drift:** with `P_i⁰` off the manifold, `RU_e` shrinks by the drift term. Assert the exact formula.
- **Drift exceeding one interval's ramp ⇒ `RU_e` clamps to 0, a diagnostic is raised, and the solve still succeeds** (via slack). Assert no infeasibility.
- Asymmetry: `RU_e ≠ RD_e` when member up/down rates differ in proportion.

**Reserve**
- Mode A: headroom capped by `RampRate · T_reserve`, not by `Pmax − P` alone.
- Mode B: a unit backs down **energy** to sell reserve; the reserve dual is nonzero and exposed.
- Nuclear never provides reserve.

**Feasibility / control-room**
- Load exceeding total capacity → **solves**, unserved > 0, λ = VoLL, scarcity flag set. **Never "infeasible".**
- BESS: post-solve assert no simultaneous charge and discharge.

---

## 12. UI — deliberately minimal, but with the right seam

**Purpose:** edit inputs, run, inspect results. Nothing more. A better UI comes later, so **the boundary matters more than the pixels.**

- **Backend:** thin **FastAPI** service exposing `POST /dispatch` (inputs → results) and `GET /schema`. The service is a *thin* wrapper: it must contain **no domain logic**.
- **Frontend:** minimal **Streamlit** app calling that HTTP endpoint — **not** importing `ed/` directly. This one discipline is what lets you swap Streamlit for a real control-room UI later without touching the engine.
- **Inputs:** JSON/YAML case files, round-trippable, version-stamped. Ship 3–4 example cases (incl. the CT1-simple-cycle case and a scarcity case). The UI edits these; hand-editing a file must remain a first-class path.
- **Display:** table of unit setpoints; entity base points; λ and reserve price prominently; curtailment; **scarcity flags and diagnostics rendered loudly** (an operator must never miss a clamped ramp or a VoLL-setting λ).
- Config toggles surfaced: cost input mode, reserve mode, disaggregation strategy, QP-vs-LP path.

---

## 13. Build order

1. Domain model + capabilities + enums + the **partition assertion**.
2. `CostCurve` canonical form, both input modes, **all three validators**. Test hard.
3. `SolverAdapter` over `highspy`; trivial 2-unit QP; **assert duals**.
4. `ModelBuilder` + `BalanceModule` + slack pseudo-units. End-to-end copperplate dispatch, λ out.
5. `Disaggregator` protocol + `RangeProRata`, including **`aggregate_ramp` with drift**. Test hardest.
6. `build_entities()` — config resolution from HRV vector, validation, the CT1-simple-cycle case.
7. `ReserveModule` mode A, then mode B.
8. Renewables, nuclear must-run. BESS per §5.5 (flagged).
9. FastAPI service; Streamlit UI; example cases.
10. Warm-start + LP-fallback path; benchmark at 5-minute cycle time.

---

## 14. Reference formulations

- Wood, Wollenberg & Sheblé, *Power Generation, Operation, and Control* — ED / UC / AGC foundations.
- Morales-España, Latorre & Ramos (2013), "Tight and Compact MILP Formulation for the Thermal Unit Commitment Problem," *IEEE Trans. Power Systems* 28(4) — for when commitment is added.
- Chen & Wang (2017), "MIP formulation improvement for large-scale SCUC with configuration-based combined cycle modeling," *Electric Power Systems Research* 148 — the configuration-based CC model underlying §5.3.
- HiGHS documentation: `https://ergo-code.github.io/HiGHS/stable/` — solver options, QP active-set, duals.

---

## 15. Working agreement

- Ask before deviating from any **[DECISION]**. They encode reasoning that is not obvious from the code.
- If a **[DECISION]** turns out to be wrong under implementation, say so explicitly and explain why — do not silently route around it.
- Prefer a failing assertion over a plausible-looking wrong number. This software sets MW into a live grid.
