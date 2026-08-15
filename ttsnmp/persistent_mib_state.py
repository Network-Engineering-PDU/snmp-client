"""Crash-safe persistent state used by writable Nee-MIB objects."""

import json
import os
import tempfile
from typing import Any, Dict


STATE_SECTIONS = ("outlets", "summary", "sensors", "alarms", "last_event")


class PersistentMibState:
    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {
            section: {} for section in STATE_SECTIONS
        }
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as state_file:
                loaded = json.load(state_file)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return
        if not isinstance(loaded, dict):
            return
        for section in STATE_SECTIONS:
            value = loaded.get(section)
            if isinstance(value, dict):
                self.data[section] = value

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".nee-snmp-", dir=directory, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(self.data, state_file, sort_keys=True)
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise
