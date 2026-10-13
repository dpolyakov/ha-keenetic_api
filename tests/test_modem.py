"""USB modem polling tests without a Home Assistant installation."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch


def load_keenetic():
    aiofiles = ModuleType("aiofiles")
    aiofiles.os = ModuleType("aiofiles.os")
    path = Path(__file__).resolve().parents[1] / "custom_components/keenetic_api/keenetic.py"
    spec = importlib.util.spec_from_file_location("keenetic_for_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.dict(sys.modules, {
        "aiohttp": ModuleType("aiohttp"),
        "aiofiles": aiofiles,
        "aiofiles.os": aiofiles.os,
    }):
        spec.loader.exec_module(module)
    return module


keenetic = load_keenetic()


def bulk_response(interfaces):
    return [
        {"show": {"system": {}}},
        {"show": {"interface": interfaces}},
        {"show": {"associations": {}}},
        {"show": {"rc": {"system": {"usb": []}}}},
        {"show": {"rc": {"ip": {"http": {}}}}},
        {"show": {"ip": {"hotspot": {"host": []}}}},
        {"show": {"rc": {"interface": {"ip": {"global": {}}}}}},
        {"show": {"rc": {"ip": {"static": []}}}},
        {"show": {"rc": {"ip": {"hotspot": {"host": []}}}}},
        {"show": {"ping-check": {"pingcheck": []}}},
    ]


class ModemPollingTest(unittest.TestCase):
    def setUp(self):
        self.router = keenetic.Router(None)
        self.router._mac = "00:11:22:33:44:55"
        self.router._hw_type = "router"
        self.interfaces = {}
        self.details = {}
        self.sms = {}
        self.requests = []

        async def api(method, endpoint, json=None):
            self.requests.append(json)
            if isinstance(json, dict) and "sms" in json:
                interface = json["sms"]["list"]["interface"]
                if interface not in self.sms:
                    raise ValueError("SMS unavailable")
                return {"sms": {"list": self.sms[interface]}}
            first = json[0]
            if "system" in first.get("show", {}):
                return bulk_response(self.interfaces)
            if "stat" in first.get("show", {}).get("interface", {}):
                return [
                    {"show": {"interface": {"stat": {"rxbytes": 1024, "txbytes": 2048}}}}
                    for _ in json
                ]
            return [
                {"show": {"interface": self.details[name]}} if name in self.details else {"error": "unsupported"}
                for name in (item["show"]["interface"]["name"] for item in json)
            ]

        self.router.api = api

    def poll(self):
        return asyncio.run(self.router.custom_request())

    def test_no_modem(self):
        self.interfaces = {"Bridge0": {"type": "Bridge", "id": "Bridge0"}}
        self.assertEqual(self.poll().modems, {})
        self.assertFalse(any("bands" in str(request) for request in self.requests))

    def test_multiple_modems_and_partial_detail(self):
        self.interfaces = {
            "UsbQmi1": {"id": "UsbQmi1", "type": "UsbQmi", "connected": "no"},
            "UsbLte0": {"id": "UsbLte0", "type": "UsbLte", "connected": "yes"},
        }
        self.details = {"UsbLte0": {"id": "UsbLte0", "imei": "test-imei", "cinr": "25"}}
        data = self.poll()
        self.assertEqual(set(data.modems), {"UsbLte0", "UsbQmi1"})
        self.assertEqual(data.modems["UsbLte0"].details["cinr"], "25")
        self.assertEqual(data.modems["UsbQmi1"].details["connected"], "no")
        self.assertNotIn("test-imei", data.modems["UsbLte0"].identity)
        self.assertEqual(set(data.stat_interface), {"UsbLte0", "UsbQmi1"})

    def test_hotplug_unplug_and_replace(self):
        self.assertEqual(self.poll().modems, {})
        self.interfaces = {"UsbLte0": {"id": "UsbLte0", "type": "UsbLte"}}
        self.details = {"UsbLte0": {"id": "UsbLte0", "imei": "first"}}
        first = self.poll().modems["UsbLte0"].identity
        self.details = {"UsbLte0": {"id": "UsbLte0"}}
        self.assertEqual(self.poll().modems["UsbLte0"].identity, first)
        self.details = {"UsbLte0": {"id": "UsbLte0", "imei": "second"}}
        self.assertNotEqual(self.poll().modems["UsbLte0"].identity, first)
        self.interfaces = {}
        self.assertEqual(self.poll().modems, {})

    def test_detailed_request_failure_keeps_bulk_state(self):
        self.interfaces = {"UsbLte0": {"id": "UsbLte0", "type": "UsbLte", "connected": "yes"}}
        original_api = self.router.api

        async def api(method, endpoint, json=None):
            if "name" in json[0].get("show", {}).get("interface", {}):
                raise TimeoutError("modem detail unavailable")
            return await original_api(method, endpoint, json)

        self.router.api = api
        data = self.poll()
        self.assertEqual(data.modems["UsbLte0"].details["connected"], "yes")

    def test_sms_polling_is_separate_from_modem_health(self):
        self.interfaces = {"UsbQmi0": {"id": "UsbQmi0", "type": "UsbQmi"}}
        self.assertEqual(self.poll().sms, {})
        self.sms["UsbQmi0"] = {"messages-count": 1, "messages": {
            "sim-1": {"read": False, "parts": 1, "total-parts": 1, "text": "Привет"},
        }}
        data = self.poll()
        self.assertEqual(data.sms["UsbQmi0"].unread_count, 1)
        self.assertEqual(data.modems["UsbQmi0"].interface, "UsbQmi0")

    def test_statistics_error_on_one_modem_keeps_other_statistics(self):
        self.interfaces = {
            "UsbQmi1": {"id": "UsbQmi1", "type": "UsbQmi"},
            "UsbLte0": {"id": "UsbLte0", "type": "UsbLte"},
        }
        original_api = self.router.api

        async def api(method, endpoint, json=None):
            stat = json[0].get("show", {}).get("interface", {}).get("stat")
            if stat and (len(json) > 1 or stat["name"] == "UsbQmi1"):
                raise TimeoutError("statistics unavailable")
            return await original_api(method, endpoint, json)

        self.router.api = api
        data = self.poll()
        self.assertEqual(data.stat_interface["UsbQmi1"], {})
        self.assertEqual(data.stat_interface["UsbLte0"]["rxbytes"], 1024)

    def test_signal_conversion_and_sinr_precedence(self):
        self.assertEqual(keenetic.modem_sinr({"sinr": "19", "cinr": "25"}), 19)
        self.assertEqual(keenetic.modem_sinr({"cinr": "25"}), 25)
        self.assertEqual(keenetic.modem_sinr({"sinr": "", "cinr": "25"}), 25)
        self.assertIsNone(keenetic.modem_sinr({}))
        self.assertIsNone(keenetic.modem_sinr({"cinr": "invalid"}))
        self.assertEqual(keenetic.modem_number({"rssi": "-58"}, "rssi"), -58)
        self.assertEqual(keenetic.modem_frequency({"dl-freq": 2649800}, "dl-freq"), 2649.8)

    def test_detects_other_mobile_usb_types(self):
        self.assertTrue(keenetic.is_mobile_modem({"type": "UsbModem"}))
        self.assertTrue(keenetic.is_mobile_modem({"type": "Yota"}))
        self.assertTrue(keenetic.is_mobile_modem({"type": "Unknown", "traits": ["Usb", "Mobile"]}))
        self.assertFalse(keenetic.is_mobile_modem({"type": "Davicom", "traits": ["Usb", "Ethernet"]}))


if __name__ == "__main__":
    unittest.main()
