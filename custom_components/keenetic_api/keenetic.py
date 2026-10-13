"""The Keenetic API."""

from __future__ import annotations
from hashlib import md5, sha256
from collections.abc import Mapping
from typing import Literal, Any
import asyncio
import aiohttp
import logging
import math
import aiofiles.os
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime

_LOGGER = logging.getLogger(__name__)

@dataclass
class KeeneticFullData:
    show_system: dict[str, Any]
    show_ip_hotspot: DataDevice
    show_interface: dict[str, Any]
    show_rc_ip_static: dict[str, Any]
    show_associations: dict[str, Any]
    show_ip_hotspot_policy: dict[str, Any]
    priority_interface: dict[str, Any]
    show_rc_ip_http: dict[str, Any]
    show_rc_system_usb: dict[str, Any]
    show_media: dict[str, Any]
    stat_interface: dict[str, Any]
    show_pingcheck: dict[str, Any]
    modems: dict[str, KeeneticModemData]
    sms: dict[str, KeeneticSmsInbox] = field(default_factory=dict)

@dataclass
class KeeneticModemData:
    interface: str
    identity: str
    details: dict[str, Any]

@dataclass
class KeeneticSmsInbox:
    messages: list[dict[str, Any]]

    @property
    def unread_count(self) -> int:
        return sum(not message["read"] for message in self.messages)

    @property
    def latest_complete(self) -> dict[str, Any] | None:
        complete = [message for message in self.messages if message["complete"]]
        if not complete:
            return None
        def sort_key(message: dict[str, Any]) -> datetime:
            try:
                return parsedate_to_datetime(message["timestamp"]).replace(tzinfo=None)
            except (TypeError, ValueError, KeyError):
                return datetime.min
        return max(enumerate(complete), key=lambda item: (sort_key(item[1]), item[0]))[1]


class KeeneticSmsError(Exception):
    """SMS command failed or returned an invalid response."""


def is_sms_request(endpoint: str, payload: Any) -> bool:
    """Identify requests whose payload or response may contain SMS contents."""
    if endpoint.startswith("/rci/sms"):
        return True
    if isinstance(payload, dict):
        return "sms" in payload
    if isinstance(payload, list):
        return any(is_sms_request(endpoint, item) for item in payload)
    return False


def parse_sms_list(response: Any) -> KeeneticSmsInbox:
    """Normalize the Keenetic RCI message map without exposing partial text."""
    if not isinstance(response, dict) or not isinstance(response.get("messages", {}), dict):
        raise KeeneticSmsError("Invalid SMS list response")
    if response.get("messages-count", 0) and not response.get("messages"):
        raise KeeneticSmsError("SMS list contains no message details")
    messages = []
    for sms_id, item in response.get("messages", {}).items():
        if not isinstance(item, dict):
            continue
        parts = item.get("parts")
        total_parts = item.get("total-parts")
        if parts is None and total_parts is None:
            complete = True
        else:
            complete = (
                type(parts) is int
                and type(total_parts) is int
                and parts > 0
                and parts == total_parts
            )
        complete = complete and isinstance(item.get("text"), str)
        read = item.get("read")
        messages.append({
            "id": str(sms_id),
            "from": item.get("from"),
            "timestamp": item.get("timestamp"),
            "read": read is True or read == "yes",
            "parts": parts,
            "total_parts": total_parts,
            "complete": complete,
            "text": item.get("text") if complete else None,
        })
    return KeeneticSmsInbox(messages)


class SmsEventTracker:
    """Emit each newly completed SMS once, while baselining existing messages."""

    def __init__(self) -> None:
        self._seen: dict[str, dict[str, tuple[Any, Any]]] = {}

    def new_messages(self, modem: KeeneticModemData, inbox: KeeneticSmsInbox) -> list[dict[str, Any]]:
        completed = {
            message["id"]: (message["timestamp"], message["text"])
            for message in inbox.messages if message["complete"]
        }
        previous = self._seen.get(modem.identity)
        self._seen[modem.identity] = {**(previous or {}), **completed}
        if previous is None:
            return []
        return [
            message for message in inbox.messages
            if message["complete"] and previous.get(message["id"]) != completed[message["id"]]
        ]

@dataclass
class DataDevice():
    mac: str
    name: str
    hostname: str
    ip: str
    active: bool
    interface_id: str
    uptime: int
    rssi: str
    rxbytes: int
    txbytes: int

@dataclass
class DataPortForwarding():
    name: str
    interface: str
    protocol: str
    port: int
    end_port: str
    to_host: str
    index: str
    comment: str
    disable: bool = False

@dataclass
class DataRcInterface():
    id: str
    name_interface: str
    ssid: str
    password: str
    active: str
    rename: str
    description: str
    idle_timeout: int

@dataclass
class DataPingCheck():
    interface_name: str
    status: str
    # failcount: str
    # successcount: str

CONGIG_SAVE = {"system": {"configuration": {"save": {}}}}

INTERFACES_WIFI_NAME = {
    "WifiMaster0": "WiFi %s 2.4G",
    "WifiMaster1": "WiFi %s 5G",
}
INTERFACES_WIFI_NAME_LIST = {
    "WifiMaster0": "WiFi 2.4G",
    "WifiMaster1": "WiFi 5G",
}

LIST_INTERFACES = [
    "UsbModem",
    "Davicom",
    "UsbLte",
    "Yota",
    "PPPoE",
    "SSTP",
    "PPTP",
    "L2TP",
    "Wireguard",
    "OpenVPN",
    "EoIP",
    "TunnelSixInFour",
    "SSTPEthernet",
    "Proxy",
    "IPIP",
    "IKE",
    "Gre",
    "MapT",
    "DsLite",
    "UsbQmi",
]
LIST_INTERFACES_PUBLIC = [
    "GigabitEthernet",
    "Ethernet",
]

MOBILE_MODEM_TYPES = {"UsbLte", "UsbModem", "UsbQmi", "Yota"}


def is_mobile_modem(interface: dict[str, Any]) -> bool:
    """Recognize cellular USB interfaces without relying on their numbering."""
    traits = interface.get("traits") or []
    return interface.get("type") in MOBILE_MODEM_TYPES or (
        "Usb" in traits and "Mobile" in traits
    )


def modem_number(details: dict[str, Any], key: str) -> int | float | None:
    """Convert modem measurements, which RCI often returns as strings."""
    raw = details.get(key)
    if raw is None or raw == "":
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def modem_text(details: dict[str, Any], key: str) -> str | None:
    value = details.get(key)
    return str(value) if value is not None and value != "" else None


def modem_frequency(details: dict[str, Any], key: str) -> float | None:
    value = modem_number(details, key)
    return value / 1000 if value is not None else None


def modem_sinr(details: dict[str, Any]) -> int | float | None:
    key = "sinr" if details.get("sinr") not in (None, "") else "cinr"
    return modem_number(details, key)


def modem_carriers(details: dict[str, Any]) -> list[dict[str, Any]]:
    carriers = details.get("carrier") or {}
    if not isinstance(carriers, dict):
        return []
    return [{**data, "index": index} for index, data in carriers.items() if isinstance(data, dict)]

class Router:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        username="admin",
        password="admin",
        host="192.168.1.1",
        port: int = 80,
        ssl: bool | None = False,
        ):
        self._session = session
        self.host = host
        self.url_router = f'{host}:{port}'
        self._username = username
        self._password = password
        self.request_interface = {}
        self._modem_ids_by_interface: dict[str, str] = {}

        self._mac = ""
        self._serial_number = ""
        self._model = ""
        self._hw_type = ""
        self._hw_id = ""
        self._name_device = ""
        self._usb = []

    @property
    def mac(self):
        return self._mac
    @property
    def serial_number(self):
        return self._serial_number
    @property
    def model(self):
        return self._model
    @property
    def hw_type(self):
        return self._hw_type
    @property
    def hw_id(self):
        return self._hw_id
    @property
    def name_device(self):
        return self._name_device
    @property
    def usb(self):
        return self._usb


    async def async_setup_obj(self):
        await self.auth()

        data_show_identification = await self.show_identification()
        self._mac = data_show_identification["mac"]
        self._serial_number = data_show_identification["serial"]

        data_show_version = await self.show_version()
        self._model = data_show_version["model"]
        self._hw_id = data_show_version["hw_id"]
        self._name_device = data_show_version["device"]

        data_show_system_mode = await self.show_system_mode()
        self._hw_type = data_show_system_mode["active"]

        data_show_rc_system = await self.show_rc_system()
        self._usb = data_show_rc_system.get('usb', [])

        if self._hw_type == "router":
            # data_show_rc_interface_ip_global = await self.show_rc_interface_ip_global()
            data_show_interface = await self.show_interface()
            for interface, data_interface in data_show_interface.items():
                if (
                    (
                        data_interface.get('type', 'No') in LIST_INTERFACES
                    )
                    or #проверяем разные интерфейсы по разным спискам что бы исключить лишнее или системное (найти решение для точного списка)
                    (
                        data_interface.get('type', 'No') in LIST_INTERFACES_PUBLIC
                        and data_interface.get('security-level', False) == 'public'
                    )
                    or data_interface.get('global', False)
                    ):
                    self.request_interface[interface] = f"{data_interface['type']} {data_interface.get('description', '')}"
        _LOGGER.debug(f'{self._mac} request_interface - {self.request_interface}')
        return True


    async def async_download_file(self, download_url, folder):
        try:
            await aiofiles.os.makedirs(folder, exist_ok=True)
            async with self._session.get(download_url, timeout=180) as response:
                    if response.status != 200:
                        raise Exception('Got non-200 response!')
                    async with aiofiles.open(f"{folder}/{self._mac.replace(':', '')}-{response.content_disposition.filename}", 'wb') as file:
                        async for data, _ in response.content.iter_chunks():
                            await file.write(data)
        except asyncio.TimeoutError as err:
            raise Exception("TimeoutError") from err
        return True

    async def reguest_api(self, method: str, endpoint: str, json: Mapping[str, Any] | None = None, headers: str | None = None) -> tuple[aiohttp.ClientResponse]:
        url = self.url_router + endpoint
        try:
            if is_sms_request(endpoint, json):
                _LOGGER.debug('%s SMS request - %s', self._mac, endpoint)
            else:
                _LOGGER.debug(f'{self._mac} request - {endpoint} - {json}')
            async with self._session.request(method=method, url=url, json=json, headers=headers) as res:
                if res.status == 200 and res.content_type == 'application/json':
                    result = await res.json()
                elif res.status == 200 and res.content_type == 'application/javascript':
                    result = await res.text()
                    result = self.data_parser(result)
                else:
                    result = res
                _LOGGER.debug(f'{self._mac} status - {endpoint} {res.status}')
        except asyncio.TimeoutError as err:
            raise Exception("TimeoutError") from err
        return result



    async def api(self, method: str, endpoint: str, json: Mapping[str, Any] | None = {}):
        resp = await self.auth()
        return await self.reguest_api(method, endpoint, json)

    async def _sms_command(self, command: str, payload: dict[str, Any]) -> Any:
        response = await self.api("post", "/rci/", {"sms": {command: payload}})
        if getattr(response, "status", None) is not None:
            raise KeeneticSmsError(f"SMS {command} request failed")
        if not isinstance(response, dict):
            raise KeeneticSmsError(f"Invalid SMS {command} response")
        sms = response.get("sms")
        result = sms.get(command) if isinstance(sms, dict) else None
        if command == "list" and not isinstance(result, dict):
            raise KeeneticSmsError(f"Invalid SMS {command} response")
        if command != "list" and result is None:
            result = response
        if not isinstance(result, dict):
            raise KeeneticSmsError(f"Invalid SMS {command} response")
        statuses = result.get("status", [])
        if isinstance(statuses, dict):
            statuses = [statuses]
        elif isinstance(statuses, str):
            statuses = [{"status": statuses}]
        elif not isinstance(statuses, list):
            statuses = []
        if result.get("error") or any(
            item.get("status") == "error" for item in statuses if isinstance(item, dict)
        ):
            raise KeeneticSmsError(f"SMS {command} failed")
        return result

    async def list_sms(self, interface: str) -> KeeneticSmsInbox:
        return parse_sms_list(await self._sms_command("list", {"interface": interface}))

    async def mark_sms_read(self, interface: str, sms_id: str) -> None:
        await self._sms_command("read", {"interface": interface, "id": sms_id})

    async def send_sms(self, interface: str, to: str, message: str) -> None:
        await self._sms_command("send", {"interface": interface, "to": to, "message": message})

    async def auth(self):
        response = await self.reguest_api("get", "/auth")
        if response.status == 401:
            password = f"{self._username}:{response.headers['X-NDM-Realm']}:{self._password}"
            password = md5(password.encode("utf-8"))
            password = response.headers["X-NDM-Challenge"] + password.hexdigest()
            password = sha256(password.encode("utf-8")).hexdigest()
            response = await self.reguest_api("post", "/auth", json={"login": self._username, "password": password})
            if response.status == 401:
                raise Exception(response)
        return response.status == 200

    async def components_list(self):
        return await self.api("post", "/rci/components/list")

    async def release_notes(self, version: str, channel: str = "dev"):
        return await self.api("post", "/rci/webhelp/release-notes", {"version":f"{version}","locale":"ru","channel":f"{channel}"})

    async def show_identification(self):
        return await self.api("get", "/rci/show/identification")

    async def async_reboot(self):
        return await self.api("post", "/rci/system/reboot", {})

    async def async_update(self):
        return await self.api("post", "/rci/components/commit", {"reason": "manual"})

    async def async_backup(self, folder: str, type_fw: list = ["firmware", "config"]):
        if "firmware" in type_fw:
            await self.async_download_file(f"{self.url_router}/ci/firmware", folder)
        if "config" in type_fw:
            await self.async_download_file(f"{self.url_router}/ci/startup-config", folder)
        return True

    async def show_system(self):
        return await self.api("get", "/rci/show/system")

    async def show_system_mode(self):
        return await self.api("get", "/rci/show/system/mode")

    async def show_version(self):
        return await self.api("get", "/rci/show/version")

    async def ndm_components(self):
        return await self.api("get", "/ndmComponents.js")

    async def show_ip_hotspot(self):
        return await self.api("get", "/rci/show/ip/hotspot/host")

    async def show_interface(self):
        return await self.api("get", "/rci/show/interface")

    async def show_rc_interface(self):
        interfaces = await self.api("get", "/rci/show/rc/interface")
        interface_wifi = {}
        for interface, interf in interfaces.items():
            interface_main = interface.split('/')[0]
            if (
                interf.get("authentication", False)
                and interf.get("authentication").get("wpa-psk", False)
                and interf.get("authentication").get("wpa-psk").get("psk", False)
                ):
                psw = interf["authentication"]["wpa-psk"]["psk"]
            else:
                psw = None
            if interf.get('ssid', False):
                nm_inerface = (f"{INTERFACES_WIFI_NAME.get(interface_main, interface)}" % interf.get('ssid', "nameless"))
            elif "WifiMaster" in interface:
                nm_inerface = (f"{INTERFACES_WIFI_NAME_LIST.get(interface_main, interface)}")
            else:
                nm_inerface = interface
            interface_wifi[interface] = DataRcInterface(
                                                        interface,
                                                        nm_inerface,
                                                        interf.get("ssid", False),
                                                        psw,
                                                        interf.get("up", False),
                                                        interf.get("rename", None),
                                                        interf.get("description", None),
                                                        interf.get('idle-timeout', {}).get('idle-timeout', 600),
            )
        return interface_wifi

    async def show_associations(self):
        return await self.api("get", "/rci/show/associations")

    async def show_interface_stat(self, interface: str):
        return await self.api("get", f"/rci/show/interface/stat?name={interface}")

    async def show_rc_interface_ip_global(self):
        return await self.api("get", f"/rci/show/rc/interface/ip/global")

    async def show_rc_system(self):
        return await self.api("get", f"/rci/show/rc/system")

    async def ip_hotspot_host_list(self):
        return await self.api("get", "/rci/ip/hotspot/host")

    async def ip_policy_list(self):
        return await self.api("get", "/rci/ip/policy")

    async def ip_hotspot_host_policy(self, mac: str, access: str = "permit", policy: str = {"no": True}):
        data_send = {"mac": mac, access: True, "policy": policy}
        return await self.api("post", "/rci/ip/hotspot/host", data_send)

    async def turn_on_off_interface(self, interface: str, state: str):
        data_send = []
        data_send.append({"interface": {"name": interface, state: True}})
        if state == "up" and interface.startswith("WifiMaster") and "/" in interface:
            # Сделано как в вебке keeneticа для включения WiFi
            data_send.append({"interface": {"name": interface.split("/")[0], state: True}})
        data_send.append(CONGIG_SAVE)
        return await self.api("post", f"/rci/", data_send)

    async def turn_on_off_port_forwarding(self, port_forwarding: str, state: bool):
        data_send = [
            {
                "ip": {
                    "index": port_forwarding,
                    "static": {
                        "disable": not state
                    }
                }
            },
            CONGIG_SAVE
        ]
        return await self.api("post", f"/rci/", data_send)

    async def turn_on_off_web_configurator_access(self, state: bool):
        data_send = {"public": True, "ssl": True} if state else {"private": True}
        return await self.api("post", f"/rci/ip/http/security-level", data_send)

    async def turn_on_off_usb(self, state: bool, port: int):
        data_send = {"port": port, "power": {"shutdown": not state}}
        return await self.api("post", f"/rci/system/usb", data_send)

    async def set_clients_idle_timeout_wifi(self, interface: str, timeout: int):
        if not (60 <= timeout <= 2147483646):
            raise ValueError(f"Value {timeout} must be in the range from 60 to 2147483646")
        data_send = [
            {"interface": {interface: {"idle-timeout": int(timeout)}}},
            CONGIG_SAVE
        ]
        return await self.api("post", f"/rci/", data_send)

    def data_parser(self, data):
        new_data = {}
        data = data.replace('\n\t', '').replace('\n', '')
        data = data.split(';')
        for row in data:
            if row != '':
                row = row.split('=')
                nu = row[0].rstrip()
                te = row[1].lstrip()
                if '{' not in te:
                    te = te.replace('"', '')
                new_data[nu] = te
        return new_data

    async def show_stat_interface(self, stat_interfaces: list = None):
        if stat_interfaces is None:
            stat_interfaces = list(self.request_interface)
        if not stat_interfaces:
            return {}
        data_json_send=[]
        for row in stat_interfaces:
            data_json_send.append({"show": {"interface": {"stat": {"name": row}}}})
        try:
            data_show_stat_interface = await self.api("post", "/rci/", json=data_json_send)
        except Exception as err:
            _LOGGER.debug("%s interface statistics request failed: %s", self._mac, err)
            data_show_stat_interface = []
        if not isinstance(data_show_stat_interface, list):
            data_show_stat_interface = []
        stat_interface={}
        for idx, row in enumerate(stat_interfaces):
            try:
                stats = data_show_stat_interface[idx]['show']['interface']['stat']
                if not isinstance(stats, dict):
                    raise TypeError("statistics are not an object")
            except (IndexError, KeyError, TypeError):
                try:
                    response = await self.api("post", "/rci/", json=[data_json_send[idx]])
                    stats = response[0]['show']['interface']['stat']
                except Exception:
                    stats = {}
            stat_interface[row] = stats if isinstance(stats, dict) else {}
        return stat_interface

    async def show_modem_interfaces(self, interfaces: list[str]) -> dict[str, dict[str, Any]]:
        """Get full operational data for each modem using the named RCI query."""
        details = {}
        for name in interfaces:
            try:
                responses = await self.api(
                    "post", "/rci/", json=[{"show": {"interface": {"name": name}}}]
                )
            except Exception as err:
                _LOGGER.debug("%s modem %s request failed: %s", self._mac, name, err)
                continue
            if not isinstance(responses, list) or not responses:
                continue
            response = responses[0]
            if not isinstance(response, dict):
                continue
            show = response.get("show")
            interface = show.get("interface") if isinstance(show, dict) else None
            if isinstance(interface, dict) and interface.get("id", name) == name:
                details[name] = interface
        return details

    def modem_identity(self, interface: str, details: dict[str, Any]) -> str:
        """Use physical identity when present, retaining it through partial polls."""
        for field in ("imei", "serial"):
            if value := details.get(field):
                source = f"{field}:{value}"
                break
        else:
            source = self._modem_ids_by_interface.get(interface, f"interface:{interface}")
        self._modem_ids_by_interface[interface] = source
        digest = sha256(f"{self._mac}:{source}".encode()).hexdigest()[:32]
        return f"{self._mac}_modem_{digest}"

    async def custom_request(self):
        data_json_send=[]
        data_json_send.append({"show": {"system": {}}})
        data_json_send.append({"show": {"interface": {}}})
        data_json_send.append({"show": {"associations": {}}})
        data_json_send.append({"show": {"rc": {"system": {}}}})
        data_json_send.append({"show": {"rc": {"ip": {"http": {}}}}})

        if self.hw_type == "router":
            data_json_send.append({"show": {"ip": {"hotspot": {}}}})
            data_json_send.append({"show": {"rc": {"interface": {"ip": {"global": {}}}}}})
            data_json_send.append({"show": {"rc": {"ip": {"static": {}}}}})
            data_json_send.append({"show": {"rc": {"ip": {"hotspot": {}}}}})
            data_json_send.append({"show": {"ping-check": {}}})

        if self.usb:
            data_json_send.append({"show": {"media": {}}})

        full_info_other = await self.api("post", "/rci/", json=data_json_send)

        show_system = full_info_other[0]['show']['system']
        show_interface = full_info_other[1]['show']['interface']
        modem_interfaces = {
            name: data for name, data in show_interface.items()
            if isinstance(data, dict) and is_mobile_modem(data)
        }
        for name in set(self._modem_ids_by_interface) - set(modem_interfaces):
            del self._modem_ids_by_interface[name]
        detailed_modems = await self.show_modem_interfaces(list(modem_interfaces)) if modem_interfaces else {}
        modems = {}
        for name, base in modem_interfaces.items():
            details = {**base, **detailed_modems.get(name, {})}
            modems[name] = KeeneticModemData(name, self.modem_identity(name, details), details)
            self.request_interface[name] = f"{details.get('type', 'Mobile')} {details.get('description', '')}".strip()
        sms = {}
        for name in modems:
            try:
                sms[name] = await self.list_sms(name)
            except Exception as err:
                _LOGGER.debug("%s SMS polling failed for %s: %s", self._mac, name, type(err).__name__)
        show_associations = full_info_other[2]['show']['associations']
        show_rc_system_usb = full_info_other[3]['show']['rc']['system'].get('usb', [])
        show_rc_ip_http = full_info_other[4]['show']['rc']['ip']['http']

        show_ip_hotspot = {}
        show_rc_ip_static = {}
        show_ip_hotspot_policy = {}
        priority_interface = {}
        show_pingcheck = {}

        if self.hw_type == "router":
            data_show_ip_hotspot = full_info_other[5]['show']['ip']['hotspot']['host']
            for hotspot in data_show_ip_hotspot:
                show_ip_hotspot[hotspot["mac"]] = DataDevice(
                    hotspot.get('mac'),
                    hotspot.get('name'),
                    hotspot.get('hostname'),
                    hotspot.get('ip'),
                    hotspot.get('active'),
                    hotspot.get('interface', {"id": None}).get('id'),
                    hotspot.get('uptime'),
                    hotspot.get('rssi'),
                    hotspot.get('rxbytes'),
                    hotspot.get('txbytes'),
                )

            priority_interface = full_info_other[6]['show']['rc']['interface']['ip']['global']

            data_show_rc_ip_static = full_info_other[7]['show']['rc']['ip']['static']
            for port_frw in data_show_rc_ip_static:
                nm_pfrw = port_frw.get('comment', port_frw.get('index'))
                nm_pfrw = nm_pfrw if nm_pfrw != "" else port_frw.get('index')
                show_rc_ip_static[port_frw["index"]] = DataPortForwarding(
                    nm_pfrw,
                    port_frw.get('interface'),
                    port_frw.get('protocol'),
                    port_frw.get('port'),
                    port_frw.get('end-port', port_frw.get('port')),
                    port_frw.get('to-host'),
                    port_frw.get('index'),
                    port_frw.get('comment', None),
                    port_frw.get('disable', False),
                )

            data_show_ip_hotspot_policy = full_info_other[8]['show']['rc']['ip']['hotspot']['host']
            for hotspot_pl in data_show_ip_hotspot_policy:
                show_ip_hotspot_policy[hotspot_pl["mac"]] = hotspot_pl

            data_show_pingcheck = full_info_other[9]['show']['ping-check']['pingcheck']
            for pingcheck in data_show_pingcheck:
                if interfaces := pingcheck.get('interface', False):
                    for interface in interfaces:
                        interface_data = interfaces[interface]
                        interface_name = self.request_interface.get(interface, interface)
                        show_pingcheck[interface] = DataPingCheck(
                            interface_name,
                            interface_data.get('status', 'not ready'),
                        )

            show_media = full_info_other[10]['show'].get('media', {}) if self.usb else {}
        else:
            show_media = full_info_other[5]['show'].get('media', {}) if self.usb else {}

        stat_interface = await self.show_stat_interface([
            name for name in self.request_interface if name in show_interface
        ])

        return KeeneticFullData(
            show_system,
            show_ip_hotspot,
            show_interface,
            show_rc_ip_static,
            show_associations,
            show_ip_hotspot_policy,
            priority_interface,
            show_rc_ip_http,
            show_rc_system_usb,
            show_media,
            stat_interface,
            show_pingcheck,
            modems,
            sms,
            )
