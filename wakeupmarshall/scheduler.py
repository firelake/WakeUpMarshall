"""Scheduler & app state for WakeUpMarshall.

A single background thread owns all state mutations:

* periodic wake ticks (every ``interval_minutes``, only while enabled)
* manual wake / manual scan requests (set events, handled promptly)

The web server only reads state or sets events, so there are no races.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .bluetooth import AdapterInfo, BaseBackend, Device, matches_keywords
from .config import ConfigStore, Settings

log = logging.getLogger("wakeupmarshall")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class WakeScheduler:
    def __init__(self, config: ConfigStore, backend: BaseBackend, settings: Optional[Settings] = None) -> None:
        self.config = config
        self.backend = backend
        self.settings = settings or config.load_settings()

        # ---- live state (read by the web UI) ----
        self.adapter: AdapterInfo = AdapterInfo()
        self.devices: List[Device] = []
        self.last_scan_at: Optional[str] = None
        self.last_wake: Optional[Dict] = None   # most recent wake entry
        self.working: bool = False              # a wake/scan is running now
        self.last_run_at: Optional[str] = None  # last scheduled tick time

        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()
        self._scan_event = threading.Event()
        self._stop = threading.Event()
        self._started_at = 0.0
        self._first_run_delay = 5  # seconds after start before the first tick

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._loop, name="wake-scheduler", daemon=True)
        self._thread.start()
        log.info("Scheduler started (interval=%s min, enabled=%s)", self.settings.interval_minutes, self.settings.enabled)

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def trigger_wake(self, manual: bool = True) -> None:
        """Request an immediate wake attempt (runs in the scheduler thread)."""
        self._wake_event.set()
        if manual:
            log.info("Manual wake requested")

    def trigger_scan(self) -> None:
        self._scan_event.set()

    def update_settings(self, settings: Settings) -> Settings:
        self.settings = self.config.save_settings(settings)
        log.info("Settings updated: %s", settings)
        return self.settings

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while self._running:
            now = time.time()
            first_run_ok = now - self._started_at >= self._first_run_delay
            due = (
                self.settings.enabled
                and first_run_ok
                and (self.last_run_at is None or now - self._ts(self.last_run_at) >= self.settings.interval_minutes * 60)
            )
            if self._wake_event.is_set():
                self._wake_event.clear()
                self.run_wake(manual=True)
            elif self._scan_event.is_set():
                self._scan_event.clear()
                self.run_scan()
            elif due and not self.working:
                self.run_wake(manual=False)
            self._stop.wait(0.5)

    @staticmethod
    def _ts(iso: str) -> float:
        try:
            return datetime.fromisoformat(iso).timestamp()
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def run_scan(self) -> None:
        """Scan and refresh the device list (no wake attempts)."""
        if self.working:
            return
        with self._lock:
            self.working = True
            started = time.monotonic()
            try:
                self.adapter = self.backend.adapter_status()
                if not self.adapter.powered:
                    log.warning("Bluetooth adapter is not powered - scan skipped")
                    self.devices = []
                    return
                self.devices = self.backend.scan(self.settings.scan_timeout)
                self.last_scan_at = now_iso()
                log.info("Scan finished: %d device(s)", len(self.devices))
            except Exception as exc:
                log.exception("Scan failed")
                self.devices = []
                self.last_scan_at = now_iso()
                self._record_wake("scan", "error", str(exc), int((time.monotonic() - started) * 1000), manual=False)
            finally:
                self.working = False

    def run_wake(self, manual: bool = False) -> None:
        """One full wake round: adapter -> scan -> connect matching devices."""
        if self.working:
            log.info("Wake round skipped - previous round still running")
            return
        with self._lock:
            self.working = True
            self.last_run_at = now_iso()
            started = time.monotonic()
            mode = "manual" if manual else "auto"
            try:
                self.adapter = self.backend.adapter_status()
                if not self.adapter.powered:
                    self._record_wake("wake", "error", "Bluetooth adapter is off", 0, manual)
                    return

                self.devices = self.backend.scan(self.settings.scan_timeout)
                self.last_scan_at = now_iso()

                targets = [d for d in self.devices if matches_keywords(d.name, self.settings.keywords)]
                # Bluetooth advertising can be intermittent: retry a few times
                # before giving up, so a single flaky scan round doesn't miss.
                attempt = 1
                while not targets and attempt < 3:
                    log.info("No Marshall device found (attempt %d) - rescanning", attempt)
                    time.sleep(2)
                    self.devices = self.backend.scan(self.settings.scan_timeout)
                    self.last_scan_at = now_iso()
                    targets = [d for d in self.devices if matches_keywords(d.name, self.settings.keywords)]
                    attempt += 1

                if not targets:
                    self._record_wake("wake", "no_device", "No Marshall device found in scan", 0, manual)
                    return

                for dev in targets:
                    if dev.connected:
                        self._record_wake("wake", "already_awake", f"{dev.name} already connected", 0, manual)
                        continue
                    t0 = time.monotonic()
                    ok, detail = self.backend.connect(dev, self.settings.connect_timeout)
                    dur = int((time.monotonic() - t0) * 1000)
                    result = "success" if ok else "failed"
                    log.info("Wake %s %s: %s (%dms)", dev.address, result, detail, dur)
                    self._record_wake("wake", result, f"{dev.name}: {detail}", dur, manual)
            except Exception as exc:
                log.exception("Wake round failed")
                self._record_wake("wake", "error", str(exc), 0, manual)
            finally:
                self.working = False

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    def _record_wake(self, action: str, result: str, detail: str, duration_ms: int, manual: bool) -> None:
        entry = {
            "ts": now_iso(),
            "action": action,
            "mode": "manual" if manual else "auto",
            "result": result,
            "detail": detail,
            "duration_ms": duration_ms,
            "device": "",
            "mac": "",
        }
        self.config.append_history(entry)
        if action == "wake":
            self.last_wake = entry

    def history(self, limit: int = 100) -> List[Dict]:
        return self.config.load_history()[:limit]

    def clear_history(self) -> None:
        self.config.clear_history()

    def next_run(self) -> Optional[str]:
        if not self.settings.enabled:
            return None
        base = self._ts(self.last_run_at) if self.last_run_at else time.time()
        nxt = base + self.settings.interval_minutes * 60
        if nxt <= time.time():
            nxt = time.time() + 5
        return datetime.fromtimestamp(nxt).isoformat(timespec="seconds")

    def status(self) -> Dict:
        return {
            "version": self._version(),
            "adapter": {
                "present": self.adapter.present,
                "powered": self.adapter.powered,
                "name": self.adapter.name,
                "address": self.adapter.address,
            },
            "devices": [d.to_dict() for d in self.devices],
            "last_scan_at": self.last_scan_at,
            "scheduler": {
                "enabled": self.settings.enabled,
                "interval_minutes": self.settings.interval_minutes,
                "next_run": self.next_run(),
                "last_run": self.last_run_at,
                "last_wake": self.last_wake,
                "working": self.working,
            },
        }

    @staticmethod
    def _version() -> str:
        from . import __version__

        return __version__
