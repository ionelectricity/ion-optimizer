# clusters/dem.py
from enum import IntEnum
from typing import Optional, List
from energy_management_structs import (
    PowerAdjustCapabilityStruct, 
    PowerAdjustStruct,
    ForecastStruct, 
    SlotStruct,
    ForecastUpdateReasonEnum
)

# --- ENUMS (Mapped from Section 9.2.7) ---

class ESATypeEnum(IntEnum):
    EVSE = 0
    SPACE_HEATING = 1
    WATER_HEATING = 2
    SPACE_COOLING = 3
    SPACE_HEATING_COOLING = 4
    BATTERY_STORAGE = 5
    SOLAR_PV = 6
    FRIDGE_FREEZER = 7
    WASHING_MACHINE = 8
    DISHWASHER = 9
    COOKING = 10
    HOME_WATER_PUMP = 11
    IRRIGATION_WATER_PUMP = 12
    POOL_PUMP = 13
    OTHER = 255

class ESAStateEnum(IntEnum):
    OFFLINE = 0
    ONLINE = 1
    FAULT = 2
    POWER_ADJUST_ACTIVE = 3  # Valid only if PA feature supported
    PAUSED = 4               # Valid only if PAU feature supported

class OptOutStateEnum(IntEnum):
    NO_OPT_OUT = 0
    LOCAL_OPT_OUT = 1
    GRID_OPT_OUT = 2
    OPT_OUT = 3

class DEMFeatureMap(IntEnum):
    """Mapped from Section 9.2.4 Features"""
    POWER_ADJUSTMENT = 1 << 0          # PA
    POWER_FORECAST_REPORTING = 1 << 1  # PFR
    STATE_FORECAST_REPORTING = 1 << 2  # SFR
    START_TIME_ADJUSTMENT = 1 << 3     # STA
    PAUSABLE = 1 << 4                  # PAU
    FORECAST_ADJUSTMENT = 1 << 5       # FA
    CONSTRAINT_BASED_ADJUSTMENT = 1 << 6 # CON

# --- CLUSTER IMPLEMENTATION ---

class DeviceEnergyManagementCluster:
    """
    Implements the Device Energy Management (DEM) Cluster (Cluster ID 0x0098).
    Ref: Section 9.2 of the Energy Management Specification.
    """
    
    def __init__(
        self, 
        esa_type: ESATypeEnum, 
        esa_can_generate: bool, 
        abs_min_power_mw: int, 
        abs_max_power_mw: int,
        feature_map: int
    ):
        # 1. Feature Map
        self.feature_map = feature_map
        self._validate_features()

        # 2. Mandatory Attributes (Section 9.2.8)
        self.esa_type: ESATypeEnum = esa_type
        self.esa_can_generate: bool = esa_can_generate
        self.esa_state: ESAStateEnum = ESAStateEnum.ONLINE
        self.abs_min_power_mw: int = abs_min_power_mw
        self.abs_max_power_mw: int = abs_max_power_mw
        
        # 3. Optional/Feature-Dependent Attributes
        self.power_adjustment_capability: Optional[PowerAdjustCapabilityStruct] = None
        self.forecast: Optional[ForecastStruct] = None
        self.opt_out_state: OptOutStateEnum = OptOutStateEnum.NO_OPT_OUT

    def _validate_features(self):
        """Validates feature conformance rules (Section 9.2.4)."""
        has_pa = bool(self.feature_map & DEMFeatureMap.POWER_ADJUSTMENT)
        has_pfr = bool(self.feature_map & DEMFeatureMap.POWER_FORECAST_REPORTING)
        has_sfr = bool(self.feature_map & DEMFeatureMap.STATE_FORECAST_REPORTING)
        has_sta = bool(self.feature_map & DEMFeatureMap.START_TIME_ADJUSTMENT)
        has_pau = bool(self.feature_map & DEMFeatureMap.PAUSABLE)
        has_fa = bool(self.feature_map & DEMFeatureMap.FORECAST_ADJUSTMENT)
        has_con = bool(self.feature_map & DEMFeatureMap.CONSTRAINT_BASED_ADJUSTMENT)

        if not any([has_pa, has_pfr, has_sfr, has_sta, has_pau, has_fa, has_con]):
            raise ValueError("At least one feature MUST be supported.")
        
        if has_pfr and has_sfr:
            raise ValueError("At most one of SFR and PFR SHALL be supported.")
        
        if has_pa and has_sfr:
            raise ValueError("If PA is supported, SFR SHALL NOT be supported.")
            
        if any([has_sta, has_pau, has_fa, has_con]) and not (has_pfr or has_sfr):
            raise ValueError("If STA, PAU, FA, or CON are supported, PFR or SFR MUST be supported.")

    # --- API METHODS FOR THE OPTIMIZER ---

    def set_power_adjustment_capability(self, capabilities: List[PowerAdjustStruct], cause: int = 0):
        """Updates the PowerAdjustmentCapability attribute (Section 9.2.8.6)."""
        if not (self.feature_map & DEMFeatureMap.POWER_ADJUSTMENT):
            raise NotImplementedError("PowerAdjustment (PA) feature not supported.")
        
        if len(capabilities) > 8:
            raise ValueError("Maximum of 8 PowerAdjustStructs allowed.")
            
        self.power_adjustment_capability = PowerAdjustCapabilityStruct(
            power_adjust_capability=capabilities,
            cause=cause
        )

    def set_forecast(self, forecast: ForecastStruct):
        """Updates the Forecast attribute (Section 9.2.8.7)."""
        if not (self.feature_map & (DEMFeatureMap.POWER_FORECAST_REPORTING | DEMFeatureMap.STATE_FORECAST_REPORTING)):
            raise NotImplementedError("PFR or SFR feature must be supported to set a forecast.")
        
        if len(forecast.slots) > 10 or len(forecast.slots) < 1:
            raise ValueError("Slots list must contain between 1 and 10 entries.")
            
        self.forecast = forecast

    # --- API COMMANDS (Mocking incoming commands from an EMS) ---

    def handle_power_adjust_request(self, power_mw: int, duration_s: int, cause: int) -> bool:
        """
        Handles incoming PowerAdjustRequest (Section 9.2.9.1).
        Returns True if ACCEPTED, False if REJECTED.
        """
        if not (self.feature_map & DEMFeatureMap.POWER_ADJUSTMENT):
            return False
            
        if self.esa_state != ESAStateEnum.ONLINE:
            return False # Must be online
            
        if self.opt_out_state in [OptOutStateEnum.OPT_OUT]:
            return False # User opted out
            
        if self.power_adjustment_capability is None or not self.power_adjustment_capability.power_adjust_capability:
            return False # No current capabilities advertised

        # Check if the requested power and duration fall within ANY of the advertised valid ranges
        is_valid = False
        for cap in self.power_adjustment_capability.power_adjust_capability:
            if (cap.min_power_mw <= power_mw <= cap.max_power_mw) and \
               (cap.min_duration_s <= duration_s <= cap.max_duration_s):
                is_valid = True
                break
                
        if is_valid:
            self.esa_state = ESAStateEnum.POWER_ADJUST_ACTIVE
            self.power_adjustment_capability.cause = cause
            # In a real device, we would start a timer for duration_s here and emit a PowerAdjustStart Event
            return True
        else:
            return False # CONSTRAINT_ERROR

    def handle_cancel_power_adjust_request(self) -> bool:
        """Handles incoming CancelPowerAdjustRequest (Section 9.2.9.2)."""
        if self.esa_state != ESAStateEnum.POWER_ADJUST_ACTIVE:
            return False # INVALID_IN_STATE
            
        self.esa_state = ESAStateEnum.ONLINE
        if self.power_adjustment_capability:
            self.power_adjustment_capability.cause = 0 # NoAdjustment
        # In a real device, emit PowerAdjustEnd Event
        return True