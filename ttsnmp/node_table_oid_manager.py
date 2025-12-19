from typing import List, Tuple
from threading import Lock
from collections import OrderedDict

from .oid import Oid, OidType
from .base_oid_manager import BaseOidManager
from .node_entry import NodeEntry


class SnmpSetError:
    SUCCESS = "DONE"
    NOT_WRITABLE = "not-writable"
    WRONG_TYPE = "wrong-type"
    WRONG_LENGTH = "wrong-length"
    WRONG_VALUE = "wrong-value"
    INCONSISTENT_VALUE = "inconsistent-value"


class NodeTableOidManager(BaseOidManager):
    """ This node table has 25 entries. Some values are fixed, some are
    taken from the nodes. Its values are defined in the get_oid_value
    method.

    The table is located at the oid .1.3.6.1.4.1.318.1.1.10.5.1.1.1.x.y,
    with x being the entry (from 1 to 25) and y being the node number
    (variable)
    """
    ENTRIES = 25

    def __init__(self, node_csv_file):
        self.node_csv_file = node_csv_file
        self.nodes = OrderedDict()
        self.nodes_lock = Lock()
        self.file_lock = Lock()

    def load_node_list(self):
        try:
            with open(self.node_csv_file) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return

        with self.nodes_lock:
            i = 0
            for line in lines:
                if len(line.strip()) == 0:
                    continue
                try:
                    mac, name, location = line.strip().split(",")
                except ValueError:
                    continue
                self.nodes[mac] = NodeEntry(i+1, mac, name, location)
                i += 1

    def save_node_list(self):
        lines = []
        with self.nodes_lock:
            n_nodes = len(self.nodes)

        for index in range(1, n_nodes+1):
            with self.nodes_lock:
                node = list(self.nodes.values())[index-1]
                lines.append(f"{node.mac},{node.name},{node.location}\n")

        with self.file_lock:
            with open(self.node_csv_file, "w") as f:
                f.writelines(lines)

    def _oid(self, entry: int, index: int) -> Oid:
        return Oid(f".1.3.6.1.4.1.318.1.1.10.5.1.1.1.{entry}.{index}")

    def _node_value(self, index: int, attr: str) -> str:
        with self.nodes_lock:
            node = list(self.nodes.values())[index-1]
            return str(getattr(node, attr))

    def get_oid_value(self, entry: int, index: int) -> List[Tuple]:
        """ Entries from 1 to 25. """
        if entry == 1:
            return OidType.INTEGER, str(index)
        if entry == 2:
            return OidType.STRING, self._node_value(index, "mac")
        if entry == 3:
            return OidType.STRING, self._node_value(index, "name")
        if entry == 5:
            return OidType.INTEGER, self._node_value(index, "temp")
        if entry == 8:
            return OidType.INTEGER, self._node_value(index, "hum")
        if entry == 11:
            return OidType.INTEGER, self._node_value(index, "status")
        if entry == 16:
            return OidType.INTEGER, self._node_value(index, "bat")
        if entry == 19:
            return OidType.INTEGER, self._node_value(index, "rssi")
        if entry == 22:
            return OidType.STRING, self._node_value(index, "location")
        if entry == 24:
            return OidType.INTEGER, self._node_value(index, "press")
        if entry == 25:
            return OidType.INTEGER, self._node_value(index, "power")
        # Fixed values
        if entry in (4, 23):
            return OidType.INTEGER, str(0)
        if entry in (6, 7, 9, 10, 12, 13, 14, 15):
            return OidType.INTEGER, str(-1)
        if entry == 17:
            return OidType.INTEGER, str(25)
        if entry == 18:
            return OidType.INTEGER, str(22)
        if entry == 20:
            return OidType.INTEGER, str(30)
        if entry == 21:
            return OidType.INTEGER, str(10)
        return None

    def first_oid(self) -> Oid:
        return Oid(".1.3.6.1.4.1.318.1.1.10.5.1.1.1.1.1")

    def last_oid(self) -> Oid:
        with self.nodes_lock:
            return Oid(f".1.3.6.1.4.1.318.1.1.10.5.1.1.1.{self.ENTRIES}.{len(self.nodes)}")

    def get(self, oid: Oid) -> [str, str]:
        if (oid < self.first_oid() or oid > self.last_oid()
                or oid.rstrip(2) != ".1.3.6.1.4.1.318.1.1.10.5.1.1.1"):
            return None

        entry = oid.get(-2)
        index = oid.get(-1)
        with self.nodes_lock:
            n_nodes = len(self.nodes)

        if (entry < 1 or entry > self.ENTRIES
                or index < 1 or index > n_nodes):
            return None
        oid_type, value = self.get_oid_value(entry, index)
        return Oid(oid.oid, oid_type, value)

    def get_next_oid(self, oid: Oid) -> Oid:
        with self.nodes_lock:
            n_nodes = len(self.nodes)

        if n_nodes == 0 or oid > self.last_oid():
            return None

        if oid < self.first_oid():
            return self.first_oid()

        if oid.rstrip(2) == ".1.3.6.1.4.1.318.1.1.10.5.1.1.1":
            entry = oid.get(-2)
            index = oid.get(-1)
            with self.nodes_lock:
                n_nodes = len(self.nodes)
            if index < n_nodes:
                index += 1
                return Oid(f".1.3.6.1.4.1.318.1.1.10.5.1.1.1.{entry}.{index}")
            index = 1
            if entry < self.ENTRIES:
                entry += 1
                return Oid(f".1.3.6.1.4.1.318.1.1.10.5.1.1.1.{entry}.{index}")

        elif oid.rstrip(1) == ".1.3.6.1.4.1.318.1.1.10.5.1.1.1":
            entry = oid.get(-1)
            index = 1
            if entry <= self.ENTRIES:
                return Oid(f".1.3.6.1.4.1.318.1.1.10.5.1.1.1.{entry}.{index}")

        return None


    def set(self, oid: Oid, _type: str, value: str) -> str:
        if (oid < self.first_oid() or oid > self.last_oid()
                or oid.rstrip(2) != ".1.3.6.1.4.1.318.1.1.10.5.1.1.1"):
            return SnmpSetError.NOT_WRITABLE

        entry = oid.get(-2)
        index = oid.get(-1)
        with self.nodes_lock:
            n_nodes = len(self.nodes)

        if (entry < 1 or entry > self.ENTRIES
                or index < 1 or index > n_nodes):
            return SnmpSetError.NOT_WRITABLE

        if entry not in (3, 22): # Only name and location are writable
            return SnmpSetError.NOT_WRITABLE

        if _type.lower() != str(OidType.STRING): # and both are strings
            return SnmpSetError.WRONG_TYPE

        if len(value) > 200: # Max allowed value length
            return SnmpSetError.WRONG_LENGTH

        if entry == 3:
            with self.nodes_lock:
                node = list(self.nodes.values())[index-1]
                node.name = value
        elif entry == 22:
            with self.nodes_lock:
                node = list(self.nodes.values())[index-1]
                node.location = value

        self.save_node_list()
        return SnmpSetError.SUCCESS

    def update(self, data):
        nodes_not_present = False
        for mac, node_data in data.items():
            with self.nodes_lock:
                present = mac in self.nodes
                next_index = len(self.nodes) + 1
            if not present:
                nodes_not_present = True
                name = f"node{len(self.nodes)+1:04d}"
                location = "Undefined location"
                with self.nodes_lock:
                    self.nodes[mac] = NodeEntry(next_index, mac, name, location)
            with self.nodes_lock:
                self.nodes[mac].update(node_data)
        if nodes_not_present:
            self.save_node_list()
