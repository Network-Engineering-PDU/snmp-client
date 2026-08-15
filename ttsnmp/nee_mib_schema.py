"""Authoritative constants for the Network Engineering Nee-MIB v2.4.19.

Keeping the table layout here prevents the OID builder, SET handler, trap
detector, and tests from growing independent copies of the same column rules.
"""

from typing import NamedTuple, Tuple


BASE_OID = ".1.3.6.1.4.1.2000.1"
SYS_OID = BASE_OID + ".1"
POWER_OID = BASE_OID + ".2"
SENSOR_OID = BASE_OID + ".3.1.1"
EVENTS_OID = BASE_OID + ".100.1"

MAX_PDUS = 4
MAX_OUTLETS = 24
MAX_SENSORS = 32
UNAVAILABLE = -1

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
        "temperature_low", "temperature_high", -100, 120, True
    ),
    9: LimitColumn(
        "temperature_high", "temperature_low", -100, 120, False
    ),
    11: LimitColumn("humidity_low", "humidity_high", -1, 100, True),
    12: LimitColumn("humidity_high", "humidity_low", -1, 100, False),
    14: LimitColumn("wind_low", "wind_high", -1, 100, True),
    15: LimitColumn("wind_high", "wind_low", -1, 100, False),
}
SENSOR_LIMITS_BY_KEY = {
    specification.key: specification
    for specification in SENSOR_LIMIT_COLUMNS.values()
}


class SensorMetric(NamedTuple):
    key: str
    notification: int
    value_column: int
    value_minimum: int
    value_maximum: int
    limit_minimum: int


SENSOR_METRICS: Tuple[SensorMetric, ...] = (
    SensorMetric("temperature", 3, 7, -100, 120, -100),
    SensorMetric("humidity", 4, 10, 0, 100, -1),
    SensorMetric("wind", 5, 13, 0, 100, -1),
)


def outlet_cell_oid(table: int, column: int, index: int) -> str:
    return f"{POWER_OID}.{table}.1.{column}.{index}"


def summary_cell_oid(column: int, index: int) -> str:
    return f"{POWER_OID}.5.1.{column}.{index}"


def sensor_cell_oid(column: int, index: int) -> str:
    return f"{SENSOR_OID}.{column}.{index}"


def notification_oid(notification: int) -> str:
    return f"{EVENTS_OID}.{notification}"
