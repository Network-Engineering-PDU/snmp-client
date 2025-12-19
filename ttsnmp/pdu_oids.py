""" This module includes the different fixed-value, grouped by list.
A list groups continious oid values, and is managed by a FixedOidManager.
"""
from typing import List

from .oid import Oid, OidType

BASE_OID = ".1.3.6.1.4.1.318"

class SNMPData:
    def to_oid_list(self):
        raise NotImplementedError()

class ChannelData(SNMPData):
    def __init__(self, base_oid: str, channel_id: int):
        self.channel_id = Oid(base_oid + ".1", OidType.INTEGER, channel_id)
        self.current = Oid(base_oid + ".2", OidType.INTEGER, 0)
        self.power = Oid(base_oid + ".3", OidType.INTEGER, 0)

    def update(self, current:float, power: float):
        self.current.value = int(current * 100)
        self.power.value = int(power)

    def to_oid_list(self) -> List[Oid]:
        return [self.channel_id, self.current, self.power]


class PDUTable(SNMPData):
    def __init__(self, base_oid: str, size: int):
        self.entries = []
        self.size = size

        for i in range(self.size):
            self.entries.append(ChannelData(base_oid + f".{i+1}", i+1))

    def update(self, current: float, power: float, entry_id: int):
        self.entries[entry_id].update(current, power)

    def to_oid_list(self) -> List[Oid]:
        oid_list = []

        for i in range(self.size):
            oid_list += self.entries[i].to_oid_list()

        return oid_list


class PDUData(SNMPData):
    def __init__(self, n_inputs, n_outputs):
        self.inputs = PDUTable(BASE_OID + ".1", n_inputs)
        self.outputs = PDUTable(BASE_OID + ".2", n_outputs)

    def update_input(self, current: float, power: float, input_id: int):
        self.inputs.update(current, power, input_id)

    def update_output(self, current: float, power: float, output_id: int):
        self.outputs.update(current, power, output_id)

    def to_oid_list(self) -> List[Oid]:
        return self.inputs.to_oid_list() + self.outputs.to_oid_list()

