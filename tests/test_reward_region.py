import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np

import reward_vision


class RewardRegionTests(unittest.TestCase):
    def test_manual_coordinates_are_saved_per_phone(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            with patch.object(reward_vision, "REGION_DIR", Path(directory)):
                reward_vision.save_manual_region(
                    "phone-a", 1080, 2220, [748, 686, 1080, 863],
                    [796, 804], {"collect": [960, 804]},
                )
                self.assertEqual(reward_vision.load_region("phone-a")["buttons"]["collect"], [960, 804])
                self.assertIsNone(reward_vision.load_region("phone-b"))
                self.assertEqual(reward_vision.button_point("phone-a", "collect", 540, 1110), (480, 402))

    def test_optional_ocr_boxes_and_live_tab_are_saved(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            with patch.object(reward_vision, "REGION_DIR", Path(directory)):
                saved = reward_vision.save_manual_region(
                    "phone-a", 1080, 2220, [748, 686, 1080, 863],
                    [796, 804], {"live_tab": [540, 110]},
                    [850, 750, 1050, 820], [250, 900, 850, 1350],
                    [748, 500, 1060, 680], [748, 680, 1060, 860],
                )
                self.assertEqual(saved["timer_box"], [850, 750, 1050, 820])
                self.assertEqual(reward_vision.button_point("phone-a", "live_tab", 540, 1110), (270, 55))
                self.assertEqual(reward_vision.scaled_box("phone-a", "success_box", 540, 1110), (125, 450, 425, 675))
                self.assertEqual(reward_vision.scaled_box("phone-a", "red_box", 540, 1110), (374, 250, 530, 340))
                self.assertEqual(reward_vision.scaled_box("phone-a", "blue_box", 540, 1110), (374, 340, 530, 430))

    def test_one_manual_box_can_be_cleared_without_removing_others(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            with patch.object(reward_vision, "REGION_DIR", Path(directory)):
                reward_vision.save_manual_region(
                    "phone-a", 1080, 2220, [748, 680, 1060, 860],
                    [804, 802], {"live_tab": [540, 110]},
                    red_box=[748, 500, 1060, 680], blue_box=[748, 680, 1060, 860],
                )
                saved = reward_vision.save_manual_region(
                    "phone-a", 1080, 2220, [748, 680, 1060, 860],
                    [804, 802], {"live_tab": [540, 110]},
                    red_box=[748, 500, 1060, 680], blue_box=None,
                )
                self.assertNotIn("blue_box", saved)
                self.assertEqual(saved["red_box"], [748, 500, 1060, 680])
                self.assertEqual(saved["buttons"]["live_tab"], [540, 110])

    def test_timer_success_and_blue_card_parsing(self):
        self.assertEqual(reward_vision.parse_timer("倒數 O9:58"), 598)
        self.assertEqual(reward_vision.parse_timer("00;5+"), 54)
        self.assertEqual(reward_vision.parse_success_amount("成功領取 O.3 Coins"), "0.3")
        self.assertIsNone(reward_vision.parse_success_amount("領取 O.3 Coins"))
        screen = np.zeros((1000, 400, 3), dtype=np.uint8)
        self.assertFalse(reward_vision.blue_card_present(screen, "missing"))
        screen[300:400, 280:395] = (200, 100, 30)
        with patch.object(reward_vision, "_read_box_text", return_value=[]), \
             patch.object(reward_vision, "_blue_collect_button", return_value=None):
            self.assertFalse(reward_vision.blue_card_present(screen, "missing"))

    def test_blue_shape_without_reward_evidence_is_absent(self):
        screen = np.zeros((736, 360, 3), dtype=np.uint8)
        box = (248, 200, 356, 280)
        with patch.object(reward_vision, "locate_blue_box", return_value=box), \
             patch.object(reward_vision, "_read_box_text", return_value=[]), \
             patch.object(reward_vision, "_blue_amount", return_value=None), \
             patch.object(reward_vision, "_blue_collect_button", return_value=None), \
             patch.object(reward_vision, "_red_timer") as red_timer:
            found = reward_vision.inspect_reward(screen, "phone-a")
            self.assertEqual(found.state, "absent")
            self.assertFalse(reward_vision.blue_card_present(screen, "phone-a", found))
            red_timer.assert_not_called()

    def test_blue_card_evidence_still_keeps_waiting_collect_and_limit(self):
        screen = np.zeros((736, 360, 3), dtype=np.uint8)
        box = (248, 200, 356, 280)
        with patch.object(reward_vision, "locate_blue_box", return_value=box), \
             patch.object(reward_vision, "_red_timer", return_value=None), \
             patch.object(reward_vision, "_save_region"):
            with patch.object(reward_vision, "_read_box_text", return_value=[("02:15", .9)]), \
                 patch.object(reward_vision, "_blue_amount", return_value=None), \
                 patch.object(reward_vision, "_blue_collect_button", return_value=None):
                self.assertEqual(reward_vision.inspect_reward(screen, "phone-a").state, "waiting")
            with patch.object(reward_vision, "_read_box_text", return_value=[]), \
                 patch.object(reward_vision, "_blue_amount", return_value="0.35"), \
                 patch.object(reward_vision, "_blue_collect_button", return_value=(330, 250)):
                self.assertEqual(reward_vision.inspect_reward(screen, "phone-a").state, "collect")
            with patch.object(reward_vision, "_read_box_text", return_value=[("明日再來", .9)]), \
                 patch.object(reward_vision, "_blue_amount", return_value=None), \
                 patch.object(reward_vision, "_blue_collect_button", return_value=None):
                self.assertEqual(reward_vision.inspect_reward(screen, "phone-a").state, "limit")

    def test_blue_countdown_does_not_use_saved_timer_box(self):
        screen = np.zeros((736, 360, 3), dtype=np.uint8)
        box = (248, 200, 356, 280)
        with patch.object(reward_vision, "locate_blue_box", return_value=box), \
             patch.object(reward_vision, "scaled_box", side_effect=AssertionError("timer box should not be read")), \
             patch.object(reward_vision, "_read_box_text", return_value=[("02:15", .9)]), \
             patch.object(reward_vision, "_blue_amount", return_value=None), \
             patch.object(reward_vision, "_red_timer", return_value=None), \
             patch.object(reward_vision, "_save_region"):
            found = reward_vision.inspect_reward(screen, "phone-a")
        self.assertEqual(found.state, "waiting")
        self.assertEqual(found.remaining_seconds, 135)

    def test_collect_text_center_is_scaled_back_into_blue_region(self):
        screen = np.zeros((1000, 400, 3), dtype=np.uint8)
        box = (250, 300, 390, 450)
        words = [([[140, 140], [180, 140], [180, 180], [140, 180]], "領取", .93)]
        reader = type("Reader", (), {"readtext": lambda self, image: words})()
        with patch.object(reward_vision, "_get_reader", return_value=reader):
            self.assertEqual(reward_vision._blue_collect_button(screen, "phone-a", box), (350, 397))
            words[0] = (words[0][0], "蝦幣", .93)
            self.assertIsNone(reward_vision._blue_collect_button(screen, "phone-a", box))

    def test_red_timer_does_not_block_blue_collect(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            with patch.object(reward_vision, "REGION_DIR", Path(directory)):
                screen = np.zeros((736, 360, 3), dtype=np.uint8)
                screen[205:250, 248:356] = (180, 90, 30)
                screen[225:243, 297:341] = (40, 80, 240)

                def fake_ocr(_screen, box, _target_width=400):
                    if box[3] <= 205:
                        return [("00:54", .9)]
                    if box[2] - box[0] < 40:
                        return [("0.33", .9)]
                    return []

                with patch.object(reward_vision, "_read_box_text", side_effect=fake_ocr), \
                     patch.object(reward_vision, "_blue_collect_button", return_value=(330, 234)):
                    found = reward_vision.inspect_reward(screen, "phone-a")
                self.assertEqual(found.state, "collect")
                self.assertEqual(found.amount, "0.33")
                self.assertEqual(found.red_remaining_seconds, 54)
                self.assertIsNone(found.remaining_seconds)
                self.assertIsNotNone(found.collect_point)

    def test_live_tab_requires_underline_below_its_text(self):
        screen = np.zeros((2220, 1080, 3), dtype=np.uint8)
        with patch.object(reward_vision, "find_live_tab", return_value=(605, 128)):
            self.assertFalse(reward_vision.live_tab_selected(screen, "phone-a"))
            screen[156:162, 582:628] = (255, 255, 255)
            self.assertTrue(reward_vision.live_tab_selected(screen, "phone-a"))

    def test_checkin_popup_requires_orange_and_verified_text(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            with patch.object(reward_vision, "REGION_DIR", Path(directory)):
                reward_vision.save_manual_region(
                    "phone-a", 360, 736, [250, 200, 355, 330],
                    [270, 250], {"checkin_close": [180, 584]},
                )
                screen = np.zeros((736, 360, 3), dtype=np.uint8)
                self.assertIsNone(reward_vision.find_checkin_popup_close(screen, "phone-a"))
                screen[185:535, 58:302] = (40, 80, 240)
                reader = type("Reader", (), {"readtext": lambda self, image: [
                    (None, "領蝦幣或抽優惠券", .99), (None, "馬上簽到", .99),
                ]})()
                with patch.object(reward_vision, "_get_reader", return_value=reader):
                    self.assertEqual(reward_vision.find_checkin_popup_close(screen, "phone-a"), (180, 584))

    def test_rejects_out_of_bounds_coordinates(self):
        with self.assertRaises(ValueError):
            reward_vision.save_manual_region(
                "phone-a", 1080, 2220, [748, 686, 1081, 863], [796, 804], {}
            )


if __name__ == "__main__":
    unittest.main()
