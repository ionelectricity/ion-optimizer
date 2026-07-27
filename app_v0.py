import streamlit as st
import pandas as pd
from ortools.sat.python import cp_model

st.set_page_config(page_title="HEMS Simulator", layout="wide")
st.title("⚡ Home Energy Optimization Simulator")

# --- SIDEBAR: USER INPUTS ---
st.sidebar.header("Constraint Inputs")

st.sidebar.subheader("🚗 EV Constraints")
ev_needs_kwh = st.sidebar.slider("EV Energy Needed (kWh)", 0, 50, 20)
ev_max_kw = st.sidebar.slider("EV Max Charge Rate (kW)", 3, 11, 7)
ev_departure = st.sidebar.slider("Departure Time (Hour)", 6, 12, 8)

st.sidebar.subheader("🔋 Battery Constraints")
bat_capacity = st.sidebar.slider("Battery Capacity (kWh)", 0, 20, 10)
bat_max_kw = st.sidebar.slider("Battery Max Power (kW)", 1, 10, 5)

st.sidebar.subheader("🏠 Grid Constraints")
max_breaker_kw = st.sidebar.slider("Main Breaker Limit (kW)", 10, 40, 20)

# --- MOCK DATA ---
HOURS = 24
prices = [10, 10, 10, 10, 10, 10, 15, 15, 20, 20, 20, 20,
          20, 20, 20, 20, 40, 50, 60, 50, 40, 20, 10, 10]
base_load_kw = [1, 1, 1, 1, 2, 2, 4, 3, 2, 2, 2, 2,
                2, 2, 2, 3, 5, 6, 7, 6, 5, 3, 2, 1]


# --- OPTIMIZATION ENGINE ---
def solve_hems(ev_needs, ev_max, ev_dep, bat_cap, bat_max, breaker):
    model = cp_model.CpModel()

    # Variables
    ev_kw = [model.NewIntVar(0, ev_max, f'ev_kw_{t}') for t in range(HOURS)]
    bat_charge = [model.NewIntVar(0, bat_max, f'bat_chg_{t}') for t in range(HOURS)]
    bat_discharge = [model.NewIntVar(0, bat_max, f'bat_dis_{t}') for t in range(HOURS)]
    bat_soc = [model.NewIntVar(0, bat_cap, f'bat_soc_{t}') for t in range(HOURS)]
    grid_power = [model.NewIntVar(-100, 100, f'grid_{t}') for t in range(HOURS)]

    # 1. EV Charging
    for t in range(ev_dep, HOURS):
        model.Add(ev_kw[t] == 0)
    model.Add(sum(ev_kw) == ev_needs)

    # 2. Battery Physics
    for t in range(HOURS):
        if t == 0:
            model.Add(bat_soc[t] == 0 + bat_charge[t] - bat_discharge[t])
        else:
            model.Add(bat_soc[t] == bat_soc[t - 1] + bat_charge[t] - bat_discharge[t])
        if t == HOURS - 1:
            model.Add(bat_soc[t] == 0)  # Empty at end of day

    # 3. Grid Balance & Breaker Limit
    for t in range(HOURS):
        model.Add(grid_power[t] == base_load_kw[t] + ev_kw[t] + bat_charge[t] - bat_discharge[t])
        model.Add(grid_power[t] <= breaker)  # Max pull from grid
        model.Add(grid_power[t] >= -breaker)  # Max push to grid

    # 4. Objective
    grid_cost = sum(grid_power[t] * prices[t] for t in range(HOURS))
    battery_wear_penalty = sum(bat_charge[t] + bat_discharge[t] for t in range(HOURS))
    model.Minimize((grid_cost * 10) + battery_wear_penalty)

    # Solve
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1  # Mac threading fix
    solver.parameters.max_time_in_seconds = 2.0

    status = solver.Solve(model)

    if status in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
        true_cost_cents = sum(solver.Value(grid_power[t]) * prices[t] for t in range(HOURS))
        results = {
            "Hour": [str(t) for t in range(HOURS)],  # Changed to string to fix chart rendering
            "Base Load (kW)": base_load_kw,
            "EV Charge (kW)": [solver.Value(ev_kw[t]) for t in range(HOURS)],
            "Battery Charge (kW)": [solver.Value(bat_charge[t]) for t in range(HOURS)],
            "Battery Discharge (kW)": [-solver.Value(bat_discharge[t]) for t in range(HOURS)],
            "Grid Power (kW)": [solver.Value(grid_power[t]) for t in range(HOURS)],
            "Battery SoC (kWh)": [solver.Value(bat_soc[t]) for t in range(HOURS)]
        }
        return pd.DataFrame(results), true_cost_cents / 100, solver.StatusName(status)
    else:
        return None, None, solver.StatusName(status)


# --- RENDER UI ---
df, total_cost, solver_status = solve_hems(ev_needs_kwh, ev_max_kw, ev_departure, bat_capacity, bat_max_kw,
                                           max_breaker_kw)

col1, col2 = st.columns([2, 1])

with col1:
    if df is not None:
        st.success(f"✅ Solver Status: **{solver_status}** | Total Daily Energy Cost: **${total_cost:.2f}**")

        st.subheader("Power Flow Schedule (kW)")
        st.caption("Bars above 0 = Pulling from Grid. Bars below 0 = Discharging Battery.")

        # Crash-proof charting syntax
        st.bar_chart(df, x="Hour",
                     y=["Base Load (kW)", "EV Charge (kW)", "Battery Charge (kW)", "Battery Discharge (kW)"])

        st.subheader("Battery State of Charge (kWh)")
        st.line_chart(df, x="Hour", y="Battery SoC (kWh)")
    else:
        st.error(f"❌ Solver Status: **{solver_status} (INFEASIBLE)**")
        st.warning("The solver mathematically proved your constraints are impossible. Adjust the sliders on the left.")

with col2:
    st.subheader("Electricity Prices (c/kWh)")
    price_df = pd.DataFrame({"Hour": [str(t) for t in range(HOURS)], "Price (cents)": prices})
    st.line_chart(price_df, x="Hour", y="Price (cents)", color="#ffaa00")

    if df is not None:
        st.write("### Raw Schedule Array")
        st.dataframe(df, height=350)