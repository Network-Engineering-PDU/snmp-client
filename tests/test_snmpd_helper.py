import unittest
from unittest import mock

from ttsnmp.nee_mib_schema import BASE_OID, DEVICE_OID
from ttsnmp.oid import Oid, OidType
from ttsnmp.snmpd_helper import SnmpdHelper


class SnmpdHelperColdStartTest(unittest.TestCase):
    def setUp(self):
        self.helper = SnmpdHelper()
        self.helper.nee_manager = mock.Mock()
        self.helper.update_attempted = mock.Mock()

    def test_get_does_not_wait_for_initial_hardware_refresh(self):
        requested = Oid(DEVICE_OID + ".1.0")
        result = Oid(requested.oid, OidType.STRING, "NET-POWER")
        self.helper.nee_manager.get.return_value = result

        with mock.patch.object(self.helper, "print") as output:
            self.helper.get_and_print(requested)

        self.helper.update_attempted.wait.assert_not_called()
        output.assert_has_calls([
            mock.call(requested.oid),
            mock.call(OidType.STRING),
            mock.call("NET-POWER"),
        ])

    def test_getnext_does_not_wait_for_initial_hardware_refresh(self):
        requested = Oid(BASE_OID)
        result = Oid(DEVICE_OID + ".1.0", OidType.STRING, "")
        self.helper.nee_manager.get_next_oid.return_value = result

        next_oid = self.helper.get_next_oid(requested)

        self.helper.update_attempted.wait.assert_not_called()
        self.assertEqual(result, next_oid)


if __name__ == "__main__":
    unittest.main()
