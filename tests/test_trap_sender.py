import unittest
from unittest import mock

from ttsnmp.nee_pdu_oid_manager import EVENTS_OID, SENSOR_OID, SYS_OID
from ttsnmp.trap_sender import (
    SNMP_TRAP_OID,
    SYS_UPTIME_OID,
    TrapSender,
    _object_identifier_value,
)


def event(notification=1):
    return {
        "notification_oid": f"{EVENTS_OID}.{notification}",
        "source": "Outlet_1_1",
        "power_desc": "Rack PDU",
        "source_id": "PDU1",
        "status": "alarm",
        "value": 60,
        "low": 0,
        "high": 50,
    }


class TrapSenderTest(unittest.TestCase):
    def test_packet_is_snmpv2_trap_with_mandatory_varbinds(self):
        packet = TrapSender().build_packet("public", event(2))
        self.assertEqual(0x30, packet[0])
        self.assertIn(b"public", packet)
        self.assertIn(_object_identifier_value(SYS_UPTIME_OID), packet)
        self.assertIn(_object_identifier_value(SNMP_TRAP_OID), packet)
        self.assertIn(_object_identifier_value(f"{EVENTS_OID}.2"), packet)
        self.assertIn(b"Outlet_1_1", packet)
        self.assertIn(b"higher", TrapSender().build_packet(
            "public", {**event(2), "status": "higher"}
        ))

    def test_each_notification_contains_its_mib_objects(self):
        fuse_packet = TrapSender().build_packet("public", event(1))
        self.assertIn(
            _object_identifier_value(SYS_OID + ".3.0"), fuse_packet
        )
        self.assertNotIn(
            _object_identifier_value(SYS_OID + ".6.0"), fuse_packet
        )

        load_packet = TrapSender().build_packet("public", event(2))
        for column in (6, 7, 8):
            self.assertIn(
                _object_identifier_value(f"{SYS_OID}.{column}.0"),
                load_packet,
            )

        for notification, column in ((3, 7), (4, 10)):
            sensor_event = {
                **event(notification),
                "source": "",
                "sensor": "Rack sensor",
                "sensor_index": 2,
                "metric_column": column,
            }
            packet = TrapSender().build_packet("public", sensor_event)
            self.assertIn(
                _object_identifier_value(SYS_OID + ".4.0"), packet
            )
            for value_column in range(column, column + 3):
                self.assertIn(
                    _object_identifier_value(
                        f"{SENSOR_OID}.{value_column}.2"
                    ),
                    packet,
                )

        with self.assertRaises(ValueError):
            TrapSender().build_packet("public", event(5))

    @mock.patch("ttsnmp.trap_sender.socket.socket")
    def test_sends_one_udp_datagram_per_valid_target(self, socket_factory):
        context = socket_factory.return_value.__enter__.return_value
        settings = {
            "trap": {
                "alarm": True,
                "manager_1_ip": "192.0.2.1",
                "manager_2_ip": "192.0.2.1",
                "manager_3_ip": "not a host",
                "manager_4_ip": "traps.example.test",
            },
            "snmp_v1_v2c": {"read_community": "Public"},
        }
        TrapSender(port=1162).send([event()], settings)
        self.assertEqual(2, context.sendto.call_count)
        packet, destination = context.sendto.call_args_list[0].args
        self.assertIn(b"public", packet)
        self.assertEqual(("192.0.2.1", 1162), destination)
        self.assertEqual(
            ("traps.example.test", 1162),
            context.sendto.call_args_list[1].args[1],
        )

    def test_invalid_community_uses_safe_default(self):
        self.assertEqual(
            "public",
            TrapSender._community({
                "snmp_v1_v2c": {
                    "read_community": "bad\ncommunity",
                },
            }),
        )

    @mock.patch("ttsnmp.trap_sender.socket.socket")
    def test_disabled_traps_send_nothing(self, socket_factory):
        TrapSender().send([event()], {"trap": {"alarm": False}})
        socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
