"""Atomic volatile cache for the last successful PDU API snapshot."""

import json
import os
import tempfile
from typing import Any, Dict


class VolatileSnapshot:
    """Persist non-secret live data across pass_persist helper processes."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> Dict[str, Any]:
        if not self.path:
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as snapshot_file:
                snapshot = json.load(snapshot_file)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return {}
        return snapshot if isinstance(snapshot, dict) else {}

    def save(self, snapshot: Dict[str, Any]) -> None:
        if not self.path:
            return
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".nee-snapshot-", dir=directory, text=True
        )
        try:
            with os.fdopen(
                    descriptor, "w", encoding="utf-8") as snapshot_file:
                json.dump(snapshot, snapshot_file, sort_keys=True)
                snapshot_file.write("\n")
                snapshot_file.flush()
                os.fsync(snapshot_file.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise
