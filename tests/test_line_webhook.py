import base64
import hashlib
import hmac
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request

import line_webhook
import line_notifications


def signed_request(body: dict, secret: str) -> Request:
    payload = json.dumps(body).encode("utf-8")
    signature = base64.b64encode(hmac.new(secret.encode(), payload, hashlib.sha256).digest()).decode()
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/webhook",
                    "headers": [(b"x-line-signature", signature.encode())]}, receive)


def run_immediate(coroutine):
    """Drive the handler with an in-memory request and mocked network calls."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("Unexpected asynchronous I/O in webhook test")


class LineWebhookTests(unittest.TestCase):
    def test_temporary_image_is_kept_in_memory_and_expires(self):
        original = io.BytesIO()
        Image.new("RGB", (100, 200), "blue").save(original, format="PNG")
        token = line_webhook._store_image(original.getvalue())
        try:
            full = line_webhook.temporary_image(token, "original")
            preview = line_webhook.temporary_image(token, "preview")
            self.assertEqual(full.media_type, "image/png")
            self.assertEqual(full.body, original.getvalue())
            self.assertEqual(preview.media_type, "image/jpeg")
            self.assertLess(len(preview.body), 1_000_000)
            line_webhook._image_cache[token]["expires_at"] = 0
            with self.assertRaises(HTTPException) as expired:
                line_webhook.temporary_image(token, "original")
            self.assertEqual(expired.exception.status_code, 404)
        finally:
            line_webhook._image_cache.pop(token, None)

    def test_screenshot_command_only_replies_with_image_in_bound_group(self):
        def event(group_id):
            return {"events": [{"type": "message", "replyToken": "reply",
                    "source": {"type": "group", "groupId": group_id},
                    "message": {"type": "text", "text": "/畫面 A8+"}}]}
        with patch.object(line_webhook, "line_setting", side_effect=lambda key: {
                "LINE_CHANNEL_SECRET": "secret", "LINE_CHANNEL_ACCESS_TOKEN": "token"}.get(key, "")), \
             patch.object(line_webhook, "bound_group", return_value="Cbound"), \
             patch.object(line_webhook, "_dashboard_json", return_value={"devices": [
                 {"serial": "phone", "nickname": "A8+"}]}), \
             patch.object(line_webhook, "_dashboard_screenshot", return_value=b"png") as screenshot, \
             patch.object(line_webhook, "_store_image", return_value="random-token"), \
             patch.object(line_webhook, "_public_base_url", return_value="https://example.com"), \
             patch.object(line_webhook, "_reply_messages") as reply_image, \
             patch.object(line_webhook, "_reply") as reply_text:
            run_immediate(line_webhook.webhook(signed_request(event("Cother"), "secret")))
            screenshot.assert_not_called()
            reply_image.assert_not_called()
            self.assertIn("已綁定", reply_text.call_args.args[1])
            run_immediate(line_webhook.webhook(signed_request(event("Cbound"), "secret")))
            screenshot.assert_called_once_with("phone")
            messages = reply_image.call_args.args[1]
            self.assertEqual(messages[1]["type"], "image")
            self.assertEqual(messages[1]["originalContentUrl"],
                             "https://example.com/images/random-token/original")
            self.assertEqual(messages[1]["previewImageUrl"],
                             "https://example.com/images/random-token/preview")

    def test_group_binding_and_unbinding_only_affect_notifications(self):
        binding = line_notifications.BASE_DIR / "tests" / ".tmp" / "line_notification_group_test.json"
        binding.parent.mkdir(parents=True, exist_ok=True)
        binding.unlink(missing_ok=True)

        def event(command, source_type="group", group_id="Cgroup1"):
            return {"events": [{"type": "message", "replyToken": "reply",
                "source": {"type": source_type, "groupId": group_id},
                "message": {"type": "text", "text": command}}]}

        def send(command, source_type="group", group_id="Cgroup1"):
            run_immediate(line_webhook.webhook(signed_request(event(command, source_type, group_id), "secret")))

        try:
            with patch.object(line_notifications, "BINDING_PATH", binding), \
                 patch.object(line_webhook, "line_setting", side_effect=lambda key: {
                     "LINE_CHANNEL_SECRET": "secret", "LINE_CHANNEL_ACCESS_TOKEN": "token",
                     "LINE_GROUP_BIND_CODE": "test-password"}.get(key, "")), \
                 patch.object(line_webhook, "_reply") as reply:
                send("/綁定 test-password", "user")
                self.assertIsNone(line_notifications.bound_group())
                send("/綁定 wrong")
                self.assertIsNone(line_notifications.bound_group())
                send("/綁定 test-password")
                self.assertEqual(line_notifications.bound_group(), "Cgroup1")
                send("/綁定 test-password", group_id="Cgroup2")
                self.assertEqual(line_notifications.bound_group(), "Cgroup2")
                send("/解除綁定 test-password", group_id="Cgroup1")
                self.assertEqual(line_notifications.bound_group(), "Cgroup2")
                send("/解除綁定 wrong", group_id="Cgroup2")
                self.assertEqual(line_notifications.bound_group(), "Cgroup2")
                send("/解除綁定 test-password", group_id="Cgroup2")
                self.assertIsNone(line_notifications.bound_group())
                self.assertEqual(reply.call_count, 7)
        finally:
            binding.unlink(missing_ok=True)

    def test_stop_push_targets_only_bound_group(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        with patch.object(line_notifications, "bound_group", return_value="Cgroup1"), \
             patch.object(line_notifications, "line_setting", return_value="token"), \
             patch.object(line_notifications.urllib.request, "urlopen", return_value=Response()) as send:
            self.assertTrue(line_notifications.notify_device_stopped("serial", "A8+", "手動停止"))
            request = send.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(payload["to"], "Cgroup1")
            self.assertIn("A8+", payload["messages"][0]["text"])
            self.assertIn("手動停止", payload["messages"][0]["text"])
        with patch.object(line_notifications, "bound_group", return_value=None), \
             patch.object(line_notifications.urllib.request, "urlopen") as send:
            self.assertFalse(line_notifications.notify_device_stopped("serial", "A8+", "手動停止"))
            send.assert_not_called()

    def test_quiet_pause_push_reports_resume_time_to_bound_group(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        quiet_end = datetime(2026, 11, 9, 0, 30, tzinfo=timezone(timedelta(hours=8)))
        with patch.object(line_notifications, "bound_group", return_value="Cgroup1"), \
             patch.object(line_notifications, "line_setting", return_value="token"), \
             patch.object(line_notifications.urllib.request, "urlopen", return_value=Response()) as send:
            self.assertTrue(line_notifications.notify_device_paused("serial", "A8+", quiet_end))
            payload = json.loads(send.call_args.args[0].data)
            text = payload["messages"][0]["text"]
            self.assertIn("A8+", text)
            self.assertIn("暫停", text)
            self.assertIn("11/09 00:30", text)

    def test_crash_push_reports_automatic_restart_to_bound_group(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        with patch.object(line_notifications, "bound_group", return_value="Cgroup1"), \
             patch.object(line_notifications, "line_setting", return_value="token"), \
             patch.object(line_notifications.urllib.request, "urlopen", return_value=Response()) as send:
            self.assertTrue(line_notifications.notify_device_crashed("serial", "A8+", 2))
            payload = json.loads(send.call_args.args[0].data)
            self.assertEqual(payload["to"], "Cgroup1")
            self.assertIn("閃退", payload["messages"][0]["text"])
            self.assertIn("正在重新開啟蝦皮", payload["messages"][0]["text"])
            self.assertIn("第 2 次", payload["messages"][0]["text"])

    def test_status_shows_each_device_and_total(self):
        devices = [
            {"nickname": "A8+", "running": True, "today_reserved": 34, "today_amount": 10.54},
            {"nickname": "小米", "running": False, "today_reserved": 28, "today_amount": 9.20},
        ]
        message = line_webhook.format_status(
            devices, {"daily_limit": 90, "today_count": 62, "pending_count": 0,
                      "today_amount": 19.74},
            now=datetime(2026, 11, 8, 10, 40, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertIn("A8+：刷取中｜今日 34/90 次｜10.54 蝦幣", message)
        self.assertIn("小米：已停止｜今日 28/90 次｜9.20 蝦幣", message)
        self.assertIn("今日合計：62 次｜19.74 蝦幣", message)

    def test_signature_and_private_or_group_chat_without_pairing(self):
        with patch.dict("os.environ", {"LINE_CHANNEL_SECRET": "secret",
                                    "LINE_CHANNEL_ACCESS_TOKEN": "token"}), \
             patch.object(line_webhook, "_reply") as reply, \
             patch.object(line_webhook, "_dashboard_json", side_effect=[
                 {"devices": [{"nickname": "A8+", "running": True,
                               "today_reserved": 4, "today_amount": 1.20}]},
                 {"daily_limit": 90, "today_count": 4, "pending_count": 0,
                  "today_amount": 1.20},
                 {"devices": [{"nickname": "A8+", "running": True,
                               "today_reserved": 4, "today_amount": 1.20}]},
                 {"daily_limit": 90, "today_count": 4, "pending_count": 0,
                  "today_amount": 1.20},
                 {"devices": [{"nickname": "A8+", "running": True,
                               "today_reserved": 4, "today_amount": 1.20}]},
                 {"daily_limit": 90, "today_count": 4, "pending_count": 0,
                  "today_amount": 1.20},
             ]) as dashboard:
            def event(user_id, source_type="user"):
                return {"events": [{"type": "message", "replyToken": "reply",
                    "source": {"type": source_type, "userId": user_id},
                    "message": {"type": "text", "text": "/狀態"}}]}
            run_immediate(line_webhook.webhook(signed_request(event("U123"), "secret")))
            run_immediate(line_webhook.webhook(signed_request(event("U456"), "secret")))
            run_immediate(line_webhook.webhook(signed_request(event("U123", "group"), "secret")))
            self.assertIn("A8+：刷取中", reply.call_args.args[1])
            self.assertEqual(reply.call_count, 3)
            self.assertEqual(dashboard.call_count, 6)
            self.assertEqual(dashboard.call_args_list[0].args[0], "/devices")
            bad = signed_request(event("U123"), "wrong-secret")
            with self.assertRaises(HTTPException) as context:
                run_immediate(line_webhook.webhook(bad))
            self.assertEqual(context.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
