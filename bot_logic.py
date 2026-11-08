"""
bot_logic.py — Core ADB + OpenCV automation logic for each device.

State machine per device:
  - reward card collect: 圓形蝦幣定位 + EasyOCR 確認領取 → 點擊
  - reward card waiting: 倒數中 → 等待
  - reward card limit  : 顯示限制 → 滑到下一間直播
  - reward card unknown: 文字不明 → 不點擊，等待下一張畫面
  - collect modal      : 領取後的 Modal → 關閉
  - p2_limit           : p2 樣式已達上限 → 滑到下一間直播
  連續 DAILY_LIMIT_THRESHOLD 間都出現 limit → 今日上限已達
  → 關閉 Shopee → 寫入 CSV → 停止此裝置 bot
"""

import csv
import subprocess
import threading
import time
import logging
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from claim_store import TAIPEI, cancel_claim, confirm_all_pending_estimates, confirm_claim, daily_count, reserve_claim, unverified_today
from device_lock import acquire_device_lock, release_device_lock
from exception_screens import match_profile
from foreground_app import foreground_app
from settings_store import (
    get_daily_limit, get_limit_streak, get_live_refresh_minutes,
    get_min_reward, get_missing_card_limit, get_read_interval,
    get_success_capture_seconds,
)
from swipe_settings import load_swipe
from quiet_hours import quiet_window_end
from line_notifications import notify_device_crashed, notify_device_paused, notify_device_stopped
from reward_vision import blue_card_present, button_point, find_checkin_popup_close, find_home_live_entry, find_live_tab, find_startup_promo_close, inspect_reward, inspect_success, live_tab_selected

try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:  # noqa: BLE001
    RapidOCR = None

logger = logging.getLogger(__name__)

BASE_DIR    = Path(__file__).parent
ASSETS_DIR  = BASE_DIR / "assets"
CSV_FILE    = BASE_DIR / "collect_log.csv"

# ---------- 可調整參數 ---------------------------------------------------- #

# 依地區修改：tw / th / id / vn / ph / my / sg
SHOPEE_PACKAGE = "com.shopee.tw"

# 滑動參數 (往上滑 = 移到下一間直播；座標按截圖尺寸計算)
SWIPE_DURATION   = 500   # ms
DEFAULT_SWIPE_RATIOS = (.72, .30)
DEVICE_SWIPE_RATIOS = {
    "5200f803b437b41f": (.82, .18),  # A8+: a longer gesture is needed to change live rooms.
}
DEVICE_SWIPE_DURATION_MS = {
    "5200f803b437b41f": 250,  # A8+: faster fling over the same distance.
}

# 直播列表頁：下拉更新（由上往下滑）
LIST_REFRESH_X = 540
LIST_REFRESH_FROM_Y = 520
LIST_REFRESH_TO_Y = 1380
LIST_REFRESH_DURATION = 380

# 連續幾間 limit → 視為今日上限
DAILY_LIMIT_THRESHOLD = 5

# p1_collect_model 的叉叉位置 (相對螢幕中下方，可依實際截圖調整)
CLOSE_MODEL_X = 540
CLOSE_MODEL_Y = 1750
MODEL_CLOSE_OFFSET_Y_RATIO = 0.18
MODEL_CLOSE_MIN_OFFSET_Y = 180
MODEL_CLOSE_MAX_OFFSET_Y = 420
MODEL_CLOSE_MIN_Y_RATIO = 0.72
MODEL_CLOSE_SECOND_TAP_DY = 36

# 模板偵測配置: roi=(x1,y1,x2,y2) 使用相對比例座標
# 你這組畫面的關鍵在右上角獎勵區塊，先聚焦該區域可大幅提升穩定度。
REWARD_TOP_RIGHT_ROI = (0.66, 0.06, 0.98, 0.40)

TEMPLATE_CONFIG = {
    "p1_collect.png": {
        "threshold": 0.46,
        "roi": REWARD_TOP_RIGHT_ROI,
    },
    "p1_ing.png": {
        "threshold": 0.60,
        "roi": REWARD_TOP_RIGHT_ROI,
    },
    "p2_collect.png": {
        "threshold": 0.56,
        "roi": REWARD_TOP_RIGHT_ROI,
    },
    "p2_ing.png": {
        "threshold": 0.56,
        "roi": REWARD_TOP_RIGHT_ROI,
    },
    "p1_collect_model.png": {"threshold": 0.68},
    "p2_collect_model.png": {"threshold": 0.68},
    "p1_limit_1.png": {
        "threshold": 0.62,
        "roi": REWARD_TOP_RIGHT_ROI,
    },
    "p1_limit_2.png": {
        "threshold": 0.64,
        "roi": REWARD_TOP_RIGHT_ROI,
    },
    "switch_x.png": {
        "threshold": 0.66,
        "roi": (0.88, 0.00, 1.00, 0.20),
    },
    "list.png": {
        "threshold": 0.62,
    },
    "p2_limit.png": {
        "threshold": 0.62,
        "roi": REWARD_TOP_RIGHT_ROI,
    },
}

# p1_collect / p1_ing 彼此高度相似。
# collect: 只要分數接近就偏向當成可領取；ing: 需明顯高於 collect 才判定計時中。
POLL_INTERVAL_SECONDS = 5.0
COUNTDOWN_RECHECK_SECONDS = 5
READ_RETRY_SECONDS = 5
SUCCESS_CAPTURE_DELAYS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.0)
LIVE_TAB_REFRESH_INTERVAL_SECONDS = 1800

if RapidOCR is not None:
    try:
        OCR_ENGINE = RapidOCR()
    except Exception:  # noqa: BLE001
        OCR_ENGINE = None
else:
    OCR_ENGINE = None

OCR_ENABLED = OCR_ENGINE is not None

# --------------------------------------------------------------------------- #
#  低階 ADB 工具函數
# --------------------------------------------------------------------------- #

def _adb(serial: str, *args: str) -> subprocess.CompletedProcess:
    cmd = ["adb", "-s", serial, *args]
    # Keep ADB calls binary-safe by default to avoid decode errors on non-text output.
    return subprocess.run(cmd, capture_output=True, timeout=20)


def get_screenshot(serial: str) -> "np.ndarray | None":
    # 必須用 bytes 模式（不能 text=True），screencap -p 輸出二進位 PNG
    cmd = ["adb", "-s", serial, "exec-out", "screencap", "-p"]
    result = subprocess.run(cmd, capture_output=True, timeout=20)
    if result.returncode != 0 or not result.stdout:
        logger.warning("[%s] screencap failed (rc=%s)", serial, result.returncode)
        return None
    data = np.frombuffer(result.stdout, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        logger.warning("[%s] imdecode failed (got %d bytes)", serial, len(result.stdout))
    return img


def tap(serial: str, x: int, y: int) -> bool:
    result = _adb(serial, "shell", "input", "tap", str(x), str(y))
    if result.returncode != 0:
        logger.warning("[%s] tap failed at (%s,%s), rc=%s", serial, x, y, result.returncode)
    return result.returncode == 0


def _get_model_close_tap_point(
    screen: "np.ndarray",
    model_center: "tuple[int, int] | None",
) -> "tuple[int, int]":
    """Tap point for the close-X below collect modal; fallback to configured fixed point."""
    h, w = screen.shape[:2]
    if model_center is None:
        x = max(0, min(w - 1, CLOSE_MODEL_X))
        y = max(0, min(h - 1, CLOSE_MODEL_Y))
        return x, y

    offset = int(h * MODEL_CLOSE_OFFSET_Y_RATIO)
    offset = max(MODEL_CLOSE_MIN_OFFSET_Y, min(MODEL_CLOSE_MAX_OFFSET_Y, offset))
    # 白色叉叉通常位於畫面水平中心，且在 modal 下方較低處。
    x = w // 2
    y = int(model_center[1] + offset)
    y = max(int(h * MODEL_CLOSE_MIN_Y_RATIO), y)
    y = max(0, min(h - 1, y))
    return x, y


def swipe_up(serial: str, width: int, height: int) -> bool:
    """手指從畫面下方往上滑，切換到下一間直播。"""
    saved = load_swipe(serial)
    if saved:
        old_width, old_height = saved["screen_size"]
        start_x = round(saved["start"][0] * width / old_width)
        start_y = round(saved["start"][1] * height / old_height)
        end_x = round(saved["end"][0] * width / old_width)
        end_y = round(saved["end"][1] * height / old_height)
        duration = saved["duration_ms"]
    else:
        x = width // 2
        start_ratio, end_ratio = DEVICE_SWIPE_RATIOS.get(serial, DEFAULT_SWIPE_RATIOS)
        start_x, start_y = x, int(height * start_ratio)
        end_x, end_y = x, int(height * end_ratio)
        duration = DEVICE_SWIPE_DURATION_MS.get(serial, SWIPE_DURATION)
    result = _adb(
        serial, "shell", "input", "swipe",
        str(start_x), str(start_y),
        str(end_x), str(end_y),
        str(duration),
    )
    return result.returncode == 0


def format_waiting_status(reward, read_interval: int) -> str:
    """Show both the blue-card countdown and its currently readable amount."""
    remaining = (f"{reward.remaining_seconds // 60:02d}:{reward.remaining_seconds % 60:02d}"
                 if reward.remaining_seconds is not None else "OCR 未讀到")
    amount = (f"；可拿 {reward.amount} 蝦幣" if reward.amount is not None
              else "；可拿金額未讀到")
    red_note = (f"；紅色倒數 {reward.red_remaining_seconds // 60:02d}:{reward.red_remaining_seconds % 60:02d}（不影響領取）"
                if reward.red_remaining_seconds is not None else "")
    return f"⏳ 藍色蝦幣倒數剩餘 {remaining}{amount}{red_note}，{read_interval} 秒後重讀"


def refresh_live_list(serial: str) -> None:
    """直播列表頁下拉刷新。"""
    _adb(
        serial,
        "shell", "input", "swipe",
        str(LIST_REFRESH_X), str(LIST_REFRESH_FROM_Y),
        str(LIST_REFRESH_X), str(LIST_REFRESH_TO_Y),
        str(LIST_REFRESH_DURATION),
    )


def close_shopee(serial: str) -> bool:
    """關閉 Shopee 並回到手機首頁。"""
    current = foreground_app(serial)
    result = _adb(serial, "shell", "am", "force-stop", SHOPEE_PACKAGE)
    if current["kind"] == "shopee":
        _adb(serial, "shell", "input", "keyevent", "3")
    return result.returncode == 0


def open_shopee_live(serial: str, *, guard_foreground: bool = False) -> bool:
    """Launch Shopee and enter its live section, using OCR or a saved home button."""
    if quiet_window_end() is not None:
        close_shopee(serial)
        return False
    result = _adb(serial, "shell", "monkey", "-p", SHOPEE_PACKAGE,
                  "-c", "android.intent.category.LAUNCHER", "1")
    if result.returncode != 0 or b"No activities found" in result.stdout:
        return False
    time.sleep(5)
    if guard_foreground and foreground_app(serial)["kind"] != "shopee":
        return False
    if quiet_window_end() is not None:
        close_shopee(serial)
        return False
    # On some devices, launching Shopee turns auto-rotate back on. Restore
    # portrait lock after startup so saved screen coordinates stay valid.
    rotation = _adb(serial, "shell", "settings", "put", "system", "accelerometer_rotation", "0")
    if rotation.returncode != 0:
        logger.warning("[%s] Failed to lock portrait orientation after Shopee launch", serial)
    entry_tapped = False
    for attempt in range(5):
        if guard_foreground and foreground_app(serial)["kind"] != "shopee":
            return False
        if quiet_window_end() is not None:
            close_shopee(serial)
            return False
        screen = get_screenshot(serial)
        if screen is None:
            time.sleep(2)
            continue
        if find_template(screen, "list.png") or live_tab_selected(screen, serial):
            return True
        live_point = find_live_tab(screen, serial, use_manual=False)
        if live_point and tap(serial, *live_point):
            time.sleep(2)
            continue
        promo_close = find_startup_promo_close(screen)
        if promo_close and tap(serial, *promo_close):
            time.sleep(1)
            continue
        if not entry_tapped:
            entry = find_home_live_entry(screen, serial)
            if entry and tap(serial, *entry):
                entry_tapped = True
                time.sleep(3)
                continue
        if attempt == 2 and not entry_tapped:
            _adb(serial, "shell", "input", "keyevent", "4")
        time.sleep(2)
    return False


def go_home_via_alt_h(serial: str) -> None:
    """嘗試用 Alt+H 回首頁；若裝置不支援 keycombination，退回 HOME key。"""
    # KEYCODE_ALT_LEFT=57, KEYCODE_H=36
    result = _adb(serial, "shell", "input", "keycombination", "57", "36")
    if result.returncode != 0:
        _adb(serial, "shell", "input", "keyevent", "3")


def _crop_with_ratio(img: "np.ndarray", roi: tuple[float, float, float, float]):
    """Crop image by normalized ratio ROI and return (cropped, x_offset, y_offset)."""
    h, w = img.shape[:2]
    x1 = max(0, min(w - 1, int(w * roi[0])))
    y1 = max(0, min(h - 1, int(h * roi[1])))
    x2 = max(x1 + 1, min(w, int(w * roi[2])))
    y2 = max(y1 + 1, min(h, int(h * roi[3])))
    return img[y1:y2, x1:x2], x1, y1


def find_template(
    screen: "np.ndarray",
    template_name: str,
    threshold: float = 0.80,
    return_score: bool = False,
) -> "tuple[int, int] | tuple[tuple[int, int] | None, float] | None":
    """
    OpenCV 模板比對。
    回傳 (cx, cy) 若信心值 >= threshold，否則回傳 None。
    """
    path = ASSETS_DIR / template_name
    if not path.exists():
        logger.debug("Template not found: %s (skip)", template_name)
        return (None, 0.0) if return_score else None

    # cv2.imread cannot reliably open Windows paths containing non-ASCII names.
    tmpl = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if tmpl is None:
        logger.warning("Cannot read template: %s", template_name)
        return (None, 0.0) if return_score else None

    cfg = TEMPLATE_CONFIG.get(template_name, {})
    use_threshold = float(cfg.get("threshold", threshold))
    use_roi = cfg.get("roi")

    src = screen
    src_offset_x = 0
    src_offset_y = 0
    ref = tmpl

    # 針對 ROI 進行比對，避免被直播內容的高度變動干擾。
    if use_roi:
        src, src_offset_x, src_offset_y = _crop_with_ratio(screen, use_roi)
        ref, _, _ = _crop_with_ratio(tmpl, use_roi)

    if src.shape[0] < ref.shape[0] or src.shape[1] < ref.shape[1]:
        return (None, 0.0) if return_score else None

    src_gray = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)
    ref_gray = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)

    res = cv2.matchTemplate(src_gray, ref_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)

    if max_val >= use_threshold:
        h, w = ref.shape[:2]
        cx = src_offset_x + max_loc[0] + w // 2
        cy = src_offset_y + max_loc[1] + h // 2
        return ((cx, cy), float(max_val)) if return_score else (cx, cy)

    return (None, float(max_val)) if return_score else None


import re as _re

_MAX_AMOUNT_RE = _re.compile(r'最多\s*([1-9]\d?(?:\.\d)?)')


def _extract_max_live_amount_pos(screen: "np.ndarray") -> "tuple[float, tuple[int, int] | None, str]":
    """OCR list page and return (max_amount, tap_pos, matched_text)."""
    if not OCR_ENABLED:
        return 0.0, None, ""

    try:
        result, _ = OCR_ENGINE(screen)
    except Exception:  # noqa: BLE001
        return 0.0, None, ""

    best_value = 0.0
    best_pos = None
    best_text = ""

    for item in result or []:
        if len(item) < 2:
            continue
        txt = str(item[1] or "").strip()
        if not txt:
            continue
        compact = txt.replace(" ", "")
        # 排除觀看人數等非蝦幣金額場景。
        if "萬" in compact or "觀看" in compact:
            continue
        if "最多" not in compact:
            continue

        m = _MAX_AMOUNT_RE.search(compact)
        if not m:
            continue

        try:
            value = float(m.group(1))
        except Exception:  # noqa: BLE001
            continue
        if value < 1.0 or value > 10.0:
            continue

        center = _ocr_box_center(item[0])
        if center is None:
            continue
        tap_pos = (int(center[0]), int(center[1]))

        if value > best_value:
            best_value = value
            best_pos = tap_pos
            best_text = txt

    return best_value, best_pos, best_text


def _ocr_box_center(box) -> "tuple[float, float] | None":
    try:
        pts = np.array(box, dtype=float)
    except Exception:  # noqa: BLE001
        return None
    if pts.size < 2:
        return None
    if pts.ndim == 2 and pts.shape[1] >= 2:
        x = float(np.mean(pts[:, 0]))
        y = float(np.mean(pts[:, 1]))
        return x, y
    return None


# --------------------------------------------------------------------------- #
#  CSV 紀錄
# --------------------------------------------------------------------------- #

def _append_csv(row: dict) -> None:
    """將一筆蒐集紀錄寫到 collect_log.csv。"""
    fieldnames = [
        "date", "serial", "nickname",
        "coins_collected", "streams_visited",
        "start_time", "end_time", "duration_seconds",
    ]
    write_header = not CSV_FILE.exists()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    logger.info("CSV saved: %s", row)


# --------------------------------------------------------------------------- #
#  ShopeeBot — 每台手機各一個 instance
# --------------------------------------------------------------------------- #

class ShopeeBot:
    """
    管理單一 Android 裝置的蝦幣蒐集自動化流程。

    蝦幣卡片由 reward_vision.py 定位；列表、領取 Modal 與 p2 畫面
    仍使用 assets/ 中的模板。

    使用方式:
        bot = ShopeeBot(serial="70f5e12", nickname="老媽的小米")
        bot.start()
        ...
        bot.stop()
    """

    def __init__(self, serial: str, nickname: str = "", *,
                 idle_timeout_seconds: int | None = None,
                 max_runtime_seconds: int | None = None,
                 launch_on_start: bool = False,
                 guard_foreground: bool = False):
        self.serial   = serial
        self.nickname = nickname or serial

        self._stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None

        # 對外可讀狀態
        self.running        = False
        self.retrying       = False
        self.status_message = "idle"

        # 本次執行統計
        self._start_time:        "datetime | None" = None
        self._coins_collected    = 0   # 成功領取次數
        self._streams_visited    = 0   # 已跳過的直播間數
        self._consecutive_limit  = 0   # 連續 limit 計數
        self._consecutive_missing_cards = 0
        self._collect_armed = True
        self._skip_current_room = False
        self._last_live_refresh_at = time.monotonic()
        self._idle_timeout_seconds = idle_timeout_seconds
        self._max_runtime_seconds = max_runtime_seconds
        self._launch_on_start = launch_on_start
        self._guard_foreground = guard_foreground
        self._last_success_at: float | None = None
        self._started_monotonic = time.monotonic()
        self._device_lock = None
        self._stop_reason = "手動停止"
        self._skip_initial_quiet_notice = False

        # Log buffer（最多保留 200 筆）
        self.logs: deque = deque(maxlen=200)

    # ---------------------------------------------------------------------- #
    #  公開控制方法
    # ---------------------------------------------------------------------- #

    def start(self) -> None:
        if self.running:
            return
        self._device_lock = acquire_device_lock(self.serial)
        self._reset_stats()
        self._skip_initial_quiet_notice = quiet_window_end() is not None
        self._skip_current_room = unverified_today(self.serial)
        confirm_all_pending_estimates(serial=self.serial)
        self._stop_event.clear()
        self.retrying = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"bot-{self.serial}",
            daemon=True,
        )
        self.running = True
        self._thread.start()
        logger.info("[%s] Bot started.", self.serial)

    def stop(self) -> None:
        if self.running:
            self._stop_reason = "手動停止"
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)
        self.running        = False
        self.retrying       = False
        self.status_message = "stopped"
        logger.info("[%s] Bot stopped.", self.serial)

    def to_dict(self) -> dict:
        return {
            "serial":           self.serial,
            "nickname":         self.nickname,
            "running":          self.running,
            "retrying":         self.retrying,
            "status":           self.status_message,
            "coins_collected":  self._coins_collected,
            "streams_visited":  self._streams_visited,
        }

    # ---------------------------------------------------------------------- #
    #  內部工具
    # ---------------------------------------------------------------------- #

    def _reset_stats(self) -> None:
        self._start_time       = datetime.now()
        self._stop_reason = "手動停止"
        self._coins_collected  = 0
        self._streams_visited  = 0
        self._consecutive_limit = 0
        self._consecutive_missing_cards = 0
        self._collect_armed = True
        self._skip_current_room = False
        self._last_live_refresh_at = time.monotonic()
        self._last_success_at = None
        self._started_monotonic = time.monotonic()
        self.logs.clear()

    def _set_status(self, msg: str, iface: str = "") -> None:
        """更新狀態訊息並寫入 log buffer。
        iface: 'p1' / 'p2' / '' 表示偵測到的介面類型。
        """
        self.status_message = msg
        entry = {
            "ts":    datetime.now().strftime("%H:%M:%S"),
            "iface": iface,
            "msg":   msg,
        }
        self.logs.append(entry)
        logger.info("[%s][%s] %s", self.serial, iface or "-", msg)

    def _verify_claim_success(self):
        """Capture through delayed server responses before OCR blocks on another device."""
        captured = []
        max_seconds = get_success_capture_seconds()
        delays = []
        elapsed = 0.0
        for delay in SUCCESS_CAPTURE_DELAYS:
            next_delay = min(delay, max_seconds - elapsed)
            if next_delay <= 0:
                break
            delays.append(next_delay)
            elapsed += next_delay
        while elapsed < max_seconds:
            delay = min(2.0, max_seconds - elapsed)
            delays.append(delay)
            elapsed += delay
        for delay in delays:
            if self._stop_event.wait(delay):
                break
            after = get_screenshot(self.serial)
            if after is not None:
                captured.append(after)
        for after in captured:
            seen = inspect_success(after, self.serial)
            if seen.amount is not None:
                return seen, len(captured)
        return None, len(captured)

    def _next_stream(self, screen: np.ndarray) -> None:
        """滑到下一間直播並更新計數器。"""
        height, width = screen.shape[:2]
        if swipe_up(self.serial, width, height):
            self._streams_visited += 1
            self._stop_event.wait(2.5)   # 等動畫完成
        else:
            self._set_status(f"⚠️ ADB 向上滑動失敗，{get_read_interval()} 秒後重試", "p1")
            self._stop_event.wait(get_read_interval())

    def _switch_missing_card(self, screen: np.ndarray) -> bool:
        """Swipe between rooms; refresh via the live tab once every 30 minutes."""
        refresh_minutes = get_live_refresh_minutes()
        if time.monotonic() - self._last_live_refresh_at >= refresh_minutes * 60:
            point = find_live_tab(screen, self.serial)
            if point and tap(self.serial, *point):
                self._last_live_refresh_at = time.monotonic()
                self._collect_armed = True
                self._streams_visited += 1
                self._set_status(f"🔄 已滿 {refresh_minutes} 分鐘，點擊「直播」{point} 刷新", "p1")
                self._stop_event.wait(2)
                return False
            self._set_status(f"⚠️ {refresh_minutes} 分鐘刷新未找到「直播」；請在定位介面標記位置，先繼續滑動", "p1")

        height, width = screen.shape[:2]
        if swipe_up(self.serial, width, height):
            self._collect_armed = True
            self._streams_visited += 1
            self._set_status("⬆️ 藍色蝦幣卡片消失，向上滑到另一間直播", "p1")
            self._stop_event.wait(2.5)
            return True
        else:
            self._set_status(f"⚠️ ADB 向上滑動失敗，{get_read_interval()} 秒後重試", "p1")
            self._stop_event.wait(get_read_interval())
            return False

    def _finish_daily_limit(self) -> None:
        """今日上限已達：關閉 Shopee、寫 CSV、停止 bot。"""
        self._stop_reason = "今日上限已達"
        self._set_status("今日上限已達，關閉 Shopee 中…")
        close_shopee(self.serial)

        end_time = datetime.now()
        duration = int((end_time - self._start_time).total_seconds())

        _append_csv({
            "date":             self._start_time.strftime("%Y-%m-%d"),
            "serial":           self.serial,
            "nickname":         self.nickname,
            "coins_collected":  self._coins_collected,
            "streams_visited":  self._streams_visited,
            "start_time":       self._start_time.strftime("%H:%M:%S"),
            "end_time":         end_time.strftime("%H:%M:%S"),
            "duration_seconds": duration,
        })

        self.running        = False
        self.status_message = (
            f"完成 | 蒐集 {self._coins_collected} 次 | "
            f"共 {self._streams_visited} 間直播 | 耗時 {duration}s"
        )
        self._stop_event.set()

    def _finish_session(self, reason: str) -> None:
        self._stop_reason = reason
        self._set_status(f"🏁 {reason}，關閉蝦皮並返回手機首頁", "done")
        closed = close_shopee(self.serial)
        if not closed:
            self._set_status("⚠️ ADB 關閉蝦皮失敗，請檢查手機", "done")
        self.running = False
        self._stop_event.set()

    def _finish_limit_2_reached(self) -> None:
        """偵測到 limit_2 今日上限：Alt+H 回首頁、寫 CSV、停止 bot。"""
        self._stop_reason = "偵測到今日上限"
        self._set_status("偵測到 limit_2 今日上限，嘗試 Alt+H 回首頁中…")
        go_home_via_alt_h(self.serial)

        end_time = datetime.now()
        duration = int((end_time - self._start_time).total_seconds())

        _append_csv({
            "date":             self._start_time.strftime("%Y-%m-%d"),
            "serial":           self.serial,
            "nickname":         self.nickname,
            "coins_collected":  self._coins_collected,
            "streams_visited":  self._streams_visited,
            "start_time":       self._start_time.strftime("%H:%M:%S"),
            "end_time":         end_time.strftime("%H:%M:%S"),
            "duration_seconds": duration,
        })

        self.running        = False
        self.status_message = (
            f"完成 | 蒐集 {self._coins_collected} 次 | "
            f"共 {self._streams_visited} 間直播 | 耗時 {duration}s"
        )
        self._stop_event.set()

    # ---------------------------------------------------------------------- #
    #  主要自動化迴圈
    # ---------------------------------------------------------------------- #

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    self._run_loop_body()
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[%s] Collector will retry after unexpected error", self.serial)
                    self._set_status(f"⚠️ 自動流程異常：{exc}，30 秒後重新啟動", "error")
                    try:
                        close_shopee(self.serial)
                    except Exception:  # noqa: BLE001
                        logger.exception("[%s] Could not close Shopee before retry", self.serial)
                    self._wait_for_retry(30)
        finally:
            self.running = False
            self.retrying = False
            release_device_lock(self._device_lock)
            self._device_lock = None
            notify_device_stopped(self.serial, self.nickname, self._stop_reason)

    def _wait_for_retry(self, seconds: float) -> None:
        self.retrying = True
        try:
            self._stop_event.wait(seconds)
        finally:
            self.retrying = False

    def _run_loop_body(self) -> None:
        needs_launch = self._launch_on_start
        startup_failures = 0
        crash_restarts: deque[float] = deque()
        exception_retries = 0
        exception_relaunches = 0
        last_exception_id = None
        quiet_since = None
        last_quiet_close = float("-inf")
        while not self._stop_event.is_set():
            try:
                quiet_end = quiet_window_end()
                if quiet_end is not None:
                    now = time.monotonic()
                    entered_quiet = quiet_since is None
                    if quiet_since is None:
                        quiet_since = now
                        needs_launch = True
                        self._set_status(f"🌙 暫停採集至台灣時間 {quiet_end:%H:%M}，關閉蝦皮", "quiet")
                    if now - last_quiet_close >= 60:
                        if not close_shopee(self.serial):
                            self._set_status("⚠️ 暫停時段關閉蝦皮失敗，稍後重試", "quiet")
                        last_quiet_close = now
                    if entered_quiet:
                        if not self._skip_initial_quiet_notice:
                            notify_device_paused(self.serial, self.nickname, quiet_end)
                        self._skip_initial_quiet_notice = False
                    remaining = max(0.1, (quiet_end - datetime.now(TAIPEI)).total_seconds())
                    self._stop_event.wait(min(10, remaining))
                    continue
                if quiet_since is not None:
                    paused = time.monotonic() - quiet_since
                    self._started_monotonic += paused
                    self._last_live_refresh_at += paused
                    if self._last_success_at is not None:
                        self._last_success_at += paused
                    self._consecutive_limit = 0
                    self._consecutive_missing_cards = 0
                    self._collect_armed = True
                    quiet_since = None
                    self._set_status("🌅 暫停時段結束，恢復採集", "quiet")
                if self._guard_foreground:
                    current = foreground_app(self.serial)
                    if current["kind"] == "home" and not needs_launch:
                        # A transient HOME frame can occur while Android switches activities.
                        if self._stop_event.wait(1):
                            continue
                        current = foreground_app(self.serial)
                        if current["kind"] == "home":
                            now = time.monotonic()
                            while crash_restarts and now - crash_restarts[0] >= 600:
                                crash_restarts.popleft()
                            if len(crash_restarts) >= 3:
                                self._set_status("⚠️ 10 分鐘內連續回到桌面，60 秒後再重啟", "startup")
                                self._wait_for_retry(60)
                                if self._stop_event.is_set():
                                    continue
                                crash_restarts.clear()
                            crash_restarts.append(now)
                            self._set_status("⚠️ 蝦皮意外回到桌面，正在重新進入直播", "startup")
                            notify_device_crashed(self.serial, self.nickname, len(crash_restarts))
                            close_shopee(self.serial)
                            needs_launch = True
                            self._consecutive_missing_cards = 0
                            self._collect_armed = True
                    if current["kind"] == "unknown":
                        self._set_status("⚠️ 暫時無法確認手機畫面，稍後重試", "startup")
                        self._wait_for_retry(10)
                        continue
                    allowed = ("home", "shopee") if needs_launch else ("shopee",)
                    if current["kind"] not in allowed:
                        self._stop_reason = f"手機已切換至 {current['package'] or '未知 App'}"
                        self._set_status(f"⏸️ {self._stop_reason}，自動流程已停止", "paused")
                        return
                if needs_launch:
                    self._set_status("📱 正在開啟蝦皮並進入直播", "startup")
                    opened = (open_shopee_live(self.serial, guard_foreground=True)
                              if self._guard_foreground else open_shopee_live(self.serial))
                    if not opened:
                        if quiet_window_end() is not None:
                            continue
                        if self._guard_foreground:
                            current = foreground_app(self.serial)
                            if current["kind"] == "other":
                                self._stop_reason = "開啟期間手機切換了 App"
                                self._set_status("⏸️ 開啟期間手機切換了 App，自動流程已停止", "paused")
                                return
                        startup_failures += 1
                        delay = min(10 * 2 ** (startup_failures - 1), 60)
                        self._set_status(f"⚠️ 無法進入蝦皮直播，{delay} 秒後自動重試（第 {startup_failures} 次）", "startup")
                        close_shopee(self.serial)
                        self._wait_for_retry(delay)
                        continue
                    needs_launch = False
                    startup_failures = 0
                now = time.monotonic()
                read_interval = get_read_interval()
                daily_limit = get_daily_limit()
                if self._max_runtime_seconds and now - self._started_monotonic >= self._max_runtime_seconds:
                    self._finish_session("已達本次最長運行時間")
                    return
                if self._idle_timeout_seconds and self._last_success_at is not None and now - self._last_success_at >= self._idle_timeout_seconds:
                    self._finish_session("一段時間沒有新的成功領取")
                    return
                if daily_count(self.serial) >= daily_limit:
                    self._finish_session(f"今日已達 {daily_limit} 次")
                    return
                screen = get_screenshot(self.serial)
                if screen is None:
                    self._set_status("screencap 錯誤，重試中…")
                    self._stop_event.wait(read_interval)
                    continue

                exception = match_profile(screen, self.serial)
                if exception:
                    if quiet_window_end() is not None:
                        continue
                    if exception["id"] != last_exception_id:
                        exception_retries = 0
                        last_exception_id = exception["id"]
                    if exception_retries >= 3:
                        if exception_relaunches >= 2:
                            self._set_status(f"⚠️ 例外畫面「{exception['name']}」持續出現，60 秒後再重試", "recovery")
                            self._wait_for_retry(60)
                            if self._stop_event.is_set():
                                continue
                            exception_relaunches = 0
                        self._set_status(f"⚠️ 例外畫面「{exception['name']}」仍在，重新進入直播", "recovery")
                        close_shopee(self.serial)
                        needs_launch = True
                        exception_retries = 0
                        exception_relaunches += 1
                        continue
                    if exception["action"] == "back":
                        recovered = _adb(self.serial, "shell", "input", "keyevent", "4").returncode == 0
                    else:
                        old_width, old_height = exception["screen_size"]
                        height, width = screen.shape[:2]
                        x, y = exception["action_point"]
                        recovered = tap(self.serial, round(x * width / old_width), round(y * height / old_height))
                    exception_retries += 1
                    self._set_status(f"↩️ 辨識到例外畫面「{exception['name']}」，已執行返回動作"
                                     if recovered else f"⚠️ 例外畫面「{exception['name']}」返回失敗，稍後重試", "recovery")
                    self._stop_event.wait(1.5)
                    continue
                exception_retries = 0
                exception_relaunches = 0
                last_exception_id = None

                if self._skip_current_room:
                    if quiet_window_end() is not None:
                        continue
                    height, width = screen.shape[:2]
                    if swipe_up(self.serial, width, height):
                        self._skip_current_room = False
                        self._collect_armed = True
                        self._streams_visited += 1
                        self._set_status("⬆️ 上次點擊已按卡片金額計入，略過該直播間", "p1")
                        self._stop_event.wait(2.5)
                    else:
                        self._set_status("⚠️ 略過上次點擊的直播間時滑動失敗，稍後重試", "p1")
                        self._stop_event.wait(read_interval)
                    continue

                checkin_close = find_checkin_popup_close(screen, self.serial)
                if checkin_close:
                    if tap(self.serial, *checkin_close):
                        self._set_status(f"✕ 偵測到馬上簽到彈窗，已點擊關閉 {checkin_close}", "popup")
                    else:
                        self._set_status("⚠️ 偵測到馬上簽到彈窗，但 ADB 關閉點擊失敗", "popup")
                    self._stop_event.wait(1)
                    continue

                # 階段 1：先只看右上角關鍵區塊，判斷 collect / ing / limit_2。
                list_pos = find_template(screen, "list.png")
                if list_pos:
                    if quiet_window_end() is not None:
                        continue
                    self._set_status("📋 偵測到直播列表 → 先下拉刷新再挑最多金額", "p1")
                    refresh_live_list(self.serial)
                    time.sleep(1.2)

                    refreshed = get_screenshot(self.serial)
                    if refreshed is not None:
                        max_value, tap_pos, raw_txt = _extract_max_live_amount_pos(refreshed)
                        if not tap_pos:
                            self._set_status("🔄 未抓到可用金額 → 再下拉重整一次", "p1")
                            refresh_live_list(self.serial)
                            time.sleep(1.2)
                            refreshed_retry = get_screenshot(self.serial)
                            if refreshed_retry is not None:
                                max_value, tap_pos, raw_txt = _extract_max_live_amount_pos(refreshed_retry)
                                refreshed = refreshed_retry

                        if tap_pos:
                            self._set_status(
                                f"🏆 OCR 找到最多金額 {max_value:.1f}（{raw_txt}）→ 點擊進入 ({tap_pos[0]},{tap_pos[1]})",
                                "p1",
                            )
                            tap(self.serial, *tap_pos)
                        else:
                            h_ls, w_ls = refreshed.shape[:2]
                            fallback = (int(w_ls * 0.25), int(h_ls * 0.28))
                            self._set_status(
                                f"⚠️ 列表 OCR 未抓到金額，改點第一張直播 ({fallback[0]},{fallback[1]})",
                                "p1",
                            )
                            tap(self.serial, *fallback)
                    else:
                        self._set_status("⚠️ 列表刷新後截圖失敗，略過本輪", "p1")

                    time.sleep(1.5)
                    continue

                # Only the verified live-room coin card may trigger a reward action.
                reward = inspect_reward(screen, self.serial)
                if reward.state != "absent":
                    self._consecutive_missing_cards = 0
                min_reward = get_min_reward()
                if reward.amount is not None and float(reward.amount) < float(min_reward):
                    if quiet_window_end() is not None:
                        continue
                    height, width = screen.shape[:2]
                    if swipe_up(self.serial, width, height):
                        self._collect_armed = True
                        self._streams_visited += 1
                        self._set_status(f"⬆️ 藍色蝦幣 {reward.amount} 低於 {min_reward:.2f}，往上找下一間", "p1")
                        self._stop_event.wait(2.5)
                    else:
                        self._set_status(f"⚠️ 略過低金額直播時滑動失敗，{read_interval} 秒後重試", "p1")
                        self._stop_event.wait(read_interval)
                    continue
                if reward.state == "collect" and reward.collect_point:
                    if quiet_window_end() is not None:
                        continue
                    if not self._collect_armed:
                        self._set_status(f"👀 等待卡片進入下一輪倒數，{read_interval} 秒後重讀", "p1")
                        self._stop_event.wait(read_interval)
                        continue
                    if daily_count(self.serial) >= daily_limit:
                        self._set_status(f"🛑 今日已達 {daily_limit} 次，停止自動領取", "p1")
                        self._finish_session(f"今日已達 {daily_limit} 次")
                        return
                    claim_id = reserve_claim(self.serial, reward.amount)
                    red_note = (f"；紅色倒數 {reward.red_remaining_seconds // 60:02d}:{reward.red_remaining_seconds % 60:02d} 僅供參考"
                                if reward.red_remaining_seconds is not None else "")
                    self._set_status(
                        f"🔎 藍色蝦幣 {reward.amount}、領取按鈕 {reward.collect_point}{red_note} → 點擊後驗證", "p1"
                    )
                    if not tap(self.serial, *reward.collect_point):
                        cancel_claim(claim_id)
                        self._set_status("❌ ADB 點擊失敗，未計入領取紀錄", "p1")
                        self._stop_event.wait(read_interval)
                        continue
                    success, captured_count = self._verify_claim_success()
                    if success is not None:
                        confirm_claim(claim_id, success.amount)
                        self._set_status(f"✅ 成功提示確認領取 {success.amount} 蝦幣，今日累計 {daily_count(self.serial)} 次", "p1")
                    else:
                        confirm_claim(claim_id)
                        self._skip_current_room = True
                        self._set_status(
                            f"✅ 點擊後 {captured_count} 張截圖未讀到成功提示，已用卡片金額 {reward.amount} 蝦幣自動計入所得；略過這間繼續", "p1"
                        )
                    self._coins_collected += 1
                    self._last_success_at = time.monotonic()
                    self._consecutive_limit = 0
                    self._collect_armed = False
                    continue

                if reward.state == "waiting":
                    self._collect_armed = True
                    self._set_status(format_waiting_status(reward, read_interval), "p1")
                    self._stop_event.wait(read_interval)
                    continue

                if reward.state == "limit":
                    if quiet_window_end() is not None:
                        continue
                    self._consecutive_limit += 1
                    limit_streak = get_limit_streak()
                    self._set_status(
                        f"🚫 蝦幣卡片顯示限制 ({self._consecutive_limit}/{limit_streak}) → 切換直播",
                        "p1",
                    )
                    if self._consecutive_limit >= limit_streak:
                        self._finish_daily_limit()
                        return
                    self._next_stream(screen)
                    continue

                if reward.state == "unknown":
                    red_note = (f"；紅色倒數 {reward.red_remaining_seconds // 60:02d}:{reward.red_remaining_seconds % 60:02d}（不影響領取）"
                                if reward.red_remaining_seconds is not None else "")
                    self._set_status(
                        f"👀 已定位藍色蝦幣卡片，但狀態不明{red_note}，暫不點擊 | {reward.text[:40]}", "p1"
                    )
                    self._stop_event.wait(read_interval)
                    continue

                # The claim modal can hide the card; handle it separately.
                p1_collect_model_pos = find_template(screen, "p1_collect_model.png")
                if p1_collect_model_pos:
                    h_sc, w_sc = screen.shape[:2]
                    manual_close = button_point(self.serial, "modal_close", w_sc, h_sc)
                    if manual_close:
                        tap(self.serial, *manual_close)
                        self._set_status(f"✅ 使用手動標記的關閉按鈕 {manual_close}", "p1")
                        time.sleep(1.0)
                        continue
                    close_x = w_sc // 2
                    y_start = h_sc // 2
                    y_end = int(h_sc * 0.75)
                    close_ys = [
                        y_start + round(i * (y_end - y_start) / 4)
                        for i in range(5)
                    ]
                    self._set_status(
                        f"✅ 領取 Modal → 掃描叉叉 x={close_x}, ys={close_ys}", "p1"
                    )
                    for cy in close_ys:
                        tap(self.serial, close_x, cy)
                        time.sleep(0.15)
                    time.sleep(1.0)
                    continue

                if reward.state == "absent":
                    if self._stop_event.wait(1):
                        continue
                    repeated = get_screenshot(self.serial)
                    if repeated is not None and inspect_reward(repeated, self.serial).state == "absent":
                        popup_close = find_checkin_popup_close(repeated, self.serial)
                        if popup_close:
                            tap(self.serial, *popup_close)
                            self._stop_event.wait(1)
                            continue
                        missing_limit = get_missing_card_limit()
                        if self._consecutive_missing_cards + 1 >= missing_limit:
                            self._consecutive_missing_cards += 1
                            self._finish_session(
                                f"連續 {self._consecutive_missing_cards} 間直播沒有藍色蝦幣卡片，視為今日上限"
                            )
                            return
                        if quiet_window_end() is not None:
                            continue
                        if self._switch_missing_card(repeated):
                            self._consecutive_missing_cards += 1
                            self._set_status(
                                f"⬆️ 連續 {self._consecutive_missing_cards}/{missing_limit} 間無藍色蝦幣卡片，已切換直播",
                                "p1",
                            )
                    else:
                        self._set_status(f"👀 卡片狀態不穩定，{read_interval} 秒後重新辨識", "p1")
                        self._stop_event.wait(read_interval)
                    continue

                self._set_status(f"👀 卡片辨識結果不穩定，{read_interval} 秒後再辨識", "p1")
                self._stop_event.wait(read_interval)

            except Exception as exc:  # noqa: BLE001
                self._set_status(f"❌ 例外: {exc}")
                logger.exception("[%s] Unexpected error", self.serial)
                self._stop_event.wait(get_read_interval())
