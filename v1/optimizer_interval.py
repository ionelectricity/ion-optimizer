# optimizer_interval.py
from ortools.sat.python import cp_model
from typing import List, Tuple, Optional
import pandas as pd

from devices.battery import BatteryStorageDevice
from devices.evse import EVSEDevice
from devices.water_heater import WaterHeaterDevice
from devices.appliance import SmartApplianceDevice
from tariffs import ElectricalEnergyTariffDevice


class IntervalEnergyOptimizer:
    """
    Advanced CP-SAT Optimizer focusing on discrete appliance scheduling using IntervalVars.
    Fulfills the Job Shop and Scheduling paradigms of Atividade 3.
    """

    def __init__(self, time_steps: int = 24, step_duration_m: int = 60):
        self.time_steps = time_steps
        self.step_duration_m = step_duration_m
        self.model = cp_model.CpModel()
        self.scale = 1000

    def optimize(
            self,
            battery: BatteryStorageDevice,
            evse: EVSEDevice,
            water_heater: WaterHeaterDevice,
            appliances: List[SmartApplianceDevice],
            tariff: ElectricalEnergyTariffDevice,
            base_load_kw: List[float],
            solar_pv_kw: List[float],
            breaker_limit_kw: float,
            ev_arrival_step: int,
            ev_departure_step: int
    ) -> Tuple[Optional[pd.DataFrame], float, str]:

        imp_prices = tariff.get_import_prices()
        exp_prices = tariff.get_export_prices()

        scaled_base = [int(p * self.scale) for p in base_load_kw]
        scaled_solar = [int(p * self.scale) for p in solar_pv_kw]
        scaled_breaker = int(breaker_limit_kw * self.scale)

        # 1. Grid Power
        grid_import = [self.model.NewIntVar(0, scaled_breaker, f'grid_imp_{t}') for t in range(self.time_steps)]
        grid_export = [self.model.NewIntVar(0, scaled_breaker, f'grid_exp_{t}') for t in range(self.time_steps)]

        # --- 2. APPLIANCE SCHEDULING (INTERVAL VARS) ---
        # This is the new logic based on OR-Tools scheduling docs
        appliance_power_at_t = [0] * self.time_steps  # We will sum the power of all appliances here

        for app_idx, appliance in enumerate(appliances):
            if not appliance.dem_cluster.forecast:
                continue

            forecast = appliance.dem_cluster.forecast
            slot = forecast.slots[0]

            pwr_scaled = int((slot.nominal_power_mw / 1_000_000) * self.scale)
            duration_steps = int(slot.min_duration_s / 60 / self.step_duration_m)

            earliest_start = max(0, int(forecast.earliest_start_time_epoch_s / 60 / self.step_duration_m))
            latest_end = min(self.time_steps, int(forecast.latest_end_time_epoch_s / 60 / self.step_duration_m))

            # Create the interval variables
            start_var = self.model.NewIntVar(earliest_start, latest_end - duration_steps, f'app_{app_idx}_start')
            end_var = self.model.NewIntVar(earliest_start + duration_steps, latest_end, f'app_{app_idx}_end')
            interval_var = self.model.NewIntervalVar(start_var, duration_steps, end_var, f'app_{app_idx}_interval')

            # Map the interval to the 1D power array for the grid balance
            # For each timestep, we need a boolean that is true IF the interval covers this timestep
            for t in range(self.time_steps):
                is_active = self.model.NewBoolVar(f'app_{app_idx}_active_{t}')

                # Logic: is_active is True IF (start_var <= t) AND (t < end_var)
                # To do this in CP-SAT, we need two temporary booleans
                after_start = self.model.NewBoolVar(f'app_{app_idx}_after_start_{t}')
                before_end = self.model.NewBoolVar(f'app_{app_idx}_before_end_{t}')

                self.model.Add(start_var <= t).OnlyEnforceIf(after_start)
                self.model.Add(start_var > t).OnlyEnforceIf(after_start.Not())

                self.model.Add(end_var > t).OnlyEnforceIf(before_end)
                self.model.Add(end_var <= t).OnlyEnforceIf(before_end.Not())

                # is_active == after_start AND before_end
                self.model.AddBoolAnd([after_start, before_end]).OnlyEnforceIf(is_active)
                self.model.AddBoolOr([after_start.Not(), before_end.Not()]).OnlyEnforceIf(is_active.Not())

                # Add the power to the cumulative list for this timestep
                # We use a temporary integer variable to hold the power for this timestep
                step_power = self.model.NewIntVar(0, pwr_scaled, f'app_{app_idx}_pwr_{t}')
                self.model.Add(step_power == pwr_scaled).OnlyEnforceIf(is_active)
                self.model.Add(step_power == 0).OnlyEnforceIf(is_active.Not())

                if isinstance(appliance_power_at_t[t], int):
                    appliance_power_at_t[t] = step_power
                else:
                    appliance_power_at_t[t] += step_power

        # --- 3. EXISTING DEVICES (Battery, EVSE, WH) ---
        bat_cap = int((battery.capacity_wh / 1000) * self.scale)
        bat_max = int((battery.max_charge_power_w / 1000) * self.scale)
        bat_init = int((battery.current_soc_wh / 1000) * self.scale)
        bat_chg = [self.model.NewIntVar(0, bat_max, f'b_chg_{t}') for t in range(self.time_steps)]
        bat_dis = [self.model.NewIntVar(0, bat_max, f'b_dis_{t}') for t in range(self.time_steps)]
        bat_soc = [self.model.NewIntVar(0, bat_cap, f'b_soc_{t}') for t in range(self.time_steps)]

        for t in range(self.time_steps):
            c_en = bat_chg[t] * self.step_duration_m
            d_en = bat_dis[t] * self.step_duration_m
            if t == 0:
                self.model.Add(60 * bat_soc[t] == 60 * bat_init + c_en - d_en)
            else:
                self.model.Add(60 * bat_soc[t] == 60 * bat_soc[t - 1] + c_en - d_en)

        ev_max = int((evse.max_charge_power_w / 1000) * self.scale)
        ev_cap = int((evse.vehicle_capacity_wh / 1000) * self.scale)
        ev_init = int((evse.current_vehicle_soc_wh / 1000) * self.scale)
        ev_chg = [self.model.NewIntVar(0, ev_max, f'ev_chg_{t}') for t in range(self.time_steps)]
        ev_dis = [self.model.NewIntVar(0, ev_max if evse.supports_v2x else 0, f'ev_dis_{t}') for t in
                  range(self.time_steps)]
        ev_soc = [self.model.NewIntVar(0, ev_cap, f'ev_soc_{t}') for t in range(self.time_steps)]

        if ev_arrival_step < ev_departure_step:
            home_hours = list(range(ev_arrival_step, ev_departure_step))
        elif ev_arrival_step > ev_departure_step:
            home_hours = list(range(ev_arrival_step, self.time_steps)) + list(range(0, ev_departure_step))
        else:
            home_hours = []

        for t in range(self.time_steps):
            if t not in home_hours:
                self.model.Add(ev_chg[t] == 0)
                self.model.Add(ev_dis[t] == 0)
                self.model.Add(ev_soc[t] == 0)

        for i in range(len(home_hours)):
            t = home_hours[i]
            c_en = ev_chg[t] * self.step_duration_m
            d_en = ev_dis[t] * self.step_duration_m
            if i == 0:
                self.model.Add(60 * ev_soc[t] == 60 * ev_init + c_en - d_en)
            else:
                prev_t = home_hours[i - 1]
                self.model.Add(60 * ev_soc[t] == 60 * ev_soc[prev_t] + c_en - d_en)

        if home_hours and evse.charging_target_schedules:
            target_pct = evse.charging_target_schedules[0].charging_targets[0].target_soc_percent
            target_scaled = int(ev_cap * (target_pct / 100.0))
            self.model.Add(ev_soc[home_hours[-1]] >= target_scaled)

        wh_power = [self.model.NewIntVar(0, 0, f'wh_pwr_empty_{t}') for t in range(self.time_steps)]
        wh_active = [self.model.NewBoolVar(f'wh_act_{t}') for t in range(self.time_steps)]
        if water_heater.dem_cluster.forecast:
            wh_slot = water_heater.dem_cluster.forecast.slots[0]
            wh_pwr_scaled = int((wh_slot.nominal_power_mw / 1_000_000) * self.scale)
            slots_needed = int(wh_slot.min_duration_s / 60 / self.step_duration_m)
            latest_end_step = min(
                int(water_heater.dem_cluster.forecast.latest_end_time_epoch_s / 60 / self.step_duration_m),
                self.time_steps - 1)
            wh_power = [self.model.NewIntVar(0, wh_pwr_scaled, f'wh_pwr_{t}') for t in range(self.time_steps)]
            for t in range(self.time_steps):
                self.model.Add(wh_power[t] == wh_pwr_scaled).OnlyEnforceIf(wh_active[t])
                self.model.Add(wh_power[t] == 0).OnlyEnforceIf(wh_active[t].Not())
                if t >= latest_end_step:
                    self.model.Add(wh_active[t] == 0)
            self.model.Add(sum(wh_active) == slots_needed)

        # --- 4. GLOBAL GRID BALANCE ---
        for t in range(self.time_steps):
            demand = scaled_base[t] + ev_chg[t] + wh_power[t] + bat_chg[t] + appliance_power_at_t[t]
            supply = scaled_solar[t] + ev_dis[t] + bat_dis[t]
            self.model.Add(grid_import[t] - grid_export[t] == demand - supply)

        # --- 5. OBJECTIVE ---
        total_cost = 0
        for t in range(self.time_steps):
            eagerness_penalty = t * 1
            cost_at_t = (grid_import[t] * imp_prices[t]) - (grid_export[t] * exp_prices[t])
            wh_delay_penalty = wh_active[t] * t * 2
            total_cost += cost_at_t + eagerness_penalty + wh_delay_penalty

        wear_penalty = sum(bat_chg[t] + bat_dis[t] + ev_chg[t] + ev_dis[t] for t in range(self.time_steps))
        self.model.Minimize((total_cost * 10) + wear_penalty)

        # --- 6. SOLVE ---
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.max_time_in_seconds = 10.0
        status = solver.Solve(self.model)

        if status in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
            true_cost = sum(
                solver.Value(grid_import[t]) * imp_prices[t] - solver.Value(grid_export[t]) * exp_prices[t] for t in
                range(self.time_steps))

            # Ensure the appliance array contains values, not just OR-Tools variables
            extracted_app_power = [
                solver.Value(appliance_power_at_t[t]) if isinstance(appliance_power_at_t[t], cp_model.IntVar) else 0
                for t in range(self.time_steps)
            ]

            results = {
                "TimeStep": [f"{t:02d}:00" for t in range(self.time_steps)],
                "Import Price (c)": imp_prices,
                "Export Price (c)": exp_prices,
                "Base Load (kW)": [scaled_base[t] / self.scale for t in range(self.time_steps)],
                "Solar PV (kW)": [-scaled_solar[t] / self.scale for t in range(self.time_steps)],
                "EV Charge (kW)": [solver.Value(ev_chg[t]) / self.scale for t in range(self.time_steps)],
                "EV V2X Discharge (kW)": [-solver.Value(ev_dis[t]) / self.scale for t in range(self.time_steps)],
                "EV SoC (kWh)": [solver.Value(ev_soc[t]) / self.scale for t in range(self.time_steps)],
                "Water Heater (kW)": [solver.Value(wh_power[t]) / self.scale for t in range(self.time_steps)],
                "Appliances (kW)": [p / self.scale for p in extracted_app_power],  # ADDED APPLIANCE POWER
                "Bat Charge (kW)": [solver.Value(bat_chg[t]) / self.scale for t in range(self.time_steps)],
                "Bat Discharge (kW)": [-solver.Value(bat_dis[t]) / self.scale for t in range(self.time_steps)],
                "Grid Import (kW)": [solver.Value(grid_import[t]) / self.scale for t in range(self.time_steps)],
                "Grid Export (kW)": [-solver.Value(grid_export[t]) / self.scale for t in range(self.time_steps)],
                "Battery SoC (kWh)": [solver.Value(bat_soc[t]) / self.scale for t in range(self.time_steps)]
            }
            results["Net Grid Flow (kW)"] = [results["Grid Import (kW)"][t] + results["Grid Export (kW)"][t] for t in
                                             range(self.time_steps)]

            return pd.DataFrame(results), true_cost / (self.scale * 100), solver.StatusName(status)
        return None, 0.0, solver.StatusName(status)