import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from claim_store import TAIPEI
from quiet_hours import quiet_window_end
from settings_store import save_settings


class QuietHoursTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=root)
        self.path = Path(self.directory.name) / "settings.json"

    def tearDown(self):
        self.directory.cleanup()

    def test_cross_midnight_boundaries(self):
        def at(day, hour, minute):
            return datetime(2026, 11, day, hour, minute, tzinfo=TAIPEI)
        self.assertIsNone(quiet_window_end(at(7, 23, 29), path=self.path))
        self.assertEqual(quiet_window_end(at(7, 23, 30), path=self.path), at(8, 0, 30))
        self.assertEqual(quiet_window_end(at(8, 0, 0), path=self.path), at(8, 0, 30))
        self.assertEqual(quiet_window_end(at(8, 0, 29), path=self.path), at(8, 0, 30))
        self.assertIsNone(quiet_window_end(at(8, 0, 30), path=self.path))

    def test_disable_and_same_day_window(self):
        moment = datetime(2026, 11, 8, 12, 30, tzinfo=TAIPEI)
        save_settings(quiet_enabled=False, path=self.path)
        self.assertIsNone(quiet_window_end(moment, path=self.path))
        save_settings(quiet_enabled=True, quiet_start="12:00", quiet_end="13:00", path=self.path)
        self.assertEqual(quiet_window_end(moment, path=self.path),
                         datetime(2026, 11, 8, 13, 0, tzinfo=TAIPEI))

    def test_running_dashboard_closes_connected_devices_during_quiet_window(self):
        with patch.object(main, "quiet_window_end", return_value=None), \
             patch.object(main, "_adb_devices") as devices, \
             patch.object(main, "close_shopee") as close:
            self.assertFalse(main._enforce_quiet_window())
            devices.assert_not_called()
            close.assert_not_called()
        with patch.object(main, "quiet_window_end", return_value=datetime(2026, 11, 8, 0, 30, tzinfo=TAIPEI)), \
             patch.object(main, "_adb_devices", return_value=["phone-a", "phone-b"]), \
             patch.object(main, "close_shopee") as close:
            self.assertTrue(main._enforce_quiet_window())
            self.assertEqual(close.call_count, 2)

    def test_dashboard_marks_waiting_bot_as_not_started(self):
        bot = SimpleNamespace(running=True, retrying=False, status_message="🌙 暫停採集至台灣時間 00:30，關閉蝦皮",
                              _coins_collected=0, _streams_visited=0)
        stats = {"per_device": [], "pending_by_device": []}
        quiet_end = datetime(2026, 11, 9, 0, 30, tzinfo=TAIPEI)
        with patch.object(main, "_bots", {"phone-a": bot}), \
             patch.object(main, "_adb_devices", return_value=["phone-a"]), \
             patch.object(main, "_load_nicknames", return_value={"phone-a": "A8+"}), \
             patch.object(main, "summary", return_value=stats), \
             patch.object(main, "read_status", return_value=None), \
             patch.object(main, "daily_count", return_value=0), \
             patch.object(main, "foreground_app", return_value={"kind": "home"}), \
             patch.object(main, "load_region", return_value=None), \
             patch.object(main, "quiet_window_end", return_value=quiet_end) as quiet:
            device = main.get_devices()["devices"][0]
            quiet.return_value = None
            bot.retrying = True
            retrying_device = main.get_devices()["devices"][0]
        self.assertFalse(device["running"])
        self.assertTrue(device["paused_for_quiet"])
        self.assertEqual(device["resume_at"], "00:30")
        self.assertTrue(bot.running)
        self.assertFalse(retrying_device["running"])
        self.assertTrue(retrying_device["retrying"])


if __name__ == "__main__":
    unittest.main()
