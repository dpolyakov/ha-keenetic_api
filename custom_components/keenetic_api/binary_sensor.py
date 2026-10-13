"""The Keenetic API binary sensor entities."""

from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    COORD_FULL,
)
from .coordinator import KeeneticRouterCoordinator
from .keenetic import KeeneticFullData, KeeneticModemData

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class KeeneticBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes Keenetic sensor entity."""
    value_fn: Callable[[KeeneticRouterCoordinator, str], bool]
    attributes_fn: Callable[[KeeneticRouterCoordinator, str], bool] | None = None
    available_fn: Callable[[KeeneticFullData, str], bool] = lambda data, _: True


BINARY_SENSOR_TYPES: dict[str, KeeneticBinarySensorEntityDescription] = {
    "connected_to_router": KeeneticBinarySensorEntityDescription(
        key="connected_to_router",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn= lambda coordinator, obj_id: coordinator.last_update_success,
    ),
    "connected_to_interface": KeeneticBinarySensorEntityDescription(
        key="connected_to_interface",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn= lambda coordinator, obj_id: coordinator.data.show_interface[obj_id].get('connected', "no") == "yes",
        available_fn=lambda data, obj_id: obj_id in data.show_interface,
    ),
    "connected_to_media": KeeneticBinarySensorEntityDescription(
        key="connected_to_media",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn= lambda coordinator, obj_id: coordinator.data.show_media.get(obj_id, False),
        attributes_fn=lambda coordinator, obj_id: {
            "media": coordinator.data.show_media.get(obj_id, None),
        },
    ),
    "interface_pingcheck": KeeneticBinarySensorEntityDescription(
        key="interface_pingcheck",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn= lambda coordinator, obj_id: coordinator.data.show_pingcheck.get(obj_id, {}).status == "pass",
        available_fn=lambda data, obj_id: obj_id in data.show_pingcheck,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant, 
    entry: ConfigEntry, 
    async_add_entities: AddEntitiesCallback
) -> None:

    coordinator = hass.data[DOMAIN][entry.entry_id][COORD_FULL]
    binary_sensors: list[BinarySensorEntity] = []
    known_legacy_interfaces = set()

    binary_sensors.append(KeeneticBinarySensorEntity(coordinator, BINARY_SENSOR_TYPES["connected_to_router"], "connected_to_router", coordinator.router.name_device))

    for interface, data_interface in coordinator.data.show_interface.items():
        if interface in coordinator.router.request_interface:
            known_legacy_interfaces.add(interface)
            new_name = coordinator.router.request_interface[interface]
            binary_sensors.append(
                KeeneticBinarySensorEntity(
                    coordinator,
                    BINARY_SENSOR_TYPES["connected_to_interface"],
                    interface,
                    new_name,
                )
            )
    for pc_interface, pc_data_interface in coordinator.data.show_pingcheck.items():
            binary_sensors.append(
                KeeneticBinarySensorEntity(
                    coordinator,
                    BINARY_SENSOR_TYPES["interface_pingcheck"],
                    pc_interface,
                    pc_data_interface.interface_name,
                )
            )

    for usb in coordinator.data.show_rc_system_usb:
        name_media = f"Media{int(usb['port'])-1}"
        binary_sensors.append(KeeneticBinarySensorEntity(coordinator, BINARY_SENSOR_TYPES["connected_to_media"], name_media, name_media))

    known_modem_sensors: set[tuple[str, str]] = set()

    def collect_modem_sensors() -> list[BinarySensorEntity]:
        new_sensors = []
        for interface, modem in coordinator.data.modems.items():
            if interface not in known_legacy_interfaces:
                known_legacy_interfaces.add(interface)
                name = coordinator.router.request_interface.get(interface, interface)
                new_sensors.append(
                    KeeneticBinarySensorEntity(
                        coordinator, BINARY_SENSOR_TYPES["connected_to_interface"], interface, name
                    )
                )
            for key in ("modem_roaming", "modem_plugged"):
                if (modem.identity, key) in known_modem_sensors:
                    continue
                if key == "modem_roaming" and "roaming" not in modem.details:
                    continue
                if key == "modem_plugged" and "plugged" not in modem.details:
                    continue
                known_modem_sensors.add((modem.identity, key))
                new_sensors.append(KeeneticModemBinarySensor(coordinator, modem, key))
        return new_sensors

    binary_sensors.extend(collect_modem_sensors())
    async_add_entities(binary_sensors, False)

    def add_new_modem_sensors() -> None:
        if new_sensors := collect_modem_sensors():
            async_add_entities(new_sensors, False)

    entry.async_on_unload(coordinator.async_add_listener(add_new_modem_sensors))


class KeeneticBinarySensorEntity(CoordinatorEntity[KeeneticRouterCoordinator], BinarySensorEntity):

    _attr_has_entity_name = True
    entity_description: KeeneticRouterSensorEntityDescription

    def __init__(
        self,
        coordinator: KeeneticRouterCoordinator,
        description: KeeneticBinarySensorEntityDescription,
        obj_id: str,
        obj_name: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._obj_id = obj_id
        self._attr_key = description.key
        self.entity_description = description
        modem = coordinator.data.modems.get(obj_id)
        self._attr_device_info = coordinator.modem_device_info(modem) if modem else coordinator.device_info
        self._attr_unique_id = f"{coordinator.unique_id}_{self._attr_key}_{self._obj_id}"
        self._attr_translation_key = self._attr_key
        self._attr_translation_placeholders = {"name": f"{obj_name}"}

    @property
    def is_on(self) -> bool:
        return self.entity_description.value_fn(self.coordinator, self._obj_id) if self.available else False

    @property
    def available(self) -> bool:
        if self.entity_description.key == "connected_to_router":
            return True
        else:
            return (super().available and self.entity_description.available_fn(self.coordinator.data, self._obj_id))

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        if self.entity_description.attributes_fn is not None:
            return self.entity_description.attributes_fn(self.coordinator, self._obj_id)
        else:
            return None


class KeeneticModemBinarySensor(CoordinatorEntity[KeeneticRouterCoordinator], BinarySensorEntity):
    """Physical modem roaming or USB connection state."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: KeeneticRouterCoordinator, modem: KeeneticModemData, key: str) -> None:
        super().__init__(coordinator)
        self.identity = modem.identity
        self.key = key
        self._attr_unique_id = f"{coordinator.unique_id}_{modem.identity}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = coordinator.modem_device_info(modem)
        if key == "modem_roaming":
            self._attr_device_class = None

    @property
    def modem(self) -> KeeneticModemData | None:
        return next(
            (modem for modem in self.coordinator.data.modems.values() if modem.identity == self.identity),
            None,
        )

    @property
    def available(self) -> bool:
        modem = self.modem
        field = "roaming" if self.key == "modem_roaming" else "plugged"
        return super().available and modem is not None and modem.details.get(field) is not None

    @property
    def is_on(self) -> bool:
        modem = self.modem
        if modem is None:
            return False
        value = modem.details.get("roaming" if self.key == "modem_roaming" else "plugged")
        return value is True or str(value).lower() in {"yes", "true", "1"}
