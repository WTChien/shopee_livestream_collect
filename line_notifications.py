"""One bound LINE group for collector stop notifications."""

import json
import logging
import os
import urllib.request
from datetime import datetime
from pathlib import Path

from claim_store import TAIPEI


logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
BINDING_PATH = BASE_DIR / "data" / "line_notification_group.json"
PUSH_URL = "https://api.line.me/v2/bot/message/push"


def line_setting(key: str) -> str:
    value = os.environ.get(key, "")
    if value:
        return value
    try:
        for line in (BASE_DIR / "line.env").read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == key:
                return value.strip()
    except OSError:
        pass
    return ""


def bound_group() -> str | None:
    try:
        group_id = json.loads(BINDING_PATH.read_text(encoding="utf-8"))["group_id"]
        return group_id if isinstance(group_id, str) and group_id.startswith("C") else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def bind_group(group_id: str) -> None:
    if not group_id.startswith("C"):
        raise ValueError("Invalid LINE group ID")
    BINDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = BINDING_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"group_id": group_id}) + "\n", encoding="utf-8")
    temporary.replace(BINDING_PATH)


def unbind_group(group_id: str) -> bool:
    if bound_group() != group_id:
        return False
    BINDING_PATH.unlink(missing_ok=True)
    return True


def _push_bound_group(serial: str, message: str) -> bool:
    group_id = bound_group()
    access_token = line_setting("LINE_CHANNEL_ACCESS_TOKEN")
    if not group_id or not access_token:
        return False
    payload = json.dumps({"to": group_id, "messages": [{"type": "text", "text": message}]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        PUSH_URL, data=payload, method="POST",
        headers={"Authorization": "Bearer " + access_token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8):
            pass
        return True
    except OSError:
        logger.exception("[%s] LINE group notification failed", serial)
        return False


def notify_device_stopped(serial: str, nickname: str, reason: str) -> bool:
    """Send once per completed collector run. Errors never interrupt shutdown."""
    label = nickname if nickname == serial else f"{nickname}（{serial}）"
    message = f"🛑 蝦幣裝置已停止\n裝置：{label}\n原因：{reason}\n時間：{datetime.now(TAIPEI):%m/%d %H:%M}"
    return _push_bound_group(serial, message)


def notify_device_paused(serial: str, nickname: str, quiet_end: datetime) -> bool:
    """Notify the bound group when an active collector enters daily quiet hours."""
    label = nickname if nickname == serial else f"{nickname}（{serial}）"
    message = (f"🌙 蝦幣裝置暫停\n裝置：{label}\n原因：每日停止採集時段"
               f"\n預計恢復：{quiet_end:%m/%d %H:%M}（台灣時間）")
    return _push_bound_group(serial, message)


def notify_device_crashed(serial: str, nickname: str, restart_number: int) -> bool:
    """Notify the bound group after a running collector confirms the launcher is foreground."""
    label = nickname if nickname == serial else f"{nickname}（{serial}）"
    message = (f"⚠️ 蝦皮疑似閃退\n裝置：{label}\n目前畫面：手機桌面"
               f"\n處理：正在重新開啟蝦皮並進入直播（10 分鐘內第 {restart_number} 次）"
               f"\n時間：{datetime.now(TAIPEI):%m/%d %H:%M}")
    return _push_bound_group(serial, message)
