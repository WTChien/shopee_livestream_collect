import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from settings_store import (
    DEFAULT_MIN_REWARD, DEFAULT_MISSING_CARD_LIMIT, get_min_reward,
    get_missing_card_limit, load_settings, save_min_reward, save_missing_card_limit,
    save_settings,
)


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=temp_root)
        self.path = Path(self.directory.name) / "settings.json"

    def tearDown(self):
        self.directory.cleanup()

    def test_default_save_and_reload(self):
        self.assertEqual(get_min_reward(path=self.path), DEFAULT_MIN_REWARD)
        self.assertEqual(get_missing_card_limit(path=self.path), DEFAULT_MISSING_CARD_LIMIT)
        self.assertEqual(load_settings(path=self.path)["quiet_start"], "23:30")
        self.assertEqual(load_settings(path=self.path)["quiet_end"], "00:30")
        self.assertEqual(save_min_reward("0.42", path=self.path), Decimal("0.42"))
        self.assertEqual(get_min_reward(path=self.path), Decimal("0.42"))
        self.assertEqual(get_missing_card_limit(path=self.path), 20)
        self.assertEqual(save_missing_card_limit(12, path=self.path), 12)
        self.assertEqual(get_min_reward(path=self.path), Decimal("0.42"))
        self.assertEqual(get_missing_card_limit(path=self.path), 12)
        save_settings(quiet_start="22:45", quiet_end="01:15", path=self.path)
        self.assertEqual(load_settings(path=self.path)["quiet_start"], "22:45")
        self.assertEqual(get_min_reward(path=self.path), Decimal("0.42"))
        self.assertEqual(save_min_reward("0", path=self.path), Decimal("0"))
        self.assertEqual(get_min_reward(path=self.path), Decimal("0"))
        self.assertEqual(get_missing_card_limit(path=self.path), 12)

    def test_invalid_amount_does_not_replace_saved_setting(self):
        save_min_reward("0.30", path=self.path)
        for value in ("-0.01", "10.01", "0.301", "abc", "NaN"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                save_min_reward(value, path=self.path)
        self.assertEqual(get_min_reward(path=self.path), Decimal("0.30"))
        for value in (0, 101, "1.5", "abc", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                save_missing_card_limit(value, path=self.path)
        self.assertEqual(get_missing_card_limit(path=self.path), 20)
        for start, end in (("24:00", "00:30"), ("23:30", "23:30")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                save_settings(quiet_start=start, quiet_end=end, path=self.path)
        self.assertEqual(load_settings(path=self.path)["quiet_start"], "23:30")

    def test_runtime_parameters_persist_and_reject_invalid_values(self):
        saved = save_settings(daily_limit=75, read_interval_seconds=5,
                              live_refresh_minutes=12, limit_streak=3,
                              success_capture_seconds=10, path=self.path)
        for name, expected in (("daily_limit", 75), ("read_interval_seconds", 5),
                               ("live_refresh_minutes", 12), ("limit_streak", 3),
                               ("success_capture_seconds", 10)):
            self.assertEqual(saved[name], expected)
            self.assertEqual(load_settings(path=self.path)[name], expected)
        for name, invalid in (("daily_limit", 100), ("read_interval_seconds", 0),
                              ("live_refresh_minutes", 121), ("limit_streak", 21),
                              ("success_capture_seconds", 1)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                save_settings(path=self.path, **{name: invalid})
        self.assertEqual(load_settings(path=self.path)["daily_limit"], 75)


if __name__ == "__main__":
    unittest.main()
