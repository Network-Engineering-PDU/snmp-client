import sys
import json
import time
import socket
import threading
import logging


from . import fixed_oids
from .oid import Oid
from .fixed_oid_manager import FixedOidManager
from .node_table_oid_manager import NodeTableOidManager
from .nee_pdu_oid_manager import NeePduOidManager
from .pdu_api_error import PduApiError
from .trap_sender import TrapSender

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
#logger.addHandler(logging.FileHandler("/tmp/output.log"))

GW_APP_SOCKET = "/tmp/ttgw_snmp.socket"
SOCKET_READ_PERIOD = 120
NODE_CSV_FILE = "/home/root/snmp/node_list.csv"
NEE_STATE_FILE = "/home/root/snmp/nee_mib_state.json"
NEE_SNAPSHOT_FILE = "/var/run/nesnmp_snapshot.json"


class LazyPduBackend:
    """Delay importing requests until the background refresh actually runs."""

    def __init__(self):
        self.backend = None

    def _instance(self):
        if self.backend is None:
            from .pdu_backend import PduBackend
            self.backend = PduBackend()
        return self.backend

    def snapshot(self):
        return self._instance().snapshot()

    def set_outlet(self, line_id, enabled):
        return self._instance().set_outlet(line_id, enabled)


class SnmpdHelper:
    def __init__(self):
        self.fixed_manager = FixedOidManager()
        self.data = None
        self.data_lock = threading.Lock()

        self.node_table_manager = None
        self.nee_manager = None
        self.update_attempted = threading.Event()

    def run(self):
        self.update_th = threading.Thread(target=self.read_from_gw, daemon=True)
        self.fixed_manager.set_oids(sorted(fixed_oids.oid_list))
        self.node_table_manager = NodeTableOidManager(NODE_CSV_FILE)

        self.node_table_manager.load_node_list()
        self.update_th.start()
        self.main()

    def ne_initialize(self):
        self.nee_manager = NeePduOidManager(
            LazyPduBackend(), NEE_STATE_FILE, NEE_SNAPSHOT_FILE
        )
        if self.nee_manager.snapshot:
            self.update_attempted.set()
        self.update_th = threading.Thread(target=self.read_from_ttne,
                daemon=True)
        self.update_th.start()

    def ne_run(self):
        self.ne_initialize()
        self.main()

    def ne_warm_cache(self):
        """Populate the volatile live-data cache before snmpd accepts traffic."""
        manager = NeePduOidManager(
            LazyPduBackend(), NEE_STATE_FILE, NEE_SNAPSHOT_FILE
        )
        manager.refresh()

    def read_from_gw(self):
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(GW_APP_SOCKET)
            except (FileNotFoundError, ConnectionRefusedError) as e:
                logger.info(f"Read thread exception: {e}")
                time.sleep(SOCKET_READ_PERIOD)
                continue

            msg = bytearray()
            while True:
                read = s.recv(4096)
                if not read:
                    break
                msg += read
            s.close()

            data = json.loads(msg.decode())
            self.node_table_manager.update(data)
            logger.info("Read")
            time.sleep(SOCKET_READ_PERIOD)

    def read_from_ttne(self):
        trap_sender = TrapSender()
        while True:
            try:
                events = self.nee_manager.refresh()
                trap_sender.send(
                    events,
                    self.nee_manager.snapshot.get("trap_settings", {}),
                )
                logger.info("Updated Nee-MIB data")
            except PduApiError as exc:
                logger.warning("PDU API unavailable: %s", exc)
            except Exception:
                # A malformed hardware response must not permanently kill the
                # daemon's only data-update thread.
                logger.exception("Unexpected Nee-MIB update failure")
            finally:
                self.update_attempted.set()
            refresh_period = self.nee_manager.snapshot.get(
                "trap_settings", {}
            ).get("refresh_period", SOCKET_READ_PERIOD)
            if isinstance(refresh_period, bool) or not isinstance(
                    refresh_period, int):
                refresh_period = SOCKET_READ_PERIOD
            time.sleep(min(max(refresh_period, 1), 3600))

    def input(self, reader):
        raw = reader.readline()
        if raw == "":
            logger.info("Input: EOF")
            return None
        line = raw.strip()
        logger.info(f"Input: {line}")
        return line

    def print(self, line: str, writer):
        logger.info(f"Output {line}")
        print(line, file=writer, flush=True)

    def get_and_print(self, oid: Oid, writer=sys.stdout) -> None:
        result = self.fixed_manager.get(oid)
        if result is None and self.nee_manager is not None:
            result = self.nee_manager.get(oid)
        if result is None and self.node_table_manager != None:
            result = self.node_table_manager.get(oid)
        if result is None:
            self.print("NONE", writer)
            return

        self.print(oid.oid, writer)
        self.print(result.oidtype, writer)
        self.print(result.value, writer)

    def get_next_oid(self, oid: Oid) -> Oid:
        candidates = [self.fixed_manager.get_next_oid(oid)]
        if self.nee_manager is not None:
            candidates.append(self.nee_manager.get_next_oid(oid))
        if self.node_table_manager is not None:
            candidates.append(self.node_table_manager.get_next_oid(oid))
        candidates = [candidate for candidate in candidates
                      if candidate is not None]
        return min(candidates) if candidates else None

    def main(self, reader=sys.stdin, writer=sys.stdout):
        while True:
# pylint: disable=broad-except
            line = ""
            try:
                line = self.input(reader)
                if line is None:
                    return
                if line == "":
                    # Net-SNMP 5.8 sends an empty transaction separator after
                    # SET. It is not EOF and the persistent helper must stay
                    # available for the next request.
                    continue

                if "PING" == line:
                    self.print("PONG", writer)

                elif "DUMP" == line:
                    if self.node_table_manager is not None:
                        for node in self.node_table_manager.nodes.values():
                            self.print(str(node), writer)

                elif "get" == line:
                    oid_str = self.input(reader)
                    self.get_and_print(Oid(oid_str), writer)

                elif "getnext" == line:
                    oid_str = self.input(reader)
                    oid = self.get_next_oid(Oid(oid_str))
                    if oid is None:
                        self.print("NONE", writer)
                    else:
                        self.get_and_print(oid, writer)

                elif "set" == line:
                    oid_str = self.input(reader)
                    oid = Oid(oid_str)
                    _type, value = self.input(reader).split(" ", 1)
                    value = value.strip("\"")
                    if (self.nee_manager is not None and
                            not self.update_attempted.is_set()):
                        # Writes require a hardware snapshot for validation,
                        # but must fail promptly rather than timing out.
                        self.print("resource-unavailable", writer)
                    elif self.nee_manager is not None:
                        result = self.nee_manager.set(oid, _type, value)
                        self.print(result, writer)
                    elif self.node_table_manager is not None:
                        self.print(self.node_table_manager.set(
                            oid, _type, value
                        ), writer)
                    else:
                        self.print("not-writable", writer)

                else:
                    logger.info(f"Invalid input ({len(line)}): {line}")
            except Exception as e:
                logger.exception(f"Exception: {e}")
                if line == "set":
                    self.print("wrong-value", writer)
                elif line in ("get", "getnext"):
                    self.print("NONE", writer)
# pylint: enable=broad-except


#TODO remove APC data from snmpd.conf
#TODO copy snmpd.conf from usr and start snmpd server from ttne
