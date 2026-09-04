import copy
import os
import tempfile
import unittest
from unittest import mock

from ttsnmp.nee_mib_schema import (
    BASE_OID, BLUETOOTH_OID, DEVICE_OID, EMAIL_RECIPIENT_ENTRY_OID,
    EVENTS_OID, INPUT_ENTRY_OID, INPUT_OID, INPUT_PHASE_ENTRY_OID,
    MODBUS_OID, NATIVE_OUTLET_OID, NATIVE_SENSOR_OID,
    NATIVE_UNAVAILABLE, NETWORK_OID, NTP_OID, SERVICE_OID,
    SNMP_CONFIG_OID, SYS_OID, TRAP_MANAGER_ENTRY_OID, WEB_EMAIL_OID,
)
from ttsnmp.nee_pdu_oid_manager import NeePduOidManager
from ttsnmp.node_table_oid_manager import SnmpSetError
from ttsnmp.oid import Oid


def make_snapshot():
    return {
        "system": {
            "product_name": "NET-POWER", "product_pn": "NE0001001000",
            "product_sn": "ABCDEF1234", "lan_mac": "00:11:22:33:44:55",
            "ip": "192.168.1.100", "sw_version": "1.2.3",
            "om_version": "2.0.0", "pmb_version": "3.0.0",
            "uptime": "1 day",
        },
        "pdu_info": {"outlet_count": 1, "type": "SMART_PDU",
                     "controller": "VAR-SOM-MX7", "rated_current": 32.0},
        "license": {
            "type_id": "B2",
            "wifi_licensed": True,
            "outlet_switch_licensed": True,
            "outlet_metering_licensed": True,
        },
        "nms": {"system_name": "Rack PDU",
                "system_contact": "noc@example.com",
                "system_location": "Rack A"},
        "switches": {"branch": 0, "sys_type": 2, "curr_type": 0},
        "inputs": [
            {"current": 1.24, "voltage": 229.6, "energy": 1000.0,
             "active_power": 270.1, "reactive_power": 30.2,
             "apparent_power": 285.0, "power_factor": 0.95,
             "phase": 18.2, "frequency": 50.01},
            {"current": 2.25, "voltage": 230.4, "energy": 2000.0,
             "active_power": 490.0, "reactive_power": 50.0,
             "apparent_power": 518.4, "power_factor": 0.945,
             "phase": 19.1, "frequency": 50.02},
            {"current": 3.26, "voltage": 231.5, "energy": 3000.0,
             "active_power": 700.0, "reactive_power": 70.0,
             "apparent_power": 754.7, "power_factor": 0.928,
             "phase": 21.8, "frequency": 50.03},
            None, None, None,
        ],
        "pdus": [[{
            "line_id": 0, "number": "1", "description": "Output 1",
            "socket": "C13", "low_limit": 0.0, "high_limit": 5.0,
            "on": True, "relay_writable": True,
            "data": {"voltage": 230.1, "current": 1.24,
                     "active_power": 270.1, "reactive_power": 30.2,
                     "apparent_power": 285.0, "power_factor": 0.95,
                     "phase": 18.2, "frequency": 50.01,
                     "energy": 123.4, "fuse": 1},
        }], [], [], []],
        "sensors": [{
            "api_id": 7, "mac": "C2:03:03:00:19:60",
            "name": "MST01", "type": "MST01", "location": "",
            "description": "MST01", "temperature": 33.51,
            "humidity": 61.0, "rssi": -72, "battery_mv": 2500,
            "data_datetime": "2026-08-29T20:00:00",
        }],
        "communications": {
            "network": {
                "dhcp": False, "eth_interface": "eth1", "nw_mode": 2,
                "params": {"ip": "192.168.1.100",
                           "subnet_mask": "255.255.255.0",
                           "gateway_ip": "192.168.1.1", "dns": "8.8.8.8",
                           "ssid": "PDU-WIFI"},
                "lan1_ip": "192.168.1.100",
                "lan1_gateway": "192.168.1.1",
                "lan2_ip": "192.168.2.100",
                "lan2_gateway": "192.168.2.1", "wifi_ip": "",
                "ethernet_mac": "00:11:22:33:44:55",
                "wifi_mac": "00:11:22:33:44:66",
            },
            "network_info": {"connected": True},
            "services": {"ssh": True, "snmp": True, "modbus": True},
            "snmp": {
                "enabled": True, "version": "V3", "port": 161,
                "set_enabled": True, "traps_enabled": True,
                "v3_user": "operator", "v3_security_level": "authPriv",
                "v3_access_right": "readWrite", "v3_auth_algorithm": "SHA",
                "v3_privacy_algorithm": "AES", "v3_configured": True,
                "trap_managers": [{"name": "NMS 1",
                                   "address": "192.0.2.10"}],
            },
            "modbus": {"addr": 125},
            "ntp": {"enabled": True, "server": "pool.ntp.org",
                    "time_offset": 2, "running": True,
                    "synchronized": True},
            "email_web": {
                "web_protocol": "https", "web_port": 443,
                "smtp_server": "smtp.example.com", "smtp_port": 587,
                "smtp_auth": "login", "from_address": "pdu@example.com",
                "password_configured": True,
                "recipients": ["noc@example.com"],
            },
            "bluetooth": {"controller_mac": "AA:BB:CC:DD:EE:FF",
                          "name": "NET-POWER", "powered": True,
                          "pairable": False, "discoverable": False,
                          "discovering": False, "device_count": 1},
        },
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

    def test_product_groups_start_at_one_and_are_contiguous(self):
        self.assertEqual(".1.3.6.1.4.1.66547.1", BASE_OID)
        self.assertEqual(BASE_OID + ".1", DEVICE_OID)
        self.assertEqual(BASE_OID + ".2", INPUT_OID)
        self.assertEqual(BASE_OID + ".3.1.1", NATIVE_OUTLET_OID)
        self.assertEqual(BASE_OID + ".4.1.1", NATIVE_SENSOR_OID)
        self.assertEqual(BASE_OID + ".5.1", NETWORK_OID)
        self.assertEqual(BASE_OID + ".6", EVENTS_OID)
        self.assertEqual(DEVICE_OID + ".1.0", self.manager.first_oid().oid)

    def test_placeholder_tree_exists_before_initial_backend_refresh(self):
        cold_manager = NeePduOidManager(mock.Mock(), self.state_path + ".cold")

        product_name = cold_manager.get(Oid(DEVICE_OID + ".1.0"))
        self.assertIsNotNone(product_name)
        self.assertEqual("", product_name.value)
        self.assertIsNotNone(cold_manager.get_next_oid(Oid(BASE_OID)))

    def test_cached_snapshot_is_available_to_replacement_helper(self):
        snapshot_path = self.state_path + ".snapshot"
        first = NeePduOidManager(
            self.backend, self.state_path + ".first", snapshot_path
        )
        first.refresh()

        replacement = NeePduOidManager(
            mock.Mock(), self.state_path + ".replacement", snapshot_path
        )

        self.assertEqual(
            "NET-POWER",
            replacement.get(Oid(DEVICE_OID + ".1.0")).value,
        )
        self.assertEqual(
            "3351",
            replacement.get(Oid(NATIVE_SENSOR_OID + ".7.1")).value,
        )

    def test_device_identity_and_scaling(self):
        self.assertEqual("NET-POWER", self.value(DEVICE_OID + ".1.0"))
        self.assertEqual("ABCDEF1234", self.value(DEVICE_OID + ".3.0"))
        self.assertEqual("VAR-SOM-MX7", self.value(DEVICE_OID + ".4.0"))
        self.assertEqual("320", self.value(DEVICE_OID + ".6.0"))
        self.assertEqual("Rack A", self.value(DEVICE_OID + ".17.0"))
        self.assertEqual("1", self.value(DEVICE_OID + ".18.0"))
        self.assertEqual("1", self.value(DEVICE_OID + ".19.0"))
        self.assertEqual("1", self.value(DEVICE_OID + ".20.0"))

    def test_two_input_three_phase_topology_and_measurements(self):
        self.backend.data["switches"]["branch"] = 1
        self.backend.data["inputs"] += self.backend.data["inputs"][:3]
        self.manager.refresh()
        self.assertEqual("2", self.value(INPUT_OID + ".3.0"))
        self.assertEqual("3", self.value(INPUT_ENTRY_OID + ".5.1"))
        self.assertEqual("2296", self.value(INPUT_PHASE_ENTRY_OID + ".4.1.1"))
        self.assertEqual("1240", self.value(INPUT_PHASE_ENTRY_OID + ".5.1.1"))
        self.assertEqual("2701", self.value(INPUT_PHASE_ENTRY_OID + ".6.1.1"))
        self.assertEqual("5001", self.value(INPUT_PHASE_ENTRY_OID + ".11.1.1"))

    def test_outlet_contains_status_metering_and_capabilities(self):
        expected = {2: "Output 1", 3: "C13", 4: "1", 5: "1",
                    6: "2301", 7: "1240", 8: "2701", 14: "1234",
                    15: "0", 16: "50", 17: "1", 18: "1"}
        for column, value in expected.items():
            self.assertEqual(value, self.value(
                f"{NATIVE_OUTLET_OID}.{column}.1"))

    def test_outlet_set_validation_and_real_relay_call(self):
        self.assertEqual(SnmpSetError.SUCCESS, self.manager.set(
            Oid(NATIVE_OUTLET_OID + ".2.1"), "string", "Rack outlet"))
        self.assertEqual(SnmpSetError.SUCCESS, self.manager.set(
            Oid(NATIVE_OUTLET_OID + ".16.1"), "integer", "60"))
        self.assertEqual(SnmpSetError.SUCCESS, self.manager.set(
            Oid(NATIVE_OUTLET_OID + ".5.1"), "integer", "2"))
        self.assertEqual([(0, False)], self.backend.relay_calls)
        self.assertEqual("2", self.value(NATIVE_OUTLET_OID + ".5.1"))
        self.assertEqual(SnmpSetError.WRONG_VALUE, self.manager.set(
            Oid(NATIVE_OUTLET_OID + ".5.1"), "integer", "3"))

    def test_mst01_values_location_and_limits(self):
        expected = {2: "7", 3: "C2:03:03:00:19:60", 5: "MST01",
                    7: "3351", 10: "610", 13: "-72", 14: "2500",
                    15: "2026-08-29T20:00:00"}
        for column, value in expected.items():
            self.assertEqual(value, self.value(
                f"{NATIVE_SENSOR_OID}.{column}.1"))
        self.assertEqual(SnmpSetError.NOT_WRITABLE, self.manager.set(
            Oid(NATIVE_SENSOR_OID + ".5.1"), "string", "Wrong column"))
        for column, value in ((6, "Cold aisle"), (8, "1000"),
                              (9, "4000"), (11, "200"), (12, "800")):
            value_type = "string" if column == 6 else "integer"
            self.assertEqual(SnmpSetError.SUCCESS, self.manager.set(
                Oid(f"{NATIVE_SENSOR_OID}.{column}.1"), value_type, value))
            self.assertEqual(value, self.value(
                f"{NATIVE_SENSOR_OID}.{column}.1"))

    def test_unsupported_sensor_values_do_not_exist(self):
        self.assertIsNone(self.manager.get(Oid(NATIVE_SENSOR_OID + ".16.1")))

    def test_communications_are_read_only_and_exclude_secrets(self):
        self.assertEqual("192.168.1.1", self.value(NETWORK_OID + ".10.0"))
        self.assertEqual("192.168.2.1", self.value(NETWORK_OID + ".12.0"))
        self.assertEqual("1", self.value(SERVICE_OID + ".2.0"))
        self.assertEqual("3", self.value(SNMP_CONFIG_OID + ".2.0"))
        self.assertEqual("125", self.value(MODBUS_OID + ".2.0"))
        self.assertEqual("pool.ntp.org", self.value(NTP_OID + ".2.0"))
        self.assertEqual("smtp.example.com", self.value(WEB_EMAIL_OID + ".3.0"))
        self.assertEqual("1", self.value(BLUETOOTH_OID + ".7.0"))
        self.assertEqual("192.0.2.10", self.value(
            TRAP_MANAGER_ENTRY_OID + ".3.1"))
        self.assertEqual("noc@example.com", self.value(
            EMAIL_RECIPIENT_ENTRY_OID + ".2.1"))
        self.assertNotIn("secret", repr(self.manager.snapshot).lower())
        self.assertEqual(SnmpSetError.NOT_WRITABLE, self.manager.set(
            Oid(NETWORK_OID + ".10.0"), "string", "192.0.2.1"))

    def test_getnext_crosses_all_product_groups(self):
        boundaries = ((DEVICE_OID + ".99", INPUT_ENTRY_OID + ".1.1"),
                      (INPUT_OID + ".99", NATIVE_OUTLET_OID + ".1.1"),
                      (NATIVE_OUTLET_OID + ".99", NATIVE_SENSOR_OID + ".1.1"),
                      (NATIVE_SENSOR_OID + ".99", NETWORK_OID + ".1.0"),
                      (BLUETOOTH_OID + ".99", SYS_OID + ".1.0"))
        for current, expected in boundaries:
            self.assertEqual(expected, self.manager.get_next_oid(
                Oid(current)).oid)
        self.assertIsNone(self.manager.get_next_oid(Oid(SYS_OID + ".99")))

    def test_unavailable_measurements_are_not_zero(self):
        self.backend.data["inputs"] = []
        self.backend.data["pdus"][0][0]["data"] = None
        self.manager.refresh()
        self.assertEqual(str(NATIVE_UNAVAILABLE), self.value(
            INPUT_PHASE_ENTRY_OID + ".4.1.1"))
        self.assertEqual(str(NATIVE_UNAVAILABLE), self.value(
            NATIVE_OUTLET_OID + ".7.1"))

    def test_fuse_and_load_traps_are_transition_only(self):
        self.backend.data["pdus"][0][0]["data"]["fuse"] = 2
        events = self.manager.refresh()
        self.assertEqual([EVENTS_OID + ".1"],
                         [event["notification_oid"] for event in events])
        self.assertEqual([], self.manager.refresh())
        self.backend.data["pdus"][0][0]["data"]["current"] = 7.0
        events = self.manager.refresh()
        self.assertEqual([EVENTS_OID + ".2"],
                         [event["notification_oid"] for event in events])
        self.assertEqual("70", self.value(SYS_OID + ".6.0"))

    def test_temperature_and_humidity_traps_use_product_scaling(self):
        for column, value in ((8, "3000"), (9, "4000"),
                              (11, "200"), (12, "800")):
            self.assertEqual(SnmpSetError.SUCCESS, self.manager.set(
                Oid(f"{NATIVE_SENSOR_OID}.{column}.1"), "integer", value))
        self.manager.refresh()
        self.backend.data["sensors"][0]["temperature"] = 45.0
        events = self.manager.refresh()
        self.assertEqual(EVENTS_OID + ".3", events[0]["notification_oid"])
        self.assertEqual(4500, events[0]["value"])
        self.backend.data["sensors"][0]["temperature"] = 35.0
        self.manager.refresh()
        self.backend.data["sensors"][0]["humidity"] = 90.0
        events = self.manager.refresh()
        self.assertEqual(EVENTS_OID + ".4", events[0]["notification_oid"])
        self.assertEqual(900, events[0]["value"])

    def test_failed_persistence_rolls_back_value(self):
        oid = Oid(NATIVE_OUTLET_OID + ".2.1")
        original = self.value(oid.oid)
        with mock.patch.object(
                self.manager.state, "save", side_effect=OSError("disk full")):
            self.assertEqual(SnmpSetError.INCONSISTENT_VALUE,
                             self.manager.set(oid, "string", "Changed"))
        self.assertEqual(original, self.value(oid.oid))

    def test_index_and_type_bounds(self):
        self.assertEqual(SnmpSetError.WRONG_TYPE, self.manager.set(
            Oid(NATIVE_OUTLET_OID + ".5.1"), "string", "2"))
        self.assertEqual(SnmpSetError.NOT_WRITABLE, self.manager.set(
            Oid(NATIVE_OUTLET_OID + ".5.24"), "integer", "1"))
        self.assertEqual(SnmpSetError.WRONG_VALUE, self.manager.set(
            Oid(NATIVE_SENSOR_OID + ".9.1"), "integer", "13000"))


if __name__ == "__main__":
    unittest.main()
