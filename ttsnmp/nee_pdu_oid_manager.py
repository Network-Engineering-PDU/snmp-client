"""Implementation of the Network Engineering ``Nee-MIB`` v2.4.19 tree."""

from bisect import bisect_right
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from .base_oid_manager import BaseOidManager
from .nee_mib_schema import (
    BASE_OID,
    EVENTS_OID,
    MAX_OUTLETS,
    MAX_PDUS,
    MAX_SENSORS,
    OUTLET_LIMIT_COLUMNS,
    POWER_OID,
    SENSOR_LIMIT_COLUMNS,
    SENSOR_LIMITS_BY_KEY,
    SENSOR_METRICS,
    SENSOR_OID,
    SUMMARY_LIMIT_COLUMNS,
    SYS_OID,
    UNAVAILABLE,
    notification_oid,
    outlet_cell_oid,
    sensor_cell_oid,
    summary_cell_oid,
)
from .node_table_oid_manager import SnmpSetError
from .oid import Oid, OidType
from .pdu_backend import PduApiError, PduBackend
from .persistent_mib_state import PersistentMibState
from .mib_values import (
    display_string as _display,
    finite_number as _finite_number,
    scaled_integer as _scaled,
    stored_integer as _stored_integer,
    valid_display_string as _valid_display,
)


logger = logging.getLogger(__name__)
_MISSING = object()


class NeePduOidManager(BaseOidManager):
    """Thread-safe OID view backed by the local PDU REST API."""

    def __init__(self, backend: PduBackend, state_file: str):
        self.backend = backend
        self.state = PersistentMibState(state_file)
        self.lock = threading.RLock()
        self.oids: List[Oid] = []
        self._oid_by_key: Dict[Tuple[int, ...], Oid] = {}
        self._oid_keys: List[Tuple[int, ...]] = []
        self.snapshot: Dict[str, Any] = {}
        self.pending_traps: List[dict] = []

    @staticmethod
    def _key(oid: Oid) -> Tuple[int, ...]:
        return tuple(oid.splitted)

    def _replace_oids(self, oids: List[Oid]) -> None:
        self.oids = sorted(oids)
        self._oid_keys = [self._key(oid) for oid in self.oids]
        self._oid_by_key = dict(zip(self._oid_keys, self.oids))

    def first_oid(self) -> Optional[Oid]:
        with self.lock:
            return self.oids[0] if self.oids else None

    def last_oid(self) -> Optional[Oid]:
        with self.lock:
            return self.oids[-1] if self.oids else None

    def get(self, oid: Oid) -> Optional[Oid]:
        with self.lock:
            return self._oid_by_key.get(self._key(oid))

    def get_next_oid(self, oid: Oid) -> Optional[Oid]:
        with self.lock:
            index = bisect_right(self._oid_keys, self._key(oid))
            return self.oids[index] if index < len(self.oids) else None

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
            self.snapshot = snapshot
            self._replace_oids(self._build_oids(snapshot))
            self.pending_traps = self._detect_traps(old_snapshot, snapshot)
            if self.pending_traps:
                self._replace_oids(self._build_oids(snapshot))
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
                    oids.append(self._oid(outlet_cell_oid(
                        pdu_index, column, outlet_index
                    ), oid_type, value))

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
            self._oid(summary_cell_oid(column, pdu_index), oid_type, value)
            for column, oid_type, value in values
        ]

    def _build_sensor(self, index: int, sensor: Dict[str, Any]) -> List[Oid]:
        saved = self.state.data["sensors"].get(str(index), {})
        if not isinstance(saved, dict):
            saved = {}

        def sensor_limit(key: str) -> int:
            spec = SENSOR_LIMITS_BY_KEY[key]
            if key in saved:
                return _stored_integer(
                    saved, key, spec.minimum, spec.maximum
                )
            return _scaled(
                sensor.get(key), 1, spec.minimum, spec.maximum
            )

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
            self._oid(sensor_cell_oid(column, index), oid_type, value)
            for column, oid_type, value in values
        ]

    def _persist(self) -> str:
        try:
            self.state.save()
        except (OSError, TypeError, ValueError):
            logger.exception("Could not persist SNMP MIB state")
            return SnmpSetError.INCONSISTENT_VALUE
        self._replace_oids(self._build_oids(self.snapshot))
        return SnmpSetError.SUCCESS

    def _persist_value(self, state: Dict[str, Any], key: str,
                       value: Any) -> str:
        """Persist one writable field and roll back RAM on write failure."""
        previous = state.get(key, _MISSING)
        state[key] = value
        result = self._persist()
        if result != SnmpSetError.SUCCESS:
            if previous is _MISSING:
                state.pop(key, None)
            else:
                state[key] = previous
            self._replace_oids(self._build_oids(self.snapshot))
        return result

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
            return self._persist_value(state, "description", value)

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
            self._replace_oids(self._build_oids(self.snapshot))
            return SnmpSetError.SUCCESS

        if column in OUTLET_LIMIT_COLUMNS:
            if value_type.lower() != str(OidType.INTEGER):
                return SnmpSetError.WRONG_TYPE
            spec = OUTLET_LIMIT_COLUMNS[column]
            parsed = self._parse_integer(
                value, spec.minimum, spec.maximum
            )
            if parsed is None:
                return SnmpSetError.WRONG_VALUE
            other = state.get(spec.peer_key)
            if other is None:
                metadata_key = spec.peer_key + "_limit"
                other = _scaled(
                    outlets[index - 1].get(metadata_key), 10, 0, 255
                )
            if parsed != UNAVAILABLE and other != UNAVAILABLE:
                if spec.is_low and parsed > other:
                    return SnmpSetError.INCONSISTENT_VALUE
                if not spec.is_low and parsed < other:
                    return SnmpSetError.INCONSISTENT_VALUE
            return self._persist_value(state, spec.key, parsed)
        return SnmpSetError.NOT_WRITABLE

    def _set_summary(self, column: int, index: int, value_type: str,
                     value: str) -> str:
        if not 1 <= index <= MAX_PDUS or self.get(Oid(
                summary_cell_oid(1, index))) is None:
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
            return self._persist_value(state, "name", value)
        if column == 6:
            if value_type.lower() != str(OidType.INTEGER):
                return SnmpSetError.WRONG_TYPE
            if self._parse_integer(value, -1, 255) is None:
                return SnmpSetError.WRONG_VALUE
            return SnmpSetError.INCONSISTENT_VALUE
        if column not in SUMMARY_LIMIT_COLUMNS:
            return SnmpSetError.NOT_WRITABLE
        if value_type.lower() != str(OidType.INTEGER):
            return SnmpSetError.WRONG_TYPE
        spec = SUMMARY_LIMIT_COLUMNS[column]
        parsed = self._parse_integer(value, spec.minimum, spec.maximum)
        if parsed is None:
            return SnmpSetError.WRONG_VALUE
        other = _stored_integer(
            state, spec.peer_key, spec.minimum, spec.maximum
        )
        if parsed != UNAVAILABLE and other != UNAVAILABLE and (
                (spec.is_low and parsed > other) or
                (not spec.is_low and parsed < other)):
            return SnmpSetError.INCONSISTENT_VALUE
        return self._persist_value(state, spec.key, parsed)

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
            return self._persist_value(state, "location", value)
        if column not in SENSOR_LIMIT_COLUMNS:
            return SnmpSetError.NOT_WRITABLE
        if value_type.lower() != str(OidType.INTEGER):
            return SnmpSetError.WRONG_TYPE
        spec = SENSOR_LIMIT_COLUMNS[column]
        parsed = self._parse_integer(value, spec.minimum, spec.maximum)
        if parsed is None:
            return SnmpSetError.WRONG_VALUE
        other = _stored_integer(
            state, spec.peer_key, spec.minimum, spec.maximum
        )
        if other == UNAVAILABLE and spec.peer_key not in state:
            sensor = sensors[index - 1]
            other = _scaled(
                sensor.get(spec.peer_key), 1,
                spec.minimum, spec.maximum
            )
        if parsed != UNAVAILABLE and other != UNAVAILABLE and (
                (spec.is_low and parsed > other) or
                (not spec.is_low and parsed < other)):
            return SnmpSetError.INCONSISTENT_VALUE
        return self._persist_value(state, spec.key, parsed)

    def _detect_traps(self, old: Dict[str, Any],
                      new: Dict[str, Any]) -> List[dict]:
        candidates = self._alarm_candidates(new)
        if not old:
            self._prime_alarm_state(candidates)
            return []
        previous_alarm_state = dict(self.state.data["alarms"])
        traps: List[dict] = []
        for candidate in candidates:
            traps.extend(self._transition(candidate))
        if previous_alarm_state != self.state.data["alarms"]:
            try:
                self.state.save()
            except (OSError, TypeError, ValueError):
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
                    "notification_oid": notification_oid(1),
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
                    "notification_oid": notification_oid(2),
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
            for metric in SENSOR_METRICS:
                value = _scaled(
                    sensor.get(metric.key), 1,
                    metric.value_minimum, metric.value_maximum
                )
                low_key = metric.key + "_low"
                high_key = metric.key + "_high"
                low = _stored_integer(
                    saved, low_key, metric.limit_minimum,
                    metric.value_maximum
                )
                high = _stored_integer(
                    saved, high_key, metric.limit_minimum,
                    metric.value_maximum
                )
                if low_key not in saved:
                    low = _scaled(
                        sensor.get(low_key), 1, metric.limit_minimum,
                        metric.value_maximum
                    )
                if high_key not in saved:
                    high = _scaled(
                        sensor.get(high_key), 1, metric.limit_minimum,
                        metric.value_maximum
                    )
                candidates.append({
                    "key": f"sensor.{sensor_index}.{metric.key}",
                    "status": self._threshold_status(value, low, high),
                    "active": ("lower", "higher"),
                    "notification_oid": notification_oid(
                        metric.notification
                    ),
                    "source": "",
                    "sensor": sensor_desc,
                    "sensor_index": sensor_index,
                    "metric_column": metric.value_column,
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
                "notification_oid": notification_oid(6),
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
        except (OSError, TypeError, ValueError):
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
