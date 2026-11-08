"""LINE status and group-only stop alerts. Expose this app, never the dashboard."""

import base64
import hashlib
import hmac
import io
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from PIL import Image
from line_notifications import bind_group, bound_group, line_setting, unbind_group


app = FastAPI(title="Shopee LINE Status Bot", docs_url=None, redoc_url=None, openapi_url=None)
DASHBOARD_URL = "http://127.0.0.1:4001"
REPLY_URL = "https://api.line.me/v2/bot/message/reply"
PUBLIC_BASE_URL = "https://shopeeline.weiting.win"
IMAGE_TTL_SECONDS = 600
_image_cache: dict[str, dict] = {}
_image_cache_lock = threading.Lock()


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = base64.b64encode(hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()).decode("ascii")
    return hmac.compare_digest(expected, signature)


def format_status(devices: list[dict], stats: dict, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone(timedelta(hours=8)))
    devices = [device for device in devices if device.get("connected", True)]
    lines = [f"📱 手機狀態與蝦幣｜{now:%m/%d %H:%M}"]
    if not devices:
        lines.append("目前沒有連接的手機。")
    for device in devices:
        running = (f"未啟動（{device.get('resume_at', '稍後')} 自動恢復）"
                   if device.get("paused_for_quiet") else "未啟動（自動重試中）"
                   if device.get("retrying") else "刷取中" if device["running"] else "已停止")
        lines.append(
            f"{device['nickname']}：{running}｜今日 {device['today_reserved']}/{stats['daily_limit']} 次"
            f"｜{device['today_amount']:.2f} 蝦幣"
        )
        lines.append(f"　↳ 目前畫面：{_app_name(device.get('foreground'))}")
        if device.get("status") not in (None, "", "idle", "stopped"):
            lines.append(f"　↳ {device['status']}")
    lines.append(f"今日合計：{stats['today_count'] + stats.get('pending_count', 0)} 次｜{stats['today_amount']:.2f} 蝦幣")
    lines.append("※ 未讀到成功提示的點擊，按卡片金額計入。")
    return "\n".join(lines)


def _dashboard_json(path: str, *, timeout: int = 12) -> dict:
    with urllib.request.urlopen(DASHBOARD_URL + path, timeout=timeout) as response:
        return json.load(response)


def _dashboard_post(path: str, data: dict | None = None) -> dict:
    request = urllib.request.Request(DASHBOARD_URL + path, data=json.dumps(data or {}).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)


def _dashboard_screenshot(serial: str) -> bytes:
    path = "/devices/" + urllib.parse.quote(serial, safe="") + "/screenshot"
    with urllib.request.urlopen(DASHBOARD_URL + path, timeout=25) as response:
        if response.headers.get_content_type() != "image/png":
            raise ValueError("工作台未回傳 PNG 截圖")
        data = response.read(20_000_001)
    if not data or len(data) > 20_000_000:
        raise ValueError("手機截圖無效或過大")
    return data


def _store_image(screenshot: bytes) -> str:
    """Hold one screenshot briefly in memory; never write an image file."""
    with Image.open(io.BytesIO(screenshot)) as image:
        preview = image.convert("RGB")
        preview.thumbnail((480, 960))
        preview_buffer = io.BytesIO()
        preview.save(preview_buffer, format="JPEG", quality=80, optimize=True)
        preview_bytes = preview_buffer.getvalue()
        if len(screenshot) > 10_000_000:
            original_buffer = io.BytesIO()
            image.convert("RGB").save(original_buffer, format="JPEG", quality=85, optimize=True)
            screenshot = original_buffer.getvalue()
            original_type = "image/jpeg"
        else:
            original_type = "image/png"
    if len(screenshot) > 10_000_000 or len(preview_bytes) > 1_000_000:
        raise ValueError("截圖超過 LINE 圖片大小限制")
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _image_cache_lock:
        for stale in [key for key, item in _image_cache.items() if item["expires_at"] <= now]:
            del _image_cache[stale]
        while len(_image_cache) >= 20:
            del _image_cache[next(iter(_image_cache))]
        _image_cache[token] = {"expires_at": now + IMAGE_TTL_SECONDS,
                               "original": (screenshot, original_type),
                               "preview": (preview_bytes, "image/jpeg")}
    return token


@app.get("/images/{token}/{variant}", include_in_schema=False)
def temporary_image(token: str, variant: str):
    if variant not in ("original", "preview"):
        raise HTTPException(status_code=404, detail="找不到圖片")
    with _image_cache_lock:
        item = _image_cache.get(token)
        if item is None or item["expires_at"] <= time.monotonic():
            _image_cache.pop(token, None)
            raise HTTPException(status_code=404, detail="圖片已過期")
        data, media_type = item[variant]
    return Response(data, media_type=media_type,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def _public_base_url() -> str:
    base = (line_setting("LINE_PUBLIC_BASE_URL") or PUBLIC_BASE_URL).rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("LINE_PUBLIC_BASE_URL 必須是公開 HTTPS 網域")
    return base


def _app_name(foreground: dict | None) -> str:
    foreground = foreground or {}
    kind, package = foreground.get("kind"), foreground.get("package")
    if kind == "home":
        return "主畫面"
    if kind == "shopee":
        return "蝦皮"
    return {"com.nianticlabs.pikmin": "皮克敏 Bloom",
            "com.nianticlabs.pokemongo": "Pokémon GO",
            "com.android.vending": "Google Play 商店"}.get(package, package or "無法判斷")


def format_help() -> str:
    return ("🦐 可用指令\n"
            "/指令：查看這份說明\n"
            "/狀態：查看手機畫面、刷取狀態、今日次數與蝦幣\n"
            "/啟動 裝置名稱：直播藍色卡片可辨識就接續，否則重進直播，例如 /啟動 A8+\n"
            "/重啟 裝置名稱：僅限刷取中手機，關閉蝦皮後重進直播\n"
            "/停止 裝置名稱：停止刷取，例如 /停止 A8+\n"
            "/畫面 裝置名稱：將目前手機畫面傳到已綁定群組\n"
            "/綁定 密碼：在群組綁定停止通知及控制權\n"
            "/解除綁定 密碼：在已綁定群組解除綁定\n"
            "※ 啟動／重啟／停止限已綁定群組或已設定 LINE_CONTROL_USER_ID 的私人聊天；畫面只限已綁定群組。")


def _control_authorized(source: dict) -> bool:
    if source.get("type") == "group":
        group_id = bound_group()
        return bool(group_id and source.get("groupId") == group_id)
    control_user = line_setting("LINE_CONTROL_USER_ID")
    return bool(control_user and source.get("type") == "user" and
                hmac.compare_digest(source.get("userId", ""), control_user))


def _dashboard_error(exc: urllib.error.HTTPError) -> str:
    try:
        return json.load(exc).get("detail", "工作台拒絕此操作")
    except (ValueError, OSError, AttributeError):
        return "工作台拒絕此操作"


def _restart_device(device: dict, *, force_restart: bool = False) -> str:
    """Use the same device start decision as the dashboard button."""
    name, serial = device["nickname"], device["serial"]
    result = _dashboard_post(f"/devices/{serial}/start", {"force_restart": force_restart})
    mode = result.get("mode")
    if mode == "continued":
        return f"{name} 已在可辨識藍色卡片的直播畫面刷取中，維持目前流程。"
    if mode == "resumed":
        return f"已從 {name} 目前的直播畫面接續刷取，不重開蝦皮。"
    verb = "重啟" if force_restart else "啟動"
    return f"已讓 {name} 關閉蝦皮並回到主畫面，正重新開啟並進入直播。{verb}進度可用 /狀態 查看。"


def _reply_messages(reply_token: str, messages: list[dict], access_token: str) -> None:
    payload = json.dumps({"replyToken": reply_token, "messages": messages}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        REPLY_URL, data=payload, method="POST",
        headers={"Authorization": "Bearer " + access_token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=12):
        pass


def _reply(reply_token: str, message: str, access_token: str) -> None:
    _reply_messages(reply_token, [{"type": "text", "text": message}], access_token)


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    secret = line_setting("LINE_CHANNEL_SECRET")
    access_token = line_setting("LINE_CHANNEL_ACCESS_TOKEN")
    if not secret or not access_token:
        raise HTTPException(status_code=503, detail="LINE 尚未設定")
    body = await request.body()
    if not verify_signature(body, request.headers.get("x-line-signature", ""), secret):
        raise HTTPException(status_code=401, detail="LINE 簽章不正確")
    try:
        events = json.loads(body).get("events", [])
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="Webhook 格式不正確") from exc
    for event in events:
        if event.get("type") != "message" or event.get("message", {}).get("type") != "text":
            continue
        reply_token = event.get("replyToken")
        source = event.get("source", {})
        if source.get("type") not in ("user", "group"):
            continue
        if not reply_token:
            continue
        command = event["message"].get("text", "").strip()
        if command == "/指令":
            _reply(reply_token, format_help(), access_token)
            continue
        if command.startswith("/綁定") or command.startswith("/解除綁定"):
            parts = command.split()
            if source.get("type") != "group":
                message = "停止通知只能在要接收通知的群組中綁定或解除。"
            elif len(parts) != 2 or parts[0] not in ("/綁定", "/解除綁定"):
                message = "請輸入 /綁定 密碼 或 /解除綁定 密碼。"
            elif not line_setting("LINE_GROUP_BIND_CODE"):
                message = "尚未設定群組綁定密碼，請檢查本機 line.env。"
            elif not hmac.compare_digest(parts[1], line_setting("LINE_GROUP_BIND_CODE")):
                message = "密碼不正確，群組通知設定未變更。"
            elif not source.get("groupId"):
                message = "無法辨識群組，請稍後再試。"
            elif parts[0] == "/綁定":
                bind_group(source["groupId"])
                message = "已綁定此群組。裝置停止時會在這裡通知。"
            else:
                message = ("已解除此群組的停止通知。" if unbind_group(source["groupId"])
                           else "此群組目前沒有綁定停止通知。")
            _reply(reply_token, message, access_token)
            continue
        if command == "/蝦幣":
            _reply(reply_token, "查詢指令已改為 /狀態。", access_token)
            continue
        if command == "/狀態":
            try:
                devices = _dashboard_json("/devices")["devices"]
                message = format_status(devices, _dashboard_json("/stats"))
            except (OSError, KeyError, ValueError, urllib.error.URLError):
                message = "目前無法讀取本機工作台，請確認 4001 服務仍在執行。"
            _reply(reply_token, message, access_token)
            continue
        if command.startswith("/畫面"):
            parts = command.split()
            if len(parts) != 2 or parts[0] != "/畫面":
                _reply(reply_token, "請輸入 /畫面 裝置名稱，例如 /畫面 A8+。", access_token)
                continue
            group_id = bound_group()
            if source.get("type") != "group" or not group_id or source.get("groupId") != group_id:
                _reply(reply_token, "畫面指令只能在已綁定的 LINE 群組使用。", access_token)
                continue
            try:
                devices = _dashboard_json("/devices")["devices"]
                matches = [device for device in devices if device["nickname"].casefold() == parts[1].casefold()
                           or device["serial"].casefold() == parts[1].casefold()]
                if len(matches) != 1:
                    names = "、".join(device["nickname"] for device in devices) or "無"
                    _reply(reply_token, f"找不到唯一的已連線裝置「{parts[1]}」。目前可用：{names}。", access_token)
                    continue
                device = matches[0]
                screenshot = _dashboard_screenshot(device["serial"])
                token = _store_image(screenshot)
                base = _public_base_url()
            except (OSError, KeyError, ValueError, urllib.error.URLError):
                _reply(reply_token, "無法擷取手機畫面或準備圖片，請確認工作台、手機及圖片網址設定。", access_token)
                continue
            _reply_messages(reply_token, [
                {"type": "text", "text": f"{device['nickname']} 目前畫面"},
                {"type": "image", "originalContentUrl": f"{base}/images/{token}/original",
                 "previewImageUrl": f"{base}/images/{token}/preview"},
            ], access_token)
            continue
        parts = command.split()
        if not parts or parts[0] not in ("/啟動", "/重啟", "/停止"):
            continue
        if len(parts) != 2:
            message = f"請輸入 {parts[0]} 裝置名稱，例如 {parts[0]} A8+。"
        elif not _control_authorized(source):
            message = "控制指令只能在已綁定通知的群組使用；私人聊天需設定 LINE_CONTROL_USER_ID。"
        else:
            try:
                devices = _dashboard_json("/devices")["devices"]
                matches = [device for device in devices if device["nickname"].casefold() == parts[1].casefold()
                           or device["serial"].casefold() == parts[1].casefold()]
                if len(matches) != 1:
                    names = "、".join(device["nickname"] for device in devices) or "無"
                    message = f"找不到唯一的已連線裝置「{parts[1]}」。目前可用：{names}。"
                else:
                    device = matches[0]
                    if parts[0] in ("/啟動", "/重啟"):
                        message = _restart_device(device, force_restart=parts[0] == "/重啟")
                    else:
                        result = _dashboard_post(f"/devices/{device['serial']}/stop")
                        message = (f"已通知 {device['nickname']} 停止每日排程。" if result.get("stopping") else
                                   f"{device['nickname']} 已停止刷取。")
            except urllib.error.HTTPError as exc:
                message = _dashboard_error(exc)
            except (OSError, KeyError, ValueError, urllib.error.URLError):
                message = "目前無法控制本機工作台，請確認 4001 服務仍在執行。"
        _reply(reply_token, message, access_token)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("line_webhook:app", host="127.0.0.1", port=4002)
