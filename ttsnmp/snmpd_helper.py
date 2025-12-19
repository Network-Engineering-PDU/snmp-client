import sys
import json
import time
import socket
import threading
import logging


from . import fixed_oids
from .pdu_oids import PDUData
from .oid import Oid
from .fixed_oid_manager import FixedOidManager
from .node_table_oid_manager import NodeTableOidManager

from .http_helper import pdu_info, input_data, output_data, get_license

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
#logger.addHandler(logging.FileHandler("/tmp/output.log"))

GW_APP_SOCKET = "/tmp/ttgw_snmp.socket"
SOCKET_READ_PERIOD = 120
NODE_CSV_FILE = "/home/root/snmp/node_list.csv"


class SnmpdHelper:
    def __init__(self):
        self.fixed_manager = FixedOidManager()
        self.data = None
        self.data_lock = threading.Lock()

        self.node_table_manager = None

    def run(self):
        self.update_th = threading.Thread(target=self.read_from_gw, daemon=True)
        self.fixed_manager.set_oids(sorted(fixed_oids.oid_list))
        self.node_table_manager = NodeTableOidManager(NODE_CSV_FILE)

        self.node_table_manager.load_node_list()
        self.update_th.start()
        self.main()

    def ne_run(self):

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
        #TODO call pdu_info to get number of inputs and outputs and license
        while True:
            n_inputs, n_outputs = pdu_info()
            if n_inputs == None:
                time.sleep(1)
                continue
            license = get_license()
            if license in ["B2", "A2"]:
                self.data = PDUData(n_inputs, n_outputs)
            else:
                self.data = PDUData(0, 0)
            oid_list = self.data.to_oid_list()
            self.data_lock.acquire()
            self.fixed_manager.set_oids(sorted(oid_list))
            self.data_lock.release()
            break

        while True:
            #TODO update data
            logger.info("Read")

            in_data = []
            out_data = []

            for i in range(self.data.inputs.size):
                ret = input_data(i)
                in_data.append(ret)

            for i in range(self.data.outputs.size):
                ret = output_data(i)
                out_data.append(ret)

            self.data_lock.acquire()
            for i in range(self.data.inputs.size):
                self.data.update_input(in_data[i][0], in_data[i][1], i)

            for i in range(self.data.outputs.size):
                self.data.update_output(out_data[i][0], out_data[i][1], i)
            self.data_lock.release()

            time.sleep(SOCKET_READ_PERIOD)


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

    def input(self) -> str:
        line = sys.stdin.readline().strip()
        logger.info(f"Input: {line}")
        return line

    def print(self, line: str):
        logger.info(f"Output {line}")
        print(line, flush=True)

    def get_and_print(self, oid: Oid) -> None:
        result = self.fixed_manager.get(oid)
        if result is None and self.node_table_manager != None:
            result = self.node_table_manager.get(oid)
        if result is None:
            self.print("NONE")
            return

        self.print(oid.oid)
        self.print(result.oidtype)
        self.print(result.value)

    def get_next_oid(self, oid: Oid) -> Oid:
        result = self.fixed_manager.get_next_oid(oid)
        if result is None and self.node_table_manager != None:
            result = self.node_table_manager.get_next_oid(oid)
        return result

    def main(self):
        while True:
# pylint: disable=broad-except
            try:
                line = self.input()

                if "PING" == line:
                    self.print("PONG")

                elif "DUMP" == line:
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
                    oid_str = self.input()
                    oid = Oid(oid_str)
                    _type, value = self.input().split(" ", 1)
                    value = value.strip("\"")
                    self.print(self.node_table_manager.set(oid, _type, value))

                else:
                    logger.info(f"Invalid input ({len(line)}): {line}")
            except Exception as e:
                logger.exception(f"Exception: {e}")
# pylint: enable=broad-except


#TODO remove APC data from snmpd.conf
#TODO copy snmpd.conf from usr and start snmpd server from ttne
