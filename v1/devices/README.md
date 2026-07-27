# Energy Management Device Physics & Scheduling Models

## 1. Overview
This folder contains the Object-Oriented representations of Energy Smart Appliances (ESAs). These classes (`BatteryStorageDevice`, `EVSEDevice`, `WaterHeaterDevice`, `GenericSmartAppliance`) serve two purposes:
1.  **State Management:** They hold the internal physical state of the device (Capacity, State of Charge, Hardware limits).
2.  **Struct Translation:** They map user intent (e.g., "Eco Wash by 8 AM") into strict `ForecastStruct` and `PowerAdjustCapability` payloads.

The `HomeEnergyOptimizer` then reads these objects and injects their physical constraints into the CP-SAT algebraic matrix.

---

## 2. Battery Storage (Thermodynamics)
**File:** `battery.py`
**Type:** 0x0018 (Battery Storage)

Batteries are modeled as continuous variables capable of both charging and discharging. 
The core challenge in CP-SAT is tracking the State of Charge ($SoC$) across time without using floating-point division, as integer division truncates data and destroys energy conservation laws.

### The Integer Thermodynamics Equation
Standard physics: 
$$SoC(t) = SoC(t-1) + \Big(Chg \times \eta_{chg}\Big) - \Big(\frac{Dis}{\eta_{dis}}\Big)$$

To eliminate the division by $\eta_{dis}$ (Efficiency), we multiply the entire equation by a lowest common multiple based on efficiency $\eta$ (e.g., $95\%$).

**The Code Translation:**
```python
# Variables scaled to Watt-Minute-Percents (wm100)
c_term = bat_chg[t] * step_duration_m * eff
d_term = bat_dis[t] * step_duration_m * (10000 // eff)

# mult = 100 * eff
model.Add(mult * bat_soc[t] == mult * bat_soc[t-1] + c_term - d_term)
```
This guarantees 100% precise energy tracking without ever dropping a decimal point.

---

## 3. EVSE & V2X (The Midnight Crossing Topology)
**File:** `evse.py`
**Type:** 0x050C (Energy EVSE)

The EVSE bridges user intent (Departure Time, Target SoC) with power modulation. If V2X is enabled, the EV acts as a secondary home battery.

### The Midnight Bug & Circular Sessions
A standard timeline runs $t=0$ (00:00) to $t=95$ (23:45). 
If an EV arrives at 17:00 ($t=68$) and leaves at 08:00 ($t=32$), naively enforcing $SoC(t) = SoC(t-1)$ will crash the solver because $SoC(0)$ cannot mathematically link to $SoC(95)$.

**The Solution: Topological Session Mapping**
We dynamically generate a `home_session` array.
*   If Arrival < Departure: `[17, 18, 19, 20]`
*   If Arrival > Departure (Midnight cross): `[68, 69... 95] + [0, 1... 31]`

The optimizer then iterates strictly over the `home_session` array. 
```python
# Link SoC chronologically through the home session
prev_t = home_session[i-1]
model.Add(ev_soc[t] == ev_soc[prev_t] + c_en - d_en)
```
This forces $SoC(0)$ to directly inherit the energy state from $SoC(95)$, creating a perfect continuous loop. Variables outside the `home_session` are hard-constrained to $0$.

---

## 4. Water Heater (Single-Slot Thermal Mass)
**File:** `water_heater.py`
**Type:** 0x050F (Water Heater)

Unlike batteries, a water heater does not track continuous $kWh$ in the optimizer. It is modeled as a discrete block of thermal energy that must be fulfilled. 

**The Constraints:**
*   **Presence Booleans:** Instead of a continuous power variable, we create a boolean `wh_active[t]`.
*   **Power Linking:** `model.Add(power == NOMINAL_POWER).OnlyEnforceIf(wh_active[t])`
*   **Deadline:** `model.Add(wh_active[t] == 0)` for all $t \ge LatestEndTime$.
*   **Duration:** `model.Add(sum(wh_active) == SlotsNeeded)`.

Because Water Heaters are thermally insulated, the `wh_active` blocks are allowed to be non-contiguous (the solver can pause heating for 15 minutes during a price spike).

---

## 5. Smart Appliances (Multi-Slot Job-Shop Scheduling)
**File:** `smart_appliances.py` (Dishwasher, Laundry Washer)
**Type:** 0x0075 (Dishwasher), 0x0073 (Laundry Washer)

This is the most mathematically complex component, utilizing OR-Tools' Job-Shop logic (`IntervalVar`).
When a user selects "Eco Wash", the device generates a `ForecastStruct` containing multiple `SlotStruct`s (e.g., Slot 0: Heating, Slot 1: Washing, Slot 2: Drying).

### The Job-Shop Formulation
For each slot in the forecast, the optimizer creates:
1.  `start_var`: The time index the slot begins.
2.  `end_var`: The time index the slot finishes.
3.  `interval_var`: Links the start, size (duration), and end.

**Strict Contiguity (Precedence):**
By default, the user expects a washing machine to run its cycle from start to finish without pausing. We enforce this by locking the intervals together:
```python
for i in range(len(slot_starts) - 1):
    model.Add(slot_starts[i+1] == slot_ends[i])
```

**Grid Power Linking (Boolean Projection):**
`IntervalVar`s do not inherently hold power values. We project the interval onto the timeline using boolean arrays.
```python
# Is time 't' inside the [start, end) interval?
model.Add(start_var <= t).OnlyEnforceIf(is_after_start)
model.Add(end_var > t).OnlyEnforceIf(is_before_end)

# If yes, slot is active, and grid power = Nominal Power
model.AddBoolAnd([is_after_start, is_before_end]).OnlyEnforceIf(slot_active[t])
model.Add(power_at_t[t] == NOMINAL_POWER).OnlyEnforceIf(slot_active[t])
```

### Search Space Implication
Because the solver must test the starting position of these contiguous blocks across 96 time-steps, this creates a combinatorial search space. This is why the solver time jumps from 0.05 seconds (pure continuous flow) to ~1.0 seconds. If deadlines are mathematically impossible, the bounding constraints on `end_var` will immediately trigger an `INFEASIBLE` state, safely aborting the run.