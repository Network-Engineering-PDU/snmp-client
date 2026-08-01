"""Implementation of the Network Engineering ``Nee-MIB`` v2.4.19 tree."""

import json
import logging
import math
import os
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple

from .base_oid_manager import BaseOidManager
from .node_table_oid_manager import SnmpSetError
from .oid import Oid, OidType
from .pdu_backend import PduApiError, PduBackend


logger = logging.getLogger(__name__)

BASE_OID = ".1.3.6.1.4.1.2000.1"
SYS_OID = BASE_OID + ".1"
POWER_OID = BASE_OID + ".2"
SENSOR_OID = BASE_OID + ".3.1.1"
EVENTS_OID = BASE_OID + ".100.1"

MAX_PDUS = 4
MAX_OUTLETS = 24
MAX_SENSORS = 32
UNAVAILABLE = -1


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _scaled(value: Any, factor: float, minimum: int, maximum: int) -> int:
    number = _finite_number(value)
    if number is None:
        return UNAVAILABLE
    result = int(round(number * factor))
    if result < minimum or result > maximum:
        return UNAVAILABLE
    return result


def _display(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    # pass_persist values are line-oriented. Keep API and persisted strings
    # from injecting protocol lines, and enforce the MIB's SIZE in octets.
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\0", "")
    encoded = text.encode("utf-8")[:maximum]
    return encoded.decode("utf-8", errors="ignore")


def _valid_display(value: str, maximum: int) -> bool:
    return (
        "\r" not in value
        and "\n" not in value
        and "\0" not in value
        and len(value.encode("utf-8")) <= maximum
    )


def _stored_integer(values: Dict[str, Any], key: str, minimum: int,
                    maximum: int, default: int = UNAVAILABLE) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if minimum <= value <= maximum else default


class PersistentMibState:
    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {
            "outlets": {},
            "summary": {},
            "sensors": {},
            "alarms": {},
            "last_event": {},
        }
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as state_file:
                loaded = json.load(state_file)
            if isinstance(loaded, dict):
                for key in self.data:
                    if isinstance(loaded.get(key), dict):
                        self.data[key] = loaded[key]
        except (FileNotFoundError, OSError, ValueError):
            return

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".nee-snmp-", dir=directory, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(self.data, state_file, sort_keys=True)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, self.path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise


class NeePduOidManager(BaseOidManager):
    """Thread-safe OID view backed by the local PDU REST API."""

    def __init__(self, backend: PduBackend, state_file: str):
        self.backend = backend
        self.state = PersistentMibState(state_file)
        self.lock = threading.RLock()
        self.oids: List[Oid] = []
        self.snapshot: Dict[str, Any] = {}
        self.pending_traps: List[dict] = []

    def first_oid(self) -> Optional[Oid]:
        with self.lock:
            return self.oids[0] if self.oids else None

    def last_oid(self) -> Optional[Oid]:
        with self.lock:
            return self.oids[-1] if self.oids else None

    def get(self, oid: Oid) -> Optional[Oid]:
        with self.lock:
            for candidate in self.oids:
                if candidate == oid:
                    return candidate
        return None

    def get_next_oid(self, oid: Oid) -> Optional[Oid]:
        with self.lock:
            for candidate in self.oids:
                if oid < candidate:
                    return candidate
        return None

    @staticmethod
    def _oid(path: str, oid_type: OidType, value: Any) -> Oid:
        return Oid(path, oid_type, str(value))

    @staticmethod
    def _integer(path: str, value: Any) -> Oid:
        return NeePduOidManager._oid(path, OidType.INTEGER, value)

    @staticmethod
    def _string(path: str, value: Any, maximum: int = 30) -> Oid:
        return NeePduOidManager._oid(
            path, OidType.STRING, _display(value, maximum)
        )

    def refresh(self) -> List[dict]:
        snapshot = self.backend.snapshot()
        with self.lock:
            old_snapshot = self.snapshot
            new_oids = sorted(self._build_oids(snapshot))
            self.snapshot = snapshot
            self.oids = new_oids
            self.pending_traps = self._detect_traps(old_snapshot, snapshot)
            if self.pending_traps:
                self.oids = sorted(self._build_oids(snapshot))
            return list(self.pending_traps)

    def _build_oids(self, snapshot: Dict[str, Any]) -> List[Oid]:
        oids: List[Oid] = []
        system = snapshot.get("system", {})
        nms = snapshot.get("nms", {})
        summary_state = self.state.data["summary"].get("1", {})
        if not isinstance(summary_state, dict):
            summary_state = {}
        source_id = _display(system.get("product_sn", "N/A"), 10)
        power_desc = _display(
            summary_state.get(
                "name", nms.get("system_name") or system.get("product_name")
            ),
            30,
        )

        event = self.state.data.get("last_event", {})
        if not isinstance(event, dict):
            event = {}
        oids.extend([
            self._string(SYS_OID + ".1.0", source_id, 10),
            self._string(SYS_OID + ".2.0", power_desc),
            self._string(SYS_OID + ".3.0", event.get("source", "")),
            self._string(SYS_OID + ".4.0", event.get("sensor", "")),
            self._string(SYS_OID + ".5.0", event.get("status", "")),
            self._integer(SYS_OID + ".6.0", _stored_integer(
                event, "load_value", -1, 65535)),
            self._integer(SYS_OID + ".7.0", _stored_integer(
                event, "load_low", -1, 65535)),
            self._integer(SYS_OID + ".8.0", _stored_integer(
                event, "load_high", -1, 65535)),
        ])

        pdus = snapshot.get("pdus", [])
        for pdu_index in range(1, MAX_PDUS + 1):
            outlets = pdus[pdu_index - 1] if pdu_index <= len(pdus) else []
            for outlet_index, outlet in enumerate(
                    outlets[:MAX_OUTLETS], start=1):
                base = f"{POWER_OID}.{pdu_index}.1"
                state_key = f"{pdu_index}.{outlet_index}"
                saved = self.state.data["outlets"].get(state_key, {})
                if not isinstance(saved, dict):
                    saved = {}
                data = outlet.get("data") if isinstance(
                    outlet.get("data"), dict
                ) else {}
                low = _stored_integer(saved, "low", -1, 255)
                high = _stored_integer(saved, "high", -1, 255)
                if "low" not in saved:
                    low = _scaled(outlet.get("low_limit"), 10, 0, 255)
                if "high" not in saved:
                    high = _scaled(outlet.get("high_limit"), 10, 0, 255)
                fuse = {
                    1: "Normal",
                    2: "Alarm",
                    0: "Unknown",
                    3: "Unavailable",
                }.get(data.get("fuse"), "Unavailable")
                on_value = outlet.get("on")
                on_off = "ON" if on_value is True else (
                    "OFF" if on_value is False else "Unknown"
                )
                values = (
                    (1, OidType.INTEGER, outlet_index),
                    (2, OidType.STRING, _display(
                        outlet.get("number", outlet_index), 10)),
                    (3, OidType.STRING, _display(
                        saved.get("description", outlet.get("description", "")),
                        30)),
                    (4, OidType.STRING, _display(outlet.get("socket", ""), 30)),
                    (5, OidType.STRING, fuse),
                    (6, OidType.STRING, on_off),
                    (7, OidType.INTEGER, _scaled(
                        data.get("current"), 10, 0, 255)),
                    (8, OidType.INTEGER, low),
                    (9, OidType.INTEGER, high),
                )
                for column, oid_type, value in values:
                    oids.append(self._oid(
                        f"{base}.{column}.{outlet_index}", oid_type, value
                    ))

        summary_count = snapshot.get("summary_count")
        if isinstance(summary_count, bool) or not isinstance(
                summary_count, int):
            summary_indexes = [
                index for index, outlets in enumerate(
                    pdus[:MAX_PDUS], start=1
                ) if outlets
            ]
        else:
            summary_indexes = range(
                1, min(max(summary_count, 0), MAX_PDUS) + 1
            )
        for pdu_index in summary_indexes:
            oids.extend(self._build_summary(
                snapshot, pdu_index, source_id, power_desc
            ))

        # Environmental rows are emitted only for sensors supplied by the
        # hardware/API provider; absent sensor types are not fabricated.
        for sensor_index, sensor in enumerate(
                snapshot.get("sensors", [])[:MAX_SENSORS], start=1):
            oids.extend(self._build_sensor(sensor_index, sensor))
        return oids

    def _build_summary(self, snapshot: Dict[str, Any], pdu_index: int,
                       source_id: str, power_desc: str) -> List[Oid]:
        summaries = snapshot.get("summaries", [])
        if not isinstance(summaries, list):
            summaries = []
        summary = (
            summaries[pdu_index - 1]
            if pdu_index <= len(summaries)
            and isinstance(summaries[pdu_index - 1], dict)
            else {}
        )
        system = summary.get(
            "system", snapshot.get("system", {}) if pdu_index == 1 else {}
        )
        pdu_info = summary.get(
            "pdu_info",
            snapshot.get("pdu_info", {}) if pdu_index == 1 else {},
        )
        saved = self.state.data["summary"].get(str(pdu_index), {})
        if not isinstance(saved, dict):
            saved = {}
        inputs = summary.get(
            "inputs", snapshot.get("inputs", []) if pdu_index == 1 else []
        )
        switches = summary.get(
            "switches",
            snapshot.get("switches", {}) if pdu_index == 1 else {},
        )
        if not isinstance(inputs, list):
            inputs = []
        if not isinstance(switches, dict):
            switches = {}
        phase_count = {
            0: 1, 1: 2, 2: 3, 3: 3
        }.get(switches.get("sys_type"), 0)
        row_source_id = _display(summary.get("id", source_id), 10)
        row_power_desc = _display(
            summary.get("name", power_desc), 30
        )

        def phase_value(index: int, key: str, factor: float,
                        maximum: int) -> int:
            if index >= phase_count:
                return UNAVAILABLE
            branch_count = (
                2 if switches.get("branch") == 1 else 1
            )
            line_indexes = [
                index + (branch * 3) for branch in range(branch_count)
            ]
            values = [
                _finite_number(inputs[line_index].get(key))
                for line_index in line_indexes
                if line_index < len(inputs)
                and isinstance(inputs[line_index], dict)
            ]
            values = [value for value in values if value is not None]
            if not values:
                return UNAVAILABLE
            # Currents from main and auxiliary branches contribute to the
            # phase total. Their voltage is common, so use the first measured
            # phase voltage rather than adding voltages.
            value = sum(values) if key == "current" else values[0]
            return _scaled(value, factor, 0, maximum)

        branch_count = (
            2 if switches.get("branch") == 1 else 1
        )
        active_indexes = [
            phase + branch * 3
            for branch in range(branch_count)
            for phase in range(phase_count)
        ]
        defaults = [
            inputs[index] for index in active_indexes
            if index < len(inputs) and isinstance(inputs[index], dict)
        ]
        energy_values = [
            value for value in (
                _finite_number(item.get("energy")) for item in defaults
            ) if value is not None
        ]
        energy_value = (
            _scaled(sum(energy_values), 0.01, 0, 2147483647)
            if energy_values else UNAVAILABLE
        )
        base = POWER_OID + ".5.1"
        power_on_gap = _scaled(
            summary.get("power_on_gap"), 1, 0, 255
        )
        values = (
            (1, OidType.INTEGER, pdu_index),
            (2, OidType.STRING, row_source_id),
            (3, OidType.STRING, _display(
                saved.get("name", row_power_desc), 30)),
            (4, OidType.STRING, _display(pdu_info.get("type", ""), 30)),
            (5, OidType.STRING, _display(system.get("product_pn", ""), 30)),
            # The current backend has no power-sequencing API and therefore
            # supplies no power_on_gap. Such a value is represented by -1.
            (6, OidType.INTEGER, power_on_gap),
            (7, OidType.INTEGER, phase_value(0, "current", 10, 65535)),
            (8, OidType.INTEGER, _stored_integer(
                saved, "load_a_low", -1, 65535)),
            (9, OidType.INTEGER, _stored_integer(
                saved, "load_a_high", -1, 65535)),
            (10, OidType.INTEGER, phase_value(1, "current", 10, 65535)),
            (11, OidType.INTEGER, _stored_integer(
                saved, "load_b_low", -1, 65535)),
            (12, OidType.INTEGER, _stored_integer(
                saved, "load_b_high", -1, 65535)),
            (13, OidType.INTEGER, phase_value(2, "current", 10, 65535)),
            (14, OidType.INTEGER, _stored_integer(
                saved, "load_c_low", -1, 65535)),
            (15, OidType.INTEGER, _stored_integer(
                saved, "load_c_high", -1, 65535)),
            (16, OidType.INTEGER, phase_value(0, "voltage", 1, 65535)),
            (17, OidType.INTEGER, phase_value(1, "voltage", 1, 65535)),
            (18, OidType.INTEGER, phase_value(2, "voltage", 1, 65535)),
            # API energy is Wh; the MIB unit is 0.1 kWh == 100 Wh.
            (19, OidType.INTEGER, energy_value),
        )
        return [
            self._oid(
                f"{base}.{column}.{pdu_index}", oid_type, value
            )
            for column, oid_type, value in values
        ]

    def _build_sensor(self, index: int, sensor: Dict[str, Any]) -> List[Oid]:
        saved = self.state.data["sensors"].get(str(index), {})
        if not isinstance(saved, dict):
            saved = {}
        limits = {
            "temperature_low": (-100, 120),
            "temperature_high": (-100, 120),
            "humidity_low": (-1, 100),
            "humidity_high": (-1, 100),
            "wind_low": (-1, 100),
            "wind_high": (-1, 100),
        }

        def sensor_limit(key: str) -> int:
            minimum, maximum = limits[key]
            if key in saved:
                return _stored_integer(
                    saved, key, minimum, maximum
                )
            return _scaled(
                sensor.get(key), 1, minimum, maximum
            )

        base = SENSOR_OID
        values = (
            (1, OidType.INTEGER, index),
            (2, OidType.STRING, _display(sensor.get("number", index), 10)),
            (3, OidType.STRING, _display(sensor.get("type", ""), 30)),
            (4, OidType.STRING, _display(sensor.get("id", ""), 10)),
            (5, OidType.STRING, _display(
                saved.get("location", sensor.get("location", "")), 30)),
            (6, OidType.STRING, _display(sensor.get("value", ""), 30)),
            (7, OidType.INTEGER, _scaled(
                sensor.get("temperature"), 1, -100, 120)),
            (8, OidType.INTEGER, sensor_limit("temperature_low")),
            (9, OidType.INTEGER, sensor_limit("temperature_high")),
            (10, OidType.INTEGER, _scaled(
                sensor.get("humidity"), 1, 0, 100)),
            (11, OidType.INTEGER, sensor_limit("humidity_low")),
            (12, OidType.INTEGER, sensor_limit("humidity_high")),
            (13, OidType.INTEGER, _scaled(
                sensor.get("wind"), 1, 0, 100)),
            (14, OidType.INTEGER, sensor_limit("wind_low")),
            (15, OidType.INTEGER, sensor_limit("wind_high")),
        )
        return [
            self._oid(f"{base}.{column}.{index}", oid_type, value)
            for column, oid_type, value in values
        ]

    def _persist(self) -> str:
        try:
            self.state.save()
        except (OSError, TypeError, ValueError):
            logger.exception("Could not persist SNMP MIB state")
            return SnmpSetError.INCONSISTENT_VALUE
        self.oids = sorted(self._build_oids(self.snapshot))
        return SnmpSetError.SUCCESS

    @staticmethod
    def _parse_integer(value: str, minimum: int,
                       maximum: int) -> Optional[int]:
        try:
            parsed = int(value, 10)
        except (TypeError, ValueError):
            return None
        return parsed if minimum <= parsed <= maximum else None

    def set(self, oid: Oid, value_type: str, value: str) -> str:
        with self.lock:
            path = oid.oid
            if path.startswith(POWER_OID + "."):
                return self._set_power(path, value_type, value)
            if path.startswith(SENSOR_OID + "."):
                return self._set_sensor(path, value_type, value)
            return SnmpSetError.NOT_WRITABLE

    def _set_power(self, path: str, value_type: str, value: str) -> str:
        suffix = path[len(POWER_OID) + 1:].split(".")
        if len(suffix) != 4 or suffix[1] != "1":
            return SnmpSetError.NOT_WRITABLE
        try:
            table, column, index = int(suffix[0]), int(suffix[2]), int(suffix[3])
        except ValueError:
            return SnmpSetError.NOT_WRITABLE

        if table in range(1, 5):
            return self._set_outlet(table, column, index, value_type, value)
        if table == 5:
            return self._set_summary(column, index, value_type, value)
        return SnmpSetError.NOT_WRITABLE

    def _set_outlet(self, table: int, column: int, index: int,
                    value_type: str, value: str) -> str:
        pdus = self.snapshot.get("pdus", [])
        outlets = pdus[table - 1] if table <= len(pdus) else []
        if not 1 <= index <= min(len(outlets), MAX_OUTLETS):
            return SnmpSetError.NOT_WRITABLE
        key = f"{table}.{index}"
        state = self.state.data["outlets"].setdefault(key, {})
        if not isinstance(state, dict):
            state = {}
            self.state.data["outlets"][key] = state

        if column == 3:
            if value_type.lower() != str(OidType.STRING):
                return SnmpSetError.WRONG_TYPE
            if not _valid_display(value, 30):
                return SnmpSetError.WRONG_LENGTH
            state["description"] = value
            return self._persist()

        if column == 6:
            if value_type.lower() != str(OidType.STRING):
                return SnmpSetError.WRONG_TYPE
            normalized = value.upper()
            if normalized not in ("ON", "OFF"):
                return SnmpSetError.WRONG_VALUE
            if outlets[index - 1].get("relay_writable") is False:
                return SnmpSetError.INCONSISTENT_VALUE
            line_id = outlets[index - 1].get("line_id")
            if not isinstance(line_id, int):
                return SnmpSetError.INCONSISTENT_VALUE
            try:
                self.backend.set_outlet(line_id, normalized == "ON")
            except PduApiError:
                logger.exception("Outlet relay SET failed")
                return SnmpSetError.INCONSISTENT_VALUE
            outlets[index - 1]["on"] = normalized == "ON"
            self.oids = sorted(self._build_oids(self.snapshot))
            return SnmpSetError.SUCCESS

        if column in (8, 9):
            if value_type.lower() != str(OidType.INTEGER):
                return SnmpSetError.WRONG_TYPE
            parsed = self._parse_integer(value, -1, 255)
            if parsed is None:
                return SnmpSetError.WRONG_VALUE
            other_key = "high" if column == 8 else "low"
            other = state.get(other_key)
            if other is None:
                metadata_key = "high_limit" if column == 8 else "low_limit"
                other = _scaled(
                    outlets[index - 1].get(metadata_key), 10, 0, 255
                )
            if parsed != UNAVAILABLE and other != UNAVAILABLE:
                if column == 8 and parsed > other:
                    return SnmpSetError.INCONSISTENT_VALUE
                if column == 9 and parsed < other:
                    return SnmpSetError.INCONSISTENT_VALUE
            state["low" if column == 8 else "high"] = parsed
            return self._persist()
        return SnmpSetError.NOT_WRITABLE

    def _set_summary(self, column: int, index: int, value_type: str,
                     value: str) -> str:
        if not 1 <= index <= MAX_PDUS or self.get(Oid(
                f"{POWER_OID}.5.1.1.{index}")) is None:
            return SnmpSetError.NOT_WRITABLE
        state = self.state.data["summary"].setdefault(str(index), {})
        if not isinstance(state, dict):
            state = {}
            self.state.data["summary"][str(index)] = state
        if column == 3:
            if value_type.lower() != str(OidType.STRING):
                return SnmpSetError.WRONG_TYPE
            if not _valid_display(value, 30):
                return SnmpSetError.WRONG_LENGTH
            state["name"] = value
            return self._persist()
        if column == 6:
            if value_type.lower() != str(OidType.INTEGER):
                return SnmpSetError.WRONG_TYPE
            if self._parse_integer(value, -1, 255) is None:
                return SnmpSetError.WRONG_VALUE
            return SnmpSetError.INCONSISTENT_VALUE
        limit_keys = {
            8: ("load_a_low", "load_a_high", True),
            9: ("load_a_high", "load_a_low", False),
            11: ("load_b_low", "load_b_high", True),
            12: ("load_b_high", "load_b_low", False),
            14: ("load_c_low", "load_c_high", True),
            15: ("load_c_high", "load_c_low", False),
        }
        if column not in limit_keys:
            return SnmpSetError.NOT_WRITABLE
        if value_type.lower() != str(OidType.INTEGER):
            return SnmpSetError.WRONG_TYPE
        parsed = self._parse_integer(value, -1, 65535)
        if parsed is None:
            return SnmpSetError.WRONG_VALUE
        key, other_key, is_low = limit_keys[column]
        other = _stored_integer(
            state, other_key, -1, 65535
        )
        if parsed != UNAVAILABLE and other != UNAVAILABLE and (
                (is_low and parsed > other) or
                (not is_low and parsed < other)):
            return SnmpSetError.INCONSISTENT_VALUE
        state[key] = parsed
        return self._persist()

    def _set_sensor(self, path: str, value_type: str, value: str) -> str:
        suffix = path[len(SENSOR_OID) + 1:].split(".")
        if len(suffix) != 2:
            return SnmpSetError.NOT_WRITABLE
        try:
            column, index = int(suffix[0]), int(suffix[1])
        except ValueError:
            return SnmpSetError.NOT_WRITABLE
        sensors = self.snapshot.get("sensors", [])
        if not 1 <= index <= min(len(sensors), MAX_SENSORS):
            return SnmpSetError.NOT_WRITABLE
        state = self.state.data["sensors"].setdefault(str(index), {})
        if not isinstance(state, dict):
            state = {}
            self.state.data["sensors"][str(index)] = state
        if column == 5:
            if value_type.lower() != str(OidType.STRING):
                return SnmpSetError.WRONG_TYPE
            if not _valid_display(value, 30):
                return SnmpSetError.WRONG_LENGTH
            state["location"] = value
            return self._persist()
        limits = {
            8: ("temperature_low", "temperature_high", -100, 120, True),
            9: ("temperature_high", "temperature_low", -100, 120, False),
            11: ("humidity_low", "humidity_high", -1, 100, True),
            12: ("humidity_high", "humidity_low", -1, 100, False),
            14: ("wind_low", "wind_high", -1, 100, True),
            15: ("wind_high", "wind_low", -1, 100, False),
        }
        if column not in limits:
            return SnmpSetError.NOT_WRITABLE
        if value_type.lower() != str(OidType.INTEGER):
            return SnmpSetError.WRONG_TYPE
        key, other_key, minimum, maximum, is_low = limits[column]
        parsed = self._parse_integer(value, minimum, maximum)
        if parsed is None:
            return SnmpSetError.WRONG_VALUE
        other = _stored_integer(
            state, other_key, minimum, maximum
        )
        if other == UNAVAILABLE and other_key not in state:
            sensor = sensors[index - 1]
            other = _scaled(
                sensor.get(other_key), 1, minimum, maximum
            )
        if parsed != UNAVAILABLE and other != UNAVAILABLE and (
                (is_low and parsed > other) or
                (not is_low and parsed < other)):
            return SnmpSetError.INCONSISTENT_VALUE
        state[key] = parsed
        return self._persist()

    def _detect_traps(self, old: Dict[str, Any],
                      new: Dict[str, Any]) -> List[dict]:
        candidates = self._alarm_candidates(new)
        if not old:
            self._prime_alarm_state(candidates)
            return []
        traps: List[dict] = []
        for candidate in candidates:
            traps.extend(self._transition(candidate))
        if traps:
            try:
                self.state.save()
            except OSError:
                logger.exception("Could not persist SNMP alarm state")
        return traps

    @staticmethod
    def _threshold_status(value: int, low: int, high: int) -> str:
        if value == UNAVAILABLE:
            return "unavailable"
        if low != UNAVAILABLE and value < low:
            return "lower"
        if high != UNAVAILABLE and value > high:
            return "higher"
        return "normal"

    def _identity(self) -> Dict[str, str]:
        power_desc = self.get(Oid(SYS_OID + ".2.0"))
        source_id = self.get(Oid(SYS_OID + ".1.0"))
        return {
            "power_desc": power_desc.value if power_desc else "",
            "source_id": source_id.value if source_id else "",
        }

    def _alarm_candidates(self, snapshot: Dict[str, Any]) -> List[dict]:
        candidates: List[dict] = []
        identity = self._identity()
        for pdu_index, outlets in enumerate(
                snapshot.get("pdus", [])[:MAX_PDUS], start=1):
            for outlet_index, outlet in enumerate(
                    outlets[:MAX_OUTLETS], start=1):
                data = outlet.get("data") if isinstance(
                    outlet.get("data"), dict
                ) else {}
                prefix = f"outlet.{pdu_index}.{outlet_index}"
                fuse_status = {
                    1: "normal",
                    2: "alarm",
                }.get(data.get("fuse"), "unavailable")
                candidates.append({
                    "key": prefix + ".fuse",
                    "status": fuse_status,
                    "active": ("alarm",),
                    "notification_oid": f"{EVENTS_OID}.1",
                    "source": f"Outlet_{pdu_index}_{outlet_index} Fuse",
                    "sensor": "",
                    "value": UNAVAILABLE,
                    "low": UNAVAILABLE,
                    "high": UNAVAILABLE,
                    "load_value": UNAVAILABLE,
                    "load_low": UNAVAILABLE,
                    "load_high": UNAVAILABLE,
                    **identity,
                })

                saved = self.state.data["outlets"].get(
                    f"{pdu_index}.{outlet_index}", {}
                )
                if not isinstance(saved, dict):
                    saved = {}
                low = _stored_integer(saved, "low", -1, 255)
                high = _stored_integer(saved, "high", -1, 255)
                if "low" not in saved:
                    low = _scaled(
                        outlet.get("low_limit"), 10, 0, 255
                    )
                if "high" not in saved:
                    high = _scaled(
                        outlet.get("high_limit"), 10, 0, 255
                    )
                load = _scaled(data.get("current"), 10, 0, 255)
                candidates.append({
                    "key": prefix + ".load",
                    "status": self._threshold_status(load, low, high),
                    "active": ("lower", "higher"),
                    "notification_oid": f"{EVENTS_OID}.2",
                    "source": f"Outlet_{pdu_index}_{outlet_index} Load",
                    "sensor": "",
                    "value": load,
                    "low": low,
                    "high": high,
                    "load_value": load,
                    "load_low": low,
                    "load_high": high,
                    **identity,
                })

        for sensor_index, sensor in enumerate(
                snapshot.get("sensors", [])[:MAX_SENSORS], start=1):
            if not isinstance(sensor, dict):
                continue
            saved = self.state.data["sensors"].get(str(sensor_index), {})
            if not isinstance(saved, dict):
                saved = {}
            sensor_desc = _display(
                sensor.get("description")
                or sensor.get("location")
                or f"Sensor_{sensor_index}",
                30,
            )
            metrics = (
                ("temperature", 3, 7, -100, 120),
                ("humidity", 4, 10, 0, 100),
                ("wind", 5, 13, 0, 100),
            )
            for metric, notification, column, minimum, maximum in metrics:
                value = _scaled(
                    sensor.get(metric), 1, minimum, maximum
                )
                low_key = metric + "_low"
                high_key = metric + "_high"
                limit_minimum = -100 if metric == "temperature" else -1
                low = _stored_integer(
                    saved, low_key, limit_minimum, maximum
                )
                high = _stored_integer(
                    saved, high_key, limit_minimum, maximum
                )
                if low_key not in saved:
                    low = _scaled(
                        sensor.get(low_key), 1, limit_minimum, maximum
                    )
                if high_key not in saved:
                    high = _scaled(
                        sensor.get(high_key), 1, limit_minimum, maximum
                    )
                candidates.append({
                    "key": f"sensor.{sensor_index}.{metric}",
                    "status": self._threshold_status(value, low, high),
                    "active": ("lower", "higher"),
                    "notification_oid": (
                        f"{EVENTS_OID}.{notification}"
                    ),
                    "source": "",
                    "sensor": sensor_desc,
                    "sensor_index": sensor_index,
                    "metric_column": column,
                    "value": value,
                    "low": low,
                    "high": high,
                    "load_value": UNAVAILABLE,
                    "load_low": UNAVAILABLE,
                    "load_high": UNAVAILABLE,
                    **identity,
                })

            alarm = sensor.get("alarm")
            environment_status = (
                "alarm" if alarm is True else
                "normal" if alarm is False else
                "unavailable"
            )
            candidates.append({
                "key": f"sensor.{sensor_index}.environment",
                "status": environment_status,
                "active": ("alarm",),
                "notification_oid": f"{EVENTS_OID}.6",
                "source": "",
                "sensor": sensor_desc,
                "sensor_index": sensor_index,
                "value": UNAVAILABLE,
                "low": UNAVAILABLE,
                "high": UNAVAILABLE,
                "load_value": UNAVAILABLE,
                "load_low": UNAVAILABLE,
                "load_high": UNAVAILABLE,
                **identity,
            })
        return candidates

    def _prime_alarm_state(self, candidates: List[dict]) -> None:
        for candidate in candidates:
            self.state.data["alarms"][candidate["key"]] = (
                candidate["status"]
            )
        try:
            self.state.save()
        except OSError:
            logger.exception("Could not persist initial alarm state")

    def _transition(self, candidate: dict) -> List[dict]:
        key = candidate["key"]
        status = candidate["status"]
        previous = self.state.data["alarms"].get(key)
        self.state.data["alarms"][key] = status
        if (
                previous is None
                or previous == status
                or status not in candidate["active"]):
            return []
        event = {
            key: value for key, value in candidate.items()
            if key not in ("key", "active")
        }
        self.state.data["last_event"] = event
        return [event]
