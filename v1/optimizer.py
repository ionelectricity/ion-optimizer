# optimizer.py
from typing import List, Tuple, Optional
import pandas as pd
from ortools.sat.python import cp_model

from devices.battery import BatteryStorageDevice
from devices.evse import EVSEDevice
from devices.water_heater import WaterHeaterDevice
from devices.smart_appliances import GenericSmartAppliance
from tariffs import ElectricalEnergyTariffDevice

class HomeEnergyOptimizer:
    """
    CP-SAT Optimization Engine acting as the Home Energy Management System (HEMS).
    
    Mathematical Domains:
    - Power: Measured in pure Watts (W) to maintain precision.
    - Energy (Storage): Measured in Watt-Minute-Percents (wm100).
      1 kWh = 1,000 W * 60 min * 100% = 6,000,000 wm100.
      This strictly eliminates floating-point truncation in thermodynamic equations.
    """
    def __init__(self, time_steps: int = 96, step_duration_m: int = 15):
        self.time_steps = time_steps
        self.step_duration_m = step_duration_m
        self.model = cp_model.CpModel()
        
        # Scaling factor: Input is kW, solver domain is W
        self.scale = 1000 

    def optimize(
        self,
        battery: BatteryStorageDevice,
        evse: EVSEDevice,
        water_heater: WaterHeaterDevice,
        appliances: List[GenericSmartAppliance],
        tariff: ElectricalEnergyTariffDevice,
        base_load_kw: List[float],
        solar_pv_kw: List[float],
        breaker_limit_kw: float,
        ev_arrival_step: int,
        ev_departure_step: int
    ) -> Tuple[Optional[pd.DataFrame], float, str, str]:
        
        log = ["### 🧠 OR-Tools Mathematical Trace Log\n"]
        
        # ---------------------------------------------------------------------
        # 1. PRE-SOLVER DATA SANITIZATION (Edge-Reality Abstraction)
        # ---------------------------------------------------------------------
        # We acknowledge the IEC 62962 LSE will physically shed loads if base load 
        # exceeds the breaker. Thus, the solver only plans around sanitized capacity.
        sanitized_base_load_w = [int(min(load, breaker_limit_kw) * self.scale) for load in base_load_kw]
        solar_pv_w = [int(p * self.scale) for p in solar_pv_kw]
        breaker_limit_w = int(breaker_limit_kw * self.scale)

        # Interpolate tariffs to match solver time steps
        imp_prices = tariff.get_import_prices()
        exp_prices = tariff.get_export_prices()
        if len(imp_prices) != self.time_steps:
            ratio = self.time_steps // len(imp_prices)
            imp_prices = [imp_prices[i // ratio] for i in range(self.time_steps)]
            exp_prices = [exp_prices[i // ratio] for i in range(self.time_steps)]

        # ---------------------------------------------------------------------
        # 2. VARIABLES: GRID POWER (Net Metering)
        # ---------------------------------------------------------------------
        grid_import = [self.model.NewIntVar(0, breaker_limit_w, f'grid_imp_{t}') for t in range(self.time_steps)]
        grid_export = [self.model.NewIntVar(0, breaker_limit_w, f'grid_exp_{t}') for t in range(self.time_steps)]

        # ---------------------------------------------------------------------
        # 3. VARIABLES: HOME BATTERY (Thermodynamics)
        # ---------------------------------------------------------------------
        # Domain: wm100
        bat_cap_wm100 = int(battery.capacity_wh * 60 * 100)
        bat_max_w = int(battery.max_charge_power_w)
        bat_init_wm100 = int(battery.current_soc_wh * 60 * 100)

        bat_chg = [self.model.NewIntVar(0, bat_max_w, f'bat_chg_{t}') for t in range(self.time_steps)]
        bat_dis = [self.model.NewIntVar(0, bat_max_w, f'bat_dis_{t}') for t in range(self.time_steps)]
        bat_soc_wm100 = [self.model.NewIntVar(0, bat_cap_wm100, f'bat_soc_{t}') for t in range(self.time_steps)]

        eff = battery.efficiency_pct
        eff_dis_factor = 10000 // eff

        for t in range(self.time_steps):
            c_term = bat_chg[t] * self.step_duration_m * eff
            d_term = bat_dis[t] * self.step_duration_m * eff_dis_factor
            
            if t == 0:
                self.model.Add(bat_soc_wm100[t] == bat_init_wm100 + c_term - d_term)
            else:
                self.model.Add(bat_soc_wm100[t] == bat_soc_wm100[t-1] + c_term - d_term)

        log.append(f"- **Battery Config**: Cap {battery.capacity_wh/1000}kWh | I/O {bat_max_w/1000}kW | Eff {eff}%")

        # ---------------------------------------------------------------------
        # 4. VARIABLES: EVSE & V2X (Midnight Crossing Topology)
        # ---------------------------------------------------------------------
        # If the EV is not home yet, we use the forecasted/expected capacity and arrival SoC
        expected_ev_cap = evse.vehicle_capacity_wh if evse.vehicle_capacity_wh is not None else 70_000
        expected_ev_init_soc = evse.current_vehicle_soc_wh if evse.current_vehicle_soc_wh is not None else 14_000

        ev_cap_wm100 = int(expected_ev_cap * 60 * 100)
        ev_max_w = int(evse.max_charge_power_w)
        ev_init_wm100 = int(expected_ev_init_soc * 60 * 100)
        
        ev_chg = [self.model.NewIntVar(0, ev_max_w, f'ev_chg_{t}') for t in range(self.time_steps)]
        ev_dis = [self.model.NewIntVar(0, ev_max_w if evse.supports_v2x else 0, f'ev_dis_{t}') for t in range(self.time_steps)]
        ev_soc_wm100 = [self.model.NewIntVar(0, ev_cap_wm100, f'ev_soc_{t}') for t in range(self.time_steps)]

        # Generate chronological home session (elegantly handles midnight wrap-around)
        if ev_arrival_step < ev_departure_step:
            home_session = list(range(ev_arrival_step, ev_departure_step))
        elif ev_arrival_step > ev_departure_step:
            home_session = list(range(ev_arrival_step, self.time_steps)) + list(range(0, ev_departure_step))
        else:
            home_session = []

        # Zero out EV variables when physically absent
        for t in range(self.time_steps):
            if t not in home_session:
                self.model.Add(ev_chg[t] == 0)
                self.model.Add(ev_dis[t] == 0)
                self.model.Add(ev_soc_wm100[t] == 0) 
        
        # Link SoC chronologically through the home session
        for i, t in enumerate(home_session):
            c_term = ev_chg[t] * self.step_duration_m * 100  # Assume 100% efficient internal EV cell math
            d_term = ev_dis[t] * self.step_duration_m * 100
            
            if i == 0:
                self.model.Add(ev_soc_wm100[t] == ev_init_wm100 + c_term - d_term)
            else:
                prev_t = home_session[i-1]
                self.model.Add(ev_soc_wm100[t] == ev_soc_wm100[prev_t] + c_term - d_term)

        # Apply EV Targets at departure
        if home_session and evse.charging_target_schedules:
            target_pct = evse.charging_target_schedules[0].charging_targets[0].target_soc_percent
            target_wm100 = int(ev_cap_wm100 * (target_pct / 100.0))
            last_home_step = home_session[-1]
            self.model.Add(ev_soc_wm100[last_home_step] >= target_wm100)

        # ---------------------------------------------------------------------
        # 5. VARIABLES: WATER HEATER (Thermal Mass Scheduling)
        # ---------------------------------------------------------------------
        wh_pwr_w = [self.model.NewIntVar(0, 0, f'wh_pwr_empty_{t}') for t in range(self.time_steps)]
        wh_active = [self.model.NewBoolVar(f'wh_act_{t}') for t in range(self.time_steps)]
        
        if water_heater.dem_cluster.forecast:
            wh_slot = water_heater.dem_cluster.forecast.slots[0]
            nominal_wh_w = int(wh_slot.nominal_power_mw / 1000)
            slots_needed = (wh_slot.min_duration_s + (self.step_duration_m * 60) - 1) // (self.step_duration_m * 60)
            
            latest_end_step = min(int(water_heater.dem_cluster.forecast.latest_end_time_epoch_s / 60 / self.step_duration_m), self.time_steps - 1)

            wh_pwr_w = [self.model.NewIntVar(0, nominal_wh_w, f'wh_pwr_{t}') for t in range(self.time_steps)]
            for t in range(self.time_steps):
                self.model.Add(wh_pwr_w[t] == nominal_wh_w).OnlyEnforceIf(wh_active[t])
                self.model.Add(wh_pwr_w[t] == 0).OnlyEnforceIf(wh_active[t].Not())
                if t >= latest_end_step:
                    self.model.Add(wh_active[t] == 0)
                    
            self.model.Add(sum(wh_active) == slots_needed)

        # ---------------------------------------------------------------------
        # 6. VARIABLES: SMART APPLIANCES (Contiguous Job Shop)
        # ---------------------------------------------------------------------
        appliance_power = [self.model.NewIntVar(0, breaker_limit_w, f'app_tot_{t}') for t in range(self.time_steps)]
        appliance_power_dict = {app.node_id: [self.model.NewConstant(0)] * self.time_steps for app in appliances}
        all_app_booleans = []

        for app in appliances:
            if not app.dem_cluster.forecast: continue
            
            forecast = app.dem_cluster.forecast
            latest_end_step = min(int(forecast.latest_end_time_epoch_s / 60 / self.step_duration_m), self.time_steps)
            
            slot_starts, slot_ends, slot_powers_at_t = [], [], []

            for slot_idx, slot in enumerate(forecast.slots):
                slots_needed = (slot.min_duration_s + (self.step_duration_m * 60) - 1) // (self.step_duration_m * 60)
                nominal_pwr_w = int(slot.nominal_power_mw / 1000)
                
                # Interval Variables define the start and end of this physical stage
                start_var = self.model.NewIntVar(0, self.time_steps, f'{app.node_id}_s{slot_idx}_start')
                end_var = self.model.NewIntVar(0, self.time_steps, f'{app.node_id}_s{slot_idx}_end')
                self.model.Add(end_var <= latest_end_step) # Hard User Deadline
                
                self.model.NewIntervalVar(start_var, slots_needed, end_var, f'{app.node_id}_s{slot_idx}_int')
                slot_starts.append(start_var)
                slot_ends.append(end_var)
                
                # Boolean presence array linking Intervals to the Power Grid
                slot_active = [self.model.NewBoolVar(f'{app.node_id}_s{slot_idx}_act_{t}') for t in range(self.time_steps)]
                all_app_booleans.extend(slot_active)
                
                power_at_t = [self.model.NewIntVar(0, nominal_pwr_w, f'{app.node_id}_s{slot_idx}_pwr_{t}') for t in range(self.time_steps)]
                slot_powers_at_t.append(power_at_t)

                for t in range(self.time_steps):
                    self.model.Add(power_at_t[t] == nominal_pwr_w).OnlyEnforceIf(slot_active[t])
                    self.model.Add(power_at_t[t] == 0).OnlyEnforceIf(slot_active[t].Not())
                    
                    is_after_start = self.model.NewBoolVar('')
                    is_before_end = self.model.NewBoolVar('')
                    self.model.Add(start_var <= t).OnlyEnforceIf(is_after_start)
                    self.model.Add(start_var > t).OnlyEnforceIf(is_after_start.Not())
                    self.model.Add(end_var > t).OnlyEnforceIf(is_before_end)
                    self.model.Add(end_var <= t).OnlyEnforceIf(is_before_end.Not())
                    
                    self.model.AddBoolAnd([is_after_start, is_before_end]).OnlyEnforceIf(slot_active[t])
                    self.model.AddBoolOr([is_after_start.Not(), is_before_end.Not()]).OnlyEnforceIf(slot_active[t].Not())

            # Enforce Strict Contiguity: Slot N+1 MUST start exactly when Slot N ends
            for i in range(len(slot_starts) - 1):
                self.model.Add(slot_starts[i+1] == slot_ends[i])

            # Aggregate this specific appliance's total power
            app_pwr_var = [self.model.NewIntVar(0, breaker_limit_w, f'{app.node_id}_tot_{t}') for t in range(self.time_steps)]
            for t in range(self.time_steps):
                self.model.Add(app_pwr_var[t] == sum(slot_powers[t] for slot_powers in slot_powers_at_t))
                appliance_power_dict[app.node_id][t] = app_pwr_var[t]

        # Aggregate ALL appliances into the master array
        for t in range(self.time_steps):
            self.model.Add(appliance_power[t] == sum(app_dict_array[t] for app_dict_array in appliance_power_dict.values()))

        # ---------------------------------------------------------------------
        # 7. GLOBAL CONSTRAINT: GRID BALANCE
        # ---------------------------------------------------------------------
        for t in range(self.time_steps):
            demand = sanitized_base_load_w[t] + ev_chg[t] + wh_pwr_w[t] + bat_chg[t] + appliance_power[t]
            supply = solar_pv_w[t] + ev_dis[t] + bat_dis[t]
            self.model.Add(grid_import[t] - grid_export[t] == demand - supply)

        # ---------------------------------------------------------------------
        # 8. OBJECTIVE FUNCTION (Economics & LCOS)
        # ---------------------------------------------------------------------
        total_cost = 0
        scaled_lcos_penalty = int(battery.lcos_cents_per_kwh * 10 * (self.step_duration_m / 60.0))
        log.append(f"- **LCOS Applied**: {battery.lcos_cents_per_kwh} c/kWh degradation mapped to solver domain.")

        for t in range(self.time_steps):
            eagerness_penalty = t * 1
            cost_at_t = (grid_import[t] * imp_prices[t] * 10) - (grid_export[t] * exp_prices[t] * 10)
            
            bat_cycle_cost = (bat_chg[t] + bat_dis[t]) * scaled_lcos_penalty
            ev_cycle_cost = (ev_chg[t] + ev_dis[t]) * scaled_lcos_penalty
            
            wh_delay_penalty = wh_active[t] * t * 2 
            app_delay_penalty = sum(app_bool * t * 2 for app_bool in all_app_booleans)
            
            total_cost += cost_at_t + bat_cycle_cost + ev_cycle_cost + eagerness_penalty + wh_delay_penalty + app_delay_penalty

        self.model.Minimize(total_cost)

        # ---------------------------------------------------------------------
        # 9. SOLVE & MAP RESULTS
        # ---------------------------------------------------------------------
        log.append("- **Execution**: Initiating Google OR-Tools CP-SAT (Single Thread, 10s Timeout)...")
        
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1 
        solver.parameters.max_time_in_seconds = 10.0
        status = solver.Solve(self.model)
        
        if status in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
            true_cost = sum(solver.Value(grid_import[t]) * imp_prices[t] - solver.Value(grid_export[t]) * exp_prices[t] for t in range(self.time_steps))
            
            time_labels = [f"{(t * self.step_duration_m) // 60:02d}:{(t * self.step_duration_m) % 60:02d}" for t in range(self.time_steps)]
            
            # Helper to map and print the trace log
            self._generate_trace_log(solver, grid_import, grid_export, imp_prices, exp_prices, bat_chg, bat_dis, ev_chg, ev_dis, wh_pwr_w, appliance_power, log)

            results = {
                "TimeStep": time_labels, 
                "Import Price (c)": imp_prices,
                "Export Price (c)": exp_prices,
                "Original Base Load (kW)": base_load_kw,
                "Sanitized Base Load (kW)": [sanitized_base_load_w[t] / self.scale for t in range(self.time_steps)],
                "Solar PV (kW)": [-solar_pv_w[t] / self.scale for t in range(self.time_steps)],
                "EV Charge (kW)": [solver.Value(ev_chg[t]) / self.scale for t in range(self.time_steps)],
                "EV V2X Discharge (kW)": [-solver.Value(ev_dis[t]) / self.scale for t in range(self.time_steps)],
                "EV SoC (kWh)": [solver.Value(ev_soc_wm100[t]) / (60 * 100 * self.scale) for t in range(self.time_steps)],
                "Water Heater (kW)": [solver.Value(wh_pwr_w[t]) / self.scale for t in range(self.time_steps)],
                "Bat Charge (kW)": [solver.Value(bat_chg[t]) / self.scale for t in range(self.time_steps)],
                "Bat Discharge (kW)": [-solver.Value(bat_dis[t]) / self.scale for t in range(self.time_steps)],
                "Grid Import (kW)": [solver.Value(grid_import[t]) / self.scale for t in range(self.time_steps)],
                "Grid Export (kW)": [-solver.Value(grid_export[t]) / self.scale for t in range(self.time_steps)],
                "Battery SoC (kWh)": [solver.Value(bat_soc_wm100[t]) / (60 * 100 * self.scale) for t in range(self.time_steps)]
            }
            results["Net Grid Flow (kW)"] = [results["Grid Import (kW)"][t] + results["Grid Export (kW)"][t] for t in range(self.time_steps)]
            
            for app in appliances:
                results[f"{app.node_id} (kW)"] = [solver.Value(appliance_power_dict[app.node_id][t]) / self.scale for t in range(self.time_steps)]
            
            return pd.DataFrame(results), true_cost / (self.scale * 100), solver.StatusName(status), "\n".join(log)
            
        return None, 0.0, solver.StatusName(status), "\n".join(log)

    def _generate_trace_log(self, solver, grid_import, grid_export, imp_prices, exp_prices, bat_chg, bat_dis, ev_chg, ev_dis, wh_power, appliance_power, log: List[str]):
        """Generates a human-readable dispatch string for each timestep."""
        log.append("\n" + "="*60 + "\n💡 DISPATCH TIMELINE\n" + "="*60)
        for t in range(self.time_steps):
            h, m = (t * self.step_duration_m) // 60, (t * self.step_duration_m) % 60
            
            g_imp = solver.Value(grid_import[t]) / self.scale
            g_exp = solver.Value(grid_export[t]) / self.scale
            price = imp_prices[t] if g_imp > 0 else exp_prices[t]
            
            actions = []
            if solver.Value(bat_chg[t]) > 0: actions.append(f"BatChg({solver.Value(bat_chg[t])/self.scale}kW)")
            if solver.Value(bat_dis[t]) > 0: actions.append(f"BatDis({solver.Value(bat_dis[t])/self.scale}kW)")
            if solver.Value(ev_chg[t]) > 0: actions.append(f"EVChg({solver.Value(ev_chg[t])/self.scale}kW)")
            if solver.Value(ev_dis[t]) > 0: actions.append(f"V2X({solver.Value(ev_dis[t])/self.scale}kW)")
            if solver.Value(wh_power[t]) > 0: actions.append(f"WaterHeater({solver.Value(wh_power[t])/self.scale}kW)")
            if solver.Value(appliance_power[t]) > 0: actions.append(f"Appliance({solver.Value(appliance_power[t])/self.scale}kW)")
            
            action_str = " | ".join(actions) if actions else "Idling"
            log.append(f"[{h:02d}:{m:02d}] Tariff: {price}c | Net: {g_imp - g_exp:+.1f}kW | {action_str}")
        log.append("="*60 + "\n")
        print("\n".join(log)) # Print to console as well