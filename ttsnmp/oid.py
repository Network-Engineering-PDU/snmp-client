from enum import Enum

class OidType(Enum):
    STRING = "string"
    INTEGER = "integer"

    def __str__(self):
        return self._value_

class Oid:
    def __init__(self, oid: str, oidtype: OidType = None, value = None):
        self.oid = oid
        self.oidtype = oidtype
        self.value = value

        self.splitted = [int(x) for x in oid[1:].split(".")]
        self.length = len(self.splitted)

    def _cmp(self, other):
        for i in range(0, min(len(self), len(other))):
            if self.splitted[i] > other.splitted[i]:
                return 1
            if self.splitted[i] < other.splitted[i]:
                return -1
        if len(self) > len(other):
            return 1
        if len(self) < len(other):
            return -1
        return 0

    def __len__(self):
        return self.length

    def __eq__(self, other):
        return self._cmp(other) == 0

    def __ne__(self, other):
        return self._cmp(other) != 0

    def __gt__(self, other):
        return self._cmp(other) == 1

    def __lt__(self, other):
        return self._cmp(other) == -1

    def __str__(self):
        return self.oid

    def __repr__(self):
        return self.__str__()

    def get(self, n: int):
        return self.splitted[n]

    def rstrip(self, n: int):
        if n > len(self):
            return "."
        return "." + ".".join([str(x) for x in self.splitted[:-n]])
