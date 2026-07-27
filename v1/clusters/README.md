# Energy Management Clusters & Structs

## 1. Overview
This folder (along with `energy_management_structs.py`) contains the strict structural definitions required to interface with the Energy Management ecosystem. 

Rather than using arbitrary JSON dictionaries, the simulator enforces strict Object-Oriented typing. Every data class, enumeration, and cluster state machine maps directly to a specific section of the Energy Management Specification Document.

---

## 2. Core Data Structures (`energy_management_structs.py`)

### 2.1. `SlotStruct` (Section 9.2.7.14)
The fundamental unit of operational time for an Energy Smart Appliance (ESA). 
*   **Purpose:** Defines the duration, pausability, and power consumption (`NominalPower`, `MinPower`, `MaxPower`) of a specific phase of operation.
*   **Simulator Usage:** Used primarily by multi-stage appliances (Dishwashers, Washing Machines) to define their execution profiles (e.g., Heating $\rightarrow$ Tumbling $\rightarrow$ Spinning).

### 2.2. `ForecastStruct` (Section 9.2.7.13)
*   **Purpose:** The payload exposed by the DEM cluster. It contains the sequence of `SlotStruct`s and defines the overall execution window (`EarliestStartTime`, `LatestEndTime`).
*   **Simulator Usage:** The OR-Tools engine parses this struct to generate `IntervalVar`s. The `LatestEndTime` is extracted and applied as a hard upper bound (`end_var <= latest_end_step`) in the CP-SAT model.

### 2.3. `PowerAdjustStruct` (Section 9.2.7.10)
*   **Purpose:** Exposes the continuous power modulation limits for batteries and EVSEs. 
*   **Sign Convention:** As defined in Section 9.2.6.1, Power is a signed integer (milliwatts). Positive values indicate consumption (charging). Negative values indicate generation (discharging/V2X).

### 2.4. `ChargingTargetStruct` (Section 9.3.7.6)
*   **Purpose:** Captures user intent for an EV (e.g., "I need 80% SoC by 08:00").
*   **Precedence Logic:** Section 9.3.7.6.2 dictates that `TargetSoC` takes strict precedence over `AddedEnergy` if the EVSE supports the `SOC` feature. The simulator enforces this hierarchy when translating the target into the baseline `ForecastStruct`.

---

## 3. The Device Energy Management (DEM) Cluster (`dem.py`)
**Cluster ID:** 0x0098 | **Reference:** Section 9.2

The DEM cluster is the universal translator between a physical device and the Home Energy Management System (HEMS). 

### 3.1. Feature Map Enforcement (Section 9.2.4)
The DEM cluster constructor (`_validate_features`) enforces the standard's strict topological rules using bitwise operations (`DEMFeatureMap`).
*   **Rule:** At least one feature MUST be supported.
*   **Rule:** If `PA` (PowerAdjustment) is supported, `SFR` (StateForecastReporting) SHALL NOT be supported.
*   **Rule:** If `PAU` (Pausable) or `STA` (StartTimeAdjustment) are supported, `PFR` (PowerForecastReporting) MUST be supported.

### 3.2. State Machine (`ESAStateEnum`)
The cluster maintains the `esa_state` attribute (Section 9.2.8.3). 
When the simulator issues a power modulation command, the cluster validates the request against the currently advertised `PowerAdjustCapability` bounds. If the request is mathematically valid, the state transitions from `ONLINE` to `POWER_ADJUST_ACTIVE`.

### 3.3. Opt-Out State (`OptOutStateEnum`)
(Section 9.2.8.8). If the user physically overrides the appliance (e.g., pressing "Start Now" on the washing machine), the `opt_out_state` transitions to `LOCAL_OPT_OUT` or `OPT_OUT`. The `handle_power_adjust_request()` API checks this state and will safely reject automated CP-SAT schedules by returning `False` (CONSTRAINT_ERROR) if an override is active.

---

## 4. Commodity Pricing & Tariffs (`tariffs.py`)
**Cluster IDs:** 0x0095 (Commodity Price) & 0x0700 (Commodity Tariff)

The financial driver for the CP-SAT objective function.

### 4.1. `CommodityPriceStruct` (Section 9.9.5.3)
Represents a specific price over a specific time epoch. 
*   **Validation Rules (Section 9.9.6.4):** The `set_dynamic_price_forecast()` method rigorously validates incoming tariff arrays. It ensures that the array is strictly chronological, that start/end epochs do not overlap, and that only the final entry in the array is permitted to have a `null` EndTime.

### 4.2. Net Metering Architecture
While the Energy Management standard defines prices, it does not dictate how an optimizer should handle simultaneous bi-directional flow. 
To satisfy the standard's requirement to track export versus import (Section 14.6.7.3 - Export Rate topologies), the `ElectricalEnergyTariffDevice` maintains **two distinct Commodity Price Clusters**:
1.  **Import Cluster:** Tracks the cost of pulling power from the grid.
2.  **Export Cluster:** Tracks the compensation for pushing solar/V2X/battery power to the grid.
The optimizer extracts these two arrays and pairs them with `grid_import` and `grid_export` CP-SAT variables to calculate net-metered financial outcomes perfectly.