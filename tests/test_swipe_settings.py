import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_logic
import swipe_settings


class SwipeSettingsTests(unittest.TestCase):
    def test_custom_swipe_scales_to_current_screen_and_restores_default(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory, \
             patch.object(swipe_settings, "SWIPE_DIR", Path(directory)):
            saved = swipe_settings.save_swipe("phone", [1080, 2220], [1000, 1800], [1000, 400], 220)
            self.assertEqual(saved["duration_ms"], 220)
            with patch("bot_logic._adb", return_value=type("Result", (), {"returncode": 0})()) as adb:
                self.assertTrue(bot_logic.swipe_up("phone", 540, 1110))
            adb.assert_called_once_with("phone", "shell", "input", "swipe",
                                        "500", "900", "500", "200", "220")
            swipe_settings.clear_swipe("phone")
            self.assertIsNone(swipe_settings.load_swipe("phone"))

    def test_rejects_too_short_or_reversed_path(self):
        with self.assertRaises(ValueError):
            swipe_settings.save_swipe("phone", [1080, 2220], [1000, 500], [1000, 1800], 220)
        with self.assertRaises(ValueError):
            swipe_settings.save_swipe("phone", [1080, 2220], [1000, 1800], [1000, 1700], 220)


if __name__ == "__main__":
    unittest.main()
