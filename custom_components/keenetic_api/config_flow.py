"""The Keenetic API Config flow."""

import logging
import voluptuous as vol
from typing import Any
import operator

from homeassistant.config_entries import (
    ConfigEntry,
    OptionsFlowWithReload,
    ConfigFlowResult,
    ConfigFlow,
)
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import format_mac
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SSL,
    CONF_VERIFY_SSL,
    CONF_USERNAME,
    CONF_PORT,
)

from . import get_api
from .const import (
    DOMAIN,
    COORD_FULL,
)
from .keenetic import Router
from .const import (
    DEFAULT_SCAN_INTERVAL, 
    MIN_SCAN_INTERVAL,
    CONF_CLIENTS_SELECT_POLICY,
    CONF_CREATE_ALL_CLIENTS_POLICY,
    CONF_CREATE_DT,
    CONF_CREATE_PORT_FRW,
    DEFAULT_BACKUP_TYPE_FILE,
    CONF_BACKUP_TYPE_FILE,
    CONF_SELECT_CREATE_DT,
    CONF_SMS_RECIPIENT,
)

_LOGGER = logging.getLogger(__name__)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME, default='admin'): cv.string,
        vol.Required(CONF_PASSWORD, default=''): cv.string,
        vol.Required(CONF_HOST, default='http://192.168.1.1'): str,
        vol.Required(CONF_PORT, default=80): int,
        vol.Required(CONF_SSL, default=False): bool,
    }
)


class KeeneticOptionsFlowHandler(OptionsFlowWithReload):
    """Handle a options flow for Keenetic."""

    def __init__(self):
        """Initialize Keenetic options flow."""
        self._conf_app_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self.config_entry.entry_id not in self.hass.data[DOMAIN]:
            return self.async_abort(reason="integration_not_setup")
        self.router = self.hass.data[DOMAIN][self.config_entry.entry_id][COORD_FULL].router

        if self.router.hw_type != "router":
            return await self.async_step_configure_other()

        return await self.async_step_configure_router()


    async def async_step_configure_router(
        self, 
        user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        data_clients = await self.router.show_ip_hotspot()
        _LOGGER.debug(f'CONF_CLIENTS_SELECT_POLICY - {self.config_entry.options.get(CONF_CLIENTS_SELECT_POLICY, [])}')
        clients = {
            client['mac']: f"{client['name'] or client['hostname']} ({client['mac']})"
            for client in data_clients
        }
        clients_policy = clients
        clients_policy |= {
            mac: f"Unknown ({mac})"
            for mac in self.config_entry.options.get(CONF_CLIENTS_SELECT_POLICY, [])
            if mac not in clients
        }
        clients_dt = clients
        clients_dt |= {
            mac: f"Unknown ({mac})"
            for mac in self.config_entry.options.get(CONF_SELECT_CREATE_DT, [])
            if mac not in clients
        }

        return self.async_show_form(
            step_id="configure_router",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(cv.positive_int, vol.Clamp(min=MIN_SCAN_INTERVAL)),
                    vol.Optional(
                        CONF_SMS_RECIPIENT,
                        default=self.config_entry.options.get(CONF_SMS_RECIPIENT, ""),
                    ): cv.string,
                    vol.Optional(
                        CONF_CREATE_ALL_CLIENTS_POLICY,
                        default=self.config_entry.options.get(
                            CONF_CREATE_ALL_CLIENTS_POLICY, False
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_CLIENTS_SELECT_POLICY,
                        default=self.config_entry.options.get(CONF_CLIENTS_SELECT_POLICY, []),
                    ): cv.multi_select(
                        dict(sorted(clients_policy.items(), key=operator.itemgetter(1)))
                    ),
                    vol.Optional(
                        CONF_CREATE_DT,
                        default=self.config_entry.options.get(
                            CONF_CREATE_DT, False
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_SELECT_CREATE_DT,
                        default=self.config_entry.options.get(CONF_SELECT_CREATE_DT, []),
                    ): cv.multi_select(
                        dict(sorted(clients_dt.items(), key=operator.itemgetter(1)))
                    ),
                    vol.Optional(
                        CONF_CREATE_PORT_FRW,
                        default=self.config_entry.options.get(
                            CONF_CREATE_PORT_FRW, False
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_BACKUP_TYPE_FILE,
                        default=self.config_entry.options.get(CONF_BACKUP_TYPE_FILE, DEFAULT_BACKUP_TYPE_FILE),
                    ): cv.multi_select([
                        "config",
                        "firmware",
                    ]),
                }
            ),
            last_step=False,
        )

    async def async_step_configure_other(
        self, 
        user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="configure_other",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(cv.positive_int, vol.Clamp(min=MIN_SCAN_INTERVAL)),
                    vol.Optional(
                        CONF_SMS_RECIPIENT,
                        default=self.config_entry.options.get(CONF_SMS_RECIPIENT, ""),
                    ): cv.string,
                    vol.Optional(
                        CONF_BACKUP_TYPE_FILE,
                        default=self.config_entry.options.get(CONF_BACKUP_TYPE_FILE, DEFAULT_BACKUP_TYPE_FILE),
                    ): cv.multi_select([
                        "config",
                        "firmware",
                    ]),
                }
            ),
            last_step=False,
        )

class KeeneticConfigFlow(ConfigFlow, domain=DOMAIN):

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        title = ""
        if user_input is not None:
            try:
                router = await get_api(self.hass, user_input)
                keen = await router.show_version()

                title = f"{keen['vendor']} {keen['model']} {user_input['host']}"

            except Exception as error:
                _LOGGER.error('Keenetic Api Integration Exception - {}'.format(error))
                errors['base'] = str(error)
            if title != "":
                unique_id: str = f"{keen['vendor']} {keen['device']} {format_mac(router.mac)[-8:].replace(':', '')}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)


    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> KeeneticOptionsFlowHandler:
        """Get the options flow for this handler."""
        return KeeneticOptionsFlowHandler()
