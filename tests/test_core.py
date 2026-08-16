"""Core unit tests (no real Bluetooth hardware needed - FakeBackend)."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from wakeupmarshall.bluetooth import Device, FakeBackend, matches_keywords
from wakeupmarshall.config import ConfigStore, Settings, MAX_HISTORY_ENTRIES
from wakeupmarshall.scheduler import WakeScheduler


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


class TestKeywords(unittest.TestCase):
    def test_matches(self):
        self.assertTrue(matches_keywords("WOBURN III", ["woburn", "marshall"]))
        self.assertTrue(matches_keywords("Marshall Woburn 3", ["woburn"]))
        self.assertFalse(matches_keywords("midea", ["woburn", "marshall"]))
        self.assertFalse(matches_keywords("", ["woburn"]))


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

    def test_status_shape(self):
        sched = self.make(FakeBackend())
        sched.run_wake()
        st = sched.status()
        self.assertIn("adapter", st)
        self.assertIn("devices", st)
        self.assertIn("scheduler", st)
        self.assertTrue(st["adapter"]["powered"])
        self.assertGreaterEqual(len(st["devices"]), 1)


if __name__ == "__main__":
    unittest.main()
