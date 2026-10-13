"""The Keenetic API sensor entities."""

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any
from datetime import UTC, datetime, timedelta
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import (
    PERCENTAGE, 
    UnitOfInformation, 
    EntityCategory, 
    UnitOfDataRate,
    UnitOfTemperature,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import KeeneticRouterCoordinator
from .keenetic import (
    KeeneticFullData, KeeneticModemData, modem_number, modem_text,
    modem_frequency, modem_sinr, modem_carriers,
)
from .const import (
    DOMAIN,
    COORD_FULL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class KeeneticRouterSensorEntityDescription(SensorEntityDescription):
    """A class that describes sensor entities."""
    value: Callable[[KeeneticFullData, Any], Any] = (
        lambda coordinator, key: coordinator.data.show_system[key] if coordinator.data.show_system[key] is not None else None
    )
    attributes_fn: Callable[[KeeneticFullData], dict[str, Any]] | None = None


def convert_uptime(uptime: int) -> datetime:
    """Convert uptime."""
    if uptime != None:
        return (datetime.now(tz=UTC) - timedelta(seconds=int(uptime))).replace(second=0, microsecond=0)
    else:
        return None

def convert_data_size(data_size: int = 0) -> float:
    """Convert data_size."""
    return round(data_size/1024/1024, 3)

def ind_wan_ip_adress(fdata: KeeneticFullData):
    """Определение внешнего IP адреса."""
    try:
        data_p_i = fdata.priority_interface
        show_interface = fdata.show_interface
        priority_interface = sorted(data_p_i, key=lambda x: data_p_i[x]['order'])
        for row in priority_interface:
            if show_interface[row]["connected"] == "yes":
                if row.startswith('Wireguard'):
                    return show_interface[row]["wireguard"]["peer"][0].get("remote-endpoint-address") or show_interface[row]["wireguard"]["peer"][0].get("remote") or "-"
                else:
                    return show_interface[row]["address"]
    except Exception as ex:
        _LOGGER.debug(f'Not ind_wan_ip_adress - {ex}')
        return None


SENSOR_TYPES: tuple[KeeneticRouterSensorEntityDescription, ...] = (
    KeeneticRouterSensorEntityDescription(
        key="cpuload",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    KeeneticRouterSensorEntityDescription(
        key="memory",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value=lambda coordinator, key: int(float(coordinator.data.show_system[key].split('/')[0])/float(coordinator.data.show_system[key].split('/')[1])*100),
    ),
    KeeneticRouterSensorEntityDescription(
        key="uptime",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda coordinator, key: convert_uptime(coordinator.data.show_system[key]),
    ),
    KeeneticRouterSensorEntityDescription(
        key="wan_ip_adress",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda coordinator, key: ind_wan_ip_adress(coordinator.data),
    ),
    KeeneticRouterSensorEntityDescription(
        key="temperature_2_4g",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda coordinator, key: coordinator.data.show_interface['WifiMaster0']['temperature'],
    ),
    KeeneticRouterSensorEntityDescription(
        key="temperature_5g",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda coordinator, key: coordinator.data.show_interface['WifiMaster1']['temperature'],
    ),
    KeeneticRouterSensorEntityDescription(
        key="clients_wifi",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda coordinator, key: len(coordinator.data.show_associations.get("station", [])),
    ),
)

SENSORS_STAT_INTERFACE: tuple[KeeneticRouterSensorEntityDescription, ...] = (
    KeeneticRouterSensorEntityDescription(
        key="rxbytes",
        state_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        value=lambda coordinator, obj_id: convert_data_size(coordinator.data.stat_interface[obj_id].get('rxbytes')),
    ),
    KeeneticRouterSensorEntityDescription(
        key="txbytes",
        state_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        value=lambda coordinator, obj_id: convert_data_size(coordinator.data.stat_interface[obj_id].get('txbytes')),
    ),
    KeeneticRouterSensorEntityDescription(
        key="timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda coordinator, obj_id: convert_uptime(coordinator.data.show_interface[obj_id].get('uptime')),
    ),
    KeeneticRouterSensorEntityDescription(
        key="rxspeed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        value=lambda coordinator, obj_id: convert_data_size(coordinator.data.stat_interface[obj_id].get('rxspeed')),
    ),
    KeeneticRouterSensorEntityDescription(
        key="txspeed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        value=lambda coordinator, obj_id: convert_data_size(coordinator.data.stat_interface[obj_id].get('txspeed')),
    ),
)


@dataclass(frozen=True, kw_only=True)
class KeeneticModemSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], StateType]
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


MODEM_SENSOR_TYPES: tuple[KeeneticModemSensorDescription, ...] = (
    KeeneticModemSensorDescription(
        key="modem_info",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: modem_text(d, "product") or modem_text(d, "description") or d.get("id"),
        attributes_fn=lambda d: {
            key: d[key] for key in ("imei", "imsi", "iccid", "phone-number", "serial")
            if d.get(key) not in (None, "")
        },
    ),
    *(
        KeeneticModemSensorDescription(
            key=f"modem_{key}",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda d, field=field: modem_text(d, field),
        )
        for key, field in (
            ("connection_state", "connection-state"), ("sim", "sim"),
            ("operator", "operator"), ("mobile", "mobile"),
            ("apn", "apn"), ("ip_address", "address"),
            ("band", "band"), ("plmn", "plmn"),
            ("bssid", "bssid"), ("enb_id", "enb-id"),
            ("sector_id", "sector-id"), ("tac", "tac"),
            ("phy_cell_id", "phy-cell-id"),
        )
    ),
    *(
        KeeneticModemSensorDescription(
            key=key,
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            native_unit_of_measurement=unit,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=(lambda d: modem_sinr(d)) if key == "sinr" else (lambda d, field=key: modem_number(d, field)),
            attributes_fn=(lambda d: {"source": "sinr" if d.get("sinr") not in (None, "") else "cinr"}) if key == "sinr" else None,
        )
        for key, unit in (("rssi", "dBm"), ("rsrp", "dBm"), ("rsrq", "dB"), ("sinr", "dB"))
    ),
    KeeneticModemSensorDescription(
        key="modem_signal_level", state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: modem_number(d, "signal-level"),
    ),
    KeeneticModemSensorDescription(
        key="modem_temperature", device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: modem_number(d, "temperature"),
    ),
    KeeneticModemSensorDescription(
        key="modem_bandwidth", native_unit_of_measurement="MHz",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: modem_number(d, "bandwidth"),
    ),
    *(
        KeeneticModemSensorDescription(
            key=f"modem_{key}_frequency", device_class=SensorDeviceClass.FREQUENCY,
            native_unit_of_measurement="MHz", state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda d, field=field: modem_frequency(d, field),
        )
        for key, field in (("downlink", "dl-freq"), ("uplink", "ul-freq"))
    ),
    KeeneticModemSensorDescription(
        key="modem_earfcn", entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: modem_number(d, "earfcn"),
    ),
    KeeneticModemSensorDescription(
        key="modem_pin_attempts", entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: modem_number(d, "pin-attempts"),
    ),
    KeeneticModemSensorDescription(
        key="modem_carriers", entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: len(modem_carriers(d)) if "carrier" in d else None,
        attributes_fn=lambda d: {"carriers": modem_carriers(d)},
    ),
)

ALWAYS_PRESENT_MODEM_SENSORS = {"modem_info", "rssi", "rsrp", "rsrq", "sinr"}
STAT_SENSOR_KEYS = {description.key for description in SENSORS_STAT_INTERFACE}

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry, 
    async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id][COORD_FULL]

    sensors = []
    known_legacy_interfaces = set()
    for description in SENSOR_TYPES:
        try:
            if description.value(coordinator, description.key) is not None:
                sensors.append(KeeneticRouterSensor(coordinator, description, description.key, description.key))
        except Exception as err:
            _LOGGER.debug(f'async_setup_entry sensor SENSOR_TYPES {description} err - {err}')

    for interface, data_interface in coordinator.data.show_interface.items():
        if interface in coordinator.router.request_interface:
            known_legacy_interfaces.add(interface)
            new_name = coordinator.router.request_interface[interface]
            for description in SENSORS_STAT_INTERFACE:
                try:
                    sensors.append(KeeneticRouterSensor(coordinator, description, interface, new_name))
                except Exception as err:
                    _LOGGER.debug(f'async_setup_entry sensor SENSORS_STAT_INTERFACE {description} err - {err}')

    known_modem_sensors: set[tuple[str, str]] = set()

    def collect_modem_sensors() -> list[SensorEntity]:
        new_sensors = []
        for interface, modem in coordinator.data.modems.items():
            if interface not in known_legacy_interfaces:
                known_legacy_interfaces.add(interface)
                name = coordinator.router.request_interface.get(interface, interface)
                new_sensors.extend(
                    KeeneticRouterSensor(coordinator, description, interface, name)
                    for description in SENSORS_STAT_INTERFACE
                )
            for description in MODEM_SENSOR_TYPES:
                key = (modem.identity, description.key)
                if key in known_modem_sensors:
                    continue
                if description.key not in ALWAYS_PRESENT_MODEM_SENSORS and description.value_fn(modem.details) is None:
                    continue
                known_modem_sensors.add(key)
                new_sensors.append(KeeneticModemSensor(coordinator, modem, description))
        return new_sensors

    sensors.extend(collect_modem_sensors())
    async_add_entities(sensors, False)

    def add_new_modem_sensors() -> None:
        if new_sensors := collect_modem_sensors():
            async_add_entities(new_sensors, False)

    entry.async_on_unload(coordinator.async_add_listener(add_new_modem_sensors))

class KeeneticRouterSensor(CoordinatorEntity[KeeneticRouterCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: KeeneticRouterSensorEntityDescription

    def __init__(
            self,
            coordinator: KeeneticRouterCoordinator,
            description: KeeneticRouterSensorEntityDescription,
            obj_id: str,
            obj_name: str,
    ) -> None:
        super().__init__(coordinator)

        modem = coordinator.data.modems.get(obj_id)
        self._attr_device_info = coordinator.modem_device_info(modem) if modem else coordinator.device_info
        self.obj_id = obj_id
        self._attr_unique_id = f"{coordinator.unique_id}_{description.key}_{self.obj_id}"
        self.entity_description = description
        self._attr_translation_key = description.key
        self._attr_translation_placeholders = {"name": f"{obj_name}"}

    @property
    def native_value(self) -> StateType:
        """Sensor value."""
        if not self.available:
            return None
        try:
            return self.entity_description.value(self.coordinator, self.obj_id)
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        if self.entity_description.key in STAT_SENSOR_KEYS:
            if not super().available or self.obj_id not in self.coordinator.data.show_interface:
                return False
            if self.entity_description.key == "timestamp":
                return self.coordinator.data.show_interface[self.obj_id].get("uptime") is not None
            return self.coordinator.data.stat_interface.get(self.obj_id, {}).get(self.entity_description.key) is not None
        return super().available

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return the state attributes of the sensor."""
        if self.entity_description.attributes_fn is not None:
            return self.entity_description.attributes_fn(self.coordinator.data)
        else:
            return None


class KeeneticModemSensor(CoordinatorEntity[KeeneticRouterCoordinator], SensorEntity):
    """A diagnostic value bound to a physical modem."""

    _attr_has_entity_name = True
    entity_description: KeeneticModemSensorDescription

    def __init__(
        self,
        coordinator: KeeneticRouterCoordinator,
        modem: KeeneticModemData,
        description: KeeneticModemSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.identity = modem.identity
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.unique_id}_{modem.identity}_{description.key}"
        self._attr_translation_key = description.key
        self._attr_device_info = coordinator.modem_device_info(modem)

    @property
    def modem(self) -> KeeneticModemData | None:
        return next(
            (modem for modem in self.coordinator.data.modems.values() if modem.identity == self.identity),
            None,
        )

    @property
    def available(self) -> bool:
        modem = self.modem
        return super().available and modem is not None and self.entity_description.value_fn(modem.details) is not None

    @property
    def native_value(self) -> StateType:
        modem = self.modem
        return self.entity_description.value_fn(modem.details) if modem else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        modem = self.modem
        if modem and self.entity_description.attributes_fn:
            return self.entity_description.attributes_fn(modem.details)
        return None
