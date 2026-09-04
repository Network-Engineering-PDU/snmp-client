import unittest
from unittest import mock

from ttsnmp.pdu_backend import PduApiError, PduBackend


class PduBackendTest(unittest.TestCase):

    def backend_with(self, license_type="A2", outlet_count=0,
                     wifi_licensed=False):
        backend = PduBackend()
        responses = {
            "settings/system-info": {
                "product_name": "NET-POWER",
                "product_pn": "NE0001001000",
                "product_sn": "ABCDEF1234",
            },
            "settings/pdu-info": {
                "outlet_count": outlet_count,
                "type": "SMART_PDU",
            },
            "settings/license": {
                "type_id": license_type,
                "wifi_licensed": wifi_licensed,
            },
            "settings/snmp-nms": {},
            "inputs/switches": {"branch": 0, "sys_type": 0},
            "network/snmp/detailed-settings": {},
            "network/snmp/settings": {
                "trap_alarm": True,
                "refresh_period": 60,
            },
            "network/interfaces": {
                "type": 1,
                "dhcp": False,
                "params": {
                    "ip": "192.168.1.100",
                    "subnet_mask": "255.255.255.0",
                    "gateway_ip": "192.168.1.1",
                    "dns": "8.8.8.8",
                    "ssid": "",
                },
                "eth_interface": "eth1",
                "nw_mode": 2,
                "lan1_ip": "192.168.1.100",
                "lan1_gateway": "192.168.1.1",
                "lan2_ip": "192.168.2.100",
                "lan2_gateway": "192.168.2.1",
                "wifi_ip": "",
                "ethernet_mac": "00:11:22:33:44:55",
                "wifi_mac": "00:11:22:33:44:66",
            },
            "network/info": {"connected": True},
            "network/services": {
                "ssh": True, "snmp": True, "modbus": True,
            },
            "settings/ntp": {
                "enabled": True,
                "server": "pool.ntp.org",
                "time_offset": 2,
                "running": True,
                "synchronized": True,
            },
            "settings/modbus": {"addr": 125},
            "email-web": {
                "web_protocol": "https",
                "web_port": 443,
                "smtp_server": "smtp.example.com",
                "smtp_port": 587,
                "smtp_auth": "login",
                "from_address": "pdu@example.com",
                "password_configured": True,
                "recipients": ["noc@example.com"],
            },
            "settings/bluetooth": {
                "controller_mac": "AA:BB:CC:DD:EE:FF",
                "name": "NET-POWER",
                "powered": True,
                "pairable": False,
                "discoverable": False,
                "discovering": False,
                "pairing_passkey": "must-not-leak",
                "devices": [{"mac": "11:22:33:44:55:66"}],
            },
            "outputs/": [{
                "line_id": 1,
                "name": "Outlet one",
                "socket_type": "C13",
                "low_limit": 0.5,
                "high_limit": 5.0,
            }],
            "outputs/0/data": {
                "current": 1.2,
                "fuse": 1,
            },
            "outputs/0/switch-status": {"switch_status": True},
        }
        for line_id in range(6):
            responses[f"inputs/{line_id}/data"] = {
                "current": 0.0,
                "voltage": 230.0,
                "energy": 0.0,
            }
        backend._request = mock.Mock(
            side_effect=lambda method, path, payload=None: responses[path]
        )
        backend._sensor_request = mock.Mock(return_value=[])
        return backend

    def test_licensed_pdu_has_summary_without_outlets(self):
        snapshot = self.backend_with(outlet_count=0).snapshot()
        self.assertEqual(1, snapshot["summary_count"])
        self.assertEqual([[], [], [], []], snapshot["pdus"])
        self.assertEqual(
            60, snapshot["trap_settings"]["refresh_period"]
        )

    def test_outlet_metadata_and_control_map_to_real_api(self):
        backend = self.backend_with(outlet_count=1)
        snapshot = backend.snapshot()
        outlet = snapshot["pdus"][0][0]
        self.assertEqual(0, outlet["line_id"])
        self.assertEqual("Outlet one", outlet["description"])
        self.assertEqual("C13", outlet["socket"])
        self.assertTrue(outlet["on"])

        backend.set_outlet(0, False)
        backend._request.assert_called_with(
            "PUT", "outputs/0/switch-status",
            {"switch_status": False},
        )

    def test_a1_pdu_keeps_tree_but_marks_metering_unavailable(self):
        snapshot_backend = self.backend_with(
            license_type="A1", outlet_count=1
        )
        snapshot = snapshot_backend.snapshot()
        self.assertEqual(1, snapshot["summary_count"])
        self.assertEqual([], snapshot["inputs"])
        self.assertEqual(1, len(snapshot["pdus"][0]))
        outlet = snapshot["pdus"][0][0]
        self.assertIsNone(outlet["data"])
        self.assertTrue(outlet["on"])
        self.assertFalse(outlet["relay_writable"])
        requested_paths = [
            call.args[1] for call in snapshot_backend._request.call_args_list
        ]
        self.assertIn("inputs/switches", requested_paths)
        self.assertFalse(any(
            path.startswith("inputs/") and path.endswith("/data")
            for path in requested_paths
        ))

    def test_b1_pdu_exposes_relay_without_metering(self):
        snapshot = self.backend_with(
            license_type="B1", outlet_count=1
        ).snapshot()
        outlet = snapshot["pdus"][0][0]
        self.assertIsNone(outlet["data"])
        self.assertTrue(outlet["on"])
        self.assertTrue(outlet["relay_writable"])

    def test_license_capabilities_are_normalized_for_whole_pdu(self):
        snapshot = self.backend_with(
            license_type="B2", wifi_licensed=True
        ).snapshot()

        self.assertEqual({
            "type_id": "B2",
            "wifi_licensed": True,
            "outlet_switch_licensed": True,
            "outlet_metering_licensed": True,
        }, snapshot["license"])

    def test_subscribed_sensor_maps_to_environment_table_data(self):
        backend = self.backend_with(license_type="A1")
        stored = [{
            "id": 7,
            "mac_address": "C2:03:03:00:19:60",
            "name": "MST01",
            "last_data": {
                "data_datetime": "2026-08-29T20:00:00Z",
                "temperature": 33.51,
                "humidity": 61.0,
                "pressure": 1012.4,
                "rssi": -72,
                "battery": 2.5,
            },
        }]
        backend._sensor_request.side_effect = lambda method, path, payload=None: (
            stored if path == "sensors-data/" else {
                "devices": [{
                    "mac": "C2:03:03:00:19:60",
                    "kind": "MST01",
                    "temperature_c": 34.25,
                    "humidity_pct": 62.0,
                    "pressure_hpa": 1013.2,
                    "rssi": -70,
                    "battery_mv": 2510,
                    "battery_pct": 83,
                }]
            }
        )

        sensors = backend.snapshot()["sensors"]

        self.assertEqual(1, len(sensors))
        self.assertEqual("1-S7", sensors[0]["number"])
        self.assertEqual("MST01", sensors[0]["type"])
        self.assertEqual("001960", sensors[0]["id"])
        self.assertEqual(34.25, sensors[0]["temperature"])
        self.assertEqual(62.0, sensors[0]["humidity"])
        self.assertEqual(-70, sensors[0]["rssi"])
        self.assertEqual(2510, sensors[0]["battery_mv"])
        self.assertNotIn("pressure", sensors[0])
        self.assertNotIn("battery_pct", sensors[0])
        self.assertNotIn("wind", sensors[0])

    def test_communications_are_whitelisted_and_secrets_are_excluded(self):
        backend = self.backend_with()
        detailed = {
            "port": 161,
            "version": "V3",
            "set_enabled": True,
            "snmp_v1_v2c": {
                "read_community": "secret-read",
                "write_community": "secret-write",
            },
            "snmp_v3": {
                "usm_user": "operator",
                "security_level": "authPriv",
                "access_right": "readWrite",
                "auth_algorithm": "SHA",
                "auth_pwd": "secret-auth",
                "privacy_algorithm": "AES",
                "privacy_pwd": "secret-priv",
            },
            "trap": {"alarm": True, "manager_1_ip": "192.0.2.10"},
        }
        original = backend._request.side_effect
        backend._request.side_effect = lambda method, path, payload=None: (
            detailed if path == "network/snmp/detailed-settings"
            else original(method, path, payload)
        )

        communications = backend.snapshot()["communications"]
        rendered = repr(communications)

        self.assertEqual("V3", communications["snmp"]["version"])
        self.assertEqual("operator", communications["snmp"]["v3_user"])
        self.assertNotIn("secret-read", rendered)
        self.assertNotIn("secret-write", rendered)
        self.assertNotIn("secret-auth", rendered)
        self.assertNotIn("secret-priv", rendered)
        self.assertNotIn("must-not-leak", rendered)

    def test_sensor_api_failure_produces_no_rows(self):
        backend = self.backend_with()
        backend._sensor_request.side_effect = PduApiError("unreachable")

        self.assertEqual([], backend.snapshot()["sensors"])


if __name__ == "__main__":
    unittest.main()
