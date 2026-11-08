import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import daily_schedule
import device_lock


class DailyScheduleTests(unittest.TestCase):
    def test_schedule_is_per_device_and_normalizes_time(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            with patch.object(daily_schedule, "CONFIG_FILE", Path(directory) / "schedule.json"):
                saved = daily_schedule.save_schedule(
                    "phone-a", enabled=True, time="8:00", completion_mode="idle",
                    idle_minutes=30, max_runtime_minutes=240,
                )
                self.assertEqual(saved["time"], "08:00")
                self.assertTrue(daily_schedule.get_schedule("phone-a")["enabled"])
                self.assertFalse(daily_schedule.get_schedule("phone-b")["enabled"])

    def test_device_lock_prevents_simultaneous_collectors(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            with patch.object(device_lock, "LOCK_DIR", Path(directory)):
                first = device_lock.acquire_device_lock("phone-a")
                try:
                    with self.assertRaises(RuntimeError):
                        device_lock.acquire_device_lock("phone-a")
                finally:
                    device_lock.release_device_lock(first)
                second = device_lock.acquire_device_lock("phone-a")
                device_lock.release_device_lock(second)


if __name__ == "__main__":
    unittest.main()
