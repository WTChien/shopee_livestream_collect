import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import foreground_app
import line_webhook
import main
from bot_logic import ShopeeBot
from test_line_webhook import run_immediate, signed_request


class ForegroundTests(unittest.TestCase):
    def test_reads_home_shopee_and_other(self):
        home = "mResumedActivity: ActivityRecord{d48df21 u0 com.sec.android.app.launcher/.activities.LauncherActivity t2}"
        shopee = "mResumedActivity: ActivityRecord{48bd63a u0 com.shopee.tw/com.shopee.app.HomeActivity t194}"
        pikmin = "mResumedActivity: ActivityRecord{48bd63a u0 com.nianticlabs.pikmin/.MainActivity t194}"
        launcher = "priority=0\ncom.sec.android.app.launcher/.activities.LauncherActivity\n"
        with patch.object(foreground_app, "_shell", side_effect=[home, launcher, shopee, pikmin, launcher]):
            self.assertEqual(foreground_app.foreground_app("phone")["kind"], "home")
            self.assertEqual(foreground_app.foreground_app("phone")["kind"], "shopee")
            self.assertEqual(foreground_app.foreground_app("phone")["kind"], "other")


class DashboardControlTests(unittest.TestCase):
    def test_running_live_card_keeps_existing_bot(self):
        active = SimpleNamespace(running=True)
        with patch.dict(main._bots, {"phone": active}, clear=True), \
             patch.object(main, "_adb_devices", return_value=["phone"]), \
             patch.object(main, "quiet_window_end", return_value=None), \
             patch.object(main, "read_status", return_value=None), \
             patch.object(main, "daily_count", return_value=0), \
             patch.object(main, "get_daily_limit", return_value=90), \
             patch.object(main, "foreground_app", return_value={"kind": "shopee", "package": "com.shopee.tw"}), \
             patch.object(main, "get_screenshot", return_value=object()), \
             patch.object(main, "inspect_reward", return_value=object()), \
             patch.object(main, "blue_card_present", return_value=True), \
             patch.object(main, "close_shopee") as close, \
             patch.object(main, "ShopeeBot") as new_bot:
            result = main.start_device("phone")
            self.assertEqual(result["mode"], "continued")
            close.assert_not_called()
            new_bot.assert_not_called()

    def test_force_restart_requires_running_bot(self):
        with patch.dict(main._bots, {}, clear=True), \
             patch.object(main, "_adb_devices", return_value=["phone"]), \
             patch.object(main, "quiet_window_end", return_value=None), \
             patch.object(main, "read_status", return_value=None):
            with self.assertRaises(HTTPException) as caught:
                main.start_device("phone", main.StartRequest(force_restart=True))
            self.assertEqual(caught.exception.status_code, 409)

    def test_guarded_bot_stops_if_user_switches_to_pikmin(self):
        bot = ShopeeBot("phone", guard_foreground=True)
        with patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.foreground_app", return_value={"kind": "other", "package": "com.nianticlabs.pikmin"}), \
             patch("bot_logic.get_screenshot") as screenshot:
            bot._run_loop_body()
        screenshot.assert_not_called()
        self.assertIn("com.nianticlabs.pikmin", bot.status_message)

    def test_guarded_bot_reopens_shopee_after_returning_home(self):
        bot = ShopeeBot("phone", guard_foreground=True)
        def open_and_stop(*_args, **_kwargs):
            bot._stop_event.set()
            return True
        with patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.foreground_app", return_value={"kind": "home", "package": "com.miui.home"}), \
             patch("bot_logic.close_shopee", return_value=True) as close, \
             patch("bot_logic.open_shopee_live", side_effect=open_and_stop) as open_live, \
             patch("bot_logic.notify_device_crashed") as notify, \
             patch("bot_logic.daily_count", return_value=0), \
             patch("bot_logic.get_screenshot", return_value=None):
            bot._run_loop_body()
        close.assert_called_once_with("phone")
        open_live.assert_called_once_with("phone", guard_foreground=True)
        notify.assert_called_once_with("phone", "phone", 1)

    def test_repeated_home_crashes_cool_down_before_retrying(self):
        bot = ShopeeBot("phone", guard_foreground=True)
        def cool_down(seconds):
            if seconds == 60:
                bot._stop_event.set()
        with patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.foreground_app", return_value={"kind": "home", "package": "com.miui.home"}), \
             patch("bot_logic.close_shopee", return_value=True) as close, \
             patch("bot_logic.open_shopee_live", return_value=True) as open_live, \
             patch("bot_logic.notify_device_crashed") as notify, \
             patch.object(bot, "_wait_for_retry", side_effect=cool_down) as retry, \
             patch.object(bot._stop_event, "wait", return_value=False), \
             patch("bot_logic.daily_count", return_value=0), \
             patch("bot_logic.get_screenshot", return_value=None):
            bot._run_loop_body()
        self.assertEqual(close.call_count, 3)
        self.assertEqual(open_live.call_count, 3)
        self.assertEqual([call.args[2] for call in notify.call_args_list], [1, 2, 3])
        retry.assert_called_once_with(60)
        self.assertIn("60 秒後再重啟", bot.status_message)

    def test_failed_live_entry_keeps_retrying_until_stopped(self):
        bot = ShopeeBot("phone", guard_foreground=True, launch_on_start=True)
        attempts = 0
        def open_live(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 3:
                bot._stop_event.set()
                return True
            return False
        with patch("bot_logic.quiet_window_end", return_value=None), \
             patch("bot_logic.foreground_app", return_value={"kind": "home", "package": "com.miui.home"}), \
             patch("bot_logic.open_shopee_live", side_effect=open_live) as open_call, \
             patch("bot_logic.close_shopee", return_value=True), \
             patch.object(bot, "_wait_for_retry") as retry, \
             patch("bot_logic.daily_count", return_value=0), \
             patch("bot_logic.get_screenshot", return_value=None), \
             patch.object(bot, "_finish_session") as finish:
            bot._run_loop_body()
        self.assertEqual(open_call.call_count, 3)
        self.assertEqual([call.args[0] for call in retry.call_args_list], [10, 20])
        finish.assert_not_called()

    def test_start_rejects_other_app_without_starting_bot(self):
        with patch.object(main, "_adb_devices", return_value=["phone"]), \
             patch.object(main, "quiet_window_end", return_value=None), \
             patch.object(main, "read_status", return_value=None), \
             patch.object(main, "daily_count", return_value=0), \
             patch.object(main, "get_daily_limit", return_value=90), \
             patch.object(main, "foreground_app", return_value={"kind": "other", "package": "com.nianticlabs.pikmin"}), \
             patch.object(main, "ShopeeBot") as bot:
            with self.assertRaises(HTTPException) as caught:
                main.start_device("phone")
            self.assertEqual(caught.exception.status_code, 409)
            self.assertIn("com.nianticlabs.pikmin", caught.exception.detail)
            bot.assert_not_called()

    def test_start_from_home_launches_guarded_bot(self):
        with patch.object(main, "_adb_devices", return_value=["phone"]), \
             patch.object(main, "quiet_window_end", return_value=None), \
             patch.object(main, "read_status", return_value=None), \
             patch.object(main, "daily_count", return_value=0), \
             patch.object(main, "get_daily_limit", return_value=90), \
             patch.object(main, "foreground_app", return_value={"kind": "home", "package": "com.miui.home"}), \
             patch.object(main, "get_schedule", return_value={"completion_mode": "idle", "idle_minutes": 30,
                                                             "max_runtime_minutes": 240}), \
             patch.object(main, "_load_nicknames", return_value={"phone": "A8+"}), \
             patch.object(main, "close_shopee", return_value=True) as close, \
             patch.object(main, "ShopeeBot") as bot:
            try:
                result = main.start_device("phone")
                self.assertTrue(result["running"])
                self.assertTrue(bot.call_args.kwargs["launch_on_start"])
                self.assertTrue(bot.call_args.kwargs["guard_foreground"])
                bot.return_value.start.assert_called_once()
                close.assert_called_once_with("phone")
            finally:
                main._bots.pop("phone", None)

    def test_resume_live_card_starts_without_reopening_shopee(self):
        with patch.object(main, "_adb_devices", return_value=["phone"]), \
             patch.object(main, "quiet_window_end", return_value=None), \
             patch.object(main, "read_status", return_value=None), \
             patch.object(main, "daily_count", return_value=0), \
             patch.object(main, "get_daily_limit", return_value=90), \
             patch.object(main, "foreground_app", return_value={"kind": "shopee", "package": "com.shopee.tw"}), \
             patch.object(main, "get_screenshot", return_value=object()), \
             patch.object(main, "inspect_reward", return_value=object()), \
             patch.object(main, "blue_card_present", return_value=True), \
             patch.object(main, "get_schedule", return_value={"completion_mode": "idle", "idle_minutes": 30,
                                                             "max_runtime_minutes": 240}), \
             patch.object(main, "_load_nicknames", return_value={"phone": "A8+"}), \
             patch.object(main, "ShopeeBot") as bot:
            try:
                result = main.start_device("phone")
                self.assertTrue(result["resumed_live"])
                self.assertEqual(result["mode"], "resumed")
                self.assertFalse(bot.call_args.kwargs["launch_on_start"])
                self.assertTrue(bot.call_args.kwargs["guard_foreground"])
            finally:
                main._bots.pop("phone", None)

    def test_shopee_without_blue_card_returns_home_and_relaunches(self):
        with patch.dict(main._bots, {}, clear=True), \
             patch.object(main, "_adb_devices", return_value=["phone"]), \
             patch.object(main, "quiet_window_end", return_value=None), \
             patch.object(main, "read_status", return_value=None), \
             patch.object(main, "daily_count", return_value=0), \
             patch.object(main, "get_daily_limit", return_value=90), \
             patch.object(main, "foreground_app", side_effect=[
                 {"kind": "shopee", "package": "com.shopee.tw"},
                 {"kind": "home", "package": "com.miui.home"}]), \
             patch.object(main, "get_screenshot", return_value=object()), \
             patch.object(main, "inspect_reward", return_value=object()), \
             patch.object(main, "blue_card_present", return_value=False), \
             patch.object(main, "close_shopee", return_value=True) as close, \
             patch.object(main, "get_schedule", return_value={"completion_mode": "idle", "idle_minutes": 30,
                                                             "max_runtime_minutes": 240}), \
             patch.object(main, "_load_nicknames", return_value={"phone": "A8+"}), \
             patch.object(main, "ShopeeBot") as bot:
            result = main.start_device("phone")
            self.assertEqual(result["mode"], "started")
            close.assert_called_once_with("phone")
            self.assertTrue(bot.call_args.kwargs["launch_on_start"])


class LineControlTests(unittest.TestCase):
    def test_live_card_continues_without_closing_app(self):
        device = {"serial": "phone", "nickname": "A8+", "today_reserved": 0,
                  "running": True,
                  "foreground": {"kind": "shopee", "package": "com.shopee.tw"}}
        with patch.object(line_webhook, "_dashboard_post", side_effect=[
                {"mode": "continued"}, {"mode": "resumed"}]) as post:
            self.assertIn("維持目前流程", line_webhook._restart_device(device))
            device["running"] = False
            self.assertIn("接續刷取", line_webhook._restart_device(device))
            self.assertEqual([call.args for call in post.call_args_list], [
                ("/devices/phone/start", {"force_restart": False}),
                ("/devices/phone/start", {"force_restart": False}),
            ])

    def test_restart_requires_running_and_reopens_even_with_live_card(self):
        device = {"serial": "phone", "nickname": "A8+", "today_reserved": 0,
                  "running": False,
                  "foreground": {"kind": "shopee", "package": "com.shopee.tw"}}
        with patch.object(line_webhook, "_dashboard_post", return_value={"mode": "restarted"}) as post:
            device["running"] = True
            self.assertIn("重新開啟", line_webhook._restart_device(device, force_restart=True))
            post.assert_called_once_with("/devices/phone/start", {"force_restart": True})

    def test_webhook_routes_restart_command(self):
        body = {"events": [{"type": "message", "replyToken": "reply",
                 "source": {"type": "group", "groupId": "Cbound"},
                 "message": {"type": "text", "text": "/重啟 A8+"}}]}
        with patch.object(line_webhook, "line_setting", side_effect=lambda key: {
                "LINE_CHANNEL_SECRET": "secret", "LINE_CHANNEL_ACCESS_TOKEN": "token"}.get(key, "")), \
             patch.object(line_webhook, "bound_group", return_value="Cbound"), \
             patch.object(line_webhook, "_dashboard_json", return_value={"devices": [
                 {"serial": "phone", "nickname": "A8+", "running": True}]}), \
             patch.object(line_webhook, "_restart_device", return_value="已重啟") as restart, \
             patch.object(line_webhook, "_reply") as reply:
            run_immediate(line_webhook.webhook(signed_request(body, "secret")))
            restart.assert_called_once()
            self.assertTrue(restart.call_args.kwargs["force_restart"])
            self.assertEqual(reply.call_args.args[1], "已重啟")

    def test_help_command_lists_available_commands(self):
        body = {"events": [{"type": "message", "replyToken": "reply",
                 "source": {"type": "user", "userId": "Uany"},
                 "message": {"type": "text", "text": "/指令"}}]}
        with patch.object(line_webhook, "line_setting", side_effect=lambda key: {
                "LINE_CHANNEL_SECRET": "secret", "LINE_CHANNEL_ACCESS_TOKEN": "token"}.get(key, "")), \
             patch.object(line_webhook, "_reply") as reply:
            run_immediate(line_webhook.webhook(signed_request(body, "secret")))
            message = reply.call_args.args[1]
            for command in ("/指令", "/狀態", "/啟動", "/重啟", "/停止", "/綁定", "/解除綁定"):
                self.assertIn(command, message)
            self.assertNotIn("/蝦幣", message)

    def test_webhook_start_uses_nickname_and_rejects_other_group(self):
        def event(group_id):
            return {"events": [{"type": "message", "replyToken": "reply",
                    "source": {"type": "group", "groupId": group_id},
                    "message": {"type": "text", "text": "/啟動 A8+"}}]}
        with patch.object(line_webhook, "line_setting", side_effect=lambda key: {
                "LINE_CHANNEL_SECRET": "secret", "LINE_CHANNEL_ACCESS_TOKEN": "token"}.get(key, "")), \
             patch.object(line_webhook, "bound_group", return_value="Cbound"), \
             patch.object(line_webhook, "_dashboard_json", return_value={"devices": [
                 {"serial": "phone", "nickname": "A8+", "today_reserved": 0,
                  "foreground": {"kind": "shopee", "package": "com.shopee.tw"}}]}), \
             patch.object(line_webhook, "_dashboard_post", return_value={"mode": "started"}) as post, \
             patch.object(line_webhook, "_reply") as reply:
            run_immediate(line_webhook.webhook(signed_request(event("Cother"), "secret")))
            post.assert_not_called()
            self.assertIn("只能在已綁定", reply.call_args.args[1])
            run_immediate(line_webhook.webhook(signed_request(event("Cbound"), "secret")))
            post.assert_called_once_with("/devices/phone/start", {"force_restart": False})
            self.assertIn("重新開啟", reply.call_args.args[1])

    def test_status_shows_active_app_but_omits_disconnected_phone(self):
        text = line_webhook.format_status([
            {"nickname": "A8+", "running": False, "connected": True,
             "today_reserved": 3, "today_amount": 1.2,
             "foreground": {"kind": "other", "package": "com.nianticlabs.pikmin"}},
            {"nickname": "小米", "running": False, "connected": False},
        ], {"daily_limit": 90, "today_count": 3, "today_amount": 1.2})
        self.assertIn("A8+：已停止｜今日 3/90 次｜1.20 蝦幣", text)
        self.assertIn("目前畫面：皮克敏 Bloom", text)
        self.assertNotIn("小米", text)

    def test_status_shows_quiet_wait_as_not_started(self):
        text = line_webhook.format_status([
            {"nickname": "A8+", "running": False, "paused_for_quiet": True,
             "resume_at": "00:30", "foreground": {"kind": "home"},
             "today_reserved": 0, "today_amount": 0.0}
        ], {"daily_limit": 90, "today_count": 0, "today_amount": 0.0})
        self.assertIn("A8+：未啟動（00:30 自動恢復）", text)

    def test_only_bound_group_can_control(self):
        with patch.object(line_webhook, "bound_group", return_value="Cbound"), \
             patch.object(line_webhook, "line_setting", return_value=""):
            self.assertTrue(line_webhook._control_authorized({"type": "group", "groupId": "Cbound"}))
            self.assertFalse(line_webhook._control_authorized({"type": "group", "groupId": "Cother"}))
            self.assertFalse(line_webhook._control_authorized({"type": "user", "userId": "Uany"}))


if __name__ == "__main__":
    unittest.main()
