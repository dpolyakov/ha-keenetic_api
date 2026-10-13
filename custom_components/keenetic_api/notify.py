"""Send SMS through a Keenetic modem using a Home Assistant notify action."""

import asyncio
from typing import Any

from homeassistant.components.notify import ATTR_TARGET, BaseNotificationService
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import discovery
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_SMS_RECIPIENT, CROUTER, DOMAIN
from .coordinator import KeeneticRouterCoordinator
from .services import sms_interface


async def async_setup_modem_notify(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: KeeneticRouterCoordinator
) -> None:
    """Keep a separate notify action for each SMS-capable modem."""
    registered: set[str] = set()
    lock = asyncio.Lock()

    async def sync_actions() -> None:
        async with lock:
            if entry.entry_id not in hass.data.get(DOMAIN, {}):
                return
            data = coordinator.data
            if data is None:
                return
            for interface in set(data.sms) - registered:
                await discovery.async_load_platform(
                    hass, Platform.NOTIFY, DOMAIN,
                    {
                        "name": f"Keenetic SMS {entry.unique_id} {interface}",
                        "entry_id": entry.entry_id,
                        "interface": interface,
                    },
                    {},
                )
                registered.add(interface)

    @callback
    def on_update() -> None:
        hass.async_create_task(sync_actions())

    entry.async_on_unload(coordinator.async_add_listener(on_update))
    await sync_actions()


async def async_get_service(
    hass: HomeAssistant,
    config: ConfigType,
    discovery_info: DiscoveryInfoType | None = None,
) -> BaseNotificationService | None:
    if discovery_info is None:
        return None
    entry_id = discovery_info["entry_id"]
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return None
    interface = discovery_info.get("interface")
    service = KeeneticSmsNotificationService(
        hass, entry_id, entry.options.get(CONF_SMS_RECIPIENT), interface
    )
    entry.async_on_unload(service.async_unregister_services)
    return service


class KeeneticSmsNotificationService(BaseNotificationService):
    def __init__(
        self, hass: HomeAssistant, entry_id: str,
        default_recipient: str | None, interface: str | None = None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.default_recipient = default_recipient
        self.interface = interface

    async def async_send_message(self, message: str = "", **kwargs: Any) -> None:
        if not message:
            raise HomeAssistantError("SMS message must not be empty")
        targets = kwargs.get(ATTR_TARGET)
        if targets is None:
            targets = [self.default_recipient] if self.default_recipient else []
        elif isinstance(targets, str):
            targets = [targets]
        if not targets or any(not isinstance(target, str) or not target.strip() for target in targets):
            raise HomeAssistantError("SMS recipient is required")
        extra = kwargs.get("data") or {}
        if self.interface and extra.get("interface") not in (None, self.interface):
            raise HomeAssistantError("This SMS action is bound to another modem")
        interface = sms_interface(
            self.hass, self.entry_id,
            {"interface": self.interface} if self.interface else extra,
        )
        router = self.hass.data[DOMAIN][self.entry_id][CROUTER]
        for target in targets:
            try:
                await router.send_sms(interface, target.strip(), message)
            except Exception as err:
                raise HomeAssistantError("Keenetic SMS send failed") from err
