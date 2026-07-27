# devices/battery.py
from typing import List
from energy_management_structs import PowerAdjustStruct
from clusters.dem import DeviceEnergyManagementCluster, ESATypeEnum, DEMFeatureMap

class BatteryStorageDevice:
    """
    Implements the Battery Storage Device Type (Device Type ID 0x0018).
    Ref: Section 14.4 of the Energy Management Device Library Specification.
    """
    
    def __init__(
        self,
        node_id: str,
        capacity_wh: int,
        max_charge_power_w: int,
        max_discharge_power_w: int,
        initial_soc_percent: int = 0,
        efficiency_pct: int = 95, # 95% charge, 95% discharge (approx 90% round-trip)
        lcos_cents_per_kwh: float = 4.0 # Hardware wear-and-tear cost per kWh cycled
    ):
        self.node_id = node_id
        
        # --- Internal Physical State ---
        self.capacity_wh = capacity_wh
        self.max_charge_power_w = max_charge_power_w
        self.max_discharge_power_w = max_discharge_power_w
        self.current_soc_wh = int(capacity_wh * (initial_soc_percent / 100.0))
        
        # --- Thermodynamics and Economics ---
        self.efficiency_pct = efficiency_pct
        self.lcos_cents_per_kwh = lcos_cents_per_kwh

        # --- Endpoint Composition: Clusters ---
        abs_min_power_mw = -1 * (max_discharge_power_w * 1000)
        abs_max_power_mw = (max_charge_power_w * 1000)
        
        self.dem_cluster = DeviceEnergyManagementCluster(
            esa_type=ESATypeEnum.BATTERY_STORAGE,
            esa_can_generate=True, 
            abs_min_power_mw=abs_min_power_mw,
            abs_max_power_mw=abs_max_power_mw,
            feature_map=DEMFeatureMap.POWER_ADJUSTMENT 
        )

        self.bat_percent_remaining: int = initial_soc_percent
        self.bat_capacity_mwh: int = capacity_wh * 1000
        self.active_grid_power_mw: int = 0
        
        self.update_dem_capabilities()

    def update_dem_capabilities(self):
        """
        Calculates and advertises the current power adjustment capabilities.
        Ref: Section 9.2.7.12 (PowerAdjustCapability)
        """
        capabilities: List[PowerAdjustStruct] = []
        
        if self.current_soc_wh < self.capacity_wh:
            remaining_capacity_wh = self.capacity_wh - self.current_soc_wh
            max_duration_charging_s = int((remaining_capacity_wh / self.max_charge_power_w) * 3600)
            
            capabilities.append(
                PowerAdjustStruct(
                    min_power_mw=0,
                    max_power_mw=self.max_charge_power_w * 1000,
                    min_duration_s=60, 
                    max_duration_s=max_duration_charging_s
                )
            )

        if self.current_soc_wh > 0:
            max_duration_discharging_s = int((self.current_soc_wh / self.max_discharge_power_w) * 3600)
            
            capabilities.append(
                PowerAdjustStruct(
                    min_power_mw=-1 * (self.max_discharge_power_w * 1000),
                    max_power_mw=0,
                    min_duration_s=60,
                    max_duration_s=max_duration_discharging_s
                )
            )

        self.dem_cluster.set_power_adjustment_capability(capabilities)

    def apply_power_adjustment(self, power_w: int, duration_s: int):
        accepted = self.dem_cluster.handle_power_adjust_request(
            power_mw=power_w * 1000, 
            duration_s=duration_s, 
            cause=1 # Grid Optimization
        )
        if not accepted:
            raise ValueError(f"Battery rejected PowerAdjustRequest")

        energy_transferred_wh = power_w * (duration_s / 3600.0)
        self.current_soc_wh += energy_transferred_wh
        self.current_soc_wh = max(0, min(self.capacity_wh, self.current_soc_wh))
        
        self.bat_percent_remaining = int((self.current_soc_wh / self.capacity_wh) * 100)
        self.update_dem_capabilities()
        self.dem_cluster.handle_cancel_power_adjust_request()