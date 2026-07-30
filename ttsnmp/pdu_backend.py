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
                 timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.request_lock = threading.Lock()

    def _request(self, method: str, path: str,
                 payload: Optional[dict] = None) -> Any:
        url = self.base_url + "/" + path.lstrip("/")
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

    def _optional_get(self, path: str, default: Any) -> Any:
        try:
            return self._request("GET", path)
        except PduApiError as exc:
            logger.warning("%s", exc)
            return default

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
        snmp_data_licensed = license_info.get("type_id") in ("A2", "B2")
        nms = self._optional_get("settings/snmp-nms", {})
        switches = (
            self._optional_get("inputs/switches", {})
            if snmp_data_licensed else {}
        )
        detailed = self._optional_get("network/snmp/detailed-settings", {})
        basic = self._optional_get("network/snmp/settings", {})
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
        if snmp_data_licensed:
            for line_id in range(6):
                inputs.append(self._optional_get(
                    f"inputs/{line_id}/data", None
                ))

        outlet_metadata = (
            self._optional_get("outputs/", []) if snmp_data_licensed else []
        )
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
        outlet_count = min(max(configured_outlets, 0), 24) if (
            snmp_data_licensed
        ) else 0
        for line_id in range(outlet_count):
            data = self._optional_get(f"outputs/{line_id}/data", None)
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
                "data": data,
            })

        return {
            "system": system,
            "pdu_info": pdu_info,
            "license": license_info,
            "nms": nms,
            "switches": switches,
            "inputs": inputs if snmp_data_licensed else [],
            # The current hardware API represents one local PDU.  The MIB
            # manager accepts four lists so future controllers can add the
            # remaining units without changing OID logic.
            "pdus": [outlets, [], [], []],
            # A power-summary row represents the PDU itself, not an outlet.
            # Keep row 1 present for licensed units even when no outlet
            # modules are currently connected.
            "summary_count": 1 if snmp_data_licensed else 0,
            "sensors": [],
            "trap_settings": detailed,
        }

    def set_outlet(self, line_id: int, enabled: bool) -> None:
        self._request(
            "PUT",
            f"outputs/{line_id}/switch-status",
            {"switch_status": bool(enabled)},
        )
