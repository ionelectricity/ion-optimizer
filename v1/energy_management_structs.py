# energy_management_structs.py
from dataclasses import dataclass, field
from typing import List, Optional
from enum import IntEnum

# --- ENUMS (Mapped from Section 9.2.7 and 9.3.7) ---

class CostTypeEnum(IntEnum):
    FINANCIAL = 0
    GHG_EMISSIONS = 1
    COMFORT = 2
    TEMPERATURE = 3

class AdjustmentCauseEnum(IntEnum):
    LOCAL_OPTIMIZATION = 0
    GRID_OPTIMIZATION = 1

class ForecastUpdateReasonEnum(IntEnum):
    INTERNAL_OPTIMIZATION = 0
    LOCAL_OPTIMIZATION = 1
    GRID_OPTIMIZATION = 2

# --- DATA STRUCTS ---

@dataclass
class CostStruct:
    """Represents the cost to run an appliance (Financial, GHG, Comfort)."""
    cost_type: CostTypeEnum
    value: int
    decimal_points: int
    currency: Optional[int] = None # ISO 4217 currency code (max 999)

@dataclass
class PowerAdjustStruct:
    """
    Limits for continuous power modulation (e.g., Battery, EVSE).
    Note: Power is in milliwatts (mW). Negative values indicate exporting/discharging.
    """
    min_power_mw: int
    max_power_mw: int
    min_duration_s: int
    max_duration_s: int

@dataclass
class PowerAdjustCapabilityStruct:
    """Indicates how the ESA (Energy Smart Appliance) can be adjusted at the current time."""
    power_adjust_capability: List[PowerAdjustStruct] = field(default_factory=list) # Max 8
    cause: int = 0 # PowerAdjustReasonEnum

@dataclass
class SlotStruct:
    """
    Represents a specific stage of an ESA's operation (e.g., 'Heating' vs 'Spinning' on a washer).
    If the device is a simple continuous load/battery, it might just use one slot.
    """
    min_duration_s: int
    max_duration_s: int
    default_duration_s: int
    elapsed_slot_time_s: int
    remaining_slot_time_s: int
    
    # Pausable Feature (PAU)
    slot_is_pausable: bool = False
    min_pause_duration_s: Optional[int] = None
    max_pause_duration_s: Optional[int] = None
    
    # State Forecast Reporting (SFR)
    manufacturer_esa_state: Optional[int] = None
    
    # Power Forecast Reporting (PFR)
    nominal_power_mw: Optional[int] = None
    min_power_mw: Optional[int] = None
    max_power_mw: Optional[int] = None
    nominal_energy_mwh: Optional[int] = None
    
    costs: List[CostStruct] = field(default_factory=list) # Max 5
    
    # Forecast Adjustment Feature (FA) & PFR
    min_power_adjustment_mw: Optional[int] = None
    max_power_adjustment_mw: Optional[int] = None
    min_duration_adjustment_s: Optional[int] = None
    max_duration_adjustment_s: Optional[int] = None

@dataclass
class ForecastStruct:
    """
    Indicates the overall timing of the ESA's planned energy and power use.
    Contains a list of 'slots' (SlotStructs).
    """
    forecast_id: int
    active_slot_number: Optional[int] # Null if not started
    start_time_epoch_s: int
    end_time_epoch_s: int
    
    # Start Time Adjustment (STA)
    earliest_start_time_epoch_s: Optional[int] = None
    latest_end_time_epoch_s: Optional[int] = None
    
    is_pausable: bool = False
    slots: List[SlotStruct] = field(default_factory=list) # Max 10
    forecast_update_reason: ForecastUpdateReasonEnum = ForecastUpdateReasonEnum.INTERNAL_OPTIMIZATION

# --- EVSE SPECIFIC STRUCTS (Mapped from Section 9.3.7) ---

@dataclass
class ChargingTargetStruct:
    """Represents a single user-specified charging target for an EV."""
    target_time_minutes_past_midnight: int # 0 to 1439
    target_soc_percent: Optional[int] = None # Takes precedence over added_energy if supported
    added_energy_mwh: Optional[int] = None

@dataclass
class ChargingTargetScheduleStruct:
    """Represents a set of user-specified charging targets for specific days."""
    day_of_week_for_sequence_bitmap: int # TargetDayOfWeekBitmap (map8)
    charging_targets: List[ChargingTargetStruct] = field(default_factory=list) # Max 10