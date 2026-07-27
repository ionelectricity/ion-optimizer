import os
import pandas as pd
import matplotlib.pyplot as plt

# -------------------------------------------------------
# Configuration
# -------------------------------------------------------
BASELINE_CSV = "results_Baseline_Dumb_Control.csv"
SMART_CSV = "results_OR-Tools_MPC_Control.csv"
MC_CSV = "monte_carlo_a3_results.csv"

OUTPUT_DIR = "figures"
os.makedirs(OUTPUT_DIR, exist_ok=True)

plt.rcParams.update({
    "figure.figsize": (12, 6),
    "font.size": 11,
    "axes.grid": True,
    "savefig.dpi": 300,
})

# -------------------------------------------------------
# Load data
# -------------------------------------------------------
baseline = pd.read_csv(BASELINE_CSV)
smart = pd.read_csv(SMART_CSV)
mc = pd.read_csv(MC_CSV)

time = baseline["TimeStep"]

# ============================================================
# Figure 1 - Power Dispatch
# ============================================================

fig, axs = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

signals = [
    "Base Load (kW)",
    "Solar PV (kW)",
    "EV Charge (kW)",
    "Bat Charge (kW)",
    "Bat Discharge (kW)",
    "Net Grid Flow (kW)",
]

for s in signals:
    if s in baseline.columns:
        axs[0].plot(time, baseline[s], lw=2, label=s)

axs[0].set_title("Baseline Controller")
axs[0].set_ylabel("Power (kW)")
axs[0].legend(ncol=2)

for s in signals:
    if s in smart.columns:
        axs[1].plot(time, smart[s], lw=2, label=s)

axs[1].set_title("OR-Tools MPC Controller")
axs[1].set_ylabel("Power (kW)")
axs[1].set_xlabel("Time")
axs[1].legend(ncol=2)

plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "figure1_power_dispatch.png"))
plt.savefig(os.path.join(OUTPUT_DIR, "figure1_power_dispatch.pdf"))
plt.close(fig)

# ============================================================
# Figure 2 - Battery and EV SoC
# ============================================================

fig, axs = plt.subplots(2, 1, figsize=(14, 7), sharex=True)

axs[0].plot(
    time,
    baseline["Battery SoC (kWh)"],
    lw=2,
    label="Baseline"
)

axs[0].plot(
    time,
    smart["Battery SoC (kWh)"],
    lw=2,
    label="OR-Tools"
)

axs[0].set_ylabel("Battery SoC (kWh)")
axs[0].set_title("Battery State of Charge")
axs[0].legend()

axs[1].plot(
    time,
    baseline["EV SoC (kWh)"],
    lw=2,
    label="Baseline"
)

axs[1].plot(
    time,
    smart["EV SoC (kWh)"],
    lw=2,
    label="OR-Tools"
)

axs[1].set_ylabel("EV SoC (kWh)")
axs[1].set_xlabel("Time")
axs[1].set_title("Electric Vehicle State of Charge")
axs[1].legend()

plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "figure2_soc.png"))
plt.savefig(os.path.join(OUTPUT_DIR, "figure2_soc.pdf"))
plt.close(fig)

# ============================================================
# Figure 3 - Monte Carlo Results
# ============================================================

fig, ax = plt.subplots(figsize=(14, 6))

ax.plot(
    mc["Day"],
    mc["Baseline Cost ($)"],
    lw=2,
    label="Baseline"
)

ax.plot(
    mc["Day"],
    mc["Smart Cost ($)"],
    lw=2,
    label="OR-Tools MPC"
)

ax.bar(
    mc["Day"],
    mc["Savings ($)"],
    alpha=0.30,
    label="Savings"
)

ax.set_xlabel("Simulation Day")
ax.set_ylabel("Cost ($)")
ax.set_title("100-Day Monte Carlo Simulation")
ax.legend()

plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "figure3_monte_carlo.png"))
plt.savefig(os.path.join(OUTPUT_DIR, "figure3_monte_carlo.pdf"))
plt.close(fig)

print(f"Figures saved to '{OUTPUT_DIR}/'")