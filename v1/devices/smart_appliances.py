# devices/smart_appliances.py
from typing import List, Optional
from energy_management_structs import (
    ForecastStruct,
    SlotStruct,
    ForecastUpdateReasonEnum
)
from clusters.dem import DeviceEnergyManagementCluster, ESATypeEnum, DEMFeatureMap

class GenericSmartAppliance:
    """
    A base class for smart appliances that execute
    multi-slot programs (e.g., Washing Machines, Dishwashers).
    """
    def __init__(self, node_id: str, esa_type: ESATypeEnum):
        self.node_id = node_id
        
        # Appliances are loads (esa_can_generate = False).
        # We assume they support Power Forecast Reporting (PFR) and Start Time Adjustment (STA).
        # Per user instruction: No Power Modulation (FA), No Planned Pausing (PAU).
        feature_map = (
            DEMFeatureMap.POWER_FORECAST_REPORTING | 
            DEMFeatureMap.START_TIME_ADJUSTMENT
        )
        
        # We don't know abs_max_power until a program is selected, 
        # but 3000W is a safe structural maximum for European/US home appliances.
        self.dem_cluster = DeviceEnergyManagementCluster(
            esa_type=esa_type,
            esa_can_generate=False, 
            abs_min_power_mw=0,
            abs_max_power_mw=3000 * 1000, 
            feature_map=feature_map
        )
        
        # Internal state to track user requests
        self.latest_end_time_epoch_s: Optional[int] = None

    def set_program(self, slots: List[SlotStruct], latest_end_time_epoch_s: int):
        """
        Simulates a user selecting a program on the physical appliance 
        (e.g., "Eco Wash") and pressing "Delay Start".
        """
        self.latest_end_time_epoch_s = latest_end_time_epoch_s
        
        total_duration = sum(slot.default_duration_s for slot in slots)
        
        # The baseline forecast assumes we start as late as possible 
        # to finish exactly at the user's deadline.
        start_time = max(0, latest_end_time_epoch_s - total_duration)

        forecast = ForecastStruct(
            forecast_id=1, 
            active_slot_number=None,
            start_time_epoch_s=start_time,
            end_time_epoch_s=latest_end_time_epoch_s,
            earliest_start_time_epoch_s=0, # Can start right now
            latest_end_time_epoch_s=latest_end_time_epoch_s, # Hard constraint
            is_pausable=False, # As requested, we do not proactively schedule pauses
            slots=slots,
            forecast_update_reason=ForecastUpdateReasonEnum.INTERNAL_OPTIMIZATION
        )
        
        self.dem_cluster.set_forecast(forecast)


class DishwasherDevice(GenericSmartAppliance):
    """Device Type ID 0x0075"""
    def __init__(self, node_id: str):
        super().__init__(node_id, ESATypeEnum.DISHWASHER)

    def select_eco_wash(self, latest_end_time_epoch_s: int):
        """Simulates an Eco Wash: Heating (high power), Washing (low power), Drying (med power)."""
        slots = [
            SlotStruct( # 1. Heating water (15 mins @ 2000W)
                min_duration_s=900, max_duration_s=900, default_duration_s=900,
                elapsed_slot_time_s=0, remaining_slot_time_s=900,
                nominal_power_mw=2000 * 1000,
            ),
            SlotStruct( # 2. Washing/Spraying (45 mins @ 150W)
                min_duration_s=2700, max_duration_s=2700, default_duration_s=2700,
                elapsed_slot_time_s=0, remaining_slot_time_s=2700,
                nominal_power_mw=150 * 1000,
            ),
            SlotStruct( # 3. Active Drying (30 mins @ 800W)
                min_duration_s=1800, max_duration_s=1800, default_duration_s=1800,
                elapsed_slot_time_s=0, remaining_slot_time_s=1800,
                nominal_power_mw=800 * 1000,
            )
        ]
        self.set_program(slots, latest_end_time_epoch_s)


class LaundryWasherDevice(GenericSmartAppliance):
    """Device Type ID 0x0073"""
    def __init__(self, node_id: str):
        super().__init__(node_id, ESATypeEnum.WASHING_MACHINE)

    def select_cotton_60(self, latest_end_time_epoch_s: int):
        """Simulates a Cotton 60C Wash: Heating, Tumbling, Spinning."""
        slots = [
            SlotStruct( # 1. Heating water (30 mins @ 2200W)
                min_duration_s=1800, max_duration_s=1800, default_duration_s=1800,
                elapsed_slot_time_s=0, remaining_slot_time_s=1800,
                nominal_power_mw=2200 * 1000,
            ),
            SlotStruct( # 2. Tumbling (60 mins @ 200W)
                min_duration_s=3600, max_duration_s=3600, default_duration_s=3600,
                elapsed_slot_time_s=0, remaining_slot_time_s=3600,
                nominal_power_mw=200 * 1000,
            ),
            SlotStruct( # 3. Spinning (15 mins @ 600W)
                min_duration_s=900, max_duration_s=900, default_duration_s=900,
                elapsed_slot_time_s=0, remaining_slot_time_s=900,
                nominal_power_mw=600 * 1000,
            )
        ]
        self.set_program(slots, latest_end_time_epoch_s)