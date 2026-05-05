"""
bot_logic.py — Core ADB + OpenCV automation logic for each device.

State machine per device:
  - p1_collect      : 可領蝦幣 → 點擊領取
  - p1_collect_model: 領取後跳出的 Modal → 點擊中下方叉叉關閉
  - p1_ing          : 右上角計時中 → 等待
    - p1_limit_2      : 右上角顯示明日再來（今日上限）→ 結束此裝置
  - p1_limit        : p1 樣式已達上限 → 滑到下一間直播
  - p2_limit        : p2 樣式已達上限 → 滑到下一間直播
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

# 滑動參數 (往上滑 = 移到下一間直播)
SWIPE_X          = 540
SWIPE_FROM_Y     = 1400
SWIPE_TO_Y       = 400
SWIPE_DURATION   = 500   # ms

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
STATE_SCORE_MARGIN_COLLECT = 0.00
STATE_SCORE_MARGIN_ING = 0.08
STATE_SCORE_MARGIN_LIMIT_2 = 0.05
POLL_INTERVAL_SECONDS = 5.0

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


def tap(serial: str, x: int, y: int) -> None:
    result = _adb(serial, "shell", "input", "tap", str(x), str(y))
    if result.returncode != 0:
        logger.warning("[%s] tap failed at (%s,%s), rc=%s", serial, x, y, result.returncode)


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


def swipe_up(serial: str) -> None:
    """往上滑一格，切換到下一間直播。"""
    _adb(
        serial,
        "shell", "input", "swipe",
        str(SWIPE_X), str(SWIPE_FROM_Y),
        str(SWIPE_X), str(SWIPE_TO_Y),
        str(SWIPE_DURATION),
    )


def refresh_live_list(serial: str) -> None:
    """直播列表頁下拉刷新。"""
    _adb(
        serial,
        "shell", "input", "swipe",
        str(LIST_REFRESH_X), str(LIST_REFRESH_FROM_Y),
        str(LIST_REFRESH_X), str(LIST_REFRESH_TO_Y),
        str(LIST_REFRESH_DURATION),
    )


def close_shopee(serial: str) -> None:
    """強制關閉 Shopee App。"""
    _adb(serial, "shell", "am", "force-stop", SHOPEE_PACKAGE)


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

    tmpl = cv2.imread(str(path), cv2.IMREAD_COLOR)
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


def _template_threshold(template_name: str, fallback: float = 0.80) -> float:
    cfg = TEMPLATE_CONFIG.get(template_name, {})
    return float(cfg.get("threshold", fallback))


import re as _re

_TIMER_RE = _re.compile(r'\d{1,2}:\d{2}')
_MAX_AMOUNT_RE = _re.compile(r'最多\s*([1-9]\d?(?:\.\d)?)')


def _contains_list_amount_keyword(text: str) -> bool:
    compact = text.replace(" ", "").replace("\n", "")
    return "最多" in compact

def _contains_timer(text: str) -> bool:
    """OCR 掃到計時格式（如 09:58）→ 判定為 p1_ing 計時中。"""
    if _contains_list_amount_keyword(text):
        return False
    return bool(_TIMER_RE.search(text))


def _contains_limit_1_keyword(text: str) -> bool:
    compact = text.replace(" ", "").replace("\n", "")
    return "限定" in compact


def _contains_collect_keyword(text: str) -> bool:
    compact = text.replace(" ", "").replace("\n", "")
    return any(k in compact for k in ("領取", "领取", "可領", "可领"))


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


def _extract_reward_ocr(screen: "np.ndarray") -> "tuple[str, tuple[int, int] | None]":
    """Run OCR on reward ROI and return (joined_text, collect_button_point_if_any)."""
    if not OCR_ENABLED:
        return "", None

    roi_cfg = TEMPLATE_CONFIG.get("p1_collect.png", {}).get("roi", REWARD_TOP_RIGHT_ROI)
    roi_img, offset_x, offset_y = _crop_with_ratio(screen, roi_cfg)
    scale = 2.0
    gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bin_img = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    try:
        result, _ = OCR_ENGINE(bin_img)
    except Exception:  # noqa: BLE001
        return "", None

    texts = []
    collect_pos = None
    for item in result or []:
        if len(item) < 2:
            continue
        txt = str(item[1] or "").strip()
        if txt:
            texts.append(txt)
        if collect_pos is None and _contains_collect_keyword(txt):
            center = _ocr_box_center(item[0]) if len(item) >= 1 else None
            if center:
                collect_pos = (
                    int(offset_x + center[0] / scale),
                    int(offset_y + center[1] / scale),
                )
    return " ".join(texts).replace("\n", " ").strip(), collect_pos


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

    Templates (放在 assets/ 資料夾):
        p1_collect.png       — p1 樣式：可領取按鈕
        p1_collect_model.png — p1 樣式：領取後彈出的 Modal
        p1_ing.png           — p1 樣式：右上角計時器仍在跑
        p1_limit_2.png       — p1 樣式：右上角今日上限（明日再來）
        p1_limit.png         — p1 樣式：此直播間已達上限
        p2_limit.png         — p2 樣式：此直播間已達上限

    使用方式:
        bot = ShopeeBot(serial="70f5e12", nickname="老媽的小米")
        bot.start()
        ...
        bot.stop()
    """

    def __init__(self, serial: str, nickname: str = ""):
        self.serial   = serial
        self.nickname = nickname or serial

        self._stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None

        # 對外可讀狀態
        self.running        = False
        self.status_message = "idle"

        # 本次執行統計
        self._start_time:        "datetime | None" = None
        self._coins_collected    = 0   # 成功領取次數
        self._streams_visited    = 0   # 已跳過的直播間數
        self._consecutive_limit  = 0   # 連續 limit 計數

        # Log buffer（最多保留 200 筆）
        self.logs: deque = deque(maxlen=200)

    # ---------------------------------------------------------------------- #
    #  公開控制方法
    # ---------------------------------------------------------------------- #

    def start(self) -> None:
        if self.running:
            return
        self._reset_stats()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"bot-{self.serial}",
            daemon=True,
        )
        self._thread.start()
        self.running = True
        logger.info("[%s] Bot started.", self.serial)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)
        self.running        = False
        self.status_message = "stopped"
        logger.info("[%s] Bot stopped.", self.serial)

    def to_dict(self) -> dict:
        return {
            "serial":           self.serial,
            "nickname":         self.nickname,
            "running":          self.running,
            "status":           self.status_message,
            "coins_collected":  self._coins_collected,
            "streams_visited":  self._streams_visited,
        }

    # ---------------------------------------------------------------------- #
    #  內部工具
    # ---------------------------------------------------------------------- #

    def _reset_stats(self) -> None:
        self._start_time       = datetime.now()
        self._coins_collected  = 0
        self._streams_visited  = 0
        self._consecutive_limit = 0
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

    def _next_stream(self) -> None:
        """滑到下一間直播並更新計數器。"""
        swipe_up(self.serial)
        self._streams_visited += 1
        time.sleep(2.5)   # 等動畫完成

    def _finish_daily_limit(self) -> None:
        """今日上限已達：關閉 Shopee、寫 CSV、停止 bot。"""
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

    def _finish_limit_2_reached(self) -> None:
        """偵測到 limit_2 今日上限：Alt+H 回首頁、寫 CSV、停止 bot。"""
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
        while not self._stop_event.is_set():
            try:
                screen = get_screenshot(self.serial)
                if screen is None:
                    self._set_status("screencap 錯誤，重試中…")
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                # 階段 1：先只看右上角關鍵區塊，判斷 collect / ing / limit_2。
                list_pos = find_template(screen, "list.png")
                if list_pos:
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

                p1_collect_pos, s_collect = find_template(
                    screen, "p1_collect.png", return_score=True
                )
                _, s_ing = find_template(screen, "p1_ing.png", return_score=True)
                p1_collect_model_pos, s_collect_model = find_template(
                    screen, "p1_collect_model.png", return_score=True
                )
                p1_limit_pos, s_limit = find_template(
                    screen, "p1_limit_1.png", return_score=True
                )
                _, s_limit_2 = find_template(screen, "p1_limit_2.png", return_score=True)
                p1_collect_th = _template_threshold("p1_collect.png")
                p1_ing_th = _template_threshold("p1_ing.png")
                p1_limit_2_th = _template_threshold("p1_limit_2.png")

                # ── 0. p1_limit_2：今日上限（優先） ──────────────────────── #
                if (
                    s_limit_2 >= p1_limit_2_th
                    and (s_limit_2 - max(s_collect, s_ing)) >= STATE_SCORE_MARGIN_LIMIT_2
                ):
                    self._set_status(
                        "🛑 偵測到 p1 今日上限(limit_2) → 結束本裝置流程",
                        "p1",
                    )
                    self._finish_limit_2_reached()
                    return

                # ── 1. p1_collect：可領取 ─────────────────────────────── #
                if (
                    p1_collect_pos
                    and s_collect >= p1_collect_th
                    and (s_collect - s_ing) >= STATE_SCORE_MARGIN_COLLECT
                ):
                    self._set_status("✅ 偵測到 p1 可領取 → 點擊領取", "p1")
                    tap(self.serial, *p1_collect_pos)
                    self._coins_collected += 1
                    self._consecutive_limit = 0
                    time.sleep(1.5)
                    continue

                # ── 2. p1_collect_model：領取後的 Modal → 叉叉關閉 ─────── #
                if p1_collect_model_pos:
                    h_sc, w_sc = screen.shape[:2]
                    close_x = w_sc // 2
                    y_start = h_sc // 2
                    y_end   = int(h_sc * 0.75)
                    steps   = 5
                    close_ys = [
                        y_start + round(i * (y_end - y_start) / (steps - 1))
                        for i in range(steps)
                    ]
                    self._set_status(
                        f"✅ p1 領取 Modal → 掃描叉叉 x={close_x}, ys={close_ys}",
                        "p1",
                    )
                    for cy in close_ys:
                        tap(self.serial, close_x, cy)
                        time.sleep(0.15)
                    time.sleep(1.0)
                    continue

                # ── 3. p1_ing：計時中，等待 ─────────────────────────────── #
                if s_ing >= p1_ing_th and (s_ing - s_collect) >= STATE_SCORE_MARGIN_ING:
                    self._set_status("⏳ p1 計時中，等待 10 秒…", "p1")
                    time.sleep(10)
                    continue

                if s_collect >= p1_collect_th and s_ing >= p1_ing_th:
                    reward_text, ocr_collect_pos = _extract_reward_ocr(screen)
                    if _contains_list_amount_keyword(reward_text):
                        self._set_status(
                            f"📋 OCR 命中『最多』→ 視為直播列表，先重整後挑最多金額 | ocr='{reward_text[:30]}'",
                            "p1",
                        )
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

                    if _contains_limit_1_keyword(reward_text):
                        switch_x_pos = find_template(screen, "switch_x.png")
                        if switch_x_pos:
                            tap(self.serial, *switch_x_pos)
                            self._set_status(
                                "🚫 OCR 命中『限定』(p1_limit_1) → 先點 switch_x，再切換直播 | "
                                f"x=({switch_x_pos[0]},{switch_x_pos[1]}), ocr='{reward_text[:30]}'",
                                "p1",
                            )
                            time.sleep(0.4)
                        else:
                            self._set_status(
                                "🚫 OCR 命中『限定』(p1_limit_1) → 切換直播（未命中 switch_x） | "
                                f"ocr='{reward_text[:30]}'",
                                "p1",
                            )

                        self._consecutive_limit += 1
                        if self._consecutive_limit >= DAILY_LIMIT_THRESHOLD:
                            self._finish_daily_limit()
                            return
                        self._next_stream()
                        continue

                    if _contains_timer(reward_text):
                        self._set_status(
                            f"⏳ OCR 偵測到計時器 → 視為 p1_ing，等待 10 秒 | "
                            f"score[p1_collect={s_collect:.2f}, p1_ing={s_ing:.2f}], ocr='{reward_text[:30]}'",
                            "p1",
                        )
                        time.sleep(10)
                        continue

                    if _contains_collect_keyword(reward_text):
                        # 點擊優先使用 p1_collect 模板中心（方框中心），OCR 座標只作備援。
                        tap_pos = p1_collect_pos or ocr_collect_pos
                        if tap_pos is None:
                            self._set_status(
                                "⚠️ OCR 命中領取關鍵字，但無可用點擊座標，略過本輪 | "
                                f"score[p1_collect={s_collect:.2f}, p1_ing={s_ing:.2f}, p1_collect_model={s_collect_model:.2f}, p1_limit_1={s_limit:.2f}, p1_limit_2={s_limit_2:.2f}], ocr_enabled={OCR_ENABLED}, ocr='{reward_text[:20]}'",
                                "p1",
                            )
                            time.sleep(POLL_INTERVAL_SECONDS)
                            continue

                        self._set_status(
                            "🔍 OCR 命中「領取」→ 視為 p1 可領取並點擊 | "
                            f"tap=({tap_pos[0]},{tap_pos[1]}), score[p1_collect={s_collect:.2f}, p1_ing={s_ing:.2f}, p1_collect_model={s_collect_model:.2f}, p1_limit_1={s_limit:.2f}, p1_limit_2={s_limit_2:.2f}], ocr_enabled={OCR_ENABLED}",
                            "p1",
                        )
                        tap(self.serial, *tap_pos)
                        self._coins_collected += 1
                        self._consecutive_limit = 0
                        time.sleep(1.5)
                        continue

                    self._set_status(
                        "⚠️ p1_collect / p1_ing 分數接近，先略過本輪... "
                        f"score[p1_collect={s_collect:.2f}, p1_ing={s_ing:.2f}, p1_collect_model={s_collect_model:.2f}, p1_limit_1={s_limit:.2f}, p1_limit_2={s_limit_2:.2f}], ocr_enabled={OCR_ENABLED}, ocr='{reward_text[:20]}'",
                        "p1",
                    )
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                # ── 4. p1_limit_1：p1 樣式上限 → 跳到下一間 ─────────────── #
                if p1_limit_pos:
                    self._consecutive_limit += 1
                    self._set_status(
                        f"🚫 p1_limit_1 上限 ({self._consecutive_limit}/{DAILY_LIMIT_THRESHOLD}) → 切換直播", "p1"
                    )
                    if self._consecutive_limit >= DAILY_LIMIT_THRESHOLD:
                        self._finish_daily_limit()
                        return
                    self._next_stream()
                    continue

                # ── 5. p2_limit：p2 樣式上限 → 跳到下一間 ──────────────── #
                pos = find_template(screen, "p2_limit.png")
                if pos:
                    self._consecutive_limit += 1
                    self._set_status(
                        f"🚫 p2 上限 ({self._consecutive_limit}/{DAILY_LIMIT_THRESHOLD}) → 切換直播", "p2"
                    )
                    if self._consecutive_limit >= DAILY_LIMIT_THRESHOLD:
                        self._finish_daily_limit()
                        return
                    self._next_stream()
                    continue

                # ── 6. p2 非 limit 畫面（collect / model / ing）→ 跳過 ─── #
                p2_matched = False
                for p2_tmpl, p2_label in (
                    ("p2_collect.png",       "p2 可領取（跳過）"),
                    ("p2_collect_model.png", "p2 領取 Modal（跳過）"),
                    ("p2_ing.png",           "p2 計時中（跳過）"),
                ):
                    if find_template(screen, p2_tmpl):
                        self._set_status(f"⏭ {p2_label} → 切換直播", "p2")
                        self._next_stream()
                        p2_matched = True
                        break

                if not p2_matched:
                    # ── 無符合畫面 → 等待 ────────────────────────────────── #
                    _, s_p2_collect = find_template(screen, "p2_collect.png", return_score=True)
                    self._set_status(
                        "👀 監看中，未偵測到已知畫面… "
                        f"score[p1_collect={s_collect:.2f}, p1_ing={s_ing:.2f}, p1_collect_model={s_collect_model:.2f}, p1_limit={s_limit:.2f}, p1_limit_2={s_limit_2:.2f}, p2_collect={s_p2_collect:.2f}]"
                    )
                    time.sleep(POLL_INTERVAL_SECONDS)

            except Exception as exc:  # noqa: BLE001
                self._set_status(f"❌ 例外: {exc}")
                logger.exception("[%s] Unexpected error", self.serial)
                time.sleep(5)
