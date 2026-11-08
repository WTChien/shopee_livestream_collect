import unittest
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np

from bot_logic import LIVE_TAB_REFRESH_INTERVAL_SECONDS, ShopeeBot, format_waiting_status, open_shopee_live, swipe_up
from claim_store import TAIPEI
from reward_vision import RewardObservation, SuccessObservation


class MissingCardSwitchTests(unittest.TestCase):
    def test_waiting_status_shows_current_reward_amount(self):
        reward = RewardObservation("waiting", amount="0.8", remaining_seconds=150,
                                   red_remaining_seconds=180)
        message = format_waiting_status(reward, 5)
        self.assertIn("藍色蝦幣倒數剩餘 02:30", message)
        self.assertIn("可拿 0.8 蝦幣", message)
        self.assertIn("紅色倒數 03:00", message)
        self.assertIn("可拿金額未讀到", format_waiting_status(
            RewardObservation("waiting", remaining_seconds=30), 5))

    def test_run_loop_sends_one_stop_notification_with_reason(self):
        bot = ShopeeBot("phone-a", "A8+")
        bot._device_lock = object()
        with patch.object(bot, "_run_loop_body", side_effect=lambda: bot._finish_session("今日已達 90 次")), \
             patch("bot_logic.close_shopee", return_value=True), \
             patch("bot_logic.release_device_lock"), \
             patch("bot_logic.notify_device_stopped") as notify:
            bot._run_loop()
        notify.assert_called_once_with("phone-a", "A8+", "今日已達 90 次")

    def test_run_loop_waits_and_retries_unexpected_error(self):
        bot = ShopeeBot("phone-a")
        with patch.object(bot, "_run_loop_body", side_effect=RuntimeError("ADB disconnected")), \
             patch.object(bot, "_wait_for_retry", side_effect=lambda seconds: bot._stop_event.set()) as retry, \
             patch("bot_logic.close_shopee", return_value=True), \
             patch("bot_logic.release_device_lock"), \
             patch("bot_logic.notify_device_stopped") as notify:
            bot._run_loop()
        retry.assert_called_once_with(30)
        self.assertIn("30 秒後重新啟動", bot.status_message)
        notify.assert_called_once()

    def test_swipe_up_moves_from_lower_to_upper_screen(self):
        adb_result = type("Result", (), {"returncode": 0})()
        with patch("bot_logic._adb", return_value=adb_result) as adb:
            self.assertTrue(swipe_up("phone-a", 1080, 2220))
        adb.assert_called_once_with(
            "phone-a", "shell", "input", "swipe", "540", "1598", "540", "666", "500"
        )

    def test_a8_uses_longer_swipe_without_changing_other_devices(self):
        adb_result = type("Result", (), {"returncode": 0})()
        with patch("bot_logic._adb", return_value=adb_result) as adb:
            self.assertTrue(swipe_up("5200f803b437b41f", 1080, 2220))
        adb.assert_called_once_with(
            "5200f803b437b41f", "shell", "input", "swipe",
            "540", "1820", "540", "399", "250"
        )

    def test_swipes_between_half_hour_live_tab_refreshes(self):
        bot = ShopeeBot("phone-a")
        screen = np.zeros((2220, 1080, 3), dtype=np.uint8)
        bot._last_live_refresh_at = 99 - LIVE_TAB_REFRESH_INTERVAL_SECONDS + 1
        with patch("bot_logic.swipe_up", return_value=True) as swipe, \
             patch("bot_logic.find_live_tab", return_value=(605, 122)) as locate, \
             patch("bot_logic.tap", return_value=True) as tap, \
             patch.object(bot._stop_event, "wait"), \
             patch("bot_logic.time.monotonic", side_effect=[99, 100, 100, 101]):
            bot._switch_missing_card(screen)
            swipe.assert_called_once_with("phone-a", 1080, 2220)
            locate.assert_not_called()
            bot._switch_missing_card(screen)
            tap.assert_called_once_with("phone-a", 605, 122)
            self.assertEqual(bot._last_live_refresh_at, 100)
            bot._switch_missing_card(screen)
            self.assertEqual(swipe.call_count, 2)
            self.assertEqual(tap.call_count, 1)

    def test_low_blue_amount_skips_room_without_tapping(self):
        bot = ShopeeBot("phone-a")
        screen = np.zeros((2220, 1080, 3), dtype=np.uint8)
        def swipe_and_stop(*_args):
            bot._stop_event.set()
            return True
        with patch("bot_logic.daily_count", return_value=0), \
             patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.get_min_reward", return_value=Decimal("0.30")), \
             patch("bot_logic.get_screenshot", return_value=screen), \
             patch("bot_logic.find_checkin_popup_close", return_value=None), \
             patch("bot_logic.find_template", return_value=None), \
             patch("bot_logic.inspect_reward", return_value=RewardObservation("waiting", amount="0.29")), \
             patch("bot_logic.swipe_up", side_effect=swipe_and_stop) as swipe, \
             patch("bot_logic.tap") as tap:
            bot._run_loop_body()
        swipe.assert_called_once_with("phone-a", 1080, 2220)
        tap.assert_not_called()

    def test_twenty_distinct_rooms_without_card_close_shopee(self):
        bot = ShopeeBot("phone-a")
        screen = np.zeros((736, 360, 3), dtype=np.uint8)
        with patch("bot_logic.daily_count", return_value=0), \
             patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.get_screenshot", return_value=screen), \
             patch("bot_logic.find_checkin_popup_close", return_value=None), \
             patch("bot_logic.find_template", return_value=None), \
             patch("bot_logic.inspect_reward", return_value=RewardObservation("absent")), \
             patch("bot_logic.get_missing_card_limit", return_value=20), \
             patch.object(bot, "_switch_missing_card", return_value=True) as switch, \
             patch.object(bot._stop_event, "wait", return_value=False), \
             patch("bot_logic.close_shopee", return_value=True) as close:
            bot._run_loop_body()
        self.assertEqual(switch.call_count, 19)
        self.assertEqual(bot._consecutive_missing_cards, 20)
        close.assert_called_once_with("phone-a")
        self.assertIn("連續 20 間直播", bot.status_message)

    def test_seen_card_resets_missing_room_count(self):
        bot = ShopeeBot("phone-a")
        bot._consecutive_missing_cards = 19
        screen = np.zeros((736, 360, 3), dtype=np.uint8)
        def switch_and_stop(_screen):
            bot._stop_event.set()
            return True
        observations = [RewardObservation("waiting", remaining_seconds=10),
                        RewardObservation("absent"), RewardObservation("absent")]
        with patch("bot_logic.daily_count", return_value=0), \
             patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.get_screenshot", return_value=screen), \
             patch("bot_logic.find_checkin_popup_close", return_value=None), \
             patch("bot_logic.find_template", return_value=None), \
             patch("bot_logic.inspect_reward", side_effect=observations), \
             patch("bot_logic.get_missing_card_limit", return_value=20), \
             patch.object(bot, "_switch_missing_card", side_effect=switch_and_stop), \
             patch.object(bot._stop_event, "wait", return_value=False), \
             patch("bot_logic.close_shopee") as close:
            bot._run_loop_body()
        self.assertEqual(bot._consecutive_missing_cards, 1)
        close.assert_not_called()

    def test_delayed_success_frame_is_verified_after_capture(self):
        bot = ShopeeBot("phone-a")
        events = []
        frames = [np.full((1, 1, 3), i, dtype=np.uint8) for i in range(7)]
        def screenshot(_serial):
            frame = frames[len([event for event in events if event == "capture"])]
            events.append("capture")
            return frame
        def inspect(frame, _serial):
            events.append("inspect")
            return SuccessObservation("0.45" if frame[0, 0, 0] == 5 else None, "")
        with patch.object(bot._stop_event, "wait", return_value=False), \
             patch("bot_logic.get_screenshot", side_effect=screenshot), \
             patch("bot_logic.inspect_success", side_effect=inspect):
            success, count = bot._verify_claim_success()
        self.assertEqual(count, 7)
        self.assertEqual(success.amount, "0.45")
        self.assertEqual(events[:7], ["capture"] * 7)

    def test_click_without_success_ocr_counts_estimate_then_skips_room(self):
        bot = ShopeeBot("phone-a")
        screen = np.zeros((736, 360, 3), dtype=np.uint8)
        def swipe_and_stop(*_args):
            bot._stop_event.set()
            return True
        reward = RewardObservation("collect", collect_point=(330, 250), amount="0.45")
        with patch("bot_logic.daily_count", return_value=0), \
             patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.get_screenshot", return_value=screen), \
             patch("bot_logic.find_checkin_popup_close", return_value=None), \
             patch("bot_logic.find_template", return_value=None), \
             patch("bot_logic.inspect_reward", return_value=reward), \
             patch("bot_logic.get_min_reward", return_value=Decimal("0.20")), \
             patch("bot_logic.reserve_claim", return_value=44) as reserve, \
             patch.object(bot, "_verify_claim_success", return_value=(None, 7)), \
             patch("bot_logic.tap", return_value=True), \
             patch("bot_logic.swipe_up", side_effect=swipe_and_stop) as swipe, \
             patch("bot_logic.confirm_claim") as confirm, \
             patch("bot_logic.close_shopee") as close, \
             patch.object(bot._stop_event, "wait", return_value=False):
            bot._run_loop_body()
        reserve.assert_called_once_with("phone-a", "0.45")
        swipe.assert_called_once()
        confirm.assert_called_once_with(44)
        close.assert_not_called()
        self.assertFalse(bot._skip_current_room)
        self.assertEqual(bot._coins_collected, 1)

    def test_inactivity_finishes_and_closes_shopee(self):
        bot = ShopeeBot("phone-a", idle_timeout_seconds=1800)
        bot._last_success_at = 0
        with patch("bot_logic.time.monotonic", return_value=1801), \
             patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.daily_count", return_value=1), \
             patch("bot_logic.close_shopee", return_value=True) as close, \
             patch("bot_logic.get_screenshot") as screenshot:
            bot._run_loop_body()
        close.assert_called_once_with("phone-a")
        screenshot.assert_not_called()
        self.assertFalse(bot.running)

    def test_quiet_window_closes_then_resumes_without_using_runtime_budget(self):
        bot = ShopeeBot("phone-a", max_runtime_seconds=120, launch_on_start=True)
        bot._started_monotonic = 0
        bot._last_success_at = 90
        bot._last_live_refresh_at = 1000
        end = datetime.now(TAIPEI) + timedelta(minutes=30)
        calls = []
        def close(_serial):
            calls.append("close")
            return True
        def open_live(_serial):
            calls.append("open")
            return True
        def screenshot(_serial):
            bot._stop_event.set()
            return None
        with patch("bot_logic.quiet_window_end", side_effect=[end, None]), \
             patch("bot_logic.time.monotonic", side_effect=[100, 3700, 3701]), \
             patch("bot_logic.close_shopee", side_effect=close), \
             patch("bot_logic.notify_device_paused") as notify_pause, \
             patch("bot_logic.open_shopee_live", side_effect=open_live), \
             patch("bot_logic.daily_count", return_value=0), \
             patch("bot_logic.get_screenshot", side_effect=screenshot), \
             patch("bot_logic.time.sleep"), \
             patch.object(bot._stop_event, "wait", return_value=False):
            bot._run_loop_body()
        self.assertEqual(calls, ["close", "open"])
        notify_pause.assert_called_once_with("phone-a", "phone-a", end)
        self.assertEqual(bot._started_monotonic, 3600)
        self.assertEqual(bot._last_success_at, 3690)

    def test_starting_during_quiet_does_not_send_interruption_notice(self):
        bot = ShopeeBot("phone-a")
        bot._skip_initial_quiet_notice = True
        end = datetime.now(TAIPEI) + timedelta(minutes=30)
        def wait_and_stop(_seconds):
            bot._stop_event.set()
            return True
        with patch("bot_logic.quiet_window_end", return_value=end), \
             patch("bot_logic.close_shopee", return_value=True), \
             patch("bot_logic.notify_device_paused") as notify_pause, \
             patch.object(bot._stop_event, "wait", side_effect=wait_and_stop):
            bot._run_loop_body()
        notify_pause.assert_not_called()

    def test_launch_closes_promo_and_verifies_live_page(self):
        screen = np.zeros((2220, 1080, 3), dtype=np.uint8)
        adb_result = type("Result", (), {"returncode": 0, "stdout": b"Events injected: 1"})()
        with patch("bot_logic._adb", return_value=adb_result) as adb, \
             patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.time.sleep"), \
             patch("bot_logic.get_screenshot", return_value=screen), \
             patch("bot_logic.blue_card_present", return_value=False), \
             patch("bot_logic.find_template", return_value=None), \
             patch("bot_logic.live_tab_selected", side_effect=[False, False, True]), \
             patch("bot_logic.find_live_tab", return_value=None), \
             patch("bot_logic.find_startup_promo_close", side_effect=[(950, 460), None]), \
             patch("bot_logic.find_home_live_entry", return_value=(540, 2018)), \
             patch("bot_logic.tap", return_value=True) as tap:
            self.assertTrue(open_shopee_live("phone-a"))
        self.assertEqual(tap.call_count, 2)
        adb.assert_any_call("phone-a", "shell", "settings", "put", "system", "accelerometer_rotation", "0")
        tap.assert_any_call("phone-a", 950, 460)
        tap.assert_any_call("phone-a", 540, 2018)

    def test_launch_closes_if_quiet_window_starts_during_startup(self):
        adb_result = type("Result", (), {"returncode": 0, "stdout": b"OK"})()
        with patch("bot_logic.quiet_window_end", side_effect=[None, datetime.now(TAIPEI)]), \
             patch("bot_logic._adb", return_value=adb_result), \
             patch("bot_logic.time.sleep"), \
             patch("bot_logic.close_shopee", return_value=True) as close:
            self.assertFalse(open_shopee_live("phone-a"))
        close.assert_called_once_with("phone-a")


if __name__ == "__main__":
    unittest.main()
