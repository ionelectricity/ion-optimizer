# app_interval.py
import streamlit as st
import pandas as pd

from energy_management_structs import ChargingTargetScheduleStruct, ChargingTargetStruct
from devices.battery import BatteryStorageDevice
from devices.evse import EVSEDevice
from devices.water_heater import WaterHeaterDevice
from devices.appliance import SmartApplianceDevice
from clusters.dem import ESATypeEnum
from tariffs import ElectricalEnergyTariffDevice
from optimizer_interval import IntervalEnergyOptimizer

st.set_page_config(page_title="Matter HEMS - Appliance Scheduling", layout="wide")
st.title("⚡ Matter 1.3 - Discrete Appliance Scheduling")
st.markdown("Testing OR-Tools `IntervalVar` logic for contiguous, unbreakable appliance loads.")

# --- MOCK DATA: NOON to NOON ---
time_labels = [f"{(t + 12) % 24:02d}:00" for t in range(24)]

prices = [20, 20, 20, 20, 40, 50, 60, 50, 40, 20, 10, 10] + [10, 10, 10, 10, 10, 10, 10, 15, 20, 15, 10, 10]
base_load = [1.5, 1.5, 1.5, 2.0, 3.0, 4.5, 5.0, 4.5, 3.5, 2.5, 1.5, 1.0] + [1.0, 0.8, 0.8, 0.8, 1.2, 2.0, 3.5, 2.5, 1.5,
                                                                            1.0, 1.0, 1.0]
solar = [6.0, 5.5, 4.5, 3.0, 1.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] + [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 1.5,
                                                                        3.0, 4.5, 5.5]

# --- UI CONTROLS ---
st.sidebar.header("🍽️ Smart Dishwasher")
dw_power_kw = st.sidebar.slider("Dishwasher Power (kW)", 1.0, 3.0, 2.0, step=0.5)
dw_duration_h = st.sidebar.slider("Cycle Duration (Hours)", 1, 4, 2)

# Create a clean clock-time slider that maps back to the 0-23 array index
options = list(range(6, 24))
format_func = lambda x: time_labels[x]
dw_deadline = st.sidebar.select_slider(
    "Must Finish By:",
    options=options,
    value=20,  # Default 08:00
    format_func=format_func
)

st.sidebar.header("🚿 Water Heater")
wh_draw = st.sidebar.slider("Hot Water Needed (Liters)", 0, 200, 150)

# --- INSTANTIATE DEVICES ---
tariff = ElectricalEnergyTariffDevice("tariff_1")
tariff.load_mock_dynamic_tariff(prices, [5] * 24, 0)

battery = BatteryStorageDevice("bess_1", 10_000, 5_000, 5_000, 50)
evse = EVSEDevice("evse_1", 7_000, True, 7_000)
evse.plug_in_vehicle(70_000, int(70_000 * 0.2))
evse.set_targets([ChargingTargetScheduleStruct(0x7F, [ChargingTargetStruct(20 * 60, 80)])])

water_heater = WaterHeaterDevice("wh_1", 150, 3_000)
if wh_draw > 0:
    water_heater.simulate_water_draw(volume_liters=wh_draw)

# Instantiate the new Smart Appliance
dishwasher = SmartApplianceDevice("dw_1", ESATypeEnum.DISHWASHER, int(dw_power_kw * 1000), dw_duration_h * 60)

# Schedule using the array index from the slider
dishwasher.schedule_cycle(earliest_start_epoch_s=0, latest_end_epoch_s=dw_deadline * 60 * 60)

# --- RUN OPTIMIZATION ---
optimizer = IntervalEnergyOptimizer(time_steps=24, step_duration_m=60)
df, total_cost, status = optimizer.optimize(
    battery, evse, water_heater, [dishwasher], tariff, base_load, solar, 15.0,
    ev_arrival_step=5, ev_departure_step=20  # 5PM to 8AM
)

# --- RENDER ---
if df is not None:
    df["Time"] = time_labels
    st.success(f"✅ Schedule Optimized | Status: **{status}**")

    st.subheader("Home Power Flow (kW)")
    st.caption(
        "Notice how the solver fits the contiguous Appliance block, the pausable Water Heater, and the EV charging together!")

    chart_data = df.set_index("Time")[
        ["Base Load (kW)", "Appliances (kW)", "EV Charge (kW)", "Water Heater (kW)", "Bat Charge (kW)", "Solar PV (kW)",
         "Bat Discharge (kW)", "EV V2X Discharge (kW)"]]
    st.bar_chart(chart_data)
else:
    st.error(
        "❌ INFEASIBLE: The solver could not fit the contiguous appliance block before the deadline without breaking breaker limits.")