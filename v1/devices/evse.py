# devices/evse.py
from typing import List, Optional, Tuple
from energy_management_structs import (
    PowerAdjustStruct,
    ForecastStruct,
    SlotStruct,
    ChargingTargetScheduleStruct,
    ChargingTargetStruct,
    ForecastUpdateReasonEnum
)
from clusters.dem import DeviceEnergyManagementCluster, ESATypeEnum, DEMFeatureMap

class StateEnum:
    """Mapped from Section 9.3.7.2"""
    NOT_PLUGGED_IN = 0
    PLUGGED_IN_NO_DEMAND = 1
    PLUGGED_IN_DEMAND = 2
    PLUGGED_IN_CHARGING = 3
    PLUGGED_IN_DISCHARGING = 4
    SESSION_ENDING = 5
    FAULT = 6

class EVSEDevice:
    """
    Implements the EVSE Device Type (Device Type ID 0x050C) and 
    the Energy EVSE Cluster (Cluster ID 0x0099).
    Ref: Section 14.1 and Section 9.3 of the our Proprietary Specification.
    """
    
    def __init__(
        self,
        node_id: str,
        # Physical constraints for the OR-Tools optimizer
        max_charge_power_w: int,
        supports_v2x: bool = False,
        max_discharge_power_w: int = 0
    ):
        self.node_id = node_id
        
        # --- Internal Physical State ---
        self.max_charge_power_w = max_charge_power_w
        self.supports_v2x = supports_v2x
        self.max_discharge_power_w = max_discharge_power_w
        
        # --- Energy EVSE Cluster State (Section 9.3.8) ---
        self.state: int = StateEnum.NOT_PLUGGED_IN
        self.charging_target_schedules: List[ChargingTargetScheduleStruct] = []
        
        # If we know the vehicle's state (SOC feature)
        self.vehicle_capacity_wh: Optional[int] = None
        self.current_vehicle_soc_wh: Optional[int] = None
        
        # --- Endpoint Composition: DEM Cluster ---
        # Section 14.1.6.2: "If the EVSE supports the V2X feature then the Device Energy 
        # Management cluster ... SHALL support the PowerAdjustment (PA) feature."
        
        feature_map = DEMFeatureMap.POWER_FORECAST_REPORTING # EVSEs must provide forecasts
        if self.supports_v2x:
            feature_map |= DEMFeatureMap.POWER_ADJUSTMENT
            
        abs_min_power_mw = -1 * (max_discharge_power_w * 1000) if self.supports_v2x else 0
        abs_max_power_mw = (max_charge_power_w * 1000)
        
        self.dem_cluster = DeviceEnergyManagementCluster(
            esa_type=ESATypeEnum.EVSE,
            esa_can_generate=self.supports_v2x, 
            abs_min_power_mw=abs_min_power_mw,
            abs_max_power_mw=abs_max_power_mw,
            feature_map=feature_map
        )

    # --- SIMULATION METHODS (Physical World Events) ---

    def plug_in_vehicle(self, capacity_wh: int, current_soc_wh: int):
        """Simulates an EV being plugged in."""
        self.state = StateEnum.PLUGGED_IN_NO_DEMAND
        self.vehicle_capacity_wh = capacity_wh
        self.current_vehicle_soc_wh = current_soc_wh
        self._recalculate_forecast_and_capabilities()

    def unplug_vehicle(self):
        """Simulates an EV being unplugged."""
        self.state = StateEnum.NOT_PLUGGED_IN
        self.vehicle_capacity_wh = None
        self.current_vehicle_soc_wh = None
        self.dem_cluster.forecast = None # Clear forecast
        if self.supports_v2x:
            self.dem_cluster.set_power_adjustment_capability([])

    # --- ENERGY EVSE CLUSTER COMMANDS (Section 9.3.9) ---

    def set_targets(self, targets: List[ChargingTargetScheduleStruct]):
        """
        Allows a user/app to set charging targets (Section 9.3.9.5).
        Once set, the EVSE must automatically compute a charging schedule and 
        expose it via the DEM Forecast attribute.
        """
        if len(targets) > 7:
            raise ValueError("Maximum 7 ChargingTargetSchedules allowed.")
        self.charging_target_schedules = targets
        self._recalculate_forecast_and_capabilities()

    # --- INTERNAL LOGIC: Bridging EVSE Intent to DEM Math ---

    def _recalculate_forecast_and_capabilities(self):
        """
        Translates the user's `ChargingTargetStruct` into a mathematical `ForecastStruct`
        for the DEM cluster, and updates V2X PowerAdjust capabilities.
        """
        if self.state == StateEnum.NOT_PLUGGED_IN:
            return

        # 1. Update V2X Power Adjustment Capabilities (if supported)
        if self.supports_v2x:
            capabilities: List[PowerAdjustStruct] = []
            
            # Can we charge?
            if self.current_vehicle_soc_wh < self.vehicle_capacity_wh:
                rem_wh = self.vehicle_capacity_wh - self.current_vehicle_soc_wh
                max_dur = int((rem_wh / self.max_charge_power_w) * 3600)
                capabilities.append(
                    PowerAdjustStruct(
                        min_power_mw=0,
                        max_power_mw=self.max_charge_power_w * 1000,
                        min_duration_s=60,
                        max_duration_s=max_dur
                    )
                )
            
            # Can we discharge to the home (V2X)?
            if self.current_vehicle_soc_wh > 0:
                max_dur = int((self.current_vehicle_soc_wh / self.max_discharge_power_w) * 3600)
                capabilities.append(
                    PowerAdjustStruct(
                        min_power_mw=-1 * (self.max_discharge_power_w * 1000),
                        max_power_mw=0,
                        min_duration_s=60,
                        max_duration_s=max_dur
                    )
                )
            self.dem_cluster.set_power_adjustment_capability(capabilities)

        # 2. Generate the Baseline Charging Forecast
        # If the user has set a target (e.g. "Add 30kWh by 8AM"), the EVSE generates
        # a basic "dumb" schedule assuming it charges right before departure. 
        # The EMS (OR-Tools) will later read this forecast and use the FA (ForecastAdjustment)
        # feature to shift it if electricity is cheaper elsewhere.
        
        if not self.charging_target_schedules:
            return # No targets set, no forecast to generate

        # Simplify for simulator: grab the first target of the first schedule
        target: ChargingTargetStruct = self.charging_target_schedules[0].charging_targets[0]
        
        # Calculate how much energy we need
        needed_wh = 0
        if target.target_soc_percent is not None and self.vehicle_capacity_wh is not None:
            target_wh = int(self.vehicle_capacity_wh * (target.target_soc_percent / 100.0))
            needed_wh = max(0, target_wh - self.current_vehicle_soc_wh)
        elif target.added_energy_mwh is not None:
            needed_wh = target.added_energy_mwh / 1000 # Convert mWh to Wh

        if needed_wh <= 0:
            return # Target already met

        # Calculate duration required at max power
        duration_s = int((needed_wh / self.max_charge_power_w) * 3600)
        
        # Create a single Slot representing the charging block
        # Section 9.2.7.14: We set min_power to 0 (can be paused/delayed) and 
        # max_power to max_charge_power_w.
        slot = SlotStruct(
            min_duration_s=duration_s,
            max_duration_s=duration_s,
            default_duration_s=duration_s,
            elapsed_slot_time_s=0,
            remaining_slot_time_s=duration_s,
            nominal_power_mw=self.max_charge_power_w * 1000,
            min_power_mw=0,
            max_power_mw=self.max_charge_power_w * 1000,
            nominal_energy_mwh=int(needed_wh * 1000),
            # Allow the EMS to adjust the duration/power if it wants to charge slower
            min_power_adjustment_mw=0,
            max_power_adjustment_mw=self.max_charge_power_w * 1000
        )

        # Assume "now" is epoch 0 for simulation purposes.
        # Target time is minutes past midnight. If target is 8AM (480 mins), 
        # latest end time is 480 * 60 = 28800s.
        target_s = target.target_time_minutes_past_midnight * 60
        start_s = max(0, target_s - duration_s)

        forecast = ForecastStruct(
            forecast_id=1,
            active_slot_number=None, # Has not started yet
            start_time_epoch_s=start_s,
            end_time_epoch_s=target_s,
            earliest_start_time_epoch_s=0, # Can start immediately
            latest_end_time_epoch_s=target_s, # MUST finish by target
            is_pausable=True,
            slots=[slot],
            forecast_update_reason=ForecastUpdateReasonEnum.INTERNAL_OPTIMIZATION
        )
        
        self.dem_cluster.set_forecast(forecast)