# devices/water_heater.py
from typing import Optional, List
from dataclasses import dataclass
from enum import IntEnum

from energy_management_structs import (
    ForecastStruct,
    SlotStruct,
    ForecastUpdateReasonEnum
)
from clusters.dem import DeviceEnergyManagementCluster, ESATypeEnum, DEMFeatureMap

# --- ENUMS (Mapped from Section 9.5.6) ---

class WaterHeaterHeatSourceBitmap(IntEnum):
    IMMERSION_ELEMENT_1 = 1 << 0
    IMMERSION_ELEMENT_2 = 1 << 1
    HEAT_PUMP = 1 << 2
    BOILER = 1 << 3
    OTHER = 1 << 4

class BoostStateEnum(IntEnum):
    INACTIVE = 0
    ACTIVE = 1

@dataclass
class WaterHeaterBoostInfoStruct:
    """Ref: Section 9.5.6.3"""
    duration_s: int
    one_shot: bool = False
    emergency_boost: bool = False
    temporary_setpoint: Optional[int] = None # Temperature
    target_percentage: Optional[int] = None
    target_reheat: Optional[int] = None


class WaterHeaterManagementCluster:
    """
    Implements the Water Heater Management Cluster (Cluster ID 0x0094).
    Ref: Section 9.5 of the Energy Management Specification.
    """
    def __init__(self, heater_types: int, tank_volume_liters: int):
        self.heater_types = heater_types
        self.heat_demand = 0
        self.tank_volume = tank_volume_liters
        self.estimated_heat_required_mwh = 0
        self.tank_percentage = 0
        self.boost_state = BoostStateEnum.INACTIVE
        self.active_boost_info: Optional[WaterHeaterBoostInfoStruct] = None


class WaterHeaterDevice:
    """
    Implements the Water Heater Device Type (Device Type ID 0x050F).
    Ref: Section 14.2 of the Energy Management Device Library.
    """
    
    def __init__(
        self,
        node_id: str,
        tank_volume_liters: int,
        heating_power_w: int,
        heater_type: int = WaterHeaterHeatSourceBitmap.IMMERSION_ELEMENT_1
    ):
        self.node_id = node_id
        
        # --- Internal Physical State ---
        self.heating_power_w = heating_power_w
        
        # Specific Heat Capacity of Water: 4182 J/kg °C. 
        # For simplicity in the simulator, we assume a target of 60C and incoming water of 20C.
        # Delta T = 40C. Energy = 4182 * 40 * Volume / 3600 (to get Wh).
        self.total_thermal_capacity_wh = int((4182 * 40 * tank_volume_liters) / 3600)
        self.current_thermal_energy_wh = 0
        
        # --- Endpoint Composition: Clusters ---
        
        # 1. Water Heater Management Cluster
        self.whm_cluster = WaterHeaterManagementCluster(
            heater_types=heater_type,
            tank_volume_liters=tank_volume_liters
        )
        
        # 2. Device Energy Management (DEM) Cluster
        # Water heaters are flexible loads. They can be shifted, paused, and adjusted.
        feature_map = (
            DEMFeatureMap.POWER_FORECAST_REPORTING | 
            DEMFeatureMap.START_TIME_ADJUSTMENT |
            DEMFeatureMap.PAUSABLE |
            DEMFeatureMap.FORECAST_ADJUSTMENT
        )
        
        self.dem_cluster = DeviceEnergyManagementCluster(
            esa_type=ESATypeEnum.WATER_HEATING,
            esa_can_generate=False, # It is purely a load
            abs_min_power_mw=0,
            abs_max_power_mw=heating_power_w * 1000,
            feature_map=feature_map
        )
        
        self._update_physical_state()

    def _update_physical_state(self):
        """Calculates TankPercentage and EstimatedHeatRequired based on thermal mass."""
        self.whm_cluster.tank_percentage = int((self.current_thermal_energy_wh / self.total_thermal_capacity_wh) * 100)
        needed_wh = self.total_thermal_capacity_wh - self.current_thermal_energy_wh
        self.whm_cluster.estimated_heat_required_mwh = needed_wh * 1000

    # --- SIMULATION METHODS (Physical World Events) ---

    def simulate_water_draw(self, volume_liters: int):
        """Simulates someone taking a shower or using hot water."""
        energy_lost_wh = int((4182 * 40 * volume_liters) / 3600)
        self.current_thermal_energy_wh = max(0, self.current_thermal_energy_wh - energy_lost_wh)
        self._update_physical_state()
        self._recalculate_forecast()

    # --- WATER HEATER MANAGEMENT COMMANDS (Section 9.5.8) ---

    def request_boost(self, boost_info: WaterHeaterBoostInfoStruct) -> bool:
        """
        Handles the Boost Command (0x00).
        Forces the water heater to start heating immediately, overriding normal schedules.
        """
        self.whm_cluster.boost_state = BoostStateEnum.ACTIVE
        self.whm_cluster.active_boost_info = boost_info
        
        # Update the DEM Forecast to reflect the immediate, mandatory power draw
        self._recalculate_forecast(is_boost=True)
        # In a real device, emit BoostStarted Event here
        return True

    def cancel_boost(self) -> bool:
        """Handles the CancelBoost Command (0x01)."""
        self.whm_cluster.boost_state = BoostStateEnum.INACTIVE
        self.whm_cluster.active_boost_info = None
        self._recalculate_forecast()
        # Emit BoostEnded Event here
        return True

    # --- INTERNAL LOGIC: Bridging WHM Intent to DEM Math ---

    def _recalculate_forecast(self, is_boost: bool = False):
        """
        Translates the thermal need (or Boost command) into a mathematical `ForecastStruct`
        for the OR-Tools EMS.
        """
        needed_wh = self.total_thermal_capacity_wh - self.current_thermal_energy_wh
        
        # If we are fully heated and not boosting, clear the forecast
        if needed_wh <= 0:
            self.dem_cluster.forecast = None
            return

        duration_s = int((needed_wh / self.heating_power_w) * 3600)
        
        # Create a single slot representing the heating cycle
        slot = SlotStruct(
            min_duration_s=duration_s,
            max_duration_s=duration_s,
            default_duration_s=duration_s,
            elapsed_slot_time_s=0,
            remaining_slot_time_s=duration_s,
            
            # Water Heaters are pausable! You can stop heating and resume later.
            slot_is_pausable=True,
            min_pause_duration_s=60, # 1 minute
            max_pause_duration_s=3600, # 1 hour
            
            nominal_power_mw=self.heating_power_w * 1000,
            min_power_mw=self.heating_power_w * 1000, # Assuming non-modulating element
            max_power_mw=self.heating_power_w * 1000,
            nominal_energy_mwh=int(needed_wh * 1000),
        )

        # If it's a Boost, we CANNOT delay it (earliest_start == latest_end == 0)
        # If it's normal heating, we can delay it until the tank gets too cold.
        if is_boost:
            earliest_start = 0
            latest_end = duration_s
            reason = ForecastUpdateReasonEnum.LOCAL_OPTIMIZATION
        else:
            earliest_start = 0
            # Allow the EMS to shift this up to 6 hours into the future, 
            # so long as it completes before the user takes a shower.
            latest_end = duration_s + (6 * 3600) 
            reason = ForecastUpdateReasonEnum.INTERNAL_OPTIMIZATION

        forecast = ForecastStruct(
            forecast_id=2, # Increment in a real system
            active_slot_number=None,
            start_time_epoch_s=earliest_start,
            end_time_epoch_s=latest_end,
            earliest_start_time_epoch_s=earliest_start,
            latest_end_time_epoch_s=latest_end,
            is_pausable=True,
            slots=[slot],
            forecast_update_reason=reason
        )
        
        self.dem_cluster.set_forecast(forecast)