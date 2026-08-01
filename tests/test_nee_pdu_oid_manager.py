import copy
import io
import os
import tempfile
import unittest
from unittest import mock

from ttsnmp.nee_pdu_oid_manager import (
    BASE_OID,
    POWER_OID,
    SENSOR_OID,
    SYS_OID,
    NeePduOidManager,
)
from ttsnmp.node_table_oid_manager import SnmpSetError
from ttsnmp.oid import Oid
from ttsnmp.snmpd_helper import SnmpdHelper


def make_snapshot():
    return {
        "system": {
            "product_name": "NET-POWER",
            "product_pn": "NE0001001000",
            "product_sn": "ABCDEF1234",
        },
        "pdu_info": {
            "outlet_count": 1,
            "type": "SMART_PDU",
        },
        "license": {"type_id": "A2"},
        "nms": {"system_name": "Rack PDU"},
        "switches": {"sys_type": 2},
        "inputs": [
            {"current": 1.24, "voltage": 229.6, "energy": 1000.0},
            {"current": 2.25, "voltage": 230.4, "energy": 2000.0},
            {"current": 3.26, "voltage": 231.5, "energy": 3000.0},
            None,
            None,
            None,
        ],
        "pdus": [[{
            "line_id": 0,
            "number": "1",
            "description": "Output 1",
            "socket": "C13",
            "low_limit": 0.0,
            "high_limit": 5.0,
            "on": True,
            "data": {
                "current": 1.24,
                "fuse": 1,
            },
        }], [], [], []],
        "sensors": [],
        "trap_settings": {},
    }


class FakeBackend:
    def __init__(self):
        self.data = make_snapshot()
        self.relay_calls = []

    def snapshot(self):
        return copy.deepcopy(self.data)

    def set_outlet(self, line_id, enabled):
        self.relay_calls.append((line_id, enabled))


class NeePduOidManagerTest(unittest.TestCase):
    def setUp(self):
        descriptor, self.state_path = tempfile.mkstemp()
        os.close(descriptor)
        os.unlink(self.state_path)
        self.backend = FakeBackend()
        self.manager = NeePduOidManager(self.backend, self.state_path)
        self.assertEqual([], self.manager.refresh())

    def tearDown(self):
        try:
            os.unlink(self.state_path)
        except FileNotFoundError:
            pass

    def value(self, oid):
        result = self.manager.get(Oid(oid))
        self.assertIsNotNone(result, oid)
        return result.value

    def test_authoritative_tree_and_scaling(self):
        self.assertEqual("ABCDEF1234", self.value(SYS_OID + ".1.0"))
        self.assertEqual(
            "12", self.value(POWER_OID + ".1.1.7.1")
        )
        self.assertEqual(
            "12", self.value(POWER_OID + ".5.1.7.1")
        )
        self.assertEqual(
            "232", self.value(POWER_OID + ".5.1.18.1")
        )
        self.assertEqual(
            "60", self.value(POWER_OID + ".5.1.19.1")
        )
        self.assertIsNone(
            self.manager.get(Oid(POWER_OID + ".2.1.1.1"))
        )
        self.assertTrue(all(
            oid.oid.startswith(BASE_OID + ".") for oid in self.manager.oids
        ))
        for column in range(1, 10):
            self.assertIsNotNone(self.manager.get(Oid(
                f"{POWER_OID}.1.1.{column}.1"
            )))
        for column in range(1, 20):
            self.assertIsNotNone(self.manager.get(Oid(
                f"{POWER_OID}.5.1.{column}.1"
            )))

    def test_common_outlet_layout_supports_all_four_pdu_tables(self):
        outlet = copy.deepcopy(self.backend.data["pdus"][0][0])
        self.backend.data["pdus"] = [
            [copy.deepcopy(outlet)] for _ in range(4)
        ]
        self.manager.refresh()
        for table in range(1, 5):
            for column in range(1, 10):
                self.assertIsNotNone(self.manager.get(Oid(
                    f"{POWER_OID}.{table}.1.{column}.1"
                )))

    def test_summary_exists_for_licensed_pdu_without_outlets(self):
        self.backend.data["pdus"] = [[], [], [], []]
        self.backend.data["summary_count"] = 1
        self.manager.refresh()

        for column in range(1, 20):
            self.assertIsNotNone(self.manager.get(Oid(
                f"{POWER_OID}.5.1.{column}.1"
            )))
        self.assertEqual("-1", self.value(POWER_OID + ".5.1.6.1"))

    def test_power_summary_supports_four_indexed_rows(self):
        self.backend.data["summary_count"] = 4
        self.backend.data["summaries"] = [
            {"id": f"PDU{index}", "name": f"Power {index}"}
            for index in range(1, 5)
        ]
        self.manager.refresh()

        for index in range(1, 5):
            self.assertEqual(
                str(index),
                self.value(f"{POWER_OID}.5.1.1.{index}"),
            )
            self.assertEqual(
                f"PDU{index}",
                self.value(f"{POWER_OID}.5.1.2.{index}"),
            )

    def test_getnext_is_numeric_and_crosses_tables(self):
        first = self.manager.get_next_oid(Oid(BASE_OID))
        self.assertEqual(SYS_OID + ".1.0", first.oid)
        next_after_system = self.manager.get_next_oid(Oid(SYS_OID + ".8.0"))
        self.assertEqual(POWER_OID + ".1.1.1.1", next_after_system.oid)

    def test_mib_column_types_and_access_permissions(self):
        self.backend.data["sensors"] = [{
            "temperature": 20,
            "temperature_low": 10,
            "temperature_high": 30,
            "humidity": 50,
            "humidity_low": 20,
            "humidity_high": 80,
            "wind": 10,
            "wind_low": 2,
            "wind_high": 40,
            "alarm": False,
        }]
        self.manager.refresh()

        for column in range(1, 9):
            scalar = self.manager.get(Oid(f"{SYS_OID}.{column}.0"))
            expected = "string" if column <= 5 else "integer"
            self.assertEqual(expected, str(scalar.oidtype))
            self.assertEqual(
                SnmpSetError.NOT_WRITABLE,
                self.manager.set(
                    Oid(f"{SYS_OID}.{column}.0"),
                    expected,
                    scalar.value,
                ),
            )

        outlet_strings = {2, 3, 4, 5, 6}
        outlet_writable = {3, 6, 8, 9}
        for column in range(1, 10):
            oid = Oid(f"{POWER_OID}.1.1.{column}.1")
            cell = self.manager.get(oid)
            expected = (
                "string" if column in outlet_strings else "integer"
            )
            self.assertEqual(expected, str(cell.oidtype))
            result = self.manager.set(
                oid,
                "integer" if expected == "string" else "string",
                "1",
            )
            self.assertEqual(
                SnmpSetError.WRONG_TYPE
                if column in outlet_writable
                else SnmpSetError.NOT_WRITABLE,
                result,
            )

        summary_strings = {2, 3, 4, 5}
        summary_writable = {3, 6, 8, 9, 11, 12, 14, 15}
        for column in range(1, 20):
            oid = Oid(f"{POWER_OID}.5.1.{column}.1")
            cell = self.manager.get(oid)
            expected = (
                "string" if column in summary_strings else "integer"
            )
            self.assertEqual(expected, str(cell.oidtype))
            result = self.manager.set(
                oid,
                "integer" if expected == "string" else "string",
                "1",
            )
            self.assertEqual(
                SnmpSetError.WRONG_TYPE
                if column in summary_writable
                else SnmpSetError.NOT_WRITABLE,
                result,
            )

        sensor_strings = {2, 3, 4, 5, 6}
        sensor_writable = {5, 8, 9, 11, 12, 14, 15}
        for column in range(1, 16):
            oid = Oid(f"{SENSOR_OID}.{column}.1")
            cell = self.manager.get(oid)
            expected = (
                "string" if column in sensor_strings else "integer"
            )
            self.assertEqual(expected, str(cell.oidtype))
            result = self.manager.set(
                oid,
                "integer" if expected == "string" else "string",
                "1",
            )
            self.assertEqual(
                SnmpSetError.WRONG_TYPE
                if column in sensor_writable
                else SnmpSetError.NOT_WRITABLE,
                result,
            )

    def test_writable_description_limit_and_relay(self):
        description_oid = Oid(POWER_OID + ".1.1.3.1")
        self.assertEqual(
            SnmpSetError.SUCCESS,
            self.manager.set(description_oid, "string", "Rack A outlet"),
        )
        self.assertEqual("Rack A outlet", self.value(description_oid.oid))

        relay_oid = Oid(POWER_OID + ".1.1.6.1")
        self.assertEqual(
            SnmpSetError.WRONG_VALUE,
            self.manager.set(relay_oid, "string", "REBOOT"),
        )
        self.assertEqual(
            SnmpSetError.SUCCESS,
            self.manager.set(relay_oid, "string", "OFF"),
        )
        self.assertEqual([(0, False)], self.backend.relay_calls)
        self.assertEqual("OFF", self.value(relay_oid.oid))

        self.backend.data["pdus"][0][0]["relay_writable"] = False
        self.manager.refresh()
        self.assertEqual(
            SnmpSetError.INCONSISTENT_VALUE,
            self.manager.set(relay_oid, "string", "ON"),
        )

        low_oid = Oid(POWER_OID + ".1.1.8.1")
        high_oid = Oid(POWER_OID + ".1.1.9.1")
        self.assertEqual(
            SnmpSetError.INCONSISTENT_VALUE,
            self.manager.set(low_oid, "integer", "60"),
        )
        self.assertEqual(
            SnmpSetError.SUCCESS,
            self.manager.set(high_oid, "integer", "100"),
        )
        self.assertEqual(
            SnmpSetError.SUCCESS,
            self.manager.set(low_oid, "integer", "60"),
        )
        self.assertEqual(
            SnmpSetError.WRONG_LENGTH,
            self.manager.set(description_oid, "string", "bad\nvalue"),
        )

    def test_sensor_table_types_limits_writes_and_bounds(self):
        self.backend.data["sensors"] = [{
            "number": "1-S1",
            "type": "Temperature/Humidity",
            "id": "1556BA",
            "location": "Rack rear",
            "description": "Rack rear sensor",
            "value": "Closed",
            "temperature": 24,
            "temperature_low": 10,
            "temperature_high": 40,
            "humidity": 55,
            "humidity_low": 20,
            "humidity_high": 80,
            "wind": 12,
            "wind_low": 2,
            "wind_high": 60,
            "alarm": False,
        }]
        self.manager.refresh()

        for column in range(1, 16):
            self.assertIsNotNone(self.manager.get(Oid(
                f"{SENSOR_OID}.{column}.1"
            )))
        self.assertEqual("10", self.value(SENSOR_OID + ".8.1"))
        self.assertEqual("80", self.value(SENSOR_OID + ".12.1"))
        location_oid = Oid(SENSOR_OID + ".5.1")
        self.assertEqual(
            SnmpSetError.SUCCESS,
            self.manager.set(location_oid, "string", "Cold aisle"),
        )
        self.assertEqual("Cold aisle", self.value(location_oid.oid))
        self.assertEqual(
            SnmpSetError.INCONSISTENT_VALUE,
            self.manager.set(
                Oid(SENSOR_OID + ".8.1"), "integer", "50"
            ),
        )
        self.assertEqual(
            SnmpSetError.WRONG_VALUE,
            self.manager.set(
                Oid(SENSOR_OID + ".11.1"), "integer", "101"
            ),
        )


    def test_state_survives_manager_restart(self):
        oid = Oid(POWER_OID + ".5.1.3.1")
        self.assertEqual(
            SnmpSetError.SUCCESS,
            self.manager.set(oid, "string", "PDU Alpha"),
        )
        replacement = NeePduOidManager(self.backend, self.state_path)
        replacement.refresh()
        self.assertEqual("PDU Alpha", replacement.get(oid).value)

    def test_power_on_gap_validates_type_and_reports_missing_api(self):
        oid = Oid(POWER_OID + ".5.1.6.1")
        self.assertEqual(
            SnmpSetError.WRONG_TYPE,
            self.manager.set(oid, "string", "5"),
        )
        self.assertEqual(
            SnmpSetError.WRONG_VALUE,
            self.manager.set(oid, "integer", "256"),
        )
        self.assertEqual(
            SnmpSetError.INCONSISTENT_VALUE,
            self.manager.set(oid, "integer", "5"),
        )

    def test_fuse_and_load_traps_are_transition_only(self):
        self.assertEqual([], self.manager.refresh())
        self.backend.data["pdus"][0][0]["data"]["fuse"] = 2
        events = self.manager.refresh()
        self.assertEqual(1, len(events))
        self.assertTrue(events[0]["notification_oid"].endswith(".1"))
        self.assertEqual([], self.manager.refresh())

        self.backend.data["pdus"][0][0]["data"]["current"] = 6.0
        events = self.manager.refresh()
        self.assertEqual(1, len(events))
        self.assertTrue(events[0]["notification_oid"].endswith(".2"))
        self.assertEqual("higher", events[0]["status"])
        self.assertEqual("60", self.value(SYS_OID + ".6.0"))
        self.assertEqual("0", self.value(SYS_OID + ".7.0"))
        self.assertEqual("50", self.value(SYS_OID + ".8.0"))
        self.assertEqual([], self.manager.refresh())

    def test_all_sensor_notifications_are_transition_only(self):
        sensor = {
            "description": "Rack sensor",
            "temperature": 20,
            "temperature_low": 10,
            "temperature_high": 30,
            "humidity": 50,
            "humidity_low": 20,
            "humidity_high": 80,
            "wind": 10,
            "wind_low": 2,
            "wind_high": 40,
            "alarm": False,
        }
        self.backend.data["sensors"] = [sensor]
        self.manager.refresh()

        changes = (
            ("temperature", 31, 3, 7),
            ("humidity", 81, 4, 10),
            ("wind", 41, 5, 13),
        )
        for field, value, notification, column in changes:
            self.backend.data["sensors"][0][field] = value
            events = self.manager.refresh()
            self.assertEqual(1, len(events))
            self.assertTrue(
                events[0]["notification_oid"].endswith(
                    f".{notification}"
                )
            )
            self.assertEqual(column, events[0]["metric_column"])
            self.assertEqual(1, events[0]["sensor_index"])
            self.assertEqual("-1", self.value(SYS_OID + ".6.0"))
            self.assertEqual("-1", self.value(SYS_OID + ".7.0"))
            self.assertEqual("-1", self.value(SYS_OID + ".8.0"))
            self.assertEqual([], self.manager.refresh())
            self.backend.data["sensors"][0][field] = (
                20 if field == "temperature" else
                50 if field == "humidity" else 10
            )
            self.assertEqual([], self.manager.refresh())

        self.backend.data["sensors"][0]["alarm"] = True
        events = self.manager.refresh()
        self.assertEqual(1, len(events))
        self.assertTrue(events[0]["notification_oid"].endswith(".6"))
        self.assertEqual([], self.manager.refresh())

    def test_recovery_does_not_emit_an_alarm_notification(self):
        self.backend.data["pdus"][0][0]["data"]["fuse"] = 2
        self.assertEqual(1, len(self.manager.refresh()))
        self.backend.data["pdus"][0][0]["data"]["fuse"] = 1
        self.assertEqual([], self.manager.refresh())

    def test_alarm_duplicate_is_suppressed_after_manager_restart(self):
        self.backend.data["pdus"][0][0]["data"]["fuse"] = 2
        self.assertEqual(1, len(self.manager.refresh()))

        replacement = NeePduOidManager(
            self.backend, self.state_path
        )
        self.assertEqual([], replacement.refresh())

    def test_unavailable_values_do_not_become_zero(self):
        self.backend.data["inputs"][1] = None
        self.backend.data["pdus"][0][0]["data"]["current"] = None
        self.manager.refresh()
        self.assertEqual("-1", self.value(POWER_OID + ".1.1.7.1"))
        self.assertEqual("-1", self.value(POWER_OID + ".5.1.10.1"))

    def test_display_strings_are_safe_and_octet_bounded(self):
        self.backend.data["system"]["product_name"] = "A\nB" + ("é" * 30)
        self.backend.data["nms"] = {}
        self.manager.refresh()
        value = self.value(SYS_OID + ".2.0")
        self.assertNotIn("\n", value)
        self.assertLessEqual(len(value.encode("utf-8")), 30)

    def test_corrupt_persisted_integer_is_reported_unavailable(self):
        self.manager.state.data["outlets"]["1.1"] = {
            "low": "invalid",
            "high": 9999,
        }
        self.manager.oids = sorted(
            self.manager._build_oids(self.manager.snapshot)
        )
        self.assertEqual("-1", self.value(POWER_OID + ".1.1.8.1"))
        self.assertEqual("-1", self.value(POWER_OID + ".1.1.9.1"))

    def test_pass_persist_get_getnext_and_set_framing(self):
        helper = SnmpdHelper()
        helper.nee_manager = self.manager
        helper.update_attempted.set()
        description_oid = POWER_OID + ".1.1.3.1"
        protocol_input = io.StringIO(
            "\n".join([
                "PING",
                "get",
                SYS_OID + ".1.0",
                "getnext",
                BASE_OID,
                "set",
                description_oid,
                'string "Protocol outlet"',
                "",
            ])
        )
        protocol_output = io.StringIO()
        with mock.patch("sys.stdin", protocol_input), mock.patch(
                "sys.stdout", protocol_output):
            helper.main()
        self.assertEqual([
            "PONG",
            SYS_OID + ".1.0",
            "string",
            "ABCDEF1234",
            SYS_OID + ".1.0",
            "string",
            "ABCDEF1234",
            "DONE",
        ], protocol_output.getvalue().splitlines())
        self.assertEqual(
            "Protocol outlet", self.value(description_oid)
        )


if __name__ == "__main__":
    unittest.main()
