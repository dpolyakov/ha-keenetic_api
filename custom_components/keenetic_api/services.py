import logging
from collections.abc import Mapping
from typing import Any
import voluptuous as vol

from homeassistant.helpers import device_registry as dr
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.json import json_loads

from .const import (
    DOMAIN,
    CROUTER,
    COORD_FULL,
)
from .keenetic import is_sms_request

_LOGGER = logging.getLogger(__name__)

SUPPORTED_SERVICES = [
    "request_api",
    "backup_router",
    "list_sms",
    "mark_sms_read",
]


async def async_setup_services(hass: HomeAssistant) -> None:

    services = {
        "request_api": request_api,
        "backup_router": backup_router,
        "list_sms": list_sms,
        "mark_sms_read": mark_sms_read,
    }

    async def async_call_keenetic_service(service_call: ServiceCall) -> None:
        current_entry_id = service_call.data.get("entry_id")
        if not current_entry_id and (device_id := service_call.data.get('device_id')):
            device_registry = dr.async_get(hass)
            if (device_entry := device_registry.async_get(device_id)) is None:
                raise ServiceValidationError(f"Некорректный device ID: {device_id}.", DOMAIN)
            for entry_id in device_entry.config_entries:
                if (entry := hass.config_entries.async_get_entry(entry_id)) is None:
                    continue
                if entry.domain == DOMAIN:
                    current_entry_id = entry_id
                    break
        if not current_entry_id or current_entry_id not in hass.data.get(DOMAIN, {}):
            raise ServiceValidationError("Нет параметра entry_id или device_id.", DOMAIN)
        return await services[service_call.service](hass, current_entry_id, service_call.data)

    for service in SUPPORTED_SERVICES:
        if hass.services.has_service(DOMAIN, service):
            continue
        schema = None
        if service in ("list_sms", "mark_sms_read"):
            fields = {
                vol.Optional("entry_id"): str,
                vol.Optional("device_id"): str,
                vol.Optional("interface"): str,
            }
            if service == "mark_sms_read":
                fields[vol.Required("sms_id")] = str
            schema = vol.Schema(fields)
        hass.services.async_register(
            DOMAIN,
            service,
            async_call_keenetic_service,
            schema=schema,
            supports_response=(SupportsResponse.ONLY if service == "list_sms" else
                               SupportsResponse.NONE if service == "mark_sms_read" else SupportsResponse.OPTIONAL),
        )


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    if hass.data.get(DOMAIN):
        return
    for service in SUPPORTED_SERVICES:
        hass.services.async_remove(DOMAIN, service)


async def request_api(hass: HomeAssistant, entry_id: str, data: Mapping[str, Any]):
    data_json = json_loads(data_json) if isinstance(data_json := data.get("data_json", []), str) else data_json
    response = await hass.data[DOMAIN][entry_id][CROUTER].api(data["method"], data["endpoint"], data_json)
    if is_sms_request(data["endpoint"], data_json):
        _LOGGER.debug("Services request_api SMS endpoint - %s", data["endpoint"])
    else:
        _LOGGER.debug(f'Services request_api endpoint - {data["endpoint"]}. data_json - {data_json}. response - {response}')
    return {"response": response}


async def backup_router(hass: HomeAssistant, entry_id: str, data: Mapping[str, Any]):
    response = await hass.data[DOMAIN][entry_id][CROUTER].async_backup(data["folder"], data["type"])
    return {"response": "success"}


def sms_interface(hass: HomeAssistant, entry_id: str, data: Mapping[str, Any]) -> str:
    """Select the modem explicitly when the router has multiple modems."""
    modems = hass.data[DOMAIN][entry_id][COORD_FULL].data.modems
    interface = data.get("interface")
    if interface:
        if interface not in modems:
            raise ServiceValidationError(f"Модем {interface} не найден.", DOMAIN)
        return interface
    if not modems:
        raise ServiceValidationError("SMS-модем не обнаружен.", DOMAIN)
    if len(modems) > 1:
        raise ServiceValidationError(
            f"Укажите interface для выбора SMS-модема: {', '.join(sorted(modems))}.", DOMAIN
        )
    return next(iter(modems))


async def list_sms(hass: HomeAssistant, entry_id: str, data: Mapping[str, Any]):
    interface = sms_interface(hass, entry_id, data)
    inbox = await hass.data[DOMAIN][entry_id][CROUTER].list_sms(interface)
    return {"messages": inbox.messages}


async def mark_sms_read(hass: HomeAssistant, entry_id: str, data: Mapping[str, Any]):
    interface = sms_interface(hass, entry_id, data)
    await hass.data[DOMAIN][entry_id][CROUTER].mark_sms_read(interface, data["sms_id"])
    await hass.data[DOMAIN][entry_id][COORD_FULL].async_request_refresh()
