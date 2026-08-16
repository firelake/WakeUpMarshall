"""Bluetooth backends for WakeUpMarshall.

Three backends implement the same tiny interface:

* ``LinuxBluetoothctlBackend`` - the default on Linux. Shells out to
  ``bluetoothctl`` (BlueZ), which works without root and covers both
  BR/EDR (classic) and LE discovery plus connect-to-wake. This is what
  finds "WOBURN III" speakers that only advertise in pairing/sleep mode.
* ``BleakBackend`` - cross-platform BLE via ``bleak`` (WinRT on Windows,
  BlueZ D-Bus on Linux). The primary backend on Windows.
* ``FakeBackend`` - a deterministic in-memory backend used for tests and
  for demoing the UI without hardware.
"""

from __future__ import annotations

import asyncio
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


@dataclass
class AdapterInfo:
    present: bool = False
    powered: bool = False
    name: str = ""
    address: str = ""


@dataclass
class Device:
    address: str
    name: str = ""
    connected: bool = False
    paired: bool = False
    trusted: bool = False
    last_seen: str = ""

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "name": self.name,
            "connected": self.connected,
            "paired": self.paired,
            "trusted": self.trusted,
            "last_seen": self.last_seen,
        }


class BaseBackend:
    name = "base"

    def adapter_status(self) -> AdapterInfo:
        raise NotImplementedError

    def scan(self, timeout: int = 15) -> List[Device]:
        raise NotImplementedError

    def connect(self, device: Device, timeout: int = 25) -> Tuple[bool, str]:
        """Try to (re)connect to the device; connecting wakes it from sleep."""
        raise NotImplementedError


def matches_keywords(name: str, keywords: List[str]) -> bool:
    """True if the device name contains any of the (lower-cased) keywords."""
    if not name:
        return False
    lowered = name.lower()
    return any(k in lowered for k in keywords if k)


# ---------------------------------------------------------------------------
# Linux backend (BlueZ bluetoothctl)
# ---------------------------------------------------------------------------
class LinuxBluetoothctlBackend(BaseBackend):
    name = "bluetoothctl"

    def __init__(self, bluetoothctl: str = "bluetoothctl") -> None:
        self.bin = bluetoothctl

    # -- helpers ----------------------------------------------------------
    def _run(self, args: List[str], timeout: float) -> Tuple[int, str]:
        try:
            proc = subprocess.run(
                [self.bin, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")
        except FileNotFoundError:
            raise RuntimeError(f"'{self.bin}' not found - is BlueZ installed?")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or b""
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            return 124, out

    def _available(self) -> bool:
        return shutil.which(self.bin) is not None

    # -- interface --------------------------------------------------------
    def adapter_status(self) -> AdapterInfo:
        code, out = self._run(["show"], timeout=10)
        info = AdapterInfo()
        info.present = code == 0
        for line in out.splitlines():
            line = strip_ansi(line).strip()
            if line.startswith("Controller "):
                parts = line.split()
                if len(parts) >= 2:
                    info.address = parts[1]
                    info.present = True
            elif line.startswith("Name:"):
                info.name = line.split(":", 1)[1].strip()
            elif line.startswith("Powered:"):
                info.powered = line.split(":", 1)[1].strip().lower() == "yes"
        return info

    def scan(self, timeout: int = 15) -> List[Device]:
        found: dict = {}
        # Run both a full scan and a BR/EDR scan: some speakers (e.g. Woburn
        # III in pairing mode) only show up on one of the two.
        for scan_type in ("on", "bredr"):
            code, out = self._run(["--timeout", str(timeout), "scan", scan_type], timeout=timeout + 8)
            if code not in (0, 124):
                continue
            for line in out.splitlines():
                line = strip_ansi(line)
                m = re.search(r"\[NEW\]\s+Device\s+([0-9A-Fa-f:]{17})(?:\s+(.*))?$", line)
                if not m:
                    continue
                addr = m.group(1).upper()
                name = (m.group(2) or "").strip()
                name = re.sub(r"\s*\[(LE|BR/EDR)\]$", "", name).strip()
                if addr not in found or not found[addr].name:
                    found[addr] = Device(address=addr, name=name)
        # Enrich with cached + state info from bluetoothctl.
        devices = list(found.values())
        return self.refresh(devices)

    def refresh(self, devices: List[Device]) -> List[Device]:
        for dev in devices:
            info = self._device_info(dev.address)
            if info:
                dev.connected = info.connected
                dev.paired = info.paired
                dev.trusted = info.trusted
                if info.name:
                    dev.name = info.name
        return devices

    def _device_info(self, address: str) -> Optional[Device]:
        code, out = self._run(["info", address], timeout=10)
        if code != 0:
            return None
        dev = Device(address=address)
        for line in out.splitlines():
            line = strip_ansi(line).strip()
            if line.startswith("Name:"):
                dev.name = re.sub(r"\s*\[(LE|BR/EDR)\]$", "", line.split(":", 1)[1].strip()).strip()
            elif line.startswith("Alias:"):
                if not dev.name:
                    dev.name = line.split(":", 1)[1].strip()
            elif line.startswith("Connected:"):
                dev.connected = line.split(":", 1)[1].strip().lower() == "yes"
            elif line.startswith("Paired:"):
                dev.paired = line.split(":", 1)[1].strip().lower() == "yes"
            elif line.startswith("Trusted:"):
                dev.trusted = line.split(":", 1)[1].strip().lower() == "yes"
        return dev

    def connect(self, device: Device, timeout: int = 25) -> Tuple[bool, str]:
        code, out = self._run(["connect", device.address], timeout=timeout + 5)
        text = strip_ansi(out)
        if "Connection successful" in text or "Connected: yes" in text:
            return True, "Connection successful"
        detail = "Failed to connect"
        m = re.search(r"Failed to connect:.*", text)
        if m:
            detail = m.group(0).strip()
        elif "Device not available" in text:
            detail = "Device not available"
        return False, detail


# ---------------------------------------------------------------------------
# Cross-platform BLE backend (bleak)
# ---------------------------------------------------------------------------
class BleakBackend(BaseBackend):
    name = "bleak"

    def __init__(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner  # noqa: F401
        except Exception as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "The 'bleak' package is required for this backend. "
                f"Install it with: pip install bleak (got: {exc})"
            )
        self._BleakScanner = BleakScanner
        self._BleakClient = BleakClient

    def adapter_status(self) -> AdapterInfo:
        info = AdapterInfo()
        try:
            devices = asyncio.run(self._scan(1.5))
            info.present = True
            info.powered = True
            info.name = "BLE adapter"
            info.address = ""
        except Exception:
            # Try BlueZ on Linux for a precise answer.
            if sys.platform.startswith("linux") and shutil.which("bluetoothctl"):
                return LinuxBluetoothctlBackend().adapter_status()
            info.present = False
            info.powered = False
        return info

    async def _scan(self, timeout: float) -> List[Device]:
        found = await self._BleakScanner.discover(timeout=timeout, return_adv=True)
        devices: List[Device] = []
        for addr, adv in found.items():
            name = getattr(adv, "local_name", None) or ""
            devices.append(Device(address=addr.upper(), name=name))
        return devices

    def scan(self, timeout: int = 15) -> List[Device]:
        return asyncio.run(self._scan(float(timeout)))

    def connect(self, device: Device, timeout: int = 25) -> Tuple[bool, str]:
        async def _connect() -> Tuple[bool, str]:
            client = self._BleakClient(device.address, timeout=timeout)
            try:
                await client.connect()
                return True, "Connected (BLE)"
            except Exception as exc:
                return False, f"Failed to connect: {exc}"
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        return asyncio.run(_connect())


# ---------------------------------------------------------------------------
# Deterministic fake backend (tests / demo without hardware)
# ---------------------------------------------------------------------------
class FakeBackend(BaseBackend):
    name = "fake"

    def __init__(
        self,
        adapter_powered: bool = True,
        devices: Optional[List[Device]] = None,
        connect_succeeds: bool = True,
    ) -> None:
        self._adapter_powered = adapter_powered
        self._devices = devices or [
            Device(address="CC:F7:9E:03:C7:00", name="WOBURN III", connected=False),
            Device(address="C1:C5:1E:14:A0:B7", name="WOBURN III", connected=False),
            Device(address="E8:22:81:D5:F9:49", name="midea", connected=False),
        ]
        self._connect_succeeds = connect_succeeds
        self.connect_calls: List[str] = []

    def adapter_status(self) -> AdapterInfo:
        return AdapterInfo(present=True, powered=self._adapter_powered, name="Fake adapter", address="00:00:00:00:00:00")

    def scan(self, timeout: int = 15) -> List[Device]:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for dev in self._devices:
            dev.last_seen = now
        return [Device(**d.to_dict()) for d in self._devices]

    def connect(self, device: Device, timeout: int = 25) -> Tuple[bool, str]:
        self.connect_calls.append(device.address)
        if self._connect_succeeds:
            device.connected = True
            return True, "Connection successful"
        return False, "Failed to connect (fake)"


def get_backend(name: str = "auto", fake_kwargs: Optional[dict] = None) -> BaseBackend:
    """Instantiate the requested backend (``auto`` picks for this platform)."""
    name = (name or "auto").lower()
    if name == "fake":
        return FakeBackend(**(fake_kwargs or {}))
    if name == "bluetoothctl":
        backend = LinuxBluetoothctlBackend()
        if not backend._available():
            raise RuntimeError("'bluetoothctl' not found on PATH - is BlueZ installed?")
        return backend
    if name == "bleak":
        return BleakBackend()
    if name == "auto":
        if sys.platform.startswith("win"):
            try:
                return BleakBackend()
            except RuntimeError:
                raise RuntimeError(
                    "On Windows the 'bleak' package is required: pip install bleak"
                )
        # Linux (and other POSIX): prefer bluetoothctl, fall back to bleak.
        if shutil.which("bluetoothctl"):
            return LinuxBluetoothctlBackend()
        try:
            return BleakBackend()
        except RuntimeError:
            raise RuntimeError(
                "No Bluetooth backend available: install BlueZ (bluetoothctl) "
                "or the 'bleak' package."
            )
    raise ValueError(f"Unknown backend: {name}")
