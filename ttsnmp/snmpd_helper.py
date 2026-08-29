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
from .pdu_backend import PduApiError, PduBackend
from .trap_sender import TrapSender

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
#logger.addHandler(logging.FileHandler("/tmp/output.log"))

GW_APP_SOCKET = "/tmp/ttgw_snmp.socket"
SOCKET_READ_PERIOD = 120
NODE_CSV_FILE = "/home/root/snmp/node_list.csv"
NEE_STATE_FILE = "/home/root/snmp/nee_mib_state.json"
INITIAL_DATA_TIMEOUT = 20


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

    def ne_run(self):
        self.nee_manager = NeePduOidManager(
            PduBackend(), NEE_STATE_FILE
        )
        self.update_th = threading.Thread(target=self.read_from_ttne,
                daemon=True)
        self.update_th.start()
        self.main()

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

    def input(self) -> str:
        line = sys.stdin.readline().strip()
        logger.info(f"Input: {line}")
        return line

    def print(self, line: str):
        logger.info(f"Output {line}")
        print(line, flush=True)

    def get_and_print(self, oid: Oid) -> None:
        if self.nee_manager is not None:
            self.update_attempted.wait(INITIAL_DATA_TIMEOUT)
        result = self.fixed_manager.get(oid)
        if result is None and self.nee_manager is not None:
            result = self.nee_manager.get(oid)
        if result is None and self.node_table_manager != None:
            result = self.node_table_manager.get(oid)
        if result is None:
            self.print("NONE")
            return

        self.print(oid.oid)
        self.print(result.oidtype)
        self.print(result.value)

    def get_next_oid(self, oid: Oid) -> Oid:
        if self.nee_manager is not None:
            self.update_attempted.wait(INITIAL_DATA_TIMEOUT)
        candidates = [self.fixed_manager.get_next_oid(oid)]
        if self.nee_manager is not None:
            candidates.append(self.nee_manager.get_next_oid(oid))
        if self.node_table_manager is not None:
            candidates.append(self.node_table_manager.get_next_oid(oid))
        candidates = [candidate for candidate in candidates
                      if candidate is not None]
        return min(candidates) if candidates else None

    def main(self):
        while True:
# pylint: disable=broad-except
            line = ""
            try:
                line = self.input()
                if line == "":
                    return

                if "PING" == line:
                    self.print("PONG")

                elif "DUMP" == line:
                    if self.node_table_manager is not None:
                        for node in self.node_table_manager.nodes.values():
                            self.print(str(node))

                elif "get" == line:
                    oid_str = self.input()
                    self.get_and_print(Oid(oid_str))

                elif "getnext" == line:
                    oid_str = self.input()
                    oid = self.get_next_oid(Oid(oid_str))
                    if oid is None:
                        self.print("NONE")
                    else:
                        self.get_and_print(oid)

                elif "set" == line:
                    if self.nee_manager is not None:
                        self.update_attempted.wait(INITIAL_DATA_TIMEOUT)
                    oid_str = self.input()
                    oid = Oid(oid_str)
                    _type, value = self.input().split(" ", 1)
                    value = value.strip("\"")
                    if self.nee_manager is not None:
                        self.print(self.nee_manager.set(oid, _type, value))
                    elif self.node_table_manager is not None:
                        self.print(self.node_table_manager.set(
                            oid, _type, value
                        ))
                    else:
                        self.print("not-writable")

                else:
                    logger.info(f"Invalid input ({len(line)}): {line}")
            except Exception as e:
                logger.exception(f"Exception: {e}")
                if line == "set":
                    self.print("wrong-value")
                elif line in ("get", "getnext"):
                    self.print("NONE")
# pylint: enable=broad-except


#TODO remove APC data from snmpd.conf
#TODO copy snmpd.conf from usr and start snmpd server from ttne
