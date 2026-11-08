import base64
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import cv2
import numpy as np

import exception_screens
from bot_logic import ShopeeBot


class ExceptionScreenTests(unittest.TestCase):
    def test_saves_multiple_features_and_matches_only_the_exception(self):
        rng = np.random.default_rng(42)
        screen = np.zeros((300, 200, 3), dtype=np.uint8)
        screen[40:90, 20:80] = rng.integers(0, 255, (50, 60, 3), dtype=np.uint8)
        screen[220:260, 110:170] = rng.integers(0, 255, (40, 60, 3), dtype=np.uint8)
        boxes = [[20, 40, 80, 90], [110, 220, 170, 260]]
        templates = []
        for x1, y1, x2, y2 in boxes:
            ok, image = cv2.imencode(".png", screen[y1:y2, x1:x2])
            self.assertTrue(ok)
            templates.append(base64.b64encode(image.tobytes()).decode())
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory, \
             patch.object(exception_screens, "BASE_DIR", Path(directory)):
            profile = exception_screens.save_profile(
                "phone", {"name": "商品頁", "screen_size": [200, 300], "boxes": boxes,
                          "action": "back", "action_point": None}, templates)
            found = exception_screens.match_profile(screen, "phone")
            self.assertEqual(found["name"], "商品頁")
            self.assertGreaterEqual(found["score"], exception_screens.MATCH_THRESHOLD)
            other = screen.copy()
            other[220:260, 110:170] = 0
            self.assertIsNone(exception_screens.match_profile(other, "phone"))
            self.assertTrue(exception_screens.delete_profile("phone", profile["id"]))
            self.assertIsNone(exception_screens.match_profile(screen, "phone"))

    def test_collector_returns_before_processing_reward_on_exception(self):
        bot = ShopeeBot("phone")
        screen = np.zeros((300, 200, 3), dtype=np.uint8)
        profile = {"id": "product", "name": "商品頁", "action": "back"}
        def press_back(*args):
            bot._stop_event.set()
            return CompletedProcess(args, 0)
        with patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.daily_count", return_value=0), \
             patch("bot_logic.get_screenshot", return_value=screen), \
             patch("bot_logic.match_profile", return_value=profile), \
             patch("bot_logic._adb", side_effect=press_back) as adb, \
             patch("bot_logic.inspect_reward") as inspect:
            bot._run_loop_body()
        adb.assert_called_once_with("phone", "shell", "input", "keyevent", "4")
        inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
