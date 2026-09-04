"""Implementation of the NET-POWER PDU MIB v2.4.20 tree."""

from bisect import bisect_right
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from .base_oid_manager import BaseOidManager
from .nee_mib_schema import (
    BASE_OID,
    BLUETOOTH_OID,
    DEVICE_OID,
    EVENTS_OID,
    INPUT_OID,
    MAX_OUTLETS,
    MAX_PDUS,
    MAX_SENSORS,
    MODBUS_OID,
    NATIVE_OUTLET_OID,
    NATIVE_SENSOR_OID,
    NATIVE_UNAVAILABLE,
    NETWORK_OID,
    NTP_OID,
    OUTLET_LIMIT_COLUMNS,
    POWER_OID,
    SENSOR_LIMIT_COLUMNS,
    SENSOR_LIMITS_BY_KEY,
    SENSOR_METRICS,
    SENSOR_OID,
    SERVICE_OID,
    SNMP_CONFIG_OID,
    SUMMARY_LIMIT_COLUMNS,
    SYS_OID,
    UNAVAILABLE,
    WEB_EMAIL_OID,
    email_recipient_cell_oid,
    input_cell_oid,
    input_phase_cell_oid,
    native_outlet_cell_oid,
    native_sensor_cell_oid,
    notification_oid,
    outlet_cell_oid,
    sensor_cell_oid,
    summary_cell_oid,
    trap_manager_cell_oid,
)
from .node_table_oid_manager import SnmpSetError
from .oid import Oid, OidType
from .pdu_api_error import PduApiError
from .persistent_mib_state import PersistentMibState
from .volatile_snapshot import VolatileSnapshot
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

    def __init__(self, backend: Any, state_file: str,
                 snapshot_file: str = ""):
        self.backend = backend
        self.state = PersistentMibState(state_file)
        self.snapshot_store = VolatileSnapshot(snapshot_file)
        self.lock = threading.RLock()
        self.oids: List[Oid] = []
        self._oid_by_key: Dict[Tuple[int, ...], Oid] = {}
        self._oid_keys: List[Tuple[int, ...]] = []
        self.snapshot: Dict[str, Any] = self.snapshot_store.load()
        self.pending_traps: List[dict] = []
        # Net-SNMP starts pass_persist helpers on the first enterprise
        # request. Expose a structurally valid tree immediately so that the
        # request is not held up by the initial REST/hardware refresh.
        self._replace_oids(self._build_oids(self.snapshot))

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

    @staticmethod
    def _truth(value: Any) -> int:
        if value is True:
            return 1
        if value is False:
            return 2
        return 0

    @staticmethod
    def _native_scaled(value: Any, factor: float,
                       minimum: int = -2147483647,
                       maximum: int = 2147483647) -> int:
        number = _finite_number(value)
        if number is None:
            return NATIVE_UNAVAILABLE
        scaled = int(round(number * factor))
        return scaled if minimum <= scaled <= maximum else NATIVE_UNAVAILABLE

    def refresh(self) -> List[dict]:
        snapshot = self.backend.snapshot()
        with self.lock:
            old_snapshot = self.snapshot
            self.snapshot = snapshot
            self._replace_oids(self._build_oids(snapshot))
            self.pending_traps = self._detect_traps(old_snapshot, snapshot)
            if self.pending_traps:
                self._replace_oids(self._build_oids(snapshot))
            try:
                self.snapshot_store.save(snapshot)
            except (OSError, TypeError, ValueError):
                logger.exception("Could not cache live SNMP snapshot")
            return list(self.pending_traps)

    def _build_oids(self, snapshot: Dict[str, Any]) -> List[Oid]:
        oids: List[Oid] = []
        system = snapshot.get("system", {})
        nms = snapshot.get("nms", {})
        source_id = _display(system.get("product_sn", "N/A"), 10)
        power_desc = _display(
            nms.get("system_name") or system.get("product_name"), 30
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
                event, "value", -2147483648, 2147483647)),
            self._integer(SYS_OID + ".7.0", _stored_integer(
                event, "low", -2147483648, 2147483647)),
            self._integer(SYS_OID + ".8.0", _stored_integer(
                event, "high", -2147483648, 2147483647)),
        ])
        oids.extend(self._build_native_device(snapshot))
        oids.extend(self._build_native_inputs(snapshot))
        oids.extend(self._build_native_outlets(snapshot))
        oids.extend(self._build_native_sensors(snapshot))
        oids.extend(self._build_communications(snapshot))
        return oids

    def _build_native_device(self, snapshot: Dict[str, Any]) -> List[Oid]:
        system = snapshot.get("system", {})
        pdu = snapshot.get("pdu_info", {})
        license_info = snapshot.get("license", {})
        nms = snapshot.get("nms", {})
        values = (
            (1, OidType.STRING, system.get("product_name")),
            (2, OidType.STRING, system.get("product_pn")),
            (3, OidType.STRING, system.get("product_sn")),
            (4, OidType.STRING, pdu.get("controller")),
            (5, OidType.STRING, pdu.get("type")),
            (6, OidType.INTEGER, self._native_scaled(
                pdu.get("rated_current"), 10, 0)),
            (7, OidType.INTEGER, self._native_scaled(
                pdu.get("outlet_count"), 1, 0, MAX_OUTLETS)),
            (8, OidType.STRING, license_info.get("type_id")),
            (9, OidType.STRING, system.get("sw_version")),
            (10, OidType.STRING, system.get("om_version")),
            (11, OidType.STRING, system.get("pmb_version")),
            (12, OidType.STRING, system.get("uptime")),
            (13, OidType.STRING, system.get("lan_mac")),
            (14, OidType.STRING, system.get("ip")),
            (15, OidType.STRING, nms.get("system_name")),
            (16, OidType.STRING, nms.get("system_contact")),
            (17, OidType.STRING, nms.get("system_location")),
            (18, OidType.INTEGER, self._truth(
                license_info.get("wifi_licensed"))),
            (19, OidType.INTEGER, self._truth(
                license_info.get("outlet_switch_licensed"))),
            (20, OidType.INTEGER, self._truth(
                license_info.get("outlet_metering_licensed"))),
        )
        return [
            self._oid(
                f"{DEVICE_OID}.{column}.0", oid_type,
                _display(value, 128) if oid_type == OidType.STRING else value,
            )
            for column, oid_type, value in values
        ]

    @staticmethod
    def _input_topology(switches: Dict[str, Any]) -> Tuple[int, int, int]:
        system_type = switches.get("sys_type")
        phase_count = {0: 1, 1: 2, 2: 3, 3: 3}.get(system_type, 0)
        input_count = {0: 1, 1: 2}.get(switches.get("branch"), 0)
        return system_type, phase_count, input_count

    def _build_native_inputs(self, snapshot: Dict[str, Any]) -> List[Oid]:
        switches = snapshot.get("switches", {})
        inputs = snapshot.get("inputs", [])
        if not isinstance(switches, dict):
            switches = {}
        if not isinstance(inputs, list):
            inputs = []
        system_type, phase_count, input_count = self._input_topology(switches)
        type_value = {0: 1, 1: 2, 2: 3, 3: 4}.get(system_type, 0)
        neutral = self._truth(
            True if system_type == 3 else
            False if system_type in (0, 1, 2) else None
        )
        sensor_type = {0: 1, 1: 2}.get(switches.get("curr_type"), 0)
        oids = [self._integer(INPUT_OID + ".3.0", input_count)]

        for input_index in range(1, input_count + 1):
            base = (input_index - 1) * 3
            phase_rows = [
                inputs[base + phase]
                if base + phase < len(inputs)
                and isinstance(inputs[base + phase], dict) else {}
                for phase in range(phase_count)
            ]
            active_power = [
                _finite_number(row.get("active_power")) for row in phase_rows
            ]
            active_power = [value for value in active_power if value is not None]
            energy = [_finite_number(row.get("energy")) for row in phase_rows]
            energy = [value for value in energy if value is not None]
            input_values = (
                (1, OidType.INTEGER, input_index),
                (2, OidType.STRING, "Main" if input_index == 1 else "Auxiliary"),
                (3, OidType.INTEGER, 1),
                (4, OidType.INTEGER, type_value),
                (5, OidType.INTEGER, phase_count),
                (6, OidType.INTEGER, neutral),
                (7, OidType.INTEGER, sensor_type),
                (8, OidType.INTEGER, self._native_scaled(
                    sum(active_power) if active_power else None, 10)),
                (9, OidType.INTEGER, self._native_scaled(
                    sum(energy) if energy else None, 10, 0)),
            )
            for column, oid_type, value in input_values:
                oids.append(self._oid(
                    input_cell_oid(column, input_index), oid_type, value
                ))

            for phase_index in range(1, phase_count + 1):
                row = phase_rows[phase_index - 1]
                phase_values = (
                    (1, OidType.INTEGER, input_index),
                    (2, OidType.INTEGER, phase_index),
                    (3, OidType.STRING, f"L{phase_index}"),
                    (4, OidType.INTEGER, self._native_scaled(
                        row.get("voltage"), 10, 0)),
                    (5, OidType.INTEGER, self._native_scaled(
                        row.get("current"), 1000, 0)),
                    (6, OidType.INTEGER, self._native_scaled(
                        row.get("active_power"), 10)),
                    (7, OidType.INTEGER, self._native_scaled(
                        row.get("reactive_power"), 10)),
                    (8, OidType.INTEGER, self._native_scaled(
                        row.get("apparent_power"), 10, 0)),
                    (9, OidType.INTEGER, self._native_scaled(
                        row.get("power_factor"), 1000, -1000, 1000)),
                    (10, OidType.INTEGER, self._native_scaled(
                        row.get("phase"), 10, -3600, 3600)),
                    (11, OidType.INTEGER, self._native_scaled(
                        row.get("frequency"), 100, 0)),
                    (12, OidType.INTEGER, self._native_scaled(
                        row.get("energy"), 10, 0)),
                )
                for column, oid_type, value in phase_values:
                    oids.append(self._oid(
                        input_phase_cell_oid(
                            column, input_index, phase_index
                        ), oid_type, value
                    ))
        return oids

    def _build_native_outlets(self, snapshot: Dict[str, Any]) -> List[Oid]:
        pdus = snapshot.get("pdus", [])
        outlets = pdus[0] if isinstance(pdus, list) and pdus else []
        oids = []
        for index, outlet in enumerate(outlets[:MAX_OUTLETS], start=1):
            saved = self.state.data["outlets"].get(f"1.{index}", {})
            saved = saved if isinstance(saved, dict) else {}
            data = outlet.get("data")
            data = data if isinstance(data, dict) else {}
            fuse = data.get("fuse")
            fuse = fuse if fuse in (0, 1, 2, 3) else 3
            state = 1 if outlet.get("on") is True else (
                2 if outlet.get("on") is False else 0
            )
            low = _stored_integer(saved, "low", -1, 255)
            high = _stored_integer(saved, "high", -1, 255)
            if "low" not in saved:
                low = _scaled(outlet.get("low_limit"), 10, 0, 255)
            if "high" not in saved:
                high = _scaled(outlet.get("high_limit"), 10, 0, 255)
            values = (
                (1, OidType.INTEGER, index),
                (2, OidType.STRING, _display(
                    saved.get("description", outlet.get("description")), 30)),
                (3, OidType.STRING, _display(outlet.get("socket"), 32)),
                (4, OidType.INTEGER, fuse),
                (5, OidType.INTEGER, state),
                (6, OidType.INTEGER, self._native_scaled(
                    data.get("voltage"), 10, 0)),
                (7, OidType.INTEGER, self._native_scaled(
                    data.get("current"), 1000, 0)),
                (8, OidType.INTEGER, self._native_scaled(
                    data.get("active_power"), 10)),
                (9, OidType.INTEGER, self._native_scaled(
                    data.get("reactive_power"), 10)),
                (10, OidType.INTEGER, self._native_scaled(
                    data.get("apparent_power"), 10, 0)),
                (11, OidType.INTEGER, self._native_scaled(
                    data.get("power_factor"), 1000, -1000, 1000)),
                (12, OidType.INTEGER, self._native_scaled(
                    data.get("phase"), 10, -3600, 3600)),
                (13, OidType.INTEGER, self._native_scaled(
                    data.get("frequency"), 100, 0)),
                (14, OidType.INTEGER, self._native_scaled(
                    data.get("energy"), 10, 0)),
                (15, OidType.INTEGER, low),
                (16, OidType.INTEGER, high),
                (17, OidType.INTEGER, self._truth(
                    outlet.get("relay_writable"))),
                (18, OidType.INTEGER, self._truth(bool(data))),
            )
            for column, oid_type, value in values:
                oids.append(self._oid(
                    native_outlet_cell_oid(column, index), oid_type, value
                ))
        return oids

    def _build_native_sensors(self, snapshot: Dict[str, Any]) -> List[Oid]:
        oids = []
        for index, sensor in enumerate(
                snapshot.get("sensors", [])[:MAX_SENSORS], start=1):
            saved = self.state.data["sensors"].get(str(index), {})
            saved = saved if isinstance(saved, dict) else {}
            temperature_low = _stored_integer(
                saved, "temperature_low", -10000, 12000
            )
            temperature_high = _stored_integer(
                saved, "temperature_high", -10000, 12000
            )
            humidity_low = _stored_integer(
                saved, "humidity_low", -1, 1000
            )
            humidity_high = _stored_integer(
                saved, "humidity_high", -1, 1000
            )
            values = (
                (1, OidType.INTEGER, index),
                (2, OidType.INTEGER, self._native_scaled(
                    sensor.get("api_id"), 1, 0)),
                (3, OidType.STRING, _display(sensor.get("mac"), 32)),
                (4, OidType.STRING, _display(sensor.get("name"), 64)),
                (5, OidType.STRING, _display(sensor.get("type"), 64)),
                (6, OidType.STRING, _display(
                    saved.get("location", sensor.get("location")), 30)),
                (7, OidType.INTEGER, self._native_scaled(
                    sensor.get("temperature"), 100, -10000, 12000)),
                (8, OidType.INTEGER, temperature_low),
                (9, OidType.INTEGER, temperature_high),
                (10, OidType.INTEGER, self._native_scaled(
                    sensor.get("humidity"), 10, 0, 1000)),
                (11, OidType.INTEGER, humidity_low),
                (12, OidType.INTEGER, humidity_high),
                (13, OidType.INTEGER, self._native_scaled(
                    sensor.get("rssi"), 1, -200, 0)),
                (14, OidType.INTEGER, self._native_scaled(
                    sensor.get("battery_mv"), 1, 0)),
                (15, OidType.STRING, _display(
                    sensor.get("data_datetime"), 40)),
            )
            for column, oid_type, value in values:
                oids.append(self._oid(
                    native_sensor_cell_oid(column, index), oid_type, value
                ))
        return oids

    def _build_communications(self, snapshot: Dict[str, Any]) -> List[Oid]:
        communication = snapshot.get("communications", {})
        communication = communication if isinstance(communication, dict) else {}
        network = communication.get("network", {})
        network = network if isinstance(network, dict) else {}
        params = network.get("params", {})
        params = params if isinstance(params, dict) else {}
        network_info = communication.get("network_info", {})
        network_info = network_info if isinstance(network_info, dict) else {}
        services = communication.get("services", {})
        services = services if isinstance(services, dict) else {}
        oids = []
        network_mode = network.get("nw_mode")
        if isinstance(network_mode, bool) or network_mode not in (-1, 0, 1, 2, 3):
            network_mode = -1

        network_values = (
            (1, OidType.INTEGER, self._truth(network_info.get("connected"))),
            (2, OidType.INTEGER, network_mode),
            (3, OidType.INTEGER, self._truth(network.get("dhcp"))),
            (4, OidType.STRING, network.get("eth_interface")),
            (5, OidType.STRING, params.get("ip")),
            (6, OidType.STRING, params.get("subnet_mask")),
            (7, OidType.STRING, params.get("gateway_ip")),
            (8, OidType.STRING, params.get("dns")),
            (9, OidType.STRING, network.get("lan1_ip")),
            (10, OidType.STRING, network.get("lan1_gateway")),
            (11, OidType.STRING, network.get("lan2_ip")),
            (12, OidType.STRING, network.get("lan2_gateway")),
            (13, OidType.STRING, network.get("wifi_ip")),
            (14, OidType.STRING, params.get("ssid")),
            (15, OidType.STRING, network.get("ethernet_mac")),
            (16, OidType.STRING, network.get("wifi_mac")),
        )
        for column, oid_type, value in network_values:
            oids.append(self._oid(
                f"{NETWORK_OID}.{column}.0", oid_type,
                _display(value, 128) if oid_type == OidType.STRING else value,
            ))

        for column, key in enumerate(("ssh", "snmp", "modbus"), start=1):
            oids.append(self._integer(
                f"{SERVICE_OID}.{column}.0", self._truth(services.get(key))
            ))

        snmp = communication.get("snmp", {})
        snmp = snmp if isinstance(snmp, dict) else {}
        version = {"V1": 1, "V2c": 2, "V3": 3}.get(snmp.get("version"), 0)
        security = {
            "noAuthNoPriv": 1, "authNoPriv": 2, "authPriv": 3,
        }.get(snmp.get("v3_security_level"), 0)
        access = {"readOnly": 1, "readWrite": 2}.get(
            snmp.get("v3_access_right"), 0
        )
        auth = {"MD5": 1, "SHA": 2}.get(snmp.get("v3_auth_algorithm"), 0)
        privacy = {"DES": 1, "AES": 2}.get(
            snmp.get("v3_privacy_algorithm"), 0
        )
        snmp_values = (
            (1, OidType.INTEGER, self._truth(snmp.get("enabled"))),
            (2, OidType.INTEGER, version),
            (3, OidType.INTEGER, self._native_scaled(
                snmp.get("port"), 1, 1, 65535)),
            (4, OidType.INTEGER, self._truth(snmp.get("set_enabled"))),
            (5, OidType.INTEGER, self._truth(snmp.get("traps_enabled"))),
            (6, OidType.STRING, snmp.get("v3_user")),
            (7, OidType.INTEGER, security),
            (8, OidType.INTEGER, access),
            (9, OidType.INTEGER, auth),
            (10, OidType.INTEGER, privacy),
            (11, OidType.INTEGER, self._truth(snmp.get("v3_configured"))),
        )
        for column, oid_type, value in snmp_values:
            oids.append(self._oid(
                f"{SNMP_CONFIG_OID}.{column}.0", oid_type,
                _display(value, 64) if oid_type == OidType.STRING else value,
            ))
        managers = snmp.get("trap_managers", [])
        managers = managers if isinstance(managers, list) else []
        for index in range(1, 5):
            manager = managers[index - 1] if index <= len(managers) else {}
            manager = manager if isinstance(manager, dict) else {}
            for column, oid_type, value in (
                (1, OidType.INTEGER, index),
                (2, OidType.STRING, _display(manager.get("name"), 64)),
                (3, OidType.STRING, _display(manager.get("address"), 253)),
            ):
                oids.append(self._oid(
                    trap_manager_cell_oid(column, index), oid_type, value
                ))

        modbus = communication.get("modbus", {})
        modbus = modbus if isinstance(modbus, dict) else {}
        modbus_values = (
            (1, self._truth(services.get("modbus"))),
            (2, self._native_scaled(modbus.get("addr"), 1, 0, 247)),
            (3, 502),
            (4, 115200),
            (5, 1),  # none
            (6, 8),
            (7, 1),
        )
        for column, value in modbus_values:
            oids.append(self._integer(f"{MODBUS_OID}.{column}.0", value))

        ntp = communication.get("ntp", {})
        ntp = ntp if isinstance(ntp, dict) else {}
        ntp_values = (
            (1, OidType.INTEGER, self._truth(ntp.get("enabled"))),
            (2, OidType.STRING, ntp.get("server")),
            (3, OidType.INTEGER, self._native_scaled(
                ntp.get("time_offset"), 1, -12, 12)),
            (4, OidType.INTEGER, self._truth(ntp.get("running"))),
            (5, OidType.INTEGER, self._truth(ntp.get("synchronized"))),
        )
        for column, oid_type, value in ntp_values:
            oids.append(self._oid(
                f"{NTP_OID}.{column}.0", oid_type,
                _display(value, 253) if oid_type == OidType.STRING else value,
            ))

        email = communication.get("email_web", {})
        email = email if isinstance(email, dict) else {}
        web_protocol = {"http": 1, "https": 2}.get(
            email.get("web_protocol"), 0
        )
        smtp_auth = {"none": 1, "login": 2}.get(email.get("smtp_auth"), 0)
        email_values = (
            (1, OidType.INTEGER, web_protocol),
            (2, OidType.INTEGER, self._native_scaled(
                email.get("web_port"), 1, 1, 65535)),
            (3, OidType.STRING, email.get("smtp_server")),
            (4, OidType.INTEGER, self._native_scaled(
                email.get("smtp_port"), 1, 1, 65535)),
            (5, OidType.INTEGER, smtp_auth),
            (6, OidType.STRING, email.get("from_address")),
            (7, OidType.INTEGER, self._truth(
                email.get("password_configured"))),
        )
        for column, oid_type, value in email_values:
            oids.append(self._oid(
                f"{WEB_EMAIL_OID}.{column}.0", oid_type,
                _display(value, 254) if oid_type == OidType.STRING else value,
            ))
        recipients = email.get("recipients", [])
        recipients = recipients if isinstance(recipients, list) else []
        for index, recipient in enumerate(recipients[:3], start=1):
            oids.extend([
                self._integer(email_recipient_cell_oid(1, index), index),
                self._string(
                    email_recipient_cell_oid(2, index), recipient, 254
                ),
            ])

        bluetooth = communication.get("bluetooth", {})
        bluetooth = bluetooth if isinstance(bluetooth, dict) else {}
        bluetooth_values = (
            (1, OidType.STRING, bluetooth.get("controller_mac")),
            (2, OidType.STRING, bluetooth.get("name")),
            (3, OidType.INTEGER, self._truth(bluetooth.get("powered"))),
            (4, OidType.INTEGER, self._truth(bluetooth.get("pairable"))),
            (5, OidType.INTEGER, self._truth(bluetooth.get("discoverable"))),
            (6, OidType.INTEGER, self._truth(bluetooth.get("discovering"))),
            (7, OidType.INTEGER, self._native_scaled(
                bluetooth.get("device_count"), 1, 0)),
        )
        for column, oid_type, value in bluetooth_values:
            oids.append(self._oid(
                f"{BLUETOOTH_OID}.{column}.0", oid_type,
                _display(value, 64) if oid_type == OidType.STRING else value,
            ))
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
            if path.startswith(NATIVE_OUTLET_OID + "."):
                return self._set_native_outlet(
                    path, value_type, value
                )
            if path.startswith(NATIVE_SENSOR_OID + "."):
                return self._set_native_sensor(
                    path, value_type, value
                )
            return SnmpSetError.NOT_WRITABLE

    def _set_native_outlet(self, path: str, value_type: str,
                           value: str) -> str:
        suffix = path[len(NATIVE_OUTLET_OID) + 1:].split(".")
        if len(suffix) != 2:
            return SnmpSetError.NOT_WRITABLE
        try:
            column, index = int(suffix[0]), int(suffix[1])
        except ValueError:
            return SnmpSetError.NOT_WRITABLE
        if column == 2:
            return self._set_outlet(1, 3, index, value_type, value)
        if column == 5:
            if value_type.lower() != str(OidType.INTEGER):
                return SnmpSetError.WRONG_TYPE
            state = {1: "ON", 2: "OFF"}.get(
                self._parse_integer(value, 1, 2)
            )
            if state is None:
                return SnmpSetError.WRONG_VALUE
            return self._set_outlet(
                1, 6, index, str(OidType.STRING), state
            )
        if column in (15, 16):
            return self._set_outlet(
                1, 8 if column == 15 else 9,
                index, value_type, value,
            )
        return SnmpSetError.NOT_WRITABLE

    def _set_native_sensor(self, path: str, value_type: str,
                           value: str) -> str:
        suffix = path[len(NATIVE_SENSOR_OID) + 1:].split(".")
        if len(suffix) != 2:
            return SnmpSetError.NOT_WRITABLE
        try:
            column, index = int(suffix[0]), int(suffix[1])
        except ValueError:
            return SnmpSetError.NOT_WRITABLE
        if column == 6:
            return self._set_sensor(
                sensor_cell_oid(6, index), value_type, value
            )
        if column in (8, 9, 11, 12):
            return self._set_sensor(
                sensor_cell_oid(column, index), value_type, value
            )
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
        if column == 6:
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
        power_desc = self.get(Oid(DEVICE_OID + ".15.0"))
        source_id = self.get(Oid(DEVICE_OID + ".3.0"))
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
                    sensor.get(metric.key), metric.factor,
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
