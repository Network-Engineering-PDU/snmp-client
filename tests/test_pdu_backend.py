import unittest
from unittest import mock

from ttsnmp.pdu_backend import PduBackend


class PduBackendTest(unittest.TestCase):

    def backend_with(self, license_type="A2", outlet_count=0):
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
            "settings/license": {"type_id": license_type},
            "settings/snmp-nms": {},
            "inputs/switches": {"branch": 0, "sys_type": 0},
            "network/snmp/detailed-settings": {},
            "network/snmp/settings": {
                "trap_alarm": True,
                "refresh_period": 60,
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

    def test_unlicensed_pdu_does_not_expose_metering_tables(self):
        snapshot = self.backend_with(
            license_type="A1", outlet_count=1
        ).snapshot()
        self.assertEqual(0, snapshot["summary_count"])
        self.assertEqual([[], [], [], []], snapshot["pdus"])


if __name__ == "__main__":
    unittest.main()
