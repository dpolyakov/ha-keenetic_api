"""The Keenetic API coordinator."""

from __future__ import annotations
from datetime import timedelta
import logging
import asyncio

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator, 
    UpdateFailed,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.const import CONF_HOST

from .keenetic import Router, KeeneticModemData, SmsEventTracker
from .const import (
    DOMAIN, 
    FW_SANDBOX,
    COORD_FIREWARE,
    SCAN_INTERVAL_FIREWARE,
    COUNT_REPEATED_REQUEST_FIREWARE,
    TIMER_REPEATED_REQUEST_FIREWARE,
)

_LOGGER = logging.getLogger(__name__)


class KeeneticRouterCoordinator(DataUpdateCoordinator):
    def __init__(
            self,
            hass: HomeAssistant,
            router: Router,
            update_interval: int,
            entry: ConfigEntry,
            parent_device_id: str,
    ) -> None:
        self.router = router
        self.entry = entry
        self._host = entry.data[CONF_HOST]
        self.unique_id = f"{entry.unique_id}_full"
        self.parent_device_id = parent_device_id
        self._sms_tracker = SmsEventTracker()
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{self._host}-full",
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self):
        """Asynchronous update of all data."""
        _errr = None
        try:
            full_data = await self.router.custom_request()
            self._sync_modem_devices(full_data.modems)
            for interface, inbox in full_data.sms.items():
                modem = full_data.modems.get(interface)
                if modem is None:
                    continue
                for message in self._sms_tracker.new_messages(modem, inbox):
                    self.hass.bus.async_fire("keenetic_api_sms", {
                        "entry_id": self.entry.entry_id,
                        "interface": interface,
                        "sms_id": message["id"],
                        "from": message["from"],
                        "timestamp": message["timestamp"],
                        "message": message["text"],
                    })
        except Exception as err:
            _LOGGER.debug(f"{self.router.mac} UpdateFailed _async_update_data (err {err})")
            _errr = err
        try:
            coordinator_firmware = self.hass.data[DOMAIN][self.entry.entry_id][COORD_FIREWARE]
            if (not coordinator_firmware.last_update_success) or _errr != None:
                await coordinator_firmware.async_refresh()
        except Exception:
                pass
        if _errr != None:
            raise UpdateFailed(f"{self.router.mac} UpdateFailed (err {_errr})")
        return full_data

    def modem_device_info(self, modem: KeeneticModemData) -> DeviceInfo:
        """Describe a physical modem as a device attached to this router."""
        details = modem.details
        ati = details.get("ati") or {}
        if not isinstance(ati, dict):
            ati = {}
        model = details.get("product") or details.get("description") or modem.interface
        model = str(model).strip() or modem.interface
        return DeviceInfo(
            identifiers={(DOMAIN, modem.identity)},
            via_device_id=self.parent_device_id,
            manufacturer=details.get("manufacturer") or ati.get("manufacturer"),
            model=model,
            sw_version=details.get("fw"),
            name=f"{model} ({modem.interface})",
        )

    def _sync_modem_devices(self, modems: dict[str, KeeneticModemData]) -> None:
        """Register modems and retain legacy interface entity unique IDs."""
        devices = dr.async_get(self.hass)
        entities = er.async_get(self.hass)
        existing = {
            (entry.domain, entry.unique_id): entry
            for entry in er.async_entries_for_config_entry(entities, self.entry.entry_id)
        }
        for interface, modem in modems.items():
            device = devices.async_get_or_create(
                config_entry_id=self.entry.entry_id,
                **self.modem_device_info(modem),
            )
            for domain, keys in (
                ("sensor", ("rxbytes", "txbytes", "timestamp", "rxspeed", "txspeed")),
                ("binary_sensor", ("connected_to_interface",)),
            ):
                for key in keys:
                    unique_id = f"{self.unique_id}_{key}_{interface}"
                    if entity := existing.get((domain, unique_id)):
                        if entity.device_id != device.id:
                            entities.async_update_entity(entity.entity_id, device_id=device.id)

    @property
    def device_info(self) -> DeviceInfo:
        """Set device info."""
        return DeviceInfo(
            configuration_url=self.router.url_router,
            connections={(CONNECTION_NETWORK_MAC, self.router.mac)},
            identifiers={(DOMAIN, self.router.mac)},
            manufacturer="Keenetic",
            name=self.entry.unique_id,
            model=self.router.model,
            hw_version=self.router.hw_type,
        )


class KeeneticRouterFirmwareCoordinator(DataUpdateCoordinator):
    def __init__(
            self,
            hass: HomeAssistant,
            router: Router,
            update_interval: int,
            entry: ConfigEntry
    ) -> None:
        self.router = router
        self.entry = entry
        self.unique_id = f"{entry.unique_id}_fw"
        self._host = entry.data[CONF_HOST]
        self._version_firmware = {}
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{self._host}-fw",
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self):
        repeat=0
        while repeat < COUNT_REPEATED_REQUEST_FIREWARE:
            repeat += 1
            data_components_list = await self.router.components_list()
            if not data_components_list.get('continued', False):
                break
            _LOGGER.debug(f"{self.router.mac} data_components_list not data {data_components_list}")
            await asyncio.sleep(TIMER_REPEATED_REQUEST_FIREWARE)
        firmware = {}
        firmware['new'] = data_components_list.get('firmware')
        firmware['current'] = data_components_list.get('local')
        firmware['sandbox'] = data_components_list.get('sandbox')
        if (
            self._version_firmware == {} 
            or self._version_firmware.get("new").get("version") != firmware.get("new").get("version") 
            or self._version_firmware.get("current").get("version") != firmware.get("current").get("version")
        ):
            repeat=0
            while repeat < COUNT_REPEATED_REQUEST_FIREWARE:
                repeat += 1
                data_release_notes = await self.router.release_notes(firmware['new']['version'], FW_SANDBOX[firmware['sandbox']])
                if not data_release_notes.get('continued', False):
                    break
                _LOGGER.debug(f"{self.router.mac} data_release_notes not data {data_release_notes}")
                await asyncio.sleep(TIMER_REPEATED_REQUEST_FIREWARE)
            firmware['release_notes'] = data_release_notes['webhelp']['ru'][0]['href']
            firmware['channel'] = data_release_notes['webhelp']['ru'][0]['title']
            self._version_firmware = firmware
        return self._version_firmware

    @property
    def device_info(self) -> DeviceInfo:
        """Set device info."""
        vfw = self._version_firmware.get("current")
        if vfw != None:
            sw_version = vfw.get("title")
        else:
            sw_version = None
        return DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, self.router.mac)},
            sw_version=sw_version,
        )


class KeeneticRouterRcInterfaceCoordinator(DataUpdateCoordinator):
    def __init__(
            self,
            hass: HomeAssistant,
            router: Router,
            update_interval: int,
            entry: ConfigEntry
    ) -> None:
        self.router = router
        self.entry = entry
        self._host = entry.data[CONF_HOST]
        self.unique_id = f"{entry.unique_id}_rc_interface"
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{self._host}-rc-interface",
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self):
        """Asynchronous update of all data."""
        try:
            return await self.router.show_rc_interface()
        except Exception as err:
            _LOGGER.debug(f"{self.router.mac} UpdateFailed _async_update_data (err {err})")
            raise UpdateFailed(f"{self.router.mac} UpdateFailed {err}")

    @property
    def device_info(self) -> DeviceInfo:
        """Set device info."""
        return DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, self.router.mac)}
        )
