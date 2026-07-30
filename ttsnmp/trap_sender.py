"""Minimal SNMPv2c notification encoder and UDP sender.

The embedded image contains ``snmpd`` but does not install the ``snmptrap``
command.  Keeping this encoder local avoids another runtime package and avoids
putting the community string in a process argument list.
"""

import ipaddress
import logging
import re
import socket
import time
from typing import Dict, Iterable, List, Tuple

from .nee_pdu_oid_manager import SENSOR_OID, SYS_OID, _display


logger = logging.getLogger(__name__)

SYS_UPTIME_OID = ".1.3.6.1.2.1.1.3.0"
SNMP_TRAP_OID = ".1.3.6.1.6.3.1.1.4.1.0"


def _length(length: int) -> bytes:
    if length < 0x80:
        return bytes((length,))
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(encoded),)) + encoded


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes((tag,)) + _length(len(value)) + value


def _integer_bytes(value: int) -> bytes:
    if value >= 0:
        encoded = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
        if encoded[0] & 0x80:
            encoded = b"\x00" + encoded
        return encoded
    size = 1
    while value < -(1 << (size * 8 - 1)):
        size += 1
    return value.to_bytes(size, "big", signed=True)


def _integer(value: int, tag: int = 0x02) -> bytes:
    return _tlv(tag, _integer_bytes(value))


def _base128(value: int) -> bytes:
    encoded = [value & 0x7F]
    value >>= 7
    while value:
        encoded.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(encoded))


def _object_identifier_value(oid: str) -> bytes:
    parts = [int(part) for part in oid.lstrip(".").split(".")]
    if len(parts) < 2 or parts[0] not in (0, 1, 2):
        raise ValueError(f"Invalid OID: {oid}")
    if parts[0] < 2 and not 0 <= parts[1] <= 39:
        raise ValueError(f"Invalid OID: {oid}")
    body = _base128(parts[0] * 40 + parts[1])
    for part in parts[2:]:
        if part < 0:
            raise ValueError(f"Invalid OID: {oid}")
        body += _base128(part)
    return body


def _object_identifier(oid: str) -> bytes:
    return _tlv(0x06, _object_identifier_value(oid))


def _varbind(oid: str, value_tag: int, value: bytes) -> bytes:
    return _tlv(0x30, _object_identifier(oid) + _tlv(value_tag, value))


class TrapSender:
    def __init__(self, port: int = 162):
        self.port = port

    @staticmethod
    def _targets(settings: Dict) -> Iterable[str]:
        trap = settings.get("trap", {}) if isinstance(settings, dict) else {}
        if not trap.get("alarm", False):
            return []
        targets: List[str] = []
        seen = set()
        for index in range(1, 5):
            value = trap.get(f"manager_{index}_ip")
            if not value:
                continue
            try:
                address = ipaddress.ip_address(value)
                if address.version != 4:
                    logger.warning(
                        "IPv6 trap manager is not supported: %s", value
                    )
                    continue
                normalized = str(address)
                if normalized not in seen:
                    targets.append(normalized)
                    seen.add(normalized)
            except ValueError:
                logger.warning("Ignoring invalid SNMP trap manager IP: %r", value)
        return targets

    @staticmethod
    def _community(settings: Dict) -> str:
        version = settings.get("snmp_v1_v2c") or {}
        community = str(version.get("read_community", "public"))
        if community in ("Public", "Private"):
            community = community.lower()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", community):
            logger.warning("Invalid trap community; using public")
            return "public"
        return community

    @staticmethod
    def _event_varbinds(event: dict) -> List[Tuple[str, int, bytes]]:
        notification = int(event["notification_oid"].rsplit(".", 1)[1])
        if notification not in range(1, 7):
            raise ValueError("Unsupported Nee-MIB notification")
        description_oid = (
            SYS_OID + ".3.0" if notification in (1, 2)
            else SYS_OID + ".4.0"
        )
        description = (
            event.get("source", "") if notification in (1, 2)
            else event.get("sensor", "")
        )
        varbinds = [
            (description_oid, 0x04, _display(description, 30).encode()),
            (SYS_OID + ".2.0", 0x04,
             _display(event.get("power_desc", ""), 30).encode()),
            (SYS_OID + ".1.0", 0x04,
             _display(event.get("source_id", ""), 10).encode()),
            (SYS_OID + ".5.0", 0x04,
             _display(event.get("status", ""), 30).encode()),
        ]
        if notification == 2:
            varbinds.extend([
                (SYS_OID + ".6.0", 0x02,
                 _integer_bytes(int(event.get("value", -1)))),
                (SYS_OID + ".7.0", 0x02,
                 _integer_bytes(int(event.get("low", -1)))),
                (SYS_OID + ".8.0", 0x02,
                 _integer_bytes(int(event.get("high", -1)))),
            ])
        elif notification in (3, 4, 5):
            sensor_index = int(event["sensor_index"])
            metric_column = int(event["metric_column"])
            expected_column = {3: 7, 4: 10, 5: 13}[notification]
            if (
                    not 1 <= sensor_index <= 32
                    or metric_column != expected_column):
                raise ValueError("Invalid sensor notification instance")
            for column, key in (
                    (metric_column, "value"),
                    (metric_column + 1, "low"),
                    (metric_column + 2, "high")):
                varbinds.append((
                    f"{SENSOR_OID}.{column}.{sensor_index}",
                    0x02,
                    _integer_bytes(int(event.get(key, -1))),
                ))
        return varbinds

    def build_packet(self, community: str, event: dict) -> bytes:
        uptime = int(time.monotonic() * 100) & 0xFFFFFFFF
        varbinds = [
            _varbind(SYS_UPTIME_OID, 0x43, _integer_bytes(uptime)),
            _varbind(
                SNMP_TRAP_OID,
                0x06,
                _object_identifier_value(event["notification_oid"]),
            ),
        ]
        varbinds.extend(
            _varbind(oid, tag, value)
            for oid, tag, value in self._event_varbinds(event)
        )
        varbind_list = _tlv(0x30, b"".join(varbinds))
        request_id = int(time.time() * 1000) & 0x7FFFFFFF
        pdu = _tlv(
            0xA7,
            _integer(request_id) + _integer(0) + _integer(0) + varbind_list,
        )
        return _tlv(
            0x30,
            _integer(1) + _tlv(0x04, community.encode()) + pdu,
        )

    def send(self, events: Iterable[dict], settings: Dict) -> None:
        targets = self._targets(settings)
        community = self._community(settings)
        for event in events:
            try:
                packet = self.build_packet(community, event)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Could not encode SNMP trap: %s", exc)
                continue
            for target in targets:
                try:
                    with socket.socket(
                            socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
                        udp_socket.sendto(packet, (target, self.port))
                except OSError as exc:
                    logger.warning(
                        "Could not send SNMP trap to %s: %s", target, exc
                    )
