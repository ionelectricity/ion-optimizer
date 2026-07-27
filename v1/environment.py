# environment.py
from dataclasses import dataclass
from typing import List, Dict, Optional
import pandas as pd

from devices.battery import BatteryStorageDevice
from devices.evse import EVSEDevice
from devices.water_heater import WaterHeaterDevice
from tariffs import ElectricalEnergyTariffDevice

@dataclass
class EnvironmentState:
    """Represents the true physical state of the home at time t."""
    time_step: int
    battery_soc_wh: int
    ev_soc_wh: int
    ev_is_home: bool
    water_heater_remaining_wh: int

@dataclass
class Action:
    """The commands sent by a Controller (Dumb or Smart) to the Environment for time t."""
    battery_charge_w: int = 0
    battery_discharge_w: int = 0
    ev_charge_w: int = 0
    ev_discharge_w: int = 0
    water_heater_active: bool = False

class HomeEnvironment:
    """
    The Digital Twin of the home. 
    It executes actions, enforces physical limits, and calculates true costs.
    """
    def __init__(
        self,
        time_steps: int,
        step_duration_m: int,
        battery: BatteryStorageDevice,
        evse: EVSEDevice,
        water_heater: WaterHeaterDevice,
        tariff: ElectricalEnergyTariffDevice,
        breaker_limit_kw: float
    ):
        self.time_steps = time_steps
        self.step_duration_m = step_duration_m
        self.step_duration_h = step_duration_m / 60.0
        
        # Physical Devices
        self.battery = battery
        self.evse = evse
        self.water_heater = water_heater
        self.tariff = tariff
        self.breaker_limit_w = int(breaker_limit_kw * 1000)

        # Simulation Time
        self.current_t = 0
        
        # Data Logging for Analysis
        self.history: List[Dict] = []

    def reset(self) -> EnvironmentState:
        """Resets the environment to t=0 and returns the initial state."""
        self.current_t = 0
        self.history = []
        return self.get_state()

    def get_state(self) -> EnvironmentState:
        """Returns the current physical state of the devices."""
        # For the EV, we check if it is physically plugged in. 
        # (In the simulator, we will toggle this state externally based on arrival/departure times)
        ev_is_home = self.evse.state != 0 # 0 = NOT_PLUGGED_IN
        
        wh_remaining = self.water_heater.total_thermal_capacity_wh - self.water_heater.current_thermal_energy_wh

        return EnvironmentState(
            time_step=self.current_t,
            battery_soc_wh=self.battery.current_soc_wh,
            ev_soc_wh=self.evse.current_vehicle_soc_wh if ev_is_home else 0,
            ev_is_home=ev_is_home,
            water_heater_remaining_wh=wh_remaining
        )

    def step(self, action: Action, actual_base_load_w: int, actual_solar_w: int) -> Tuple[EnvironmentState, float]:
        """
        Executes the action for 1 time step, updates physics, and calculates cost.
        Returns the NEXT state and the cost incurred during this step.
        """
        if self.current_t >= self.time_steps:
            raise RuntimeError("Simulation has reached the end of the horizon.")

        # --- 1. Enforce Physical Limits on the Action ---
        # The environment prevents the optimizer/controller from doing impossible things.
        
        # Battery Limits
        actual_bat_chg_w = min(action.battery_charge_w, self.battery.max_charge_power_w)
        actual_bat_dis_w = min(action.battery_discharge_w, self.battery.max_discharge_power_w)
        
        # Battery SoC limits (Can't charge more than capacity, can't discharge more than we have)
        max_wh_can_charge = self.battery.capacity_wh - self.battery.current_soc_wh
        max_w_can_charge = int(max_wh_can_charge / self.step_duration_h)
        actual_bat_chg_w = min(actual_bat_chg_w, max_w_can_charge)

        max_wh_can_discharge = self.battery.current_soc_wh
        max_w_can_discharge = int(max_wh_can_discharge / self.step_duration_h)
        actual_bat_dis_w = min(actual_bat_dis_w, max_w_can_discharge)

        # EV Limits
        ev_is_home = self.evse.state != 0
        actual_ev_chg_w = 0
        actual_ev_dis_w = 0
        
        if ev_is_home:
            actual_ev_chg_w = min(action.ev_charge_w, self.evse.max_charge_power_w)
            
            # SoC limits for EV
            max_wh_ev_charge = self.evse.vehicle_capacity_wh - self.evse.current_vehicle_soc_wh
            actual_ev_chg_w = min(actual_ev_chg_w, int(max_wh_ev_charge / self.step_duration_h))
            
            if self.evse.supports_v2x:
                actual_ev_dis_w = min(action.ev_discharge_w, self.evse.max_discharge_power_w)
                max_wh_ev_discharge = self.evse.current_vehicle_soc_wh
                actual_ev_dis_w = min(actual_ev_dis_w, int(max_wh_ev_discharge / self.step_duration_h))

        # Water Heater Limits
        actual_wh_w = self.water_heater.heating_power_w if action.water_heater_active else 0
        
        # Prevent over-heating
        wh_remaining_wh = self.water_heater.total_thermal_capacity_wh - self.water_heater.current_thermal_energy_wh
        if wh_remaining_wh <= 0:
            actual_wh_w = 0

        # --- 2. Calculate Grid Flow & Enforce Breaker ---
        # Demand = Base + EV Charge + WH + Bat Charge
        demand_w = actual_base_load_w + actual_ev_chg_w + actual_wh_w + actual_bat_chg_w
        # Supply = Solar + EV Discharge + Bat Discharge
        supply_w = actual_solar_w + actual_ev_dis_w + actual_bat_dis_w
        
        net_grid_w = demand_w - supply_w
        
        # (In a highly advanced simulation, if net_grid_w > breaker_limit_w, the main breaker trips.
        # For this experiment, we will just log a warning and allow it, so we can see if controllers fail).
        if net_grid_w > self.breaker_limit_w:
            print(f"⚠️ WARNING [t={self.current_t}]: Breaker Limit Exceeded! Draw: {net_grid_w}W, Limit: {self.breaker_limit_w}W")

        # --- 3. Update Device Physical States ---
        # Update Battery
        self.battery.current_soc_wh += int(actual_bat_chg_w * self.step_duration_h)
        self.battery.current_soc_wh -= int(actual_bat_dis_w * self.step_duration_h)
        
        # Update EV
        if ev_is_home:
            self.evse.current_vehicle_soc_wh += int(actual_ev_chg_w * self.step_duration_h)
            self.evse.current_vehicle_soc_wh -= int(actual_ev_dis_w * self.step_duration_h)
            
        # Update Water Heater
        self.water_heater.current_thermal_energy_wh += int(actual_wh_w * self.step_duration_h)
        self.water_heater.current_thermal_energy_wh = min(self.water_heater.current_thermal_energy_wh, self.water_heater.total_thermal_capacity_wh)

        # --- 4. Calculate Financial Cost ---
        imp_prices = self.tariff.get_import_prices()
        exp_prices = self.tariff.get_export_prices()
        
        # Handle interpolation if price array length doesn't match time_steps
        price_idx = self.current_t // (60 // self.step_duration_m) if len(imp_prices) < self.time_steps else self.current_t
        
        current_imp_price_c = imp_prices[price_idx]
        current_exp_price_c = exp_prices[price_idx]

        step_cost_cents = 0.0
        if net_grid_w > 0:
            # Importing
            energy_kwh = (net_grid_w / 1000.0) * self.step_duration_h
            step_cost_cents = energy_kwh * current_imp_price_c
        else:
            # Exporting
            energy_kwh = (abs(net_grid_w) / 1000.0) * self.step_duration_h
            step_cost_cents = -1 * (energy_kwh * current_exp_price_c)

        # --- 5. Log History ---
        self.history.append({
            "TimeStep": self.current_t,
            "Base Load (kW)": actual_base_load_w / 1000.0,
            "Solar PV (kW)": -actual_solar_w / 1000.0,
            "EV Charge (kW)": actual_ev_chg_w / 1000.0,
            "EV Discharge (kW)": -actual_ev_dis_w / 1000.0,
            "EV SoC (kWh)": self.evse.current_vehicle_soc_wh / 1000.0 if ev_is_home else 0,
            "Water Heater (kW)": actual_wh_w / 1000.0,
            "Bat Charge (kW)": actual_bat_chg_w / 1000.0,
            "Bat Discharge (kW)": -actual_bat_dis_w / 1000.0,
            "Battery SoC (kWh)": self.battery.current_soc_wh / 1000.0,
            "Net Grid Flow (kW)": net_grid_w / 1000.0,
            "Step Cost ($)": step_cost_cents / 100.0
        })

        # --- 6. Advance Time ---
        self.current_t += 1
        return self.get_state(), step_cost_cents / 100.0

    def get_history_df(self) -> pd.DataFrame:
        """Returns the simulation history as a DataFrame for visualization."""
        return pd.DataFrame(self.history)