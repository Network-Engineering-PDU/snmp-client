from typing import List, Tuple, Union

from .oid import Oid
from .base_oid_manager import BaseOidManager


class FixedOidManager(BaseOidManager):
    def __init__(self):
        self.oids = []

    def set_oids(self, oid_list: List[Oid]):
        self.oids = oid_list

    def first_oid(self) -> Oid:
        return self.oids[0][0]

    def last_oid(self) -> Oid:
        return self.oids[-1][0]

    def get(self, oid: Oid) -> Oid:
        for o in self.oids:
            if o == oid:
                return o
        return None

    def get_next_oid(self, oid: Oid) -> Oid:
        for i, o in enumerate(self.oids):
            if oid < o:
                return o
            if oid == o and (i+1) < len(self.oids):
                return self.oids[i+1]
        return None
