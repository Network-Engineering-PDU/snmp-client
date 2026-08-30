import json
import os
import stat
import tempfile
import unittest

from ttsnmp.volatile_snapshot import VolatileSnapshot


class VolatileSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "snapshot.json")
        self.store = VolatileSnapshot(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_atomic_round_trip_and_private_permissions(self):
        snapshot = {"system": {"product_name": "NET-POWER"}}

        self.store.save(snapshot)

        self.assertEqual(snapshot, self.store.load())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.path).st_mode))

    def test_missing_malformed_and_non_object_cache_are_ignored(self):
        self.assertEqual({}, self.store.load())
        for value in ("not json", json.dumps([1, 2, 3])):
            with open(self.path, "w", encoding="utf-8") as snapshot_file:
                snapshot_file.write(value)
            self.assertEqual({}, self.store.load())


if __name__ == "__main__":
    unittest.main()
