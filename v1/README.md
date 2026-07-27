# Home Energy Management System (HEMS) Optimizer

## 1. Executive Summary
This repository contains a deterministic, cloud-based Home Energy Management System (HEMS) simulator. It translates our proprietary Energy Management Specifications into a strictly typed, integer-only **Mixed-Integer Linear Programming (MILP)** and **Constraint Programming (CP)** formulation.

The optimization engine is powered by **Google OR-Tools (CP-SAT solver)**, designed to run statelessly in a cloud environment (e.g., AWS Lambda), computing the mathematically perfect day-ahead or receding-horizon schedule in milliseconds to seconds.

## 2. Dependencies & Libraries
*   **`ortools` (Google OR-Tools):** The core optimization engine. We specifically use the `cp_model.CpSolver` because it natively handles Boolean logic (`OnlyEnforceIf`), `IntervalVar` scheduling, and complex combinatorial boundaries without the numerical instability of traditional "Big-M" Linear Programming wrappers.
*   **`pandas` & `numpy`:** Used for data manipulation, interpolation (e.g., expanding 24-hour tariffs into 96 15-minute steps), and structuring the output for the UI.
*   **`streamlit`:** The front-end framework used to render the interactive simulator dashboard. It triggers the OR-Tools solver on a single thread to prevent macOS C++ deadlocks.

## 3. The "Edge-Reality" Abstraction (IEC 62962 Boundary)
**Crucial Architectural Decision:** The cloud optimizer **does not** manage legacy "dumb" loads via smart breakers. 

According to IEC 62962, Load Shedding Equipment (LSE) at the edge handles hardware-level safety and hierarchy (e.g., dropping a pool pump to prevent a main breaker trip). 
To prevent the solver from returning an `INFEASIBLE` status during an un-shiftable base load spike, the engine employs **Data Sanitization**:
```python
sanitized_base_load_w = min(forecasted_load, breaker_limit)
```
**The Math Logic:** The cloud assumes the edge LSE will physically cap the home's draw at the breaker limit. Therefore, if `base_load == breaker_limit`, the remaining capacity for flexible smart energy devices ($P_{flex}$) is mathematically forced to $0$ via standard CP constraints, gracefully suspending smart devices without using arbitrary objective penalties.

## 4. Mathematical Domains & Purity
CP-SAT operates exclusively on **integers**. To prevent floating-point division errors (which cause infinite symmetry loops or "Infeasible" crashes), the engine uses two strict internal domains:

1.  **Power Domain (`W`):** All power values (kW) are multiplied by `self.scale = 1000`. 
    *   *Example:* 7.4 kW EV Charger $\rightarrow$ `7400 W`.
2.  **Energy Domain (`wm100`):** Energy is tracked in "Watt-Minute-Percents". 
    *   *Equation:* $1 \text{ kWh} = 1000 \text{ W} \times 60 \text{ min} \times 100\% = 6,000,000 \text{ wm100}$.
    *   *Why?* This allows thermodynamic efficiency (e.g., 95%) to be applied as direct integer multiplication rather than float division. 

## 5. The Global Optimization Formulation (`optimizer.py`)

Let $T$ be the set of time steps (e.g., $96$ steps of $15$ mins).

### 5.1. Variables
*   $Grid_{imp}(t), Grid_{exp}(t) \in [0, BreakerLimit]$
*   $Bat_{chg}(t), Bat_{dis}(t) \in [0, BatMax]$
*   $EV_{chg}(t), EV_{dis}(t) \in [0, EVMax]$
*   $WH_{pwr}(t) \in \{0, NominalPower_{WH}\}$
*   $Appliance_{pwr}(t) \in \{0, NominalPower_{App}\}$

### 5.2. Constraint: Net Grid Balance
At every time step $t$, the sum of demands minus the sum of supplies dictates the net grid flow. CP-SAT automatically prevents simultaneous import and export because the objective function penalizes both.
$$Grid_{imp}(t) - Grid_{exp}(t) = \Big( BaseLoad(t) + EV_{chg}(t) + WH(t) + Bat_{chg}(t) + App(t) \Big) - \Big( Solar(t) + EV_{dis}(t) + Bat_{dis}(t) \Big)$$

### 5.3. Objective Function (Economics & LCOS)
The objective is to minimize total daily cost.

$$ \min \sum_{t \in T} \Big[ Cost_{grid}(t) + Cost_{wear}(t) + Penalty_{delay}(t) + Penalty_{eager}(t) \Big] $$

1.  **Grid Cost:** $(Grid_{imp}(t) \times Price_{imp}(t)) - (Grid_{exp}(t) \times Price_{exp}(t))$
2.  **Hardware Degradation ($Cost_{wear}$):** Based on the Levelized Cost of Storage (LCOS). Prevents the solver from executing zero-margin micro-arbitrage. 
    *   $BatCycleCost(t) = (Bat_{chg}(t) + Bat_{dis}(t)) \times LCOS$
3.  **Appliance Delay Penalty:** $Active(t) \times t \times 2$. Forces the solver to prefer earlier execution when electricity prices are completely flat (e.g., all night at 10 cents), preventing random fragmentation.
4.  **Eagerness Penalty:** $t \times 1$. A microscopic time-preference weight that pulls continuous variables (EV, Battery) to the earliest possible cheap slot, guaranteeing deterministic, left-aligned schedules.

## 6. Execution Parameters
The solver is initialized with the following bounds to guarantee stability in web-frameworks and edge hardware:
```python
solver.parameters.num_search_workers = 1  # Prevents C++ threading deadlocks on macOS/Lambda
solver.parameters.max_time_in_seconds = 10.0 # Strict cutoff for Job-Shop combinations
```