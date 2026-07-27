# monte_carlo_runner.py
# monte_carlo_runner.py
import random
import pandas as pd
import numpy as np
import time

from energy_management_structs import ChargingTargetScheduleStruct, ChargingTargetStruct
from devices.battery import BatteryStorageDevice
from devices.evse import EVSEDevice
from devices.water_heater import WaterHeaterDevice
from tariffs import ElectricalEnergyTariffDevice

from environment import HomeEnvironment
from controllers import DumbController, ORToolsMPCController

# --- 1. BASELINE SCENARIO (NOON to NOON) ---
TIME_STEPS = 24
STEP_DURATION_M = 60

# Tariffs (12pm to 11am)
prices_pm = [20, 20, 20, 20, 40, 50, 60, 50, 40, 20, 10, 10]
prices_am = [10, 10, 10, 10, 10, 10, 10, 15, 20, 15, 10, 10]
import_prices = prices_pm + prices_am
export_prices = [5] * 16 + [10, 10, 10, 10] + [5] * 4

# Base load
load_pm = [1500, 1500, 1500, 2000, 3000, 4500, 5000, 4500, 3500, 2500, 1500, 1000]
load_am = [1000, 800, 800, 800, 1200, 2000, 3500, 2500, 1500, 1000, 1000, 1000]
base_load_w = load_pm + load_am

# Base Solar (Perfect Sunny Day)
solar_pm = [6000, 5500, 4500, 3000, 1500, 500, 0, 0, 0, 0, 0, 0]
solar_am = [0, 0, 0, 0, 0, 0, 0, 500, 1500, 3000, 4500, 5500]
base_solar_pv_w = solar_pm + solar_am

# Hardware
BAT_CAPACITY_WH = 10_000
BAT_MAX_W = 5_000
EV_CAPACITY_WH = 70_000
EV_MAX_W = 7_000
EV_TARGET_SOC_PCT = 80
WH_VOLUME_L = 150
WH_POWER_W = 3_000
BREAKER_LIMIT_KW = 15.0


# --- 2. STOCHASTIC GENERATORS (The "Noise") ---
def generate_stochastic_day():
    """Generates a random day of weather and user behavior."""
    # 1. EV Arrival: Normally distributed around 17:00 (t=5), std dev = 1.5 hours
    ev_arrival = int(random.gauss(5, 1.5))
    ev_arrival = max(1, min(ev_arrival, 10))  # Clamp between 1pm and 10pm

    # 2. EV Departure: Normally distributed around 08:00 (t=20), std dev = 0.5 hours
    ev_departure = int(random.gauss(20, 0.5))
    ev_departure = max(16, min(ev_departure, 23))  # Clamp between 4am and 11am

    # 3. EV Arrival SoC: Uniformly distributed between 10% and 40%
    ev_start_soc_pct = random.randint(10, 40)
    ev_start_soc_wh = int(EV_CAPACITY_WH * (ev_start_soc_pct / 100.0))

    # 4. Solar Noise: Simulate passing clouds (multiply base curve by 0.4 to 1.0)
    noisy_solar_pv_w = []
    for s in base_solar_pv_w:
        if s > 0:
            cloud_factor = random.uniform(0.4, 1.0)
            noisy_solar_pv_w.append(int(s * cloud_factor))
        else:
            noisy_solar_pv_w.append(0)

    # 5. Base Load Noise: Add +/- 20% random noise
    noisy_base_load_w = [int(l * random.uniform(0.8, 1.2)) for l in base_load_w]

    return ev_arrival, ev_departure, ev_start_soc_wh, noisy_solar_pv_w, noisy_base_load_w


def create_devices(ev_departure_step: int):
    """Instantiates fresh devices for a clean simulation run."""
    tariff = ElectricalEnergyTariffDevice(node_id="tariff_1")
    tariff.load_mock_dynamic_tariff(import_prices, export_prices, start_epoch_s=0)

    battery = BatteryStorageDevice(
        "bess_1", BAT_CAPACITY_WH, BAT_MAX_W, BAT_MAX_W, initial_soc_percent=50
    )

    evse = EVSEDevice("evse_1", EV_MAX_W, supports_v2x=True, max_discharge_power_w=EV_MAX_W)
    ev_schedule = ChargingTargetScheduleStruct(
        day_of_week_for_sequence_bitmap=0x7F,
        charging_targets=[ChargingTargetStruct(ev_departure_step * 60, target_soc_percent=EV_TARGET_SOC_PCT)]
    )
    evse.set_targets([ev_schedule])

    water_heater = WaterHeaterDevice("wh_1", WH_VOLUME_L, WH_POWER_W)
    water_heater.simulate_water_draw(volume_liters=100)  # Morning shower

    return tariff, battery, evse, water_heater


# --- 3. THE SIMULATION RUNNER ---
def run_simulation(name: str, controller, env: HomeEnvironment, arr_step, dep_step, arr_soc, actual_solar,
                   actual_load) -> float:
    """Runs a single 24-hour simulation loop."""
    total_cost_dollars = 0.0
    state = env.reset()

    for t in range(TIME_STEPS):
        # Physical Events
        if t == arr_step:
            env.evse.plug_in_vehicle(EV_CAPACITY_WH, arr_soc)
            state = env.get_state()
        elif t == dep_step:
            env.evse.unplug_vehicle()
            state = env.get_state()

        solar = actual_solar[t]
        load = actual_load[t]

        # MPC Loop (The Controller uses base_solar_pv_w as its "Forecast", but the env experiences actual_solar)
        # Note: In a true production MPC, we would use a rolling forecast model. Here, we just pass the 24h baseline.
        action = controller.act(state, forecasted_solar_w=base_solar_pv_w, forecasted_load_w=base_load_w)
        next_state, cost_dollars = env.step(action, load, solar)

        total_cost_dollars += cost_dollars
        state = next_state

    return total_cost_dollars


# --- 4. THE MONTE CARLO ORCHESTRATOR ---
def main():
    NUM_DAYS = 100
    print(f"🚀 Starting Monte Carlo Simulation ({NUM_DAYS} Days)")
    print("Injecting stochastic noise into Solar PV, Base Load, and EV Driver Behavior...")

    results = []
    start_time = time.time()

    for day in range(NUM_DAYS):
        # 1. Generate the random "reality" for this day
        arr, dep, soc, actual_solar, actual_load = generate_stochastic_day()

        # 2. Run Baseline (Dumb Controller)
        t_b, b_b, e_b, wh_b = create_devices(dep)
        env_b = HomeEnvironment(TIME_STEPS, STEP_DURATION_M, b_b, e_b, wh_b, t_b, BREAKER_LIMIT_KW)
        dumb_ctrl = DumbController(b_b, e_b, wh_b)
        cost_baseline = run_simulation("Dumb", dumb_ctrl, env_b, arr, dep, soc, actual_solar, actual_load)

        # 3. Run Smart (OR-Tools MPC)
        t_s, b_s, e_s, wh_s = create_devices(dep)
        env_s = HomeEnvironment(TIME_STEPS, STEP_DURATION_M, b_s, e_s, wh_s, t_s, BREAKER_LIMIT_KW)
        smart_ctrl = ORToolsMPCController(
            time_steps=TIME_STEPS, step_duration_m=STEP_DURATION_M,
            battery=b_s, evse=e_s, water_heater=wh_s, tariff=t_s,
            breaker_limit_kw=BREAKER_LIMIT_KW,
            ev_arrival_step=arr, ev_departure_step=dep
        )

        # Suppress the "No Feasible Solution" prints from spamming the console during Monte Carlo
        import sys, os
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')

        cost_smart = run_simulation("Smart", smart_ctrl, env_s, arr, dep, soc, actual_solar, actual_load)

        sys.stdout = old_stdout  # Restore prints

        # 4. Log Results
        savings = cost_baseline - cost_smart
        results.append({
            "Day": day + 1,
            "Baseline Cost ($)": cost_baseline,
            "Smart Cost ($)": cost_smart,
            "Savings ($)": savings
        })

        if (day + 1) % 10 == 0:
            print(f"  ... Completed Day {day + 1}/{NUM_DAYS}")

    # --- 5. ANALYZE STATISTICS ---
    df_results = pd.DataFrame(results)
    avg_baseline = df_results["Baseline Cost ($)"].mean()
    avg_smart = df_results["Smart Cost ($)"].mean()
    avg_savings = df_results["Savings ($)"].mean()
    pct_savings = (avg_savings / avg_baseline) * 100

    print("\n" + "=" * 50)
    print("📊 FINAL MONTE CARLO A3 METRICS (100 Days)")
    print("=" * 50)
    print(f"Simulation Time: {time.time() - start_time:.1f} seconds")
    print(f"Average Daily Baseline Cost: ${avg_baseline:.2f}")
    print(f"Average Daily OR-Tools Cost: ${avg_smart:.2f}")
    print(f"Average Daily Savings:       ${avg_savings:.2f} ({pct_savings:.1f}%)")
    print(f"Max Single-Day Savings:      ${df_results['Savings ($)'].max():.2f}")
    print(f"Min Single-Day Savings:      ${df_results['Savings ($)'].min():.2f}")

    # Save for grant reporting
    df_results.to_csv("monte_carlo_a3_results.csv", index=False)
    print("\n💾 Full results saved to 'monte_carlo_a3_results.csv'")


if __name__ == "__main__":
    main()