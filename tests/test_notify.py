"""Modem-scoped notify behavior without a Home Assistant installation."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class FakeNotificationService:
    async def async_unregister_services(self):
        self.unregistered = getattr(self, "unregistered", 0) + 1


def load_notify():
    name = "sms_notify_test.notify"
    path = Path(__file__).resolve().parents[1] / "custom_components/keenetic_api/notify.py"
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    package = module("sms_notify_test")
    package.__path__ = []
    fake_modules = {
        "sms_notify_test": package,
        "sms_notify_test.const": module("sms_notify_test.const", CONF_SMS_RECIPIENT="sms_recipient", CROUTER="router", DOMAIN="keenetic_api"),
        "sms_notify_test.coordinator": module("sms_notify_test.coordinator", KeeneticRouterCoordinator=object),
        "sms_notify_test.services": module("sms_notify_test.services", sms_interface=lambda hass, entry_id, data: data["interface"]),
        "homeassistant": module("homeassistant"),
        "homeassistant.components": module("homeassistant.components"),
        "homeassistant.components.notify": module("homeassistant.components.notify", ATTR_TARGET="target", BaseNotificationService=FakeNotificationService),
        "homeassistant.config_entries": module("homeassistant.config_entries", ConfigEntry=object),
        "homeassistant.const": module("homeassistant.const", Platform=SimpleNamespace(NOTIFY="notify")),
        "homeassistant.core": module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn),
        "homeassistant.exceptions": module("homeassistant.exceptions", HomeAssistantError=RuntimeError),
        "homeassistant.helpers": module("homeassistant.helpers"),
        "homeassistant.helpers.discovery": module("homeassistant.helpers.discovery", async_load_platform=None),
        "homeassistant.helpers.typing": module("homeassistant.helpers.typing", ConfigType=dict, DiscoveryInfoType=dict),
    }
    fake_modules["homeassistant.helpers"].discovery = fake_modules["homeassistant.helpers.discovery"]
    with patch.dict(sys.modules, fake_modules):
        sys.modules[name] = result
        spec.loader.exec_module(result)
    return result


notify = load_notify()


class NotifyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []
        self.discovered = []
        self.services = {}
        self.tasks = []
        self.callbacks = []

        async def send_sms(interface, target, message):
            self.sent.append((interface, target, message))

        self.entry = SimpleNamespace(
            entry_id="entry-1", unique_id="my_router", options={"sms_recipient": "+70000000000"},
            async_on_unload=self.callbacks.append,
        )
        self.hass = SimpleNamespace(
            data={"keenetic_api": {"entry-1": {"router": SimpleNamespace(send_sms=send_sms)}}},
            config_entries=SimpleNamespace(async_get_entry=lambda entry_id: self.entry),
            async_create_task=self.create_task,
        )
        self.coordinator = SimpleNamespace(
            data=SimpleNamespace(modems={"UsbLte0": object(), "UsbLte1": object()}, sms={"UsbLte0": object()}),
            async_add_listener=lambda listener: self.add_listener(listener),
        )

        async def discover(hass, platform, domain, info, config):
            self.discovered.append(info)
            service = await notify.async_get_service(hass, config, info)
            self.services[info["interface"]] = service
            service._service_name = info["name"]

        self.original_discover = notify.discovery.async_load_platform
        notify.discovery.async_load_platform = discover

    async def asyncTearDown(self):
        notify.discovery.async_load_platform = self.original_discover

    def create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    def add_listener(self, listener):
        self.listener = listener
        return lambda: None

    async def update(self):
        self.listener()
        await asyncio.gather(*self.tasks)
        self.tasks.clear()

    async def test_registers_only_sms_modem_without_recreating_action(self):
        await notify.async_setup_modem_notify(self.hass, self.entry, self.coordinator)
        self.assertEqual([item["interface"] for item in self.discovered], ["UsbLte0"])
        service = self.services["UsbLte0"]
        await service.async_send_message("Привет", target=["+79990000000"])
        self.assertEqual(self.sent, [("UsbLte0", "+79990000000", "Привет")])
        with self.assertRaises(RuntimeError):
            await service.async_send_message(
                "Привет", target=["+79990000000"], data={"interface": "UsbLte1"}
            )
        self.assertEqual(len(self.sent), 1)

        self.coordinator.data.sms = {}
        await self.update()
        self.assertEqual(len(self.discovered), 1)

        self.coordinator.data.modems.pop("UsbLte0")
        await self.update()
        self.coordinator.data.modems["UsbLte0"] = object()
        self.coordinator.data.sms["UsbLte0"] = object()
        await self.update()
        self.assertIs(self.services["UsbLte0"], service)
        self.assertEqual(len(self.discovered), 1)

    async def test_new_sms_modem_gets_action_without_router_reload(self):
        await notify.async_setup_modem_notify(self.hass, self.entry, self.coordinator)
        self.coordinator.data.sms["UsbLte1"] = object()
        await self.update()
        self.assertEqual({item["interface"] for item in self.discovered}, {"UsbLte0", "UsbLte1"})


if __name__ == "__main__":
    unittest.main()
