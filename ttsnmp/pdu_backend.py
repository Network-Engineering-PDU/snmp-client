"""HTTP adapter for the local Network Engineering PDU API.

The SNMP helper is a separate process from ``ne-fw-api``.  This module keeps
all knowledge of that API out of the MIB implementation and converts partial
or failed responses into unavailable values instead of raising in the polling
thread.
"""

import logging
import threading
from typing import Any, Dict, List, Optional

import requests

from .pdu_api_error import PduApiError


logger = logging.getLogger(__name__)


class PduBackend:
    def __init__(self, base_url: str = "http://localhost:8001",
                 sensor_base_url: str = "http://localhost:8000/api",
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.sensor_base_url = sensor_base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.request_lock = threading.Lock()

    def _request_from(self, base_url: str, method: str, path: str,
                      payload: Optional[dict] = None) -> Any:
        url = base_url + "/" + path.lstrip("/")
        try:
            with self.request_lock:
                response = self.session.request(
                    method, url, json=payload, timeout=self.timeout
                )
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PduApiError(f"{method} {path} failed: {exc}") from exc

    def _request(self, method: str, path: str,
                 payload: Optional[dict] = None) -> Any:
        return self._request_from(self.base_url, method, path, payload)

    def _sensor_request(self, method: str, path: str) -> Any:
        return self._request_from(self.sensor_base_url, method, path)

    def _optional_get(self, path: str, default: Any) -> Any:
        try:
            return self._request("GET", path)
        except PduApiError as exc:
            logger.warning("%s", exc)
            return default

    def _optional_sensor_get(self, path: str, default: Any) -> Any:
        try:
            return self._sensor_request("GET", path)
        except PduApiError as exc:
            logger.warning("%s", exc)
            return default

    def _map_sensors(self, sensor_rows: Any) -> List[dict]:
        if not isinstance(sensor_rows, list) or not sensor_rows:
            return []

        live_response = self._optional_sensor_get("sensors-scan/live/", {})
        live_devices = (
            live_response.get("devices", [])
            if isinstance(live_response, dict) else []
        )
        live_by_mac = {}
        if isinstance(live_devices, list):
            for device in live_devices:
                if not isinstance(device, dict):
                    continue
                live_mac = "".join(
                    char for char in str(device.get("mac", ""))
                    if char.isalnum()
                ).upper()
                if live_mac:
                    live_by_mac[live_mac] = device

        sensors = []
        for index, row in enumerate(sensor_rows[:32], start=1):
            if not isinstance(row, dict):
                continue
            readings = row.get("last_data")
            if not isinstance(readings, dict):
                readings = {}
            mac_address = str(row.get("mac_address", ""))
            mac = "".join(
                char for char in str(row.get("mac_address", ""))
                if char.isalnum()
            ).upper()
            live = live_by_mac.get(mac, {})
            sensor_id = mac[-6:]
            api_id = row.get("id", index)
            temperature = live.get(
                "temperature_c", readings.get("temperature")
            )
            humidity = live.get("humidity_pct", readings.get("humidity"))
            rssi = live.get("rssi", readings.get("rssi"))
            battery_mv = live.get("battery_mv")
            if battery_mv is None:
                battery = readings.get("battery")
                try:
                    battery_number = float(battery)
                    battery_mv = round(
                        battery_number * 1000
                        if abs(battery_number) < 20 else battery_number
                    )
                except (TypeError, ValueError):
                    battery_mv = None
            sensor_type = (
                str(live.get("kind")) if live.get("kind") else
                "MST01" if (temperature is not None or humidity is not None)
                else str(row.get("name") or "Sensor")
            )
            sensors.append({
                "number": f"{index}-S{api_id}",
                "api_id": api_id,
                "type": sensor_type,
                "id": sensor_id,
                "mac": mac_address.upper(),
                "location": "",
                "value": "",
                "description": str(row.get("name") or sensor_id),
                "name": str(row.get("name") or sensor_id),
                "temperature": temperature,
                "humidity": humidity,
                "rssi": rssi,
                "battery_mv": battery_mv,
                "data_datetime": readings.get("data_datetime", ""),
            })
        return sensors

    @staticmethod
    def _safe_snmp_settings(detailed: dict, services: dict) -> dict:
        """Return only non-secret SNMP configuration for MIB exposure."""
        trap = detailed.get("trap")
        trap = trap if isinstance(trap, dict) else {}
        v3 = detailed.get("snmp_v3")
        v3 = v3 if isinstance(v3, dict) else {}
        return {
            "enabled": services.get("snmp"),
            "port": detailed.get("port"),
            "version": detailed.get("version"),
            "set_enabled": detailed.get("set_enabled"),
            "traps_enabled": trap.get("alarm"),
            "trap_managers": [
                {
                    "name": trap.get(f"manager_{index}_name"),
                    "address": trap.get(f"manager_{index}_ip"),
                }
                for index in range(1, 5)
            ],
            "v3_user": v3.get("usm_user"),
            "v3_security_level": v3.get("security_level"),
            "v3_access_right": v3.get("access_right"),
            "v3_auth_algorithm": v3.get("auth_algorithm"),
            "v3_privacy_algorithm": v3.get("privacy_algorithm"),
            "v3_configured": bool(v3.get("usm_user")),
        }

    def snapshot(self) -> Dict[str, Any]:
        system = self._request("GET", "settings/system-info")
        pdu_info = self._request("GET", "settings/pdu-info")
        if not isinstance(system, dict) or not isinstance(pdu_info, dict):
            raise PduApiError("PDU API returned an invalid system snapshot")
        license_info = self._optional_get(
            "settings/license", {"type_id": "A1"}
        )
        if not isinstance(license_info, dict):
            license_info = {"type_id": "A1"}
        license_type = license_info.get("type_id")
        metering_available = bool(license_info.get(
            "outlet_metering_licensed",
            license_type in ("A2", "B2"),
        ))
        relay_available = bool(license_info.get(
            "outlet_switch_licensed",
            license_type in ("B1", "B2"),
        ))
        license_info = dict(license_info)
        license_info["wifi_licensed"] = bool(
            license_info.get("wifi_licensed", False)
        )
        license_info["outlet_switch_licensed"] = relay_available
        license_info["outlet_metering_licensed"] = metering_available
        nms = self._optional_get("settings/snmp-nms", {})
        # Input topology is part of the PDU identity and is useful even on a
        # licence that does not expose electrical metering.
        switches = self._optional_get("inputs/switches", {})
        detailed = self._optional_get("network/snmp/detailed-settings", {})
        basic = self._optional_get("network/snmp/settings", {})
        network = self._optional_get("network/interfaces", {})
        network_info = self._optional_get("network/info", {})
        services = self._optional_get("network/services", {})
        ntp = self._optional_get("settings/ntp", {})
        modbus = self._optional_get("settings/modbus", {})
        email_web = self._optional_get("email-web", {})
        bluetooth = self._optional_get("settings/bluetooth", {})
        sensor_rows = self._optional_sensor_get("sensors-data/", [])
        nms = nms if isinstance(nms, dict) else {}
        switches = switches if isinstance(switches, dict) else {}
        detailed = detailed if isinstance(detailed, dict) else {}
        basic = basic if isinstance(basic, dict) else {}
        network = network if isinstance(network, dict) else {}
        network_info = (
            network_info if isinstance(network_info, dict) else {}
        )
        services = services if isinstance(services, dict) else {}
        ntp = ntp if isinstance(ntp, dict) else {}
        modbus = modbus if isinstance(modbus, dict) else {}
        email_web = email_web if isinstance(email_web, dict) else {}
        bluetooth = bluetooth if isinstance(bluetooth, dict) else {}
        trap = detailed.get("trap")
        if not isinstance(trap, dict):
            trap = {}
        else:
            trap = dict(trap)
        trap["alarm"] = (
            bool(trap.get("alarm", False))
            and bool(basic.get("trap_alarm", True))
        )
        detailed = dict(detailed)
        detailed["trap"] = trap
        detailed["refresh_period"] = basic.get("refresh_period", 120)

        inputs: List[Optional[dict]] = []
        if metering_available:
            for line_id in range(6):
                inputs.append(self._optional_get(
                    f"inputs/{line_id}/data", None
                ))

        outlet_metadata = self._optional_get("outputs/", [])
        metadata_by_line = {}
        if isinstance(outlet_metadata, list):
            for item in outlet_metadata:
                if not isinstance(item, dict):
                    continue
                try:
                    line_id = int(item.get("line_id"))
                except (TypeError, ValueError):
                    continue
                metadata_by_line[line_id] = item

        outlets = []
        try:
            configured_outlets = int(pdu_info.get("outlet_count", 0))
        except (TypeError, ValueError):
            configured_outlets = 0
        outlet_count = min(max(configured_outlets, 0), 24)
        for line_id in range(outlet_count):
            data = (
                self._optional_get(f"outputs/{line_id}/data", None)
                if metering_available else None
            )
            status = self._optional_get(
                f"outputs/{line_id}/switch-status", {}
            )
            metadata = metadata_by_line.get(line_id + 1, {})
            outlets.append({
                "line_id": line_id,
                "number": str(line_id + 1),
                "description": metadata.get("name", f"Output {line_id + 1}"),
                "socket": metadata.get(
                    "socket_type",
                    data.get("conn", "") if isinstance(data, dict) else "",
                ),
                "low_limit": metadata.get("low_limit"),
                "high_limit": metadata.get("high_limit"),
                "on": status.get("switch_status")
                if isinstance(status, dict) else None,
                "relay_writable": relay_available,
                "data": data,
            })

        return {
            "system": system,
            "pdu_info": pdu_info,
            "license": license_info,
            "nms": nms,
            "switches": switches,
            "inputs": inputs,
            # The current hardware API represents one local PDU.  The MIB
            # manager accepts four lists so future controllers can add the
            # remaining units without changing OID logic.
            "pdus": [outlets, [], [], []],
            # A power-summary row represents the PDU itself, not an outlet.
            # The local controller is itself PDU row 1. Keep its summary
            # present even when no outlet modules are installed or licensed;
            # unsupported measurements are represented by the MIB's -1.
            "summary_count": 1,
            "sensors": self._map_sensors(sensor_rows),
            "communications": {
                "network": network,
                "network_info": network_info,
                "services": services,
                "snmp": self._safe_snmp_settings(detailed, services),
                "ntp": ntp,
                "modbus": modbus,
                "email_web": email_web,
                # Pairing passkeys and other transient secrets are never
                # copied into the SNMP snapshot.
                "bluetooth": {
                    "controller_mac": bluetooth.get("controller_mac"),
                    "name": bluetooth.get("name"),
                    "powered": bluetooth.get("powered"),
                    "pairable": bluetooth.get("pairable"),
                    "discoverable": bluetooth.get("discoverable"),
                    "discovering": bluetooth.get("discovering"),
                    "device_count": len(bluetooth.get("devices", []))
                    if isinstance(bluetooth.get("devices"), list) else None,
                },
            },
            "trap_settings": detailed,
        }

    def set_outlet(self, line_id: int, enabled: bool) -> None:
        self._request(
            "PUT",
            f"outputs/{line_id}/switch-status",
            {"switch_status": bool(enabled)},
        )
