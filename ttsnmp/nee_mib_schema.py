"""Authoritative constants for the NET-POWER PDU MIB v2.4.20.

Keeping the table layout here prevents the OID builder, SET handler, trap
detector, and tests from growing independent copies of the same column rules.
"""

from typing import NamedTuple, Tuple


BASE_OID = ".1.3.6.1.4.1.2000.1"
DEVICE_OID = BASE_OID + ".1"
INPUT_OID = BASE_OID + ".2"
INPUT_ENTRY_OID = INPUT_OID + ".1.1"
INPUT_PHASE_ENTRY_OID = INPUT_OID + ".2.1"
NATIVE_OUTLET_OID = BASE_OID + ".3.1.1"
NATIVE_SENSOR_OID = BASE_OID + ".4.1.1"
COMMUNICATION_OID = BASE_OID + ".5"
NETWORK_OID = COMMUNICATION_OID + ".1"
SERVICE_OID = COMMUNICATION_OID + ".2"
SNMP_CONFIG_OID = COMMUNICATION_OID + ".3"
TRAP_MANAGER_ENTRY_OID = SNMP_CONFIG_OID + ".20.1"
MODBUS_OID = COMMUNICATION_OID + ".4"
NTP_OID = COMMUNICATION_OID + ".5"
WEB_EMAIL_OID = COMMUNICATION_OID + ".6"
EMAIL_RECIPIENT_ENTRY_OID = WEB_EMAIL_OID + ".20.1"
BLUETOOTH_OID = COMMUNICATION_OID + ".7"
EVENTS_OID = BASE_OID + ".6"
SYS_OID = EVENTS_OID + ".10"

# Internal aliases retained for the common validation helpers.  These paths
# are not built into the v2.4.20 OID tree.
POWER_OID = BASE_OID + ".9"
SENSOR_OID = NATIVE_SENSOR_OID

MAX_PDUS = 1
MAX_OUTLETS = 24
MAX_SENSORS = 32
UNAVAILABLE = -1
NATIVE_UNAVAILABLE = -2147483648

OUTLET_STRING_COLUMNS = frozenset((2, 3, 4, 5, 6))
OUTLET_WRITABLE_COLUMNS = frozenset((3, 6, 8, 9))
SUMMARY_STRING_COLUMNS = frozenset((2, 3, 4, 5))
SUMMARY_WRITABLE_COLUMNS = frozenset((3, 6, 8, 9, 11, 12, 14, 15))
SENSOR_STRING_COLUMNS = frozenset((2, 3, 4, 5, 6))
SENSOR_WRITABLE_COLUMNS = frozenset((5, 8, 9, 11, 12, 14, 15))


class LimitColumn(NamedTuple):
    key: str
    peer_key: str
    minimum: int
    maximum: int
    is_low: bool


OUTLET_LIMIT_COLUMNS = {
    8: LimitColumn("low", "high", -1, 255, True),
    9: LimitColumn("high", "low", -1, 255, False),
}

SUMMARY_LIMIT_COLUMNS = {
    8: LimitColumn("load_a_low", "load_a_high", -1, 65535, True),
    9: LimitColumn("load_a_high", "load_a_low", -1, 65535, False),
    11: LimitColumn("load_b_low", "load_b_high", -1, 65535, True),
    12: LimitColumn("load_b_high", "load_b_low", -1, 65535, False),
    14: LimitColumn("load_c_low", "load_c_high", -1, 65535, True),
    15: LimitColumn("load_c_high", "load_c_low", -1, 65535, False),
}

SENSOR_LIMIT_COLUMNS = {
    8: LimitColumn(
        "temperature_low", "temperature_high", -10000, 12000, True
    ),
    9: LimitColumn(
        "temperature_high", "temperature_low", -10000, 12000, False
    ),
    11: LimitColumn("humidity_low", "humidity_high", -1, 1000, True),
    12: LimitColumn("humidity_high", "humidity_low", -1, 1000, False),
}
SENSOR_LIMITS_BY_KEY = {
    specification.key: specification
    for specification in SENSOR_LIMIT_COLUMNS.values()
}


class SensorMetric(NamedTuple):
    key: str
    notification: int
    value_column: int
    factor: int
    value_minimum: int
    value_maximum: int
    limit_minimum: int


SENSOR_METRICS: Tuple[SensorMetric, ...] = (
    SensorMetric("temperature", 3, 7, 100, -10000, 12000, -10000),
    SensorMetric("humidity", 4, 10, 10, 0, 1000, -1),
)


def outlet_cell_oid(table: int, column: int, index: int) -> str:
    return f"{POWER_OID}.{table}.1.{column}.{index}"


def summary_cell_oid(column: int, index: int) -> str:
    return f"{POWER_OID}.5.1.{column}.{index}"


def sensor_cell_oid(column: int, index: int) -> str:
    return f"{SENSOR_OID}.{column}.{index}"


def notification_oid(notification: int) -> str:
    return f"{EVENTS_OID}.{notification}"


def input_cell_oid(column: int, index: int) -> str:
    return f"{INPUT_ENTRY_OID}.{column}.{index}"


def input_phase_cell_oid(column: int, input_index: int,
                         phase_index: int) -> str:
    return (
        f"{INPUT_PHASE_ENTRY_OID}.{column}.{input_index}.{phase_index}"
    )


def native_outlet_cell_oid(column: int, index: int) -> str:
    return f"{NATIVE_OUTLET_OID}.{column}.{index}"


def native_sensor_cell_oid(column: int, index: int) -> str:
    return f"{NATIVE_SENSOR_OID}.{column}.{index}"


def trap_manager_cell_oid(column: int, index: int) -> str:
    return f"{TRAP_MANAGER_ENTRY_OID}.{column}.{index}"


def email_recipient_cell_oid(column: int, index: int) -> str:
    return f"{EMAIL_RECIPIENT_ENTRY_OID}.{column}.{index}"
