"""Scheduler & app state for WakeUpMarshall.

A single background thread owns Bluetooth operations:

* periodic wake ticks (every ``interval_minutes``, only while enabled)
* manual wake / manual scan requests (set events, handled promptly)

Short-lived state updates from the web API use a lock, while an action lock
prevents scans and wake attempts from overlapping.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import datetime
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
        self._saved_devices: Dict[str, Device] = {}
        for item in config.load_devices():
            device = Device(
                address=item["address"],
                name=item["name"],
                last_seen=item["last_seen"],
                saved=True,
                is_marshall=True,
                discovered=False,
                bound_at=item["bound_at"],
            )
            self._saved_devices[self._device_key(device.address)] = device
        self.devices: List[Device] = self._sort_devices(
            [replace(device) for device in self._saved_devices.values()]
        )
        self.last_scan_at: Optional[str] = None
        self.last_wake: Optional[Dict] = next(
            (
                entry
                for entry in config.load_history()
                if entry.get("action") == "wake"
            ),
            None,
        )
        self.working: bool = False
        self.operation: Dict = self._idle_operation()
        self.last_run_at: Optional[str] = None  # last scheduled tick time

        self._state_lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()
        self._scan_event = threading.Event()
        self._stop = threading.Event()
        self._started_at = 0.0
        self._first_run_delay = 5  # seconds after start before the first tick
        self._status_refresh_seconds = 5.0

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
        self._scan_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def trigger_wake(self, manual: bool = True) -> bool:
        """Request an immediate wake attempt (runs in the scheduler thread)."""
        if not self._queue_action("wake", "等待执行唤醒"):
            return False
        self._wake_event.set()
        if manual:
            log.info("Manual wake requested")
        return True

    def trigger_scan(self) -> bool:
        if not self._queue_action("scan", "等待执行扫描"):
            return False
        self._scan_event.set()
        return True

    def update_settings(self, settings: Settings) -> Settings:
        with self._state_lock:
            self.settings = self.config.save_settings(settings)
            log.info("Settings updated: %s", settings)
            return self.settings

    def patch_settings(self, data: Dict) -> Settings:
        """Validate and atomically apply a partial settings update."""
        with self._state_lock:
            settings = replace(self.settings)
            if "enabled" in data:
                settings.enabled = bool(data["enabled"])
            if "interval_minutes" in data:
                settings.interval_minutes = int(data["interval_minutes"])
            if "scan_timeout" in data:
                settings.scan_timeout = int(data["scan_timeout"])
            if "connect_timeout" in data:
                settings.connect_timeout = int(data["connect_timeout"])
            if "port" in data:
                settings.port = int(data["port"])
            if "keywords" in data:
                if not isinstance(data["keywords"], list):
                    raise ValueError("keywords must be a JSON array")
                settings.keywords = [str(keyword) for keyword in data["keywords"]]
            settings.clamp()
            self.settings = self.config.save_settings(settings)
            log.info("Settings updated: %s", settings)
            return self.settings

    def bind_device(self, address: str) -> Dict:
        key = self._device_key(address)
        with self._state_lock:
            device = next(
                (item for item in self.devices if self._device_key(item.address) == key),
                None,
            )
            if not device:
                raise KeyError("Device was not found in the latest scan")
            if not device.discovered:
                raise ValueError("Scan the device before binding it")
            if not matches_keywords(device.name, self.settings.keywords):
                raise ValueError("Only recognized Marshall devices can be bound")

            existing = self._saved_devices.get(key)
            if existing:
                return replace(existing).to_dict()

            saved = replace(
                device,
                saved=True,
                is_marshall=True,
                bound_at=now_iso(),
            )
            self._saved_devices[key] = replace(saved)
            self.devices = self._sort_devices(
                [
                    replace(item, saved=True, bound_at=saved.bound_at)
                    if self._device_key(item.address) == key
                    else item
                    for item in self.devices
                ]
            )
            self._persist_saved_devices_locked()
            log.info("Bound device %s (%s)", saved.address, saved.name)
            return saved.to_dict()

    def unbind_device(self, address: str) -> bool:
        key = self._device_key(address)
        with self._state_lock:
            if key not in self._saved_devices:
                return False
            del self._saved_devices[key]
            remaining: List[Device] = []
            for device in self.devices:
                if self._device_key(device.address) != key:
                    remaining.append(device)
                elif device.discovered:
                    remaining.append(replace(device, saved=False, bound_at=""))
            self.devices = self._sort_devices(remaining)
            self._persist_saved_devices_locked()
            log.info("Unbound device %s", address)
            return True

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        last_status_refresh = 0.0
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
            elif not self.working and time.monotonic() - last_status_refresh >= self._status_refresh_seconds:
                self.refresh_device_status()
                last_status_refresh = time.monotonic()
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
    def run_scan(self) -> bool:
        """Scan and refresh the device list (no wake attempts)."""
        if not self._begin_action("scan", "准备扫描"):
            return False
        started = time.monotonic()
        capabilities = self.backend.capabilities()
        scan_based_adapter_status = bool(
            capabilities.get("adapter_status_requires_scan")
        )
        try:
            if not scan_based_adapter_status:
                self._set_operation("checking_adapter", "正在检查蓝牙适配器")
                adapter = self.backend.adapter_status()
                with self._state_lock:
                    self.adapter = adapter
                if not adapter.powered:
                    log.warning("Bluetooth adapter is not powered - scan skipped")
                    self._mark_devices_unavailable()
                    self._merge_scan_results([])
                    with self._state_lock:
                        self.last_scan_at = now_iso()
                    self._record_wake(
                        "scan",
                        "error",
                        "Bluetooth adapter is off",
                        int((time.monotonic() - started) * 1000),
                        manual=True,
                    )
                    return False

            self._set_operation(
                "scanning",
                f"正在扫描附近设备（最长 {self.settings.scan_timeout} 秒）",
            )
            devices = self.backend.scan(self.settings.scan_timeout)
            if scan_based_adapter_status:
                with self._state_lock:
                    self.adapter = AdapterInfo(
                        present=True,
                        powered=True,
                        name="BLE adapter",
                        checked=True,
                    )
            self._merge_scan_results(devices)
            with self._state_lock:
                self.last_scan_at = now_iso()
            log.info("Scan finished: %d device(s)", len(devices))
            return True
        except Exception as exc:
            log.exception("Scan failed")
            if scan_based_adapter_status:
                with self._state_lock:
                    self.adapter = AdapterInfo(name="BLE adapter", checked=True)
            self._merge_scan_results([])
            with self._state_lock:
                self.last_scan_at = now_iso()
            self._record_wake(
                "scan",
                "error",
                str(exc),
                int((time.monotonic() - started) * 1000),
                manual=True,
            )
            return False
        finally:
            self._finish_action()

    def run_wake(self, manual: bool = False) -> bool:
        """Wake saved devices directly, or discover Marshall devices as a fallback."""
        if not self._begin_action("wake", "准备唤醒设备"):
            log.info("Wake round skipped - previous round still running")
            return False
        with self._state_lock:
            self.last_run_at = now_iso()
            targets = [replace(device) for device in self._saved_devices.values()]
        capabilities = self.backend.capabilities()
        scan_based_adapter_status = bool(
            capabilities.get("adapter_status_requires_scan")
        )
        try:
            if not scan_based_adapter_status:
                self._set_operation("checking_adapter", "正在检查蓝牙适配器")
                adapter = self.backend.adapter_status()
                with self._state_lock:
                    self.adapter = adapter
                if not adapter.powered:
                    self._mark_devices_unavailable()
                    self._record_wake(
                        "wake",
                        "error",
                        "Bluetooth adapter is off",
                        0,
                        manual,
                    )
                    return False

            if targets:
                self._set_operation(
                    "refreshing",
                    f"正在读取 {len(targets)} 台已绑定设备的状态",
                    total=len(targets),
                )
                targets = self.backend.refresh(targets)
                self._merge_live_updates(targets)
            else:
                targets = self._scan_for_targets()

            if not targets:
                self._record_wake(
                    "wake",
                    "no_device",
                    "No saved or discoverable Marshall device found",
                    0,
                    manual,
                )
                return False

            succeeded = False
            total = len(targets)
            connection_state_supported = bool(
                self.backend.capabilities().get("connection_state")
            )
            for index, device in enumerate(targets, start=1):
                self._set_operation(
                    "connecting",
                    f"正在唤醒 {device.name or device.address}",
                    current=index,
                    total=total,
                    target=device.address,
                )
                if device.connected:
                    self._record_wake(
                        "wake",
                        "already_awake",
                        f"{device.name} already connected",
                        0,
                        manual,
                        device=device.name,
                        mac=device.address,
                    )
                    succeeded = True
                    continue

                t0 = time.monotonic()
                ok, detail = self.backend.connect(device, self.settings.connect_timeout)
                duration = int((time.monotonic() - t0) * 1000)
                if ok:
                    device.last_seen = now_iso()
                    device.status_updated_at = device.last_seen
                    if scan_based_adapter_status:
                        with self._state_lock:
                            self.adapter = AdapterInfo(
                                present=True,
                                powered=True,
                                name="BLE adapter",
                                checked=True,
                            )
                    if connection_state_supported:
                        device.connected = True
                self._merge_live_updates([device])
                result = "success" if ok else "failed"
                log.info(
                    "Wake %s %s: %s (%dms)",
                    device.address,
                    result,
                    detail,
                    duration,
                )
                self._record_wake(
                    "wake",
                    result,
                    f"{device.name}: {detail}",
                    duration,
                    manual,
                    device=device.name,
                    mac=device.address,
                )
                succeeded = succeeded or ok
            return succeeded
        except Exception as exc:
            log.exception("Wake round failed")
            if scan_based_adapter_status and not targets:
                with self._state_lock:
                    self.adapter = AdapterInfo(name="BLE adapter", checked=True)
            self._record_wake("wake", "error", str(exc), 0, manual)
            return False
        finally:
            self._finish_action()

    def refresh_device_status(self) -> None:
        """Refresh reliable connection/audio state without running discovery."""
        if self.backend.capabilities().get("adapter_status_requires_scan"):
            return
        try:
            adapter = self.backend.adapter_status()
            with self._state_lock:
                self.adapter = adapter
                targets = [
                    replace(device)
                    for device in self.devices
                    if device.saved or device.is_marshall
                ]
            if adapter.powered and targets:
                self._merge_live_updates(self.backend.refresh(targets))
            elif not adapter.powered:
                self._mark_devices_unavailable()
        except Exception as exc:
            log.warning("Device status refresh failed: %s", exc)

    def _scan_for_targets(self) -> List[Device]:
        targets: List[Device] = []
        for attempt in range(1, 4):
            self._set_operation(
                "scanning",
                f"未绑定设备，正在扫描 Marshall 音箱（第 {attempt}/3 次）",
                current=attempt,
                total=3,
            )
            devices = self.backend.scan(self.settings.scan_timeout)
            if self.backend.capabilities().get("adapter_status_requires_scan"):
                with self._state_lock:
                    self.adapter = AdapterInfo(
                        present=True,
                        powered=True,
                        name="BLE adapter",
                        checked=True,
                    )
            self._merge_scan_results(devices)
            with self._state_lock:
                self.last_scan_at = now_iso()
            targets = [
                replace(device, is_marshall=True)
                for device in devices
                if matches_keywords(device.name, self.settings.keywords)
            ]
            if targets:
                break
            if attempt < 3:
                log.info("No Marshall device found (attempt %d) - rescanning", attempt)
                time.sleep(2)
        return targets

    # ------------------------------------------------------------------
    # state helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _device_key(address: str) -> str:
        return str(address or "").strip().casefold()

    @staticmethod
    def _sort_devices(devices: List[Device]) -> List[Device]:
        return sorted(
            devices,
            key=lambda item: (
                not item.saved,
                not item.is_marshall,
                (item.name or item.address).casefold(),
            ),
        )

    @staticmethod
    def _idle_operation() -> Dict:
        return {
            "kind": None,
            "phase": "idle",
            "detail": "",
            "started_at": None,
            "current": 0,
            "total": 0,
            "target": "",
        }

    def _queue_action(self, kind: str, detail: str) -> bool:
        with self._state_lock:
            if self.working:
                return False
            self.working = True
            self.operation = {
                "kind": kind,
                "phase": "queued",
                "detail": detail,
                "started_at": now_iso(),
                "current": 0,
                "total": 0,
                "target": "",
            }
            return True

    def _begin_action(self, kind: str, detail: str) -> bool:
        if not self._action_lock.acquire(blocking=False):
            return False
        with self._state_lock:
            queued_kind = self.operation.get("kind") if self.working else None
            if queued_kind not in (None, kind):
                self._action_lock.release()
                return False
            self.working = True
            self.operation = {
                "kind": kind,
                "phase": "starting",
                "detail": detail,
                "started_at": self.operation.get("started_at") or now_iso(),
                "current": 0,
                "total": 0,
                "target": "",
            }
        return True

    def _set_operation(
        self,
        phase: str,
        detail: str,
        current: int = 0,
        total: int = 0,
        target: str = "",
    ) -> None:
        with self._state_lock:
            self.operation.update(
                {
                    "phase": phase,
                    "detail": detail,
                    "current": current,
                    "total": total,
                    "target": target,
                }
            )

    def _finish_action(self) -> None:
        with self._state_lock:
            self.working = False
            self.operation = self._idle_operation()
        self._action_lock.release()

    def _merge_scan_results(self, scanned: List[Device]) -> None:
        seen_at = now_iso()
        with self._state_lock:
            merged: Dict[str, Device] = {}
            saved_changed = False
            for source in scanned:
                device = replace(source)
                key = self._device_key(device.address)
                if not key:
                    continue
                device.discovered = True
                device.is_marshall = matches_keywords(
                    device.name, self.settings.keywords
                )
                device.last_seen = device.last_seen or seen_at
                saved = self._saved_devices.get(key)
                if saved:
                    saved_changed = saved_changed or (
                        device.name != saved.name
                        or device.last_seen != saved.last_seen
                    )
                    device.saved = True
                    device.is_marshall = True
                    device.bound_at = saved.bound_at
                    if not device.name:
                        device.name = saved.name
                    self._saved_devices[key] = replace(
                        device,
                        saved=True,
                        is_marshall=True,
                    )
                merged[key] = device

            for key, saved in self._saved_devices.items():
                if key not in merged:
                    merged[key] = replace(
                        saved,
                        saved=True,
                        is_marshall=True,
                        discovered=False,
                    )
            self.devices = self._sort_devices(list(merged.values()))
            if saved_changed:
                self._persist_saved_devices_locked()

    def _merge_live_updates(self, updates: List[Device]) -> None:
        with self._state_lock:
            current = {
                self._device_key(device.address): device for device in self.devices
            }
            saved_changed = False
            for source in updates:
                key = self._device_key(source.address)
                if not key:
                    continue
                previous = current.get(key)
                device = replace(source)
                saved = self._saved_devices.get(key)
                if not previous and not saved:
                    # The device may have been unbound while a refresh was in flight.
                    continue
                if previous:
                    device.discovered = previous.discovered
                    device.saved = previous.saved
                    device.is_marshall = previous.is_marshall
                    device.bound_at = previous.bound_at
                    device.last_seen = device.last_seen or previous.last_seen
                    device.name = device.name or previous.name
                if saved:
                    saved_changed = saved_changed or (
                        device.name != saved.name
                        or device.last_seen != saved.last_seen
                    )
                    device.saved = True
                    device.is_marshall = True
                    device.bound_at = saved.bound_at
                    self._saved_devices[key] = replace(device)
                else:
                    device.saved = False
                    device.bound_at = ""
                current[key] = device
            self.devices = self._sort_devices(list(current.values()))
            if saved_changed:
                self._persist_saved_devices_locked()

    def _mark_devices_unavailable(self) -> None:
        updated_at = now_iso()
        audio_state = (
            "unknown"
            if self.backend.capabilities().get("audio_state")
            else "unsupported"
        )
        with self._state_lock:
            self.devices = [
                replace(
                    device,
                    connected=False,
                    discovered=False,
                    audio_state=audio_state,
                    status_updated_at=updated_at,
                )
                for device in self.devices
            ]
            for key, saved in self._saved_devices.items():
                self._saved_devices[key] = replace(
                    saved,
                    connected=False,
                    discovered=False,
                    audio_state=audio_state,
                    status_updated_at=updated_at,
                )

    def _persist_saved_devices_locked(self) -> None:
        self.config.save_devices(
            [
                {
                    "address": device.address,
                    "name": device.name,
                    "bound_at": device.bound_at,
                    "last_seen": device.last_seen,
                }
                for device in self._saved_devices.values()
            ]
        )

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    def _record_wake(
        self,
        action: str,
        result: str,
        detail: str,
        duration_ms: int,
        manual: bool,
        device: str = "",
        mac: str = "",
    ) -> None:
        entry = {
            "ts": now_iso(),
            "action": action,
            "mode": "manual" if manual else "auto",
            "result": result,
            "detail": detail,
            "duration_ms": duration_ms,
            "device": device,
            "mac": mac,
        }
        self.config.append_history(entry)
        if action == "wake":
            with self._state_lock:
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
        capabilities = self.backend.capabilities()
        with self._state_lock:
            return {
                "version": self._version(),
                "adapter": {
                    "present": self.adapter.present,
                    "powered": self.adapter.powered,
                    "name": self.adapter.name,
                    "address": self.adapter.address,
                    "checked": self.adapter.checked,
                },
                "devices": [device.to_dict() for device in self.devices],
                "saved_device_count": len(self._saved_devices),
                "last_scan_at": self.last_scan_at,
                "capabilities": {
                    **capabilities,
                    "backend": self.backend.name,
                    "sleep_state_note": (
                        "通用蓝牙接口无法区分设备休眠、关机、离线或超出范围"
                    ),
                    "presence_state_note": "可发现状态来自最近一次扫描，不代表持续在线",
                    "audio_state_note": (
                        "Linux BlueZ 可读取 A2DP MediaTransport 状态"
                        if capabilities.get("audio_state")
                        else "当前平台/后端无法读取 Bluetooth Classic A2DP 音频流状态"
                    ),
                },
                "scheduler": {
                    "enabled": self.settings.enabled,
                    "interval_minutes": self.settings.interval_minutes,
                    "next_run": self.next_run(),
                    "last_run": self.last_run_at,
                    "last_wake": self.last_wake,
                    "working": self.working,
                    "operation": dict(self.operation),
                },
            }

    @staticmethod
    def _version() -> str:
        from . import __version__

        return __version__
