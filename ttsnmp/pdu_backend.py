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


logger = logging.getLogger(__name__)


class PduApiError(RuntimeError):
    pass


class PduBackend:
    def __init__(self, base_url: str = "http://localhost:8001",
                 sensor_base_url: str = "http://localhost:8000/api",
                 timeout: float = 3.0):
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

    @staticmethod
    def _map_sensors(sensor_rows: Any) -> List[dict]:
        if not isinstance(sensor_rows, list):
            return []

        sensors = []
        for index, row in enumerate(sensor_rows[:32], start=1):
            if not isinstance(row, dict):
                continue
            readings = row.get("last_data")
            if not isinstance(readings, dict):
                readings = {}
            mac = "".join(
                char for char in str(row.get("mac_address", ""))
                if char.isalnum()
            ).upper()
            sensor_id = mac[-6:]
            api_id = row.get("id", index)
            sensor_type = (
                "Temperature/Humidity"
                if (readings.get("temperature") is not None
                    or readings.get("humidity") is not None)
                else str(row.get("name") or "Sensor")
            )
            sensors.append({
                "number": f"{index}-S{api_id}",
                "type": sensor_type,
                "id": sensor_id,
                "location": "",
                "value": "",
                "description": str(row.get("name") or sensor_id),
                "temperature": readings.get("temperature"),
                "humidity": readings.get("humidity"),
                "wind": None,
            })
        return sensors

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
        metering_available = license_type in ("A2", "B2")
        relay_available = license_type in ("B1", "B2")
        nms = self._optional_get("settings/snmp-nms", {})
        switches = (
            self._optional_get("inputs/switches", {})
            if metering_available else {}
        )
        detailed = self._optional_get("network/snmp/detailed-settings", {})
        basic = self._optional_get("network/snmp/settings", {})
        sensor_rows = self._optional_sensor_get("sensors-data/", [])
        nms = nms if isinstance(nms, dict) else {}
        switches = switches if isinstance(switches, dict) else {}
        detailed = detailed if isinstance(detailed, dict) else {}
        basic = basic if isinstance(basic, dict) else {}
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
            "trap_settings": detailed,
        }

    def set_outlet(self, line_id: int, enabled: bool) -> None:
        self._request(
            "PUT",
            f"outputs/{line_id}/switch-status",
            {"switch_status": bool(enabled)},
        )
