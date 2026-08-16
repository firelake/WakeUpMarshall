"""Core unit tests (no real Bluetooth hardware needed - FakeBackend)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from wakeupmarshall.bluetooth import (
    Device,
    FakeBackend,
    BleakBackend,
    LinuxBluetoothctlBackend,
    matches_keywords,
)
from wakeupmarshall.config import ConfigStore, Settings, MAX_HISTORY_ENTRIES
from wakeupmarshall.scheduler import WakeScheduler
from wakeupmarshall.server import create_server


class TestConfig(unittest.TestCase):
    def test_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(data_dir=d)
            s = Settings(enabled=False, interval_minutes=7, port=9000)
            store.save_settings(s)
            loaded = store.load_settings()
            self.assertEqual(loaded.enabled, False)
            self.assertEqual(loaded.interval_minutes, 7)
            self.assertEqual(loaded.port, 9000)

    def test_settings_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(data_dir=d)
            s = store.load_settings()
            self.assertEqual(s.interval_minutes, 10)
            self.assertTrue(s.enabled)

    def test_settings_clamp(self):
        s = Settings(interval_minutes=0)
        s.clamp()
        self.assertEqual(s.interval_minutes, 1)

    def test_history_cap_and_clear(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(data_dir=d)
            for i in range(MAX_HISTORY_ENTRIES + 50):
                store.append_history({"ts": str(i), "result": "success"})
            entries = store.load_history()
            self.assertEqual(len(entries), MAX_HISTORY_ENTRIES)
            self.assertEqual(entries[0]["ts"], str(MAX_HISTORY_ENTRIES + 49))  # newest first
            store.clear_history()
            self.assertEqual(store.load_history(), [])

    def test_saved_devices_roundtrip_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(data_dir=d)
            stored = store.save_devices(
                [
                    {
                        "address": "CC:F7:9E:03:C7:00",
                        "name": "WOBURN III",
                        "bound_at": "2026-08-16T10:00:00",
                        "last_seen": "2026-08-16T10:01:00",
                    },
                    {
                        "address": "cc:f7:9e:03:c7:00",
                        "name": "duplicate",
                    },
                ]
            )
            self.assertEqual(len(stored), 1)
            self.assertEqual(store.load_devices(), stored)
            self.assertTrue(store.devices_file.exists())


class TestKeywords(unittest.TestCase):
    def test_matches(self):
        self.assertTrue(matches_keywords("WOBURN III", ["woburn", "marshall"]))
        self.assertTrue(matches_keywords("Marshall Woburn 3", ["woburn"]))
        self.assertFalse(matches_keywords("midea", ["woburn", "marshall"]))
        self.assertFalse(matches_keywords("", ["woburn"]))


class TestBluezStatus(unittest.TestCase):
    @patch("wakeupmarshall.bluetooth.shutil.which", return_value="/usr/bin/busctl")
    @patch("wakeupmarshall.bluetooth.subprocess.run")
    def test_reads_active_media_transport_state(self, run, _which):
        run.side_effect = [
            CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    "`-/org/bluez/hci0/dev_CC_F7_9E_03_C7_00/sep1/fd0\n"
                ),
                stderr="",
            ),
            CompletedProcess(
                args=[],
                returncode=0,
                stdout='s "active"\n',
                stderr="",
            ),
        ]
        backend = LinuxBluetoothctlBackend()
        self.assertEqual(
            backend._media_transport_states(),
            {"CC:F7:9E:03:C7:00": "active"},
        )


class TestBleakDirectConnect(unittest.TestCase):
    @patch("wakeupmarshall.bluetooth.sys.platform", "win32")
    def test_windows_saved_address_does_not_require_scanner_lookup(self):
        created = {}

        class FakeBLEDevice:
            def __init__(self, address, name, details, rssi):
                self.address = address
                self.name = name
                self.details = details
                created["rssi"] = rssi

        class FakeClient:
            def __init__(self, target, timeout):
                created["target"] = target
                created["timeout"] = timeout

            async def connect(self):
                created["connected"] = True

            async def disconnect(self):
                created["disconnected"] = True

        backend = object.__new__(BleakBackend)
        backend._BLEDevice = FakeBLEDevice
        backend._BleakClient = FakeClient
        ok, _detail = backend.connect(
            Device(address="CC:F7:9E:03:C7:00", name="WOBURN III"),
            timeout=11,
        )

        self.assertTrue(ok)
        self.assertIsInstance(created["target"], FakeBLEDevice)
        self.assertEqual(
            created["target"].details.adv.bluetooth_address,
            int("CCF79E03C700", 16),
        )
        self.assertEqual(created["timeout"], 11)
        self.assertEqual(created["rssi"], 0)


class TestSchedulerFake(unittest.TestCase):
    def make(self, backend, settings=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ConfigStore(data_dir=tmp.name)
        return WakeScheduler(store, backend, settings or Settings())

    def test_wake_success_records_history(self):
        sched = self.make(FakeBackend())
        sched.run_wake(manual=True)
        self.assertEqual(sched.last_wake["result"], "success")
        hist = sched.history()
        self.assertGreaterEqual(len(hist), 1)  # one entry per matching device
        self.assertTrue(all(h["result"] == "success" for h in hist))
        self.assertEqual(hist[0]["action"], "wake")
        self.assertEqual(hist[0]["mode"], "manual")
        self.assertIn("WOBURN", hist[0]["detail"])

    def test_already_awake_skipped(self):
        backend = FakeBackend(devices=[Device(address="CC:F7:9E:03:C7:00", name="WOBURN III", connected=True)])
        sched = self.make(backend)
        sched.run_wake()
        self.assertEqual(sched.last_wake["result"], "already_awake")
        self.assertEqual(backend.connect_calls, [])

    def test_no_device(self):
        backend = FakeBackend(devices=[Device(address="E8:22:81:D5:F9:49", name="midea")])
        sched = self.make(backend)
        sched.run_wake()
        self.assertEqual(sched.last_wake["result"], "no_device")

    def test_adapter_off(self):
        sched = self.make(FakeBackend(adapter_powered=False))
        sched.run_wake()
        self.assertEqual(sched.last_wake["result"], "error")
        self.assertIn("off", sched.last_wake["detail"].lower())

    def test_connect_failure(self):
        backend = FakeBackend(connect_succeeds=False)
        sched = self.make(backend)
        sched.run_wake()
        self.assertEqual(sched.last_wake["result"], "failed")

    def test_next_run_when_disabled(self):
        sched = self.make(FakeBackend(), Settings(enabled=False))
        self.assertIsNone(sched.next_run())

    def test_next_run_when_enabled(self):
        sched = self.make(FakeBackend(), Settings(enabled=True, interval_minutes=10))
        sched.run_wake()
        self.assertIsNotNone(sched.next_run())

    def test_settings_update_persists(self):
        sched = self.make(FakeBackend())
        sched.update_settings(Settings(enabled=False, interval_minutes=3))
        reloaded = sched.config.load_settings()
        self.assertFalse(reloaded.enabled)
        self.assertEqual(reloaded.interval_minutes, 3)

    def test_partial_settings_updates_are_atomic(self):
        scheduler = self.make(FakeBackend())
        start = threading.Barrier(3)

        def update(data):
            start.wait()
            scheduler.patch_settings(data)

        enabled_thread = threading.Thread(
            target=update,
            args=({"enabled": False},),
        )
        interval_thread = threading.Thread(
            target=update,
            args=({"interval_minutes": 7},),
        )
        enabled_thread.start()
        interval_thread.start()
        start.wait()
        enabled_thread.join(timeout=2)
        interval_thread.join(timeout=2)

        self.assertFalse(scheduler.settings.enabled)
        self.assertEqual(scheduler.settings.interval_minutes, 7)

    def test_status_shape(self):
        sched = self.make(FakeBackend())
        sched.run_wake()
        st = sched.status()
        self.assertIn("adapter", st)
        self.assertIn("devices", st)
        self.assertIn("scheduler", st)
        self.assertIn("capabilities", st)
        self.assertTrue(st["adapter"]["powered"])
        self.assertGreaterEqual(len(st["devices"]), 1)

    def test_bind_persists_across_scheduler_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ConfigStore(data_dir=tmp.name)
        scheduler = WakeScheduler(store, FakeBackend(), Settings())
        scheduler.run_scan()

        saved = scheduler.bind_device("CC:F7:9E:03:C7:00")
        self.assertTrue(saved["saved"])

        restored = WakeScheduler(store, FakeBackend(), Settings())
        status = restored.status()
        self.assertEqual(status["saved_device_count"], 1)
        self.assertTrue(status["devices"][0]["saved"])
        self.assertFalse(status["devices"][0]["discovered"])

    def test_only_recognized_marshall_devices_can_be_bound(self):
        scheduler = self.make(FakeBackend())
        scheduler.run_scan()
        with self.assertRaises(ValueError):
            scheduler.bind_device("E8:22:81:D5:F9:49")

    def test_saved_wake_targets_device_without_scanning(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ConfigStore(data_dir=tmp.name)
        backend = FakeBackend()
        scheduler = WakeScheduler(store, backend, Settings())
        scheduler.run_scan()
        scheduler.bind_device("CC:F7:9E:03:C7:00")

        backend.scan_calls = 0
        backend.connect_calls.clear()
        restored = WakeScheduler(store, backend, Settings())
        restored.run_wake(manual=True)

        self.assertEqual(backend.scan_calls, 0)
        self.assertEqual(backend.connect_calls, ["CC:F7:9E:03:C7:00"])
        self.assertEqual(restored.last_wake["result"], "success")

    def test_saved_wake_skips_scan_based_adapter_probe(self):
        class ScanBasedAdapterBackend(FakeBackend):
            def adapter_status(self):
                raise AssertionError("saved wake must not probe by scanning")

            def capabilities(self):
                capabilities = super().capabilities()
                capabilities["adapter_status_requires_scan"] = True
                return capabilities

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ConfigStore(data_dir=tmp.name)
        backend = ScanBasedAdapterBackend()
        scheduler = WakeScheduler(store, backend, Settings())
        scheduler.run_scan()
        scheduler.bind_device("CC:F7:9E:03:C7:00")
        backend.scan_calls = 0

        restored = WakeScheduler(store, backend, Settings())
        self.assertTrue(restored.run_wake(manual=True))
        self.assertEqual(backend.scan_calls, 0)

    def test_unbind_removes_saved_device(self):
        scheduler = self.make(FakeBackend())
        scheduler.run_scan()
        scheduler.bind_device("CC:F7:9E:03:C7:00")
        self.assertTrue(scheduler.unbind_device("CC:F7:9E:03:C7:00"))
        self.assertEqual(scheduler.config.load_devices(), [])
        matching = [
            device
            for device in scheduler.status()["devices"]
            if device["address"] == "CC:F7:9E:03:C7:00"
        ]
        self.assertFalse(matching[0]["saved"])

    def test_queued_operation_is_visible_and_rejects_overlap(self):
        scheduler = self.make(FakeBackend())
        self.assertTrue(scheduler.trigger_scan())
        self.assertFalse(scheduler.trigger_wake())
        queued = scheduler.status()["scheduler"]
        self.assertTrue(queued["working"])
        self.assertEqual(queued["operation"]["phase"], "queued")

        scheduler.run_scan()
        finished = scheduler.status()["scheduler"]
        self.assertFalse(finished["working"])
        self.assertEqual(finished["operation"]["phase"], "idle")

    def test_stale_refresh_cannot_restore_unbound_device(self):
        scheduler = self.make(FakeBackend())
        scheduler.run_scan()
        scheduler.bind_device("CC:F7:9E:03:C7:00")
        stale = [
            Device(
                address="CC:F7:9E:03:C7:00",
                name="WOBURN III",
                saved=True,
                is_marshall=True,
                discovered=False,
            )
        ]

        scheduler.unbind_device("CC:F7:9E:03:C7:00")
        scheduler._merge_live_updates(stale)

        self.assertEqual(scheduler.status()["saved_device_count"], 0)
        self.assertFalse(
            any(device["saved"] for device in scheduler.status()["devices"])
        )

    def test_adapter_off_clears_connected_and_audio_state(self):
        backend = FakeBackend(
            devices=[
                Device(
                    address="CC:F7:9E:03:C7:00",
                    name="WOBURN III",
                    connected=True,
                    audio_state="active",
                )
            ]
        )
        scheduler = self.make(backend)
        scheduler.run_scan()
        backend._adapter_powered = False

        scheduler.refresh_device_status()

        device = scheduler.status()["devices"][0]
        self.assertFalse(device["connected"])
        self.assertFalse(device["discovered"])
        self.assertEqual(device["audio_state"], "unsupported")


class TestApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConfigStore(data_dir=self.tmp.name)
        self.scheduler = WakeScheduler(self.store, FakeBackend(), Settings())
        self.scheduler.run_scan()
        self.server = create_server(self.scheduler, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path, method="GET", payload=None):
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_bind_and_unbind_device(self):
        status, payload = self.request(
            "/api/devices/bind",
            method="POST",
            payload={"address": "CC:F7:9E:03:C7:00"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["device"]["saved"])

        status, payload = self.request(
            "/api/devices/CC%3AF7%3A9E%3A03%3AC7%3A00",
            method="DELETE",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["deleted"])

    def test_invalid_settings_json_returns_400(self):
        request = Request(
            self.base_url + "/api/settings",
            data=b"{",
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 400)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("valid JSON", payload["error"])


if __name__ == "__main__":
    unittest.main()
