# simulator_runner.py
import pandas as pd

from energy_management_structs import ChargingTargetScheduleStruct, ChargingTargetStruct
from devices.battery import BatteryStorageDevice
from devices.evse import EVSEDevice
from devices.water_heater import WaterHeaterDevice
from tariffs import ElectricalEnergyTariffDevice

from environment import HomeEnvironment
from controllers import DumbController, ORToolsMPCController

# --- 1. DEFINING THE SCENARIO (24 HOURS: NOON to NOON) ---
TIME_STEPS = 24
STEP_DURATION_M = 60

# Tariffs (Cents per kWh) - Shifted so t=0 is 12:00 PM
# 12pm -> 11pm
prices_pm = [20, 20, 20, 20, 40, 50, 60, 50, 40, 20, 10, 10]
# 12am -> 11am
prices_am = [10, 10, 10, 10, 10, 10, 10, 15, 20, 15, 10, 10]

import_prices = prices_pm + prices_am
export_prices = [5] * 16 + [10, 10, 10, 10] + [5] * 4

# Base load (Watts) - Shifted to Noon
load_pm = [1500, 1500, 1500, 2000, 3000, 4500, 5000, 4500, 3500, 2500, 1500, 1000]
load_am = [1000, 800, 800, 800, 1200, 2000, 3500, 2500, 1500, 1000, 1000, 1000]
base_load_w = load_pm + load_am

# Solar PV (Watts) - Shifted to Noon
solar_pm = [6000, 5500, 4500, 3000, 1500, 500, 0, 0, 0, 0, 0, 0]
solar_am = [0, 0, 0, 0, 0, 0, 0, 500, 1500, 3000, 4500, 5500]
solar_pv_w = solar_pm + solar_am

# --- 2. HARDWARE CAPABILITIES ---
BAT_CAPACITY_WH = 10_000
BAT_MAX_W = 5_000

EV_CAPACITY_WH = 70_000
EV_MAX_W = 7_000

# Strict Chronology: Arrival (5) < Departure (20)
EV_ARRIVAL_STEP = 5  # 5 PM
EV_DEPARTURE_STEP = 20  # 8 AM next morning

EV_START_SOC_WH = int(70_000 * 0.20)  # Arrives at 20%
EV_TARGET_SOC_PCT = 80  # Needs 80% by 8 AM

WH_VOLUME_L = 150
WH_POWER_W = 3_000
WH_DRAW_L = 100  # Someone takes a shower at Noon (t=0)

BREAKER_LIMIT_KW = 15.0


def create_devices():
    """Instantiates fresh devices for a clean simulation run."""
    tariff = ElectricalEnergyTariffDevice(node_id="tariff_1")
    tariff.load_mock_dynamic_tariff(import_prices, export_prices, start_epoch_s=0)

    battery = BatteryStorageDevice(
        node_id="bess_1",
        capacity_wh=BAT_CAPACITY_WH,
        max_charge_power_w=BAT_MAX_W,
        max_discharge_power_w=BAT_MAX_W,
        initial_soc_percent=50  # Start half full
    )

    evse = EVSEDevice(
        node_id="evse_1",
        max_charge_power_w=EV_MAX_W,
        supports_v2x=True,
        max_discharge_power_w=EV_MAX_W
    )
    # Target time is minutes past midnight. t=20 is 8 AM (20 hours after noon + 12 = 32 hours -> 8 AM next day)
    ev_schedule = ChargingTargetScheduleStruct(
        day_of_week_for_sequence_bitmap=0x7F,
        charging_targets=[ChargingTargetStruct(EV_DEPARTURE_STEP * 60, target_soc_percent=EV_TARGET_SOC_PCT)]
    )
    evse.set_targets([ev_schedule])

    water_heater = WaterHeaterDevice("wh_1", WH_VOLUME_L, WH_POWER_W)
    water_heater.simulate_water_draw(volume_liters=WH_DRAW_L)

    return tariff, battery, evse, water_heater


def run_simulation(name: str, controller, env: HomeEnvironment) -> float:
    """Runs the 24-hour simulation loop and returns the total cost."""
    print(f"\n{'=' * 50}\n▶ RUNNING EXPERIMENT: {name}\n{'=' * 50}")

    total_cost_dollars = 0.0
    state = env.reset()

    for t in range(TIME_STEPS):
        # 1. Physical World Event: Does the EV plug in or unplug?
        if t == EV_ARRIVAL_STEP:
            env.evse.plug_in_vehicle(EV_CAPACITY_WH, EV_START_SOC_WH)
            state = env.get_state()  # <--- ADDED: Refresh sensor data!
            print(f"  [t={t:02d}] 🚗 EV Arrived (SoC: {EV_START_SOC_WH / 1000}kWh)")
        elif t == EV_DEPARTURE_STEP:
            final_ev_soc = env.evse.current_vehicle_soc_wh or 0
            env.evse.unplug_vehicle()
            state = env.get_state()  # <--- ADDED: Refresh sensor data!
            print(f"  [t={t:02d}] 🚗 EV Departed (SoC: {final_ev_soc / 1000}kWh)")

        actual_solar_w = solar_pv_w[t]
        actual_load_w = base_load_w[t]

        # 3. Agent Decides Action
        action = controller.act(state, forecasted_solar_w=solar_pv_w, forecasted_load_w=base_load_w)

        # 4. Environment Executes Action
        next_state, cost_dollars = env.step(action, actual_load_w, actual_solar_w)

        total_cost_dollars += cost_dollars
        state = next_state

    print(f"\n📊 FINAL RESULTS: {name}")
    print(f"Total Daily Grid Cost: ${total_cost_dollars:.2f}")

    # Save the history to CSV for charting later if desired
    df = env.get_history_df()
    df.to_csv(f"results_{name.replace(' ', '_')}.csv", index=False)

    return total_cost_dollars


def main():
    # --- EXPERIMENT 1: The "Dumb" Baseline ---
    tariff, battery, evse, water_heater = create_devices()
    env = HomeEnvironment(TIME_STEPS, STEP_DURATION_M, battery, evse, water_heater, tariff, BREAKER_LIMIT_KW)

    dumb_controller = DumbController(battery, evse, water_heater)
    cost_baseline = run_simulation("Baseline Dumb Control", dumb_controller, env)

    # --- EXPERIMENT 2: OR-Tools Perfect Foresight ---
    tariff, battery, evse, water_heater = create_devices()
    env = HomeEnvironment(TIME_STEPS, STEP_DURATION_M, battery, evse, water_heater, tariff, BREAKER_LIMIT_KW)

    smart_controller = ORToolsMPCController(
        time_steps=TIME_STEPS,
        step_duration_m=STEP_DURATION_M,
        battery=battery,
        evse=evse,
        water_heater=water_heater,
        tariff=tariff,
        breaker_limit_kw=BREAKER_LIMIT_KW,
        ev_arrival_step=EV_ARRIVAL_STEP,
        ev_departure_step=EV_DEPARTURE_STEP
    )
    cost_smart = run_simulation("OR-Tools MPC Control", smart_controller, env)

    # --- FINAL COMPARISON ---
    print(f"\n\n{'*' * 50}")
    print("🏆 FINAL A3 GRANT METRICS")
    print(f"{'*' * 50}")
    print(f"Baseline Cost: ${cost_baseline:.2f}")
    print(f"OR-Tools Cost: ${cost_smart:.2f}")

    savings = cost_baseline - cost_smart
    pct_savings = (savings / cost_baseline) * 100 if cost_baseline > 0 else 0
    print(f"\n💰 Total Savings: ${savings:.2f} ({pct_savings:.1f}%)")


if __name__ == "__main__":
    main()