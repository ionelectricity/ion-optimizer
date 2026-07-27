# app.py
import streamlit as st
import pandas as pd
import numpy as np

from energy_management_structs import ChargingTargetScheduleStruct, ChargingTargetStruct
from devices.battery import BatteryStorageDevice
from devices.evse import EVSEDevice
from devices.water_heater import WaterHeaterDevice
from devices.smart_appliances import DishwasherDevice, LaundryWasherDevice
from tariffs import ElectricalEnergyTariffDevice
from optimizer import HomeEnergyOptimizer

st.set_page_config(page_title="HEMS Dashboard v3.0", layout="wide", initial_sidebar_state="expanded")
st.title("⚡ Energy Management Dashboard (v3.0 - 15m Resolution)")

# --- SIDEBAR: DEVICE CONFIGURATIONS ---
st.sidebar.header("Configure Ecosystem")

with st.sidebar.expander("🚨 LSE / Base Load Simulator", expanded=False):
    st.markdown("Simulate a massive 'dumb' load turning on.")
    spike_kw = st.slider("Spike Magnitude (kW)", 0.0, 30.0, 0.0, step=1.0)
    spike_start = st.slider("Spike Start Hour", 0, 23, 18)
    spike_duration = st.slider("Spike Duration (Hours)", 1, 6, 2)

with st.sidebar.expander("☀️ Solar PV", expanded=False):
    solar_size_kw = st.slider("Solar Array Size (kW)", 0.0, 15.0, 5.0, step=0.5)

with st.sidebar.expander("🚗 EV & EVSE", expanded=True):
    ev_cap = st.slider("EV Battery Capacity (kWh)", 40, 100, 70)
    ev_start_soc = st.slider("Arrival SoC (%)", 0, 100, 20)
    ev_target_soc = st.slider("Target SoC (%) by Departure", 0, 100, 80)
    ev_arrival_idx = st.slider("Arrival Time", 0, 23, 17, format="%d:00")
    ev_depart_idx = st.slider("Departure Time", 0, 23, 8, format="%d:00")
    ev_max_kw = st.slider("EVSE Max Power (kW)", 3, 11, 7)
    supports_v2x = st.checkbox("Enable V2X (Bidirectional)", value=True)

with st.sidebar.expander("🔋 Home Battery", expanded=False):
    bat_capacity = st.slider("Battery Capacity (kWh)", 0, 20, 10)
    bat_max_kw = st.slider("Max Charge/Discharge (kW)", 1, 10, 5)
    bat_initial_soc = st.slider("Initial SoC (%)", 0, 100, 50)
    bat_efficiency = st.slider("Round-Trip Efficiency (%)", 50, 100, 90)
    bat_lcos = st.slider("LCOS (Degradation Cost c/kWh)", 0.0, 20.0, 4.0, step=0.5)

with st.sidebar.expander("🚿 Water Heater", expanded=False):
    wh_volume = st.slider("Tank Volume (Liters)", 50, 300, 150)
    wh_kw = st.slider("Heating Element (kW)", 2, 6, 3)
    wh_draw = st.slider("Hot Water Needed (Liters)", 0, 200, 100)

with st.sidebar.expander("🧺 Smart Appliances", expanded=True):
    run_dishwasher = st.checkbox("Run Dishwasher (Eco Wash)", value=True)
    dw_deadline = st.slider("Dishwasher Deadline", 0, 23, 7, format="%d:00")
    
    run_laundry = st.checkbox("Run Laundry (Cotton 60)", value=True)
    laundry_deadline = st.slider("Laundry Deadline", 0, 23, 18, format="%d:00")

st.sidebar.subheader("🏠 Grid Limits")
max_breaker_kw = st.sidebar.slider("Main Breaker Limit (kW)", 10, 40, 15)

# --- MOCK DATA ---
import_prices = [10, 10, 10, 10, 10, 10, 15, 15, 20, 20, 20, 20, 20, 20, 20, 20, 40, 50, 60, 50, 40, 20, 10, 10]
export_prices = [5]*16 + [10, 10, 10, 10] + [5]*4
base_solar_curve = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.3, 0.6, 0.8, 0.9, 1.0, 1.0, 0.9, 0.8, 0.6, 0.3, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
base_load_kw = [1.0, 0.8, 0.8, 0.8, 1.2, 2.0, 3.5, 2.5, 1.5, 1.0, 1.0, 1.0, 1.5, 1.5, 1.5, 2.0, 3.0, 4.5, 5.0, 4.5, 3.5, 2.5, 1.5, 1.0]

solar_pv_kw = [s * solar_size_kw for s in base_solar_curve]

# Inject the LSE spike
for t in range(spike_start, min(24, spike_start + spike_duration)):
    base_load_kw[t] += spike_kw

# We must interpolate the 24-hour data into 96 15-minute steps for the optimizer
def interpolate_to_15m(data_list):
    return list(np.repeat(data_list, 4))

base_load_15m = interpolate_to_15m(base_load_kw)
solar_pv_15m = interpolate_to_15m(solar_pv_kw)
import_prices_15m = interpolate_to_15m(import_prices)
export_prices_15m = interpolate_to_15m(export_prices)

# --- INSTANTIATE SMART ENERGY DEVICES ---
tariff = ElectricalEnergyTariffDevice(node_id="tariff_1")
# We load the original 24h prices into the tariff, the optimizer handles the internal resolution extraction
tariff.load_mock_dynamic_tariff(import_prices, export_prices, start_epoch_s=0)

battery = BatteryStorageDevice(
    node_id="bess_1",
    capacity_wh=bat_capacity * 1000,
    max_charge_power_w=bat_max_kw * 1000,
    max_discharge_power_w=bat_max_kw * 1000,
    initial_soc_percent=bat_initial_soc,
    efficiency_pct=bat_efficiency,
    lcos_cents_per_kwh=bat_lcos
)

evse = EVSEDevice("evse_1", ev_max_kw * 1000, supports_v2x, ev_max_kw * 1000 if supports_v2x else 0)
evse.plug_in_vehicle(capacity_wh=ev_cap * 1000, current_soc_wh=int(ev_cap * 1000 * (ev_start_soc/100.0)))
# 15m resolution index mapping
ev_arrival_15m = ev_arrival_idx * 4
ev_depart_15m = ev_depart_idx * 4
evse.set_targets([ChargingTargetScheduleStruct(0x7F, [ChargingTargetStruct(ev_depart_idx * 60, ev_target_soc)])])

water_heater = WaterHeaterDevice("wh_1", wh_volume, wh_kw * 1000)
if wh_draw > 0:
    water_heater.simulate_water_draw(volume_liters=wh_draw)

smart_appliances = []
if run_dishwasher:
    dw = DishwasherDevice("dishwasher_1")
    dw.select_eco_wash(latest_end_time_epoch_s=dw_deadline * 3600)
    smart_appliances.append(dw)

if run_laundry:
    lw = LaundryWasherDevice("laundry_1")
    lw.select_cotton_60(latest_end_time_epoch_s=laundry_deadline * 3600)
    smart_appliances.append(lw)

# --- RUN OPTIMIZATION ---
optimizer = HomeEnergyOptimizer(time_steps=96, step_duration_m=15)
df, total_cost, status, decision_log = optimizer.optimize(
    battery, evse, water_heater, smart_appliances, tariff, 
    base_load_15m, solar_pv_15m, max_breaker_kw,
    ev_arrival_step=ev_arrival_15m, ev_departure_step=ev_depart_15m
)

# --- RENDER UI TABS ---
if df is not None:
    st.success(f"✅ Schedule Optimized (15m Resolution) | Solver Status: **{status}** | Total Daily Grid Cost: **${total_cost:.2f}**")
    
    clipping_occurred = any(df["Original Base Load (kW)"] > df["Sanitized Base Load (kW)"])
    if clipping_occurred:
        st.warning("⚠️ **LSE Shedding Active:** The forecasted base load exceeded the physical main breaker limit. The Edge LSE hardware will shed legacy loads. The Cloud Optimizer has adjusted the available capacity accordingly.")

    tab1, tab2, tab3 = st.tabs(["🏠 Whole Home View", "🚗 Mobility & Storage", "🧺 Smart Appliances & Thermal"])
    
    with tab1:
        st.subheader("Net Power Flow at Grid Connection (kW)")
        st.caption("Bars show device loads. The RED LINE shows the Net Grid Flow (Import is positive, Export is negative).")
        
        st.line_chart(df.set_index("TimeStep")["Net Grid Flow (kW)"], color="#ff0000")
        
        st.write("Device Breakdown:")
        columns_to_chart = ["Sanitized Base Load (kW)", "EV Charge (kW)", "Water Heater (kW)", "Bat Charge (kW)", "Solar PV (kW)", "Bat Discharge (kW)", "EV V2X Discharge (kW)"]
        for app in smart_appliances:
            columns_to_chart.append(f"{app.node_id} (kW)")
            
        chart_data = df.set_index("TimeStep")[columns_to_chart]
        st.bar_chart(chart_data)

    with tab2:
        colA, colB = st.columns(2)
        with colA:
            st.subheader("EV Power Flow (kW)")
            st.bar_chart(df.set_index("TimeStep")[["EV Charge (kW)", "EV V2X Discharge (kW)"]], color=["#2e7bcf", "#ff4b4b"])
        with colB:
            st.subheader("EV State of Charge (kWh)")
            st.line_chart(df.set_index("TimeStep")["EV SoC (kWh)"], color="#2e7bcf")
            
        st.subheader("Battery Operations")
        colC, colD = st.columns(2)
        with colC:
            st.bar_chart(df.set_index("TimeStep")[["Bat Charge (kW)", "Bat Discharge (kW)"]], color=["#2e7bcf", "#ff4b4b"])
        with colD:
            st.line_chart(df.set_index("TimeStep")["Battery SoC (kWh)"], color="#2e7bcf")

        st.markdown(decision_log)

    with tab3:
        st.subheader("Smart Appliance Job Shop Schedule (kW)")
        st.caption("Notice how the multi-slot appliances run contiguously and finish exactly before their deadlines!")
        app_cols = [f"{app.node_id} (kW)" for app in smart_appliances]
        if app_cols:
            st.bar_chart(df.set_index("TimeStep")[app_cols])
        else:
            st.info("No Smart Appliances enabled.")
            
        st.subheader("Water Heater (kW)")
        st.bar_chart(df.set_index("TimeStep")["Water Heater (kW)"], color="#ffaa00")

else:
    st.error(f"❌ Solver Status: **{status} (INFEASIBLE)**")
    st.markdown("""
    **The requested targets are physically impossible.**
    
    The Cloud Optimizer has correctly aborted rather than generating a dangerous schedule.
    *   **Reason:** A Deadline cannot be met without exceeding the Main Breaker limit.
    *   **User Action:** Adjust the Departure Time, Appliance Deadlines, or raise the Breaker Limit.
    """)