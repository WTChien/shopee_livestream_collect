"""
main.py — FastAPI backend for the Shopee Live Automation Control Panel.

Run:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
Then open http://localhost:8000 in your browser.
"""

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import cv2

from bot_logic import ShopeeBot, close_shopee, get_screenshot, open_shopee_live
from foreground_app import foreground_app
from claim_store import confirm_claim, daily_count, delete_claim, record_claim, summary
from daily_run import read_status, request_stop
from daily_schedule import get_schedule, install_task, remove_task, save_schedule
from exception_screens import delete_profile, list_profiles, match_profile, save_profile
from reward_vision import blue_card_present, find_checkin_popup_close, find_home_live_entry, find_live_tab, inspect_reward, inspect_success, live_tab_selected, load_region, save_manual_region
from settings_store import load_settings, save_settings
from settings_store import get_daily_limit
from swipe_settings import clear_swipe, load_swipe, save_swipe
from quiet_hours import quiet_window_end

# --------------------------------------------------------------------------- #
#  App setup
# --------------------------------------------------------------------------- #

app = FastAPI(title="Shopee Live Automation Panel")

# Serve index.html at root and any other static assets from the current folder.
BASE_DIR = Path(__file__).parent
DEVICES_FILE = BASE_DIR / "devices.json"

# Mount /assets for OpenCV template images, etc.
assets_dir = BASE_DIR / "assets"
assets_dir.mkdir(exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

# --------------------------------------------------------------------------- #
#  In-memory bot registry  {serial: ShopeeBot}
# --------------------------------------------------------------------------- #

_bots: Dict[str, ShopeeBot] = {}
_control_lock = threading.Lock()
_quiet_guard_stop = threading.Event()
_quiet_guard_thread: threading.Thread | None = None

# --------------------------------------------------------------------------- #
#  Persistence helpers
# --------------------------------------------------------------------------- #

def _load_nicknames() -> Dict[str, str]:
    """Return {serial: nickname} from devices.json."""
    if DEVICES_FILE.exists():
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("devices", {})
    return {}


def _save_nicknames(mapping: Dict[str, str]) -> None:
    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        json.dump({"devices": mapping}, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
#  ADB helpers
# --------------------------------------------------------------------------- #

def _adb_devices() -> list[str]:
    """Return a list of connected ADB serial IDs (excludes 'offline')."""
    try:
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    serials = []
    for line in result.stdout.splitlines()[1:]:  # skip header
        line = line.strip()
        if line and not line.startswith("*"):
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                serials.append(parts[0])
    return serials


# --------------------------------------------------------------------------- #
#  Request / Response models
# --------------------------------------------------------------------------- #

class RenameRequest(BaseModel):
    serial: str
    nickname: str


class ToggleRequest(BaseModel):
    serial: str


class StartRequest(BaseModel):
    resume_live: bool = False
    force_restart: bool = False


class SettingsRequest(BaseModel):
    min_reward: str | None = None
    missing_card_limit: int | None = None
    daily_limit: int | None = None
    read_interval_seconds: int | None = None
    live_refresh_minutes: int | None = None
    limit_streak: int | None = None
    success_capture_seconds: int | None = None
    quiet_enabled: bool | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None


class ScheduleRequest(BaseModel):
    serial: str
    enabled: bool
    time: str = "08:00"
    completion_mode: str = "idle"
    idle_minutes: int = 30
    max_runtime_minutes: int = 240


class RegionRequest(BaseModel):
    screen_size: list[int]
    reward_box: list[int]
    coin_center: list[int]
    buttons: dict[str, list[int]] = {}
    timer_box: list[int] | None = None
    success_box: list[int] | None = None
    red_box: list[int] | None = None
    blue_box: list[int] | None = None


class ExceptionRequest(BaseModel):
    name: str
    screen_size: list[int]
    boxes: list[list[int]]
    templates: list[str]
    action: str
    action_point: list[int] | None = None


class SwipeRequest(BaseModel):
    screen_size: list[int]
    start: list[int]
    end: list[int]
    duration_ms: int


class ManualClaimRequest(BaseModel):
    serial: str
    amount: str
    day: str | None = None


def _connected(serial: str) -> None:
    if serial not in _adb_devices():
        raise HTTPException(status_code=404, detail="手機未透過 ADB 連線")


def _enforce_quiet_window() -> bool:
    if quiet_window_end() is None:
        return False
    for serial in _adb_devices():
        close_shopee(serial)
    return True


def _quiet_guard_loop() -> None:
    last_close = float("-inf")
    while not _quiet_guard_stop.is_set():
        now = time.monotonic()
        if quiet_window_end() is not None and now - last_close >= 60:
            _enforce_quiet_window()
            last_close = now
        _quiet_guard_stop.wait(10)


@app.on_event("startup")
def start_quiet_guard() -> None:
    global _quiet_guard_thread
    if _quiet_guard_thread is None or not _quiet_guard_thread.is_alive():
        _quiet_guard_stop.clear()
        _quiet_guard_thread = threading.Thread(target=_quiet_guard_loop, name="quiet-guard", daemon=True)
        _quiet_guard_thread.start()


@app.on_event("shutdown")
def stop_quiet_guard() -> None:
    _quiet_guard_stop.set()


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #

@app.get("/", include_in_schema=False)
def serve_dashboard():
    """Serve the HTML control panel."""
    html_path = BASE_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(str(html_path))


@app.get("/health", include_in_schema=False)
def health_check():
    """Basic health endpoint for frontend connectivity checks."""
    return {"ok": True, "status": "healthy"}


@app.get("/devices")
def get_devices():
    """
    Return all currently connected ADB devices merged with saved nicknames
    and live bot status.
    """
    connected_serials = _adb_devices()
    nicknames = _load_nicknames()
    stats = summary()
    amounts = {item["serial"]: item["amount"] for item in stats["per_device"]}
    pending_counts = {item["serial"]: item["count"] for item in stats["pending_by_device"]}
    quiet_end = quiet_window_end()

    devices = []
    for serial in connected_serials:
        bot = _bots.get(serial)
        scheduled = read_status(serial)
        external_running = bool(scheduled and scheduled.get("state") == "running")
        armed = (bot.running if bot else False) or external_running
        paused_for_quiet = bool(armed and quiet_end)
        retrying = bool(bot and bot.running and getattr(bot, "retrying", False) and not paused_for_quiet)
        devices.append(
            {
                "serial":          serial,
                "nickname":        nicknames.get(serial, serial),
                "connected":       True,
                "running":         bool(armed and not paused_for_quiet and not retrying),
                "retrying":        retrying,
                "paused_for_quiet": paused_for_quiet,
                "resume_at":       quiet_end.strftime("%H:%M") if paused_for_quiet else None,
                "scheduled_running": external_running,
                "status":          (bot.status_message if bot else
                                    "有待處理點擊紀錄；啟動時會按卡片金額計入所得" if pending_counts.get(serial, 0) else
                                    scheduled.get("message", "idle") if external_running else "idle"),
                "coins_collected": bot._coins_collected if bot else 0,
                "streams_visited": bot._streams_visited if bot else 0,
                "today_reserved": daily_count(serial),
                "today_amount": amounts.get(serial, 0.0),
                "pending_count": pending_counts.get(serial, 0),
                "foreground": foreground_app(serial),
                "region": load_region(serial),
            }
        )

    return {"devices": devices}


@app.get("/device-status")
def get_device_status():
    """Include saved phones that are currently disconnected for LINE status."""
    connected = get_devices()["devices"]
    seen = {device["serial"] for device in connected}
    for serial, nickname in _load_nicknames().items():
        if serial not in seen:
            connected.append({"serial": serial, "nickname": nickname, "connected": False,
                              "running": False, "status": "未連線", "foreground": None})
    return {"devices": connected}


@app.get("/devices/{serial}/screenshot")
def device_screenshot(serial: str):
    _connected(serial)
    screen = get_screenshot(serial)
    if screen is None:
        raise HTTPException(status_code=503, detail="無法擷取手機畫面")
    ok, encoded = cv2.imencode(".png", screen)
    if not ok:
        raise HTTPException(status_code=503, detail="無法編碼手機截圖")
    return Response(encoded.tobytes(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/devices/{serial}/region")
def device_region(serial: str):
    _connected(serial)
    return {"region": load_region(serial)}


@app.get("/devices/{serial}/exceptions")
def device_exceptions(serial: str):
    _connected(serial)
    return {"profiles": list_profiles(serial)}


@app.get("/devices/{serial}/swipe")
def device_swipe(serial: str):
    _connected(serial)
    return {"swipe": load_swipe(serial)}


@app.post("/devices/{serial}/swipe")
def update_device_swipe(serial: str, req: SwipeRequest):
    _connected(serial)
    try:
        swipe = save_swipe(serial, req.screen_size, req.start, req.end, req.duration_ms)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "swipe": swipe}


@app.delete("/devices/{serial}/swipe")
def remove_device_swipe(serial: str):
    _connected(serial)
    clear_swipe(serial)
    return {"ok": True}


@app.post("/devices/{serial}/exceptions")
def update_device_exception(serial: str, req: ExceptionRequest):
    _connected(serial)
    try:
        profile = save_profile(serial, req.model_dump(exclude={"templates"}), req.templates)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "profile": profile}


@app.delete("/devices/{serial}/exceptions/{profile_id}")
def remove_device_exception(serial: str, profile_id: str):
    _connected(serial)
    if not delete_profile(serial, profile_id):
        raise HTTPException(status_code=404, detail="找不到例外畫面")
    return {"ok": True}


@app.post("/devices/{serial}/exceptions/inspect")
def inspect_device_exception(serial: str):
    _connected(serial)
    screen = get_screenshot(serial)
    if screen is None:
        raise HTTPException(status_code=503, detail="無法擷取手機畫面")
    return {"match": match_profile(screen, serial)}


@app.post("/devices/{serial}/region")
def update_device_region(serial: str, req: RegionRequest):
    _connected(serial)
    try:
        if len(req.screen_size) != 2:
            raise ValueError("螢幕尺寸不正確")
        region = save_manual_region(
            serial, req.screen_size[0], req.screen_size[1],
            req.reward_box, req.coin_center, req.buttons,
            req.timer_box, req.success_box,
            req.red_box, req.blue_box,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "region": region}


@app.post("/devices/{serial}/inspect")
def inspect_device(serial: str):
    _connected(serial)
    screen = get_screenshot(serial)
    if screen is None:
        raise HTTPException(status_code=503, detail="無法擷取手機畫面")
    found = inspect_reward(screen, serial)
    success = inspect_success(screen, serial)
    return {
        "state": found.state, "reward_box": found.reward_box,
        "coin_center": found.coin_center, "collect_point": found.collect_point,
        "amount": found.amount, "text": found.text,
        "remaining_seconds": found.remaining_seconds,
        "red_remaining_seconds": found.red_remaining_seconds,
        "blue_box": found.reward_box,
        "blue_card_present": blue_card_present(screen, serial, found),
        "success_amount": success.amount,
        "success_text": success.text,
        "live_tab": find_live_tab(screen, serial) if found.state == "absent" else None,
        "live_tab_selected": live_tab_selected(screen, serial),
        "home_live_entry": find_home_live_entry(screen, serial) if found.state == "absent" else None,
        "checkin_popup_close": find_checkin_popup_close(screen, serial),
    }


@app.get("/devices/{serial}/live-card")
def device_live_card(serial: str):
    _connected(serial)
    screen = get_screenshot(serial)
    if screen is None:
        raise HTTPException(status_code=503, detail="無法擷取手機畫面")
    observation = inspect_reward(screen, serial)
    return {"blue_card_present": blue_card_present(screen, serial, observation)}


@app.get("/stats")
def get_stats(serial: str | None = None):
    result = summary(serial=serial)
    nicknames = _load_nicknames()
    for item in result["device_totals"]:
        item["nickname"] = nicknames.get(item["serial"], item["serial"])
    return result


@app.get("/settings")
def get_settings():
    settings = load_settings()
    return {"min_reward": f"{settings['min_reward']:.2f}",
            "missing_card_limit": settings["missing_card_limit"],
            "daily_limit": settings["daily_limit"],
            "read_interval_seconds": settings["read_interval_seconds"],
            "live_refresh_minutes": settings["live_refresh_minutes"],
            "limit_streak": settings["limit_streak"],
            "success_capture_seconds": settings["success_capture_seconds"],
            "quiet_enabled": settings["quiet_enabled"],
            "quiet_start": settings["quiet_start"],
            "quiet_end": settings["quiet_end"]}


@app.post("/settings")
def update_settings(req: SettingsRequest):
    try:
        if all(value is None for value in (req.min_reward, req.missing_card_limit,
                                            req.daily_limit, req.read_interval_seconds,
                                            req.live_refresh_minutes, req.limit_streak,
                                            req.success_capture_seconds,
                                            req.quiet_enabled, req.quiet_start, req.quiet_end)):
            raise ValueError("請提供要儲存的設定")
        settings = save_settings(min_reward=req.min_reward,
                                 missing_card_limit=req.missing_card_limit,
                                 daily_limit=req.daily_limit,
                                 read_interval_seconds=req.read_interval_seconds,
                                 live_refresh_minutes=req.live_refresh_minutes,
                                 limit_streak=req.limit_streak,
                                 success_capture_seconds=req.success_capture_seconds,
                                 quiet_enabled=req.quiet_enabled,
                                 quiet_start=req.quiet_start, quiet_end=req.quiet_end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"min_reward": f"{settings['min_reward']:.2f}",
            "missing_card_limit": settings["missing_card_limit"],
            "daily_limit": settings["daily_limit"],
            "read_interval_seconds": settings["read_interval_seconds"],
            "live_refresh_minutes": settings["live_refresh_minutes"],
            "limit_streak": settings["limit_streak"],
            "success_capture_seconds": settings["success_capture_seconds"],
            "quiet_enabled": settings["quiet_enabled"],
            "quiet_start": settings["quiet_start"],
            "quiet_end": settings["quiet_end"]}


@app.post("/claims/manual")
def add_manual_claim(req: ManualClaimRequest):
    try:
        claim_id = record_claim(req.serial, req.amount, source="manual", day=req.day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": claim_id}


@app.post("/claims/{claim_id}/confirm")
def confirm_pending_claim(claim_id: int):
    if not confirm_claim(claim_id):
        raise HTTPException(status_code=404, detail="找不到待確認紀錄")
    return {"ok": True}


@app.delete("/claims/{claim_id}")
def remove_claim(claim_id: int):
    if not delete_claim(claim_id):
        raise HTTPException(status_code=404, detail="找不到紀錄")
    return {"ok": True}


@app.post("/rename")
def rename_device(req: RenameRequest):
    """Update the custom nickname for a device in devices.json."""
    if not req.serial or not req.nickname.strip():
        raise HTTPException(status_code=400, detail="serial and nickname are required")

    nicknames = _load_nicknames()
    nicknames[req.serial] = req.nickname.strip()
    _save_nicknames(nicknames)

    # Update running bot's nickname if it exists
    if req.serial in _bots:
        _bots[req.serial].nickname = req.nickname.strip()

    return {"ok": True, "serial": req.serial, "nickname": req.nickname.strip()}


@app.post("/toggle")
def toggle_device(req: ToggleRequest):
    """Start or stop the automation bot for the given device serial."""
    connected = _adb_devices()
    if req.serial not in connected:
        raise HTTPException(status_code=404, detail=f"Device {req.serial} not connected")

    nicknames = _load_nicknames()
    scheduled = read_status(req.serial)
    if scheduled and scheduled.get("state") == "running":
        raise HTTPException(status_code=409, detail="這台手機的每日排程正在運行")

    if req.serial in _bots and _bots[req.serial].running:
        # Stop the bot
        _bots[req.serial].stop()
        return {"ok": True, "serial": req.serial, "running": False}
    else:
        # Start (or restart) the bot
        config = get_schedule(req.serial)
        bot = ShopeeBot(
            serial=req.serial,
            nickname=nicknames.get(req.serial, req.serial),
            idle_timeout_seconds=(config["idle_minutes"] * 60
                                  if config["completion_mode"] == "idle" else None),
            max_runtime_seconds=(config["max_runtime_minutes"] * 60
                                 if config["completion_mode"] == "idle" else None),
            launch_on_start=True,
            guard_foreground=True,
        )
        _bots[req.serial] = bot
        try:
            bot.start()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "serial": req.serial, "running": True}


@app.post("/devices/{serial}/start")
def start_device(serial: str, req: StartRequest | None = None):
    """Shared dashboard and LINE start flow, with live-card resume when possible."""
    with _control_lock:
        _connected(serial)
        if quiet_window_end() is not None:
            raise HTTPException(status_code=409, detail="目前為停止採集時段")
        scheduled = read_status(serial)
        if scheduled and scheduled.get("state") == "running":
            raise HTTPException(status_code=409, detail="這台手機的每日排程正在運行")
        bot = _bots.get(serial)
        was_running = bool(bot and bot.running)
        force_restart = bool(req and req.force_restart)
        if force_restart and not was_running:
            raise HTTPException(status_code=409, detail="這台手機目前未刷取，請使用啟動")
        if req and req.resume_live and force_restart:
            raise HTTPException(status_code=400, detail="不能同時接續直播與強制重啟")
        limit = get_daily_limit()
        if daily_count(serial) >= limit:
            raise HTTPException(status_code=409, detail=f"今日已達 {limit} 次上限")
        current = foreground_app(serial)
        if current["kind"] == "unknown":
            raise HTTPException(status_code=503, detail="無法確認手機目前使用的 App，未啟動")
        if current["kind"] == "other":
            package = current["package"]
            label = {"com.nianticlabs.pikmin": "皮克敏 Bloom",
                     "com.nianticlabs.pokemongo": "Pokémon GO"}.get(package, package)
            raise HTTPException(status_code=409, detail=f"手機正在使用 {label}（{package}），未啟動蝦皮")
        if req and req.resume_live and current["kind"] != "shopee":
            raise HTTPException(status_code=409, detail="手機已離開蝦皮，未接續直播")
        resume_live = False
        if current["kind"] == "shopee" and not force_restart:
            screen = get_screenshot(serial)
            resume_live = bool(screen is not None and
                               blue_card_present(screen, serial, inspect_reward(screen, serial)))
        if req and req.resume_live and not resume_live:
            raise HTTPException(status_code=409, detail="已無法辨識直播藍色卡片，請重新啟動")
        if resume_live and was_running:
            return {"ok": True, "serial": serial, "running": True,
                    "already_running": True, "resumed_live": True, "mode": "continued"}
        if not resume_live:
            if was_running:
                bot.stop()
                if bot._thread and bot._thread.is_alive():
                    raise HTTPException(status_code=409, detail="舊流程尚未完全停止，請稍後重試")
            if not close_shopee(serial):
                raise HTTPException(status_code=503, detail="無法關閉蝦皮，未啟動")
            after_close = foreground_app(serial)
            if after_close["kind"] != "home":
                raise HTTPException(status_code=409, detail="手機未回到主畫面，未啟動蝦皮")
        config = get_schedule(serial)
        bot = ShopeeBot(
            serial=serial,
            nickname=_load_nicknames().get(serial, serial),
            idle_timeout_seconds=(config["idle_minutes"] * 60 if config["completion_mode"] == "idle" else None),
            max_runtime_seconds=(config["max_runtime_minutes"] * 60 if config["completion_mode"] == "idle" else None),
            launch_on_start=not resume_live,
            guard_foreground=True,
        )
        try:
            bot.start()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _bots[serial] = bot
        return {"ok": True, "serial": serial, "running": True,
                "already_running": False, "resumed_live": resume_live,
                "mode": "resumed" if resume_live else "restarted" if was_running else "started"}


@app.post("/devices/{serial}/stop")
def stop_device(serial: str):
    _connected(serial)
    scheduled = read_status(serial)
    if scheduled and scheduled.get("state") == "running":
        request_stop(serial)
        return {"ok": True, "stopping": True}
    bot = _bots.get(serial)
    if not bot or not bot.running:
        return {"ok": True, "stopping": False}
    bot.stop()
    if foreground_app(serial)["kind"] == "shopee":
        close_shopee(serial)
    return {"ok": True, "stopping": False}


@app.post("/devices/{serial}/open-shopee")
def open_device_shopee(serial: str):
    _connected(serial)
    if quiet_window_end() is not None:
        raise HTTPException(status_code=409, detail="目前為停止採集時段，暫不開啟蝦皮")
    bot = _bots.get(serial)
    if bot and bot.running:
        raise HTTPException(status_code=409, detail="請先停止這台手機的自動流程再測試開啟")
    scheduled = read_status(serial)
    if scheduled and scheduled.get("state") == "running":
        raise HTTPException(status_code=409, detail="每日排程執行中，請先停止")
    if not open_shopee_live(serial):
        raise HTTPException(status_code=503, detail="蝦皮已開啟，但未能確認進入「直播」分頁；請標記首頁直播入口")
    return {"ok": True, "entered_live": True}


@app.post("/devices/{serial}/close-shopee")
def close_device_shopee(serial: str):
    _connected(serial)
    scheduled = read_status(serial)
    if scheduled and scheduled.get("state") == "running":
        raise HTTPException(status_code=409, detail="每日排程執行中，請先停止")
    bot = _bots.get(serial)
    if bot and bot.running:
        bot.stop()
    if not close_shopee(serial):
        raise HTTPException(status_code=503, detail="ADB 關閉蝦皮失敗")
    return {"ok": True}


@app.get("/schedule/{serial}")
def device_schedule(serial: str):
    _connected(serial)
    return {"schedule": get_schedule(serial), "run": read_status(serial)}


@app.post("/schedule")
def update_schedule(req: ScheduleRequest):
    _connected(req.serial)
    previous = get_schedule(req.serial)
    try:
        saved = save_schedule(
            req.serial, enabled=req.enabled, time=req.time,
            completion_mode=req.completion_mode,
            idle_minutes=req.idle_minutes,
            max_runtime_minutes=req.max_runtime_minutes,
        )
        if req.enabled:
            install_task(req.serial, saved["time"])
        else:
            remove_task(req.serial)
    except (ValueError, RuntimeError, OSError) as exc:
        save_schedule(req.serial, **{key: previous[key] for key in ("enabled", "time", "completion_mode", "idle_minutes", "max_runtime_minutes")})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "schedule": saved}


@app.post("/schedule/{serial}/stop")
def stop_scheduled_run(serial: str):
    _connected(serial)
    current = read_status(serial)
    if not current or current.get("state") != "running":
        raise HTTPException(status_code=409, detail="這台手機的每日排程沒有執行中")
    request_stop(serial)
    return {"ok": True}


@app.get("/logs/{serial}")
def get_logs(serial: str):
    """Return the last N log entries for a specific device."""
    bot = _bots.get(serial)
    if bot is None:
        return {"serial": serial, "logs": []}
    return {"serial": serial, "logs": list(bot.logs)}


@app.delete("/logs/{serial}")
def clear_logs(serial: str):
    """Clear in-memory log history for a specific device."""
    bot = _bots.get(serial)
    if bot is None:
        return {"ok": True, "serial": serial, "cleared": 0}

    cleared = len(bot.logs)
    bot.logs.clear()
    return {"ok": True, "serial": serial, "cleared": cleared}
