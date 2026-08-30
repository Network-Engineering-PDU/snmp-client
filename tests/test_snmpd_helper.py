import io
import unittest
from unittest import mock

from ttsnmp.nee_mib_schema import BASE_OID, DEVICE_OID
from ttsnmp.oid import Oid, OidType
from ttsnmp.snmpd_helper import (
    NEE_SNAPSHOT_FILE, NEE_STATE_FILE, SnmpdHelper,
)


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
            mock.call(requested.oid, mock.ANY),
            mock.call(OidType.STRING, mock.ANY),
            mock.call("NET-POWER", mock.ANY),
        ])

    def test_getnext_does_not_wait_for_initial_hardware_refresh(self):
        requested = Oid(BASE_OID)
        result = Oid(DEVICE_OID + ".1.0", OidType.STRING, "")
        self.helper.nee_manager.get_next_oid.return_value = result

        next_oid = self.helper.get_next_oid(requested)

        self.helper.update_attempted.wait.assert_not_called()
        self.assertEqual(result, next_oid)

    def test_blank_set_separator_is_not_treated_as_eof(self):
        output = io.StringIO()

        self.helper.main(io.StringIO("\nPING\n"), output)

        self.assertEqual("PONG\n", output.getvalue())

    @mock.patch("ttsnmp.snmpd_helper.NeePduOidManager")
    @mock.patch("ttsnmp.snmpd_helper.LazyPduBackend")
    def test_warm_cache_performs_one_synchronous_refresh(
            self, backend_type, manager_type):
        manager = manager_type.return_value

        SnmpdHelper().ne_warm_cache()

        manager_type.assert_called_once_with(
            backend_type.return_value, NEE_STATE_FILE, NEE_SNAPSHOT_FILE
        )
        manager.refresh.assert_called_once_with()

    def test_cached_snapshot_allows_set_before_api_refresh(self):
        helper = SnmpdHelper()
        manager = mock.Mock()
        manager.snapshot = {"system": {"product_name": "NET-POWER"}}
        with mock.patch("ttsnmp.snmpd_helper.NeePduOidManager",
                        return_value=manager), mock.patch(
                            "ttsnmp.snmpd_helper.LazyPduBackend"), mock.patch(
                                "ttsnmp.snmpd_helper.threading.Thread"), \
                mock.patch.object(helper, "main"):
            helper.ne_run()

        self.assertTrue(helper.update_attempted.is_set())


if __name__ == "__main__":
    unittest.main()
