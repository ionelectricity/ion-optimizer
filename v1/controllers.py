# controllers.py
from typing import List, Dict
import pandas as pd

from environment import EnvironmentState, Action
from optimizer import HomeEnergyOptimizer
from devices.battery import BatteryStorageDevice
from devices.evse import EVSEDevice
from devices.water_heater import WaterHeaterDevice
from tariffs import ElectricalEnergyTariffDevice

class DumbController:
    """
    A purely reactive, greedy controller.
    Represents a standard home without optimization.
    """

    def __init__(self, battery: BatteryStorageDevice, evse: EVSEDevice, water_heater: WaterHeaterDevice):
        self.battery = battery
        self.evse = evse
        self.water_heater = water_heater

    def act(self, state: EnvironmentState, forecasted_solar_w: List[int], forecasted_load_w: List[int]) -> Action:
        action = Action()

        # A reactive controller only looks at the exact current moment
        current_solar_w = forecasted_solar_w[state.time_step]
        current_load_w = forecasted_load_w[state.time_step]

        # 1. EV Logic: Charge immediately if plugged in and not full
        if state.ev_is_home and state.ev_soc_wh < self.evse.vehicle_capacity_wh:
            action.ev_charge_w = self.evse.max_charge_power_w

        # 2. Water Heater Logic: Heat immediately if thermal energy is missing
        if state.water_heater_remaining_wh > 0:
            action.water_heater_active = True

        # 3. Battery Logic: Greedy Self-Consumption
        # First, calculate what the house needs right now
        wh_power_w = self.water_heater.heating_power_w if action.water_heater_active else 0
        current_demand_w = current_load_w + action.ev_charge_w + wh_power_w

        net_balance_w = current_solar_w - current_demand_w

        if net_balance_w > 0:
            # We have excess solar! Charge the battery.
            action.battery_charge_w = min(net_balance_w, self.battery.max_charge_power_w)
        elif net_balance_w < 0:
            # We are pulling from the grid! Discharge the battery to help.
            needed_w = abs(net_balance_w)
            action.battery_discharge_w = min(needed_w, self.battery.max_discharge_power_w)

        return action


class ORToolsMPCController:
    """
    Model Predictive Control (MPC) wrapper for the CP-SAT engine.
    Solves the remaining horizon and executes the immediate next step.
    """
    def __init__(
        self, 
        time_steps: int, 
        step_duration_m: int,
        battery: BatteryStorageDevice,
        evse: EVSEDevice,
        water_heater: WaterHeaterDevice,
        tariff: ElectricalEnergyTariffDevice,
        breaker_limit_kw: float,
        ev_arrival_step: int,
        ev_departure_step: int
    ):
        self.time_steps = time_steps
        self.step_duration_m = step_duration_m
        
        # References to the devices (Needed to build the OR-Tools model)
        self.battery = battery
        self.evse = evse
        self.water_heater = water_heater
        self.tariff = tariff
        self.breaker_limit_kw = breaker_limit_kw
        
        self.ev_arrival_step = ev_arrival_step
        self.ev_departure_step = ev_departure_step

        # Internal cache of the plan so we can visualize what the solver was thinking
        self.last_optimized_plan: Optional[pd.DataFrame] = None

    def act(self, state: EnvironmentState, forecasted_solar_w: List[int], forecasted_load_w: List[int]) -> Action:
        """
        Runs the optimizer for the remaining horizon [t, T].
        """
        # Convert W to kW for the optimizer
        solar_kw = [s / 1000.0 for s in forecasted_solar_w]
        load_kw = [l / 1000.0 for l in forecasted_load_w]

        # Update the Device Objects with the TRUE physical state from the Environment
        # This is the crucial MPC feedback loop!
        self.battery.current_soc_wh = state.battery_soc_wh
        
        if state.ev_is_home:
            self.evse.current_vehicle_soc_wh = state.ev_soc_wh
            
        # Update Water Heater Forecast to reflect remaining thermal needs
        if self.water_heater.dem_cluster.forecast:
            if state.water_heater_remaining_wh <= 0:
                self.water_heater.dem_cluster.forecast = None # Done heating
            else:
                # Update remaining duration required
                rem_duration_s = int((state.water_heater_remaining_wh / self.water_heater.heating_power_w) * 3600)
                self.water_heater.dem_cluster.forecast.slots[0].min_duration_s = rem_duration_s
                self.water_heater.dem_cluster.forecast.slots[0].nominal_energy_mwh = int(state.water_heater_remaining_wh * 1000)

        # Re-initialize the optimizer for the CURRENT time horizon
        optimizer = HomeEnergyOptimizer(time_steps=self.time_steps, step_duration_m=self.step_duration_m)

        # SOLVE!
        df, cost, status, _ = optimizer.optimize(
            battery=self.battery,
            evse=self.evse,
            water_heater=self.water_heater,
            tariff=self.tariff,
            base_load_kw=load_kw,
            solar_pv_kw=solar_kw,
            breaker_limit_kw=self.breaker_limit_kw,
            ev_arrival_step=self.ev_arrival_step,
            ev_departure_step=self.ev_departure_step,
            appliances=[]
        )

        if df is None:
            # Fallback to Dumb Control if the solver mathematically proves infeasibility
            # (e.g., target impossible due to breaker limits)
            print(f"⚠️ MPC Solver Infeasible at t={state.time_step}. Falling back to greedy logic.")
            dumb = DumbController(self.battery, self.evse, self.water_heater)
            return dumb.act(state, forecasted_solar_w[state.time_step], forecasted_load_w[state.time_step])

        self.last_optimized_plan = df

        # EXTRACT THE ACTION FOR THE CURRENT TIME STEP (t)
        # The solver returned an array for [0, 23]. We only care about index `state.time_step`.
        t = state.time_step
        
        action = Action(
            battery_charge_w=int(df["Bat Charge (kW)"].iloc[t] * 1000),
            battery_discharge_w=int(df["Bat Discharge (kW)"].iloc[t] * -1000), # Un-invert the chart negative
            ev_charge_w=int(df["EV Charge (kW)"].iloc[t] * 1000),
            ev_discharge_w=int(df["EV V2X Discharge (kW)"].iloc[t] * -1000), # Un-invert the chart negative
            water_heater_active=df["Water Heater (kW)"].iloc[t] > 0
        )
        
        return action