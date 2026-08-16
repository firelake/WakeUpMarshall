"""Configuration & persistence for WakeUpMarshall.

All state lives under a single data directory (default ``~/.wakeupmarshall``):

* ``settings.json`` - user settings (schedule, interval, keywords, ...)
* ``history.json``   - wake/scan history log

The module is deliberately dependency-free (stdlib only).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_INTERVAL_MINUTES = 10
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 24 * 60
DEFAULT_KEYWORDS = ["woburn", "marshall"]
MAX_HISTORY_ENTRIES = 500
MAX_SAVED_DEVICES = 100

log = logging.getLogger("wakeupmarshall.config")


def default_data_dir() -> Path:
    """Return the default data directory for this platform."""
    override = os.environ.get("WAKEUPMARSHALL_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".wakeupmarshall"


@dataclass
class Settings:
    """User-configurable settings, persisted to settings.json."""

    enabled: bool = True                      # scheduled wake on/off
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    scan_timeout: int = 15                    # seconds per scan round
    connect_timeout: int = 25                 # seconds per connect attempt
    keywords: List[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    port: int = 8756                          # local web UI port
    backend: str = "auto"                     # auto | bluetoothctl | bleak | fake

    def clamp(self) -> "Settings":
        self.interval_minutes = max(
            MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, int(self.interval_minutes))
        )
        self.scan_timeout = max(3, min(120, int(self.scan_timeout)))
        self.connect_timeout = max(5, min(120, int(self.connect_timeout)))
        self.port = max(1, min(65535, int(self.port)))
        if not self.keywords:
            self.keywords = list(DEFAULT_KEYWORDS)
        self.keywords = [str(k).strip().lower() for k in self.keywords if str(k).strip()]
        return self


class ConfigStore:
    """Load/save :class:`Settings` with atomic writes and a lock."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.settings_file = self.data_dir / "settings.json"
        self.history_file = self.data_dir / "history.json"
        self.devices_file = self.data_dir / "devices.json"
        self._lock = threading.RLock()

    # -- settings ---------------------------------------------------------
    def load_settings(self) -> Settings:
        with self._lock:
            if not self.settings_file.exists():
                return Settings().clamp()
            try:
                raw = json.loads(self.settings_file.read_text(encoding="utf-8"))
                known = {f: getattr(Settings, f, None) for f in Settings.__dataclass_fields__}
                data = {k: v for k, v in raw.items() if k in known and v is not None}
                return Settings(**data).clamp()
            except Exception:
                return Settings().clamp()

    def save_settings(self, settings: Settings) -> Settings:
        settings.clamp()
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write(self.settings_file, json.dumps(asdict(settings), indent=2))
        return settings

    # -- history ----------------------------------------------------------
    def load_history(self) -> List[Dict[str, Any]]:
        with self._lock:
            if not self.history_file.exists():
                return []
            try:
                data = json.loads(self.history_file.read_text(encoding="utf-8"))
                return list(data) if isinstance(data, list) else []
            except Exception:
                return []

    def append_history(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            entries = self.load_history()
            entries.insert(0, entry)  # newest first
            del entries[MAX_HISTORY_ENTRIES:]
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write(self.history_file, json.dumps(entries, indent=2, ensure_ascii=False))

    def clear_history(self) -> None:
        with self._lock:
            if self.history_file.exists():
                self.history_file.unlink()

    # -- saved devices ----------------------------------------------------
    def load_devices(self) -> List[Dict[str, str]]:
        with self._lock:
            if not self.devices_file.exists():
                return []
            try:
                raw = json.loads(self.devices_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Cannot load saved devices from %s: %s", self.devices_file, exc)
                return []
            if not isinstance(raw, list):
                log.warning("Ignoring invalid saved-device data in %s", self.devices_file)
                return []
            return self._normalize_devices(raw)

    def save_devices(self, devices: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        normalized = self._normalize_devices(devices)
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write(
                self.devices_file,
                json.dumps(normalized, indent=2, ensure_ascii=False),
            )
        return normalized

    @staticmethod
    def _normalize_devices(devices: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        seen = set()
        for item in devices:
            if not isinstance(item, dict):
                continue
            address = str(item.get("address") or "").strip()
            key = address.casefold()
            if not address or key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "address": address,
                    "name": str(item.get("name") or "").strip(),
                    "bound_at": str(item.get("bound_at") or "").strip(),
                    "last_seen": str(item.get("last_seen") or "").strip(),
                }
            )
            if len(normalized) >= MAX_SAVED_DEVICES:
                break
        return normalized

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
