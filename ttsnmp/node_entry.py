import time
import threading


class NodeEntry:
    ONLINE_TIMEOUT = 3600

    def __init__(self, index: int, mac: str, name: str, location: str):
        self.index = index
        self.mac = mac
        self.name = name
        self.location = location
        self._temp = 0
        self._press = 0
        self._hum = 0
        self._rssi = 0
        self._power = 0
        self._last_timestamp = 0
        self._bat = 0
        self.lock = threading.RLock()

    @property
    def temp(self) -> int:
        with self.lock:
            return self._temp

    @property
    def hum(self) -> int:
        with self.lock:
            return self._hum

    @property
    def press(self) -> int:
        with self.lock:
            return self._press

    @property
    def rssi(self) -> int:
        with self.lock:
            return self._rssi

    @property
    def bat(self) -> int:
        with self.lock:
            return self._bat

    @property
    def power(self) -> int:
        with self.lock:
            return self._power

    @property
    def status(self) -> int:
        with self.lock:
            if time.time() > self._last_timestamp + self.ONLINE_TIMEOUT:
                return 0
            return 1

    def update(self, data: dict):
        with self.lock:
            if "temperature" in data:
                self._temp = int(round(data["temperature"]/10))
            if "humidity" in data:
                self._hum = data["humidity"]
            if "pressure" in data:
                self._press = data["pressure"]
            if "rssi" in data:
                self._rssi = data["rssi"]
            if "datetime" in data:
                self._last_timestamp = data["datetime"]
            if "battery" in data:
                self._bat = data["battery"]
            if "total_active_power" in data:
                self._power = data["total_active_power"]

    def __str__(self):
        return f"{self.index}: {self.name} ({self.mac})"

    def __repr__(self):
        return self.__str__()
