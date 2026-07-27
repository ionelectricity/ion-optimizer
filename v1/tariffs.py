# tariffs.py
from dataclasses import dataclass, field
from typing import List, Optional
from enum import IntEnum

class TariffPriceTypeEnum(IntEnum):
    STANDARD = 0
    CRITICAL = 1
    VIRTUAL = 2
    INCENTIVE = 3
    INCENTIVE_SIGNAL = 4

class TariffUnitEnum(IntEnum):
    KWH = 0
    KVAH = 1

@dataclass
class CommodityPriceComponentStruct:
    price: int 
    source: TariffPriceTypeEnum
    description: Optional[str] = None
    tariff_component_id: Optional[int] = None

@dataclass
class CommodityPriceStruct:
    period_start_epoch_s: int
    period_end_epoch_s: Optional[int] 
    price: Optional[int] = None       
    price_level: Optional[int] = None
    description: Optional[str] = None
    components: List[CommodityPriceComponentStruct] = field(default_factory=list)

class CommodityPriceCluster:
    def __init__(self, tariff_unit: TariffUnitEnum, currency_iso_4217: int):
        self.tariff_unit: TariffUnitEnum = tariff_unit
        self.currency: int = currency_iso_4217 
        self.current_price: Optional[CommodityPriceStruct] = None
        self.price_forecast: List[CommodityPriceStruct] = [] 

    def set_dynamic_price_forecast(self, forecasts: List[CommodityPriceStruct]):
        self.price_forecast = forecasts

class ElectricalEnergyTariffDevice:
    def __init__(self, node_id: str, currency_code: int = 840):
        self.node_id = node_id
        
        # We simulate two clusters: one for Import, one for Export
        self.import_cluster = CommodityPriceCluster(TariffUnitEnum.KWH, currency_code)
        self.export_cluster = CommodityPriceCluster(TariffUnitEnum.KWH, currency_code)

    def load_mock_dynamic_tariff(self, import_prices: List[int], export_prices: List[int], start_epoch_s: int):
        if len(import_prices) != 24 or len(export_prices) != 24:
            raise ValueError("Mock tariff requires exactly 24 hourly prices.")
            
        imp_forecasts = []
        exp_forecasts = []
        current_time = start_epoch_s
        hour_s = 3600
        
        for imp, exp in zip(import_prices, export_prices):
            imp_forecasts.append(CommodityPriceStruct(current_time, current_time + hour_s, imp))
            exp_forecasts.append(CommodityPriceStruct(current_time, current_time + hour_s, exp))
            current_time += hour_s
            
        self.import_cluster.set_dynamic_price_forecast(imp_forecasts)
        self.export_cluster.set_dynamic_price_forecast(exp_forecasts)

    def get_import_prices(self) -> List[int]:
        return [f.price for f in self.import_cluster.price_forecast if f.price is not None]
        
    def get_export_prices(self) -> List[int]:
        return [f.price for f in self.export_cluster.price_forecast if f.price is not None]