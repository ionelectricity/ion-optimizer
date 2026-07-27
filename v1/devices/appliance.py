# devices/appliance.py
from typing import Optional
from energy_management_structs import ForecastStruct, SlotStruct, ForecastUpdateReasonEnum
from clusters.dem import DeviceEnergyManagementCluster, ESATypeEnum, DEMFeatureMap


class SmartApplianceDevice:
    """
    Represents a discrete, shiftable appliance (e.g., Dishwasher, Washing Machine).
    Ref: Section 14.x of the Energy Management Specification.
    """

    def __init__(
            self,
            node_id: str,
            appliance_type: ESATypeEnum,
            power_w: int,
            duration_m: int
    ):
        self.node_id = node_id
        self.power_w = power_w
        self.duration_m = duration_m
        self.is_running = False

        # DEM Cluster: Needs Forecast Reporting and Start Time Adjustment
        feature_map = DEMFeatureMap.POWER_FORECAST_REPORTING | DEMFeatureMap.START_TIME_ADJUSTMENT

        self.dem_cluster = DeviceEnergyManagementCluster(
            esa_type=appliance_type,
            esa_can_generate=False,
            abs_min_power_mw=0,
            abs_max_power_mw=power_w * 1000,
            feature_map=feature_map
        )

    def schedule_cycle(self, earliest_start_epoch_s: int, latest_end_epoch_s: int):
        """
        The user loads the dishwasher and says: "Finish this by 8 AM tomorrow".
        We generate a ForecastStruct for the EMS to optimize.
        """
        duration_s = self.duration_m * 60

        # A simple non-pausable appliance has 1 slot representing the whole cycle
        slot = SlotStruct(
            min_duration_s=duration_s,
            max_duration_s=duration_s,
            default_duration_s=duration_s,
            elapsed_slot_time_s=0,
            remaining_slot_time_s=duration_s,
            slot_is_pausable=False,  # CANNOT BE PAUSED ONCE STARTED
            nominal_power_mw=self.power_w * 1000,
            min_power_mw=self.power_w * 1000,
            max_power_mw=self.power_w * 1000,
            nominal_energy_mwh=int((self.power_w * (self.duration_m / 60)) * 1000)
        )

        forecast = ForecastStruct(
            forecast_id=1,
            active_slot_number=None,
            start_time_epoch_s=earliest_start_epoch_s,
            end_time_epoch_s=earliest_start_epoch_s + duration_s,
            earliest_start_time_epoch_s=earliest_start_epoch_s,
            latest_end_time_epoch_s=latest_end_epoch_s,
            is_pausable=False,
            slots=[slot],
            forecast_update_reason=ForecastUpdateReasonEnum.INTERNAL_OPTIMIZATION
        )

        self.dem_cluster.set_forecast(forecast)