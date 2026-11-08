"""Locate the live-room coin card and read only that card."""

from __future__ import annotations

import json
import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import easyocr
import numpy as np


BASE_DIR = Path(__file__).resolve().parent
REGION_DIR = BASE_DIR / "device_regions"
MODEL_DIR = BASE_DIR / ".easyocr" / "model"
SEARCH_RATIO = (0.68, 0.17, 1.0, 0.39)
_reader = None
_reader_lock = threading.Lock()
_inference_lock = threading.Lock()
_serial_re = re.compile(r"^[A-Za-z0-9._-]+$")
_timer_re = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
_amount_re = re.compile(r"(?<!\d)(\d{1,2}\.\d{1,2})(?!\d)")
_success_amount_re = re.compile(r"([0-9Oo]{1,2}\.[0-9Oo]{1,2})", re.IGNORECASE)


@dataclass(frozen=True)
class RewardObservation:
    state: str  # absent, unknown, collect, waiting, limit
    reward_box: tuple[int, int, int, int] | None = None
    coin_center: tuple[int, int] | None = None
    collect_point: tuple[int, int] | None = None
    text: str = ""
    amount: str | None = None
    remaining_seconds: int | None = None
    red_remaining_seconds: int | None = None


@dataclass(frozen=True)
class SuccessObservation:
    amount: str | None
    text: str


def _get_reader():
    global _reader
    if _reader is None:
        with _reader_lock:
            if _reader is None:
                _reader = easyocr.Reader(
                    ["ch_tra", "en"],
                    gpu=False,
                    model_storage_directory=str(MODEL_DIR),
                    download_enabled=False,
                )
    return _reader


def _region_path(serial: str) -> Path:
    if not serial or len(serial) > 128:
        raise ValueError("Invalid device serial")
    name = serial if _serial_re.fullmatch(serial) else hashlib.sha256(serial.encode("utf-8")).hexdigest()[:16]
    return REGION_DIR / f"{name}.json"


def load_region(serial: str) -> dict | None:
    path = _region_path(serial)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("serial") != serial:
            return None
        box = data["reward_box"]
        size = data["screen_size"]
        if len(box) != 4 or len(size) != 2 or min(size) <= 0:
            return None
        if not (0 <= box[0] < box[2] <= size[0] and 0 <= box[1] < box[3] <= size[1]):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_region(serial: str, width: int, height: int, observation: RewardObservation) -> None:
    path = _region_path(serial)
    previous = load_region(serial)
    if previous and previous.get("source") == "manual":
        return
    box = list(observation.reward_box)
    coin = list(observation.coin_center)
    if previous and previous.get("screen_size") == [width, height]:
        if previous.get("reward_box") == box and previous.get("coin_center") == coin:
            return
    REGION_DIR.mkdir(exist_ok=True)
    data = {
        "serial": serial,
        "screen_size": [width, height],
        "reward_box": box,
        "blue_box": box,
        "coin_center": coin,
        "source": "blue_panel_and_easyocr",
    }
    if previous:
        for key in ("buttons", "timer_box", "success_box", "red_box"):
            if previous.get(key):
                data[key] = previous[key]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def save_manual_region(
    serial: str, width: int, height: int,
    reward_box: list[int], coin_center: list[int], buttons: dict[str, list[int]],
    timer_box: list[int] | None = None, success_box: list[int] | None = None,
    red_box: list[int] | None = None, blue_box: list[int] | None = None,
) -> dict:
    path = _region_path(serial)
    if width < 100 or height < 100 or len(reward_box) != 4 or len(coin_center) != 2:
        raise ValueError("螢幕或卡片座標不正確")
    x1, y1, x2, y2 = reward_box
    cx, cy = coin_center
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height and x1 <= cx <= x2 and y1 <= cy <= y2):
        raise ValueError("卡片範圍或蝦幣圓心超出畫面")
    allowed = {"collect", "modal_close", "live_tab", "home_live_entry", "checkin_close"}
    if not set(buttons).issubset(allowed) or any(
        len(point) != 2 or not (0 <= point[0] < width and 0 <= point[1] < height)
        for point in buttons.values()
    ):
        raise ValueError("按鈕座標不正確")
    for box in (timer_box, success_box, red_box, blue_box):
        if box is not None and not valid_box(box, width, height):
            raise ValueError("OCR 範圍超出畫面")
    data = {
        "serial": serial, "screen_size": [width, height],
        "reward_box": reward_box, "coin_center": coin_center,
        "buttons": buttons, "source": "manual",
    }
    if timer_box is not None:
        data["timer_box"] = timer_box
    if success_box is not None:
        data["success_box"] = success_box
    if red_box is not None:
        data["red_box"] = red_box
    if blue_box is not None:
        data["blue_box"] = blue_box
    REGION_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return data


def button_point(serial: str, name: str, width: int, height: int) -> tuple[int, int] | None:
    saved = load_region(serial)
    if not saved:
        return None
    point = saved.get("buttons", {}).get(name)
    if not point:
        return None
    old_width, old_height = saved["screen_size"]
    return int(point[0] * width / old_width), int(point[1] * height / old_height)


def valid_box(box: list[int] | tuple[int, ...], width: int, height: int) -> bool:
    return len(box) == 4 and 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height


def scaled_box(serial: str, name: str, width: int, height: int) -> tuple[int, int, int, int] | None:
    saved = load_region(serial)
    if not saved or not saved.get(name):
        return None
    old_width, old_height = saved["screen_size"]
    box = tuple(int(value * (width / old_width if index % 2 == 0 else height / old_height))
                for index, value in enumerate(saved[name]))
    return box if valid_box(box, width, height) else None


def _search_bounds(width: int, height: int) -> tuple[int, int, int, int]:
    return tuple(int(value * (width if index % 2 == 0 else height)) for index, value in enumerate(SEARCH_RATIO))


def _yellow_coin_candidates(screen: np.ndarray, saved: dict | None) -> list[tuple[int, int, int]]:
    height, width = screen.shape[:2]
    if saved:
        old_width, old_height = saved["screen_size"]
        old_x1, old_y1, old_x2, old_y2 = saved["reward_box"]
        margin = int(width * 0.06)
        x1 = max(0, int(old_x1 * width / old_width) - margin)
        y1 = max(0, int(old_y1 * height / old_height) - margin)
        x2 = min(width, int(old_x2 * width / old_width) + margin)
        y2 = min(height, int(old_y2 * height / old_height) + margin)
    else:
        x1, y1, x2, y2 = _search_bounds(width, height)
    crop = screen[y1:y2, x1:x2]
    gray = cv2.medianBlur(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), 5)
    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.2,
        minDist=max(18, int(width * 0.022)),
        param1=100, param2=21,
        minRadius=max(7, int(width * 0.008)),
        maxRadius=max(19, int(width * 0.027)),
    )
    if circles is None:
        return []
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    candidates = []
    for relative_x, relative_y, radius in circles[0]:
        cx, cy, radius = int(round(relative_x)), int(round(relative_y)), int(round(radius))
        xlo, xhi = max(0, cx - radius // 2), min(crop.shape[1], cx + radius // 2 + 1)
        ylo, yhi = max(0, cy - radius // 2), min(crop.shape[0], cy + radius // 2 + 1)
        inside = hsv[ylo:yhi, xlo:xhi]
        yellow = cv2.inRange(inside, (12, 80, 130), (40, 255, 255))
        if yellow.size == 0 or cv2.countNonZero(yellow) / yellow.size < 0.25:
            continue
        absolute_x, absolute_y = cx + x1, cy + y1
        # The room-coin icon is on the left side of the reward card.
        if saved is None and not (int(width * 0.70) <= absolute_x <= int(width * 0.84)):
            continue
        candidates.append((absolute_x, absolute_y, radius))
    if saved and saved.get("coin_center") and saved.get("screen_size"):
        old_w, old_h = saved["screen_size"]
        old_x, old_y = saved["coin_center"]
        expected = (old_x * width / old_w, old_y * height / old_h)
        candidates.sort(key=lambda circle: abs(circle[0] - expected[0]) + abs(circle[1] - expected[1]))
    else:
        candidates.sort(key=lambda circle: circle[1], reverse=True)
    return candidates[:3]


def _box_for_coin(width: int, height: int, coin: tuple[int, int, int]) -> tuple[int, int, int, int]:
    x, y, _ = coin
    return (max(0, x - int(width * 0.045)),
            max(0, y - int(width * 0.11)),
            min(width, x + int(width * 0.27)),
            min(height, y + int(width * 0.055)))


def parse_timer(text: str) -> int | None:
    match = _timer_re.search(text.upper().replace("O", "0").replace(";", ":").replace("+", "4"))
    if not match:
        return None
    minutes, seconds = (int(part) for part in match.group().split(":"))
    return minutes * 60 + seconds if seconds < 60 else None


def parse_success_amount(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if not any(word in compact for word in ("成功領取", "成功领取")):
        return None
    if "coins" not in compact.lower() and "蝦幣" not in compact and "虾币" not in compact:
        return None
    match = _success_amount_re.search(compact)
    if not match:
        return None
    amount = match.group(1).upper().replace("O", "0")
    value = float(amount)
    return amount if 0 < value <= 10 else None


def locate_blue_box(screen: np.ndarray, serial: str) -> tuple[int, int, int, int] | None:
    """Find the sustained blue rows of the coin card, excluding the red timer above it."""
    height, width = screen.shape[:2]
    saved = load_region(serial)
    manual = scaled_box(serial, "blue_box", width, height) if saved and saved.get("source") == "manual" else None
    search = manual or (
        int(width * .69), int(height * .15), int(width * .99), int(height * .39)
    )
    x1, y1, x2, y2 = search
    crop = screen[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (90, 45, 35), (135, 255, 240))
    active = np.flatnonzero(np.mean(mask > 0, axis=1) >= .26)
    if active.size == 0:
        return None
    max_gap = max(2, int(height * .012))
    groups: list[tuple[int, int]] = []
    start = last = int(active[0])
    for row in active[1:]:
        row = int(row)
        if row - last > max_gap:
            groups.append((start, last + 1))
            start = row
        last = row
    groups.append((start, last + 1))
    groups = [pair for pair in groups if pair[1] - pair[0] >= height * .025]
    if not groups:
        return None
    start, end = min(groups, key=lambda pair: abs((y1 + (pair[0] + pair[1]) / 2) - height * .31))
    pad = max(2, int(height * .006))
    return x1, max(y1, y1 + start - pad), x2, min(y2, y1 + end + pad)


def blue_card_present(
    screen: np.ndarray, serial: str, observation: RewardObservation | None = None
) -> bool:
    """A blue shape counts as a reward card only with reward-specific evidence."""
    return (observation or inspect_reward(screen, serial)).state != "absent"


def _read_box_text(screen: np.ndarray, box: tuple[int, int, int, int], target_width: int = 400) -> list[tuple[str, float]]:
    x1, y1, x2, y2 = box
    crop = screen[y1:y2, x1:x2]
    if crop.size == 0:
        return []
    scale = min(4.0, max(1.0, target_width / max(1, crop.shape[1])))
    if scale > 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    with _inference_lock:
        lines = _get_reader().readtext(crop)
    return [(str(value), float(confidence)) for _, value, confidence in lines]


def _time_from_lines(lines: list[tuple[str, float]]) -> int | None:
    values = [parse_timer(text) for text, confidence in lines if confidence >= .18]
    values = [value for value in values if value is not None]
    return min(values) if values else None


def _red_timer(screen: np.ndarray, serial: str, blue_box: tuple[int, int, int, int]) -> int | None:
    height, width = screen.shape[:2]
    x1, y1, x2, _ = blue_box
    box = scaled_box(serial, "red_box", width, height) or (
        x1, max(0, y1 - int(height * .085)), x2, y1
    )
    bx1, by1, bx2, by2 = box
    red_width = bx2 - bx1
    tight = (bx1 + int(red_width * .36), max(by1, by2 - int(height * .025)),
             bx2 - int(red_width * .05), by2)
    return _time_from_lines(_read_box_text(screen, tight, 250)) or _time_from_lines(_read_box_text(screen, box, 400))


def _blue_amount(screen: np.ndarray, box: tuple[int, int, int, int], lines: list[tuple[str, float]]) -> str | None:
    x1, y1, x2, y2 = box
    width, height = x2 - x1, y2 - y1
    amount_box = (x1 + int(width * .14), y1 + int(height * .52),
                  x1 + int(width * .43), y1 + int(height * .83))
    candidates = _read_box_text(screen, amount_box, 130) + lines
    for text, confidence in candidates:
        if confidence < .15:
            continue
        for match in _amount_re.finditer(text):
            amount = match.group(1)
            if 0 < float(amount) <= 10:
                return amount
    return None


def _blue_collect_button(screen: np.ndarray, serial: str, box: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """Locate the center of the OCR text 領取 inside the selected blue card."""
    x1, y1, x2, y2 = box
    width, height = x2 - x1, y2 - y1
    crop_x = x1 + int(width * .43)
    crop_y = y1 + int(height * .38)
    crop = screen[crop_y:y2, crop_x:x2]
    if crop.size == 0:
        return None
    scale = min(4.0, max(1.0, 320 / max(1, crop.shape[1])))
    image = (cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
             if scale > 1 else crop)
    with _inference_lock:
        results = _get_reader().readtext(image)
    candidates = []
    for points, text, confidence in results:
        normalized = re.sub(r"\s+", "", str(text))
        if confidence < .25 or not any(word in normalized for word in ("領取", "领取")):
            continue
        corners = np.asarray(points, dtype=float)
        if corners.shape != (4, 2):
            continue
        center = corners.mean(axis=0)
        point = (int(round(crop_x + center[0] / scale)),
                 int(round(crop_y + center[1] / scale)))
        if crop_x <= point[0] < x2 and crop_y <= point[1] < y2:
            candidates.append((float(confidence), point))
    return max(candidates, default=(0.0, None))[1]


def inspect_timer_box(screen: np.ndarray, serial: str) -> int | None:
    height, width = screen.shape[:2]
    box = scaled_box(serial, "timer_box", width, height)
    if box is None:
        return None
    x1, y1, x2, y2 = box
    crop = screen[y1:y2, x1:x2]
    scale = min(4.0, max(1.0, 500 / max(1, crop.shape[1])))
    if scale > 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    with _inference_lock:
        lines = _get_reader().readtext(crop)
    for _, text, confidence in lines:
        if confidence >= .30:
            seconds = parse_timer(str(text))
            if seconds is not None:
                return seconds
    return None


def inspect_blue_card_timer(screen: np.ndarray, serial: str) -> int | None:
    """Read a countdown from the right-hand blue card when the small coin circle is missed."""
    height, width = screen.shape[:2]
    box = scaled_box(serial, "timer_box", width, height) or (
        int(width * .67), int(height * .20), width, int(height * .43)
    )
    x1, y1, x2, y2 = box
    crop = screen[y1:y2, x1:x2]
    scale = min(3.0, max(1.0, 350 / max(1, crop.shape[1])))
    if scale > 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    with _inference_lock:
        lines = _get_reader().readtext(crop)
    timers = [parse_timer(str(text)) for _, text, confidence in lines if confidence >= .30]
    timers = [seconds for seconds in timers if seconds is not None]
    return min(timers) if timers else None


def inspect_success(screen: np.ndarray, serial: str) -> SuccessObservation:
    height, width = screen.shape[:2]
    box = scaled_box(serial, "success_box", width, height) or (
        int(width * .18), int(height * .37), int(width * .82), int(height * .62)
    )
    x1, y1, x2, y2 = box
    crop = screen[y1:y2, x1:x2]
    scale = min(4.0, max(1.0, 720 / max(1, crop.shape[1])))
    if scale > 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    with _inference_lock:
        lines = _get_reader().readtext(crop)
    text = " ".join(str(value) for _, value, confidence in lines if confidence >= .30)
    return SuccessObservation(parse_success_amount(text), text)


def find_live_tab(screen: np.ndarray, serial: str, *, use_manual: bool = True) -> tuple[int, int] | None:
    height, width = screen.shape[:2]
    manual = button_point(serial, "live_tab", width, height) if use_manual else None
    if manual:
        return manual
    x1, y1, x2, y2 = (int(width * .30), int(height * .03),
                      int(width * .75), int(height * .17))
    crop = screen[y1:y2, x1:x2]
    scale = min(4.0, max(1.0, 600 / max(1, crop.shape[1])))
    if scale > 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    with _inference_lock:
        lines = _get_reader().readtext(crop)
    for points, text, confidence in lines:
        if confidence >= .45 and text.strip() == "直播":
            center = np.mean(np.asarray(points, dtype=float), axis=0)
            return int(x1 + center[0] / scale), int(y1 + center[1] / scale)
    return None


def live_tab_selected(screen: np.ndarray, serial: str) -> bool:
    """Verify the white underline is below 直播 rather than 推薦."""
    point = find_live_tab(screen, serial, use_manual=False)
    if point is None:
        return False
    height, width = screen.shape[:2]
    x, y = point
    x1, x2 = max(0, x - int(width * .035)), min(width, x + int(width * .035))
    y1, y2 = min(height, y + int(height * .006)), min(height, y + int(height * .028))
    crop = screen[y1:y2, x1:x2]
    if crop.size == 0:
        return False
    white = np.all(crop > 235, axis=2)
    row_fraction = np.mean(white, axis=1)
    return float(np.max(row_fraction)) >= .40 and float(np.median(row_fraction)) < .20


def find_home_live_entry(screen: np.ndarray, serial: str) -> tuple[int, int] | None:
    """Find the Shopee home page's live-video entry in the bottom navigation."""
    height, width = screen.shape[:2]
    manual = button_point(serial, "home_live_entry", width, height)
    if manual:
        return manual
    x1, y1, x2, y2 = (int(width * .35), int(height * .76),
                      int(width * .76), int(height * .98))
    crop = screen[y1:y2, x1:x2]
    scale = min(4.0, max(1.0, 550 / max(1, crop.shape[1])))
    if scale > 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    with _inference_lock:
        lines = _get_reader().readtext(crop)
    for points, value, confidence in lines:
        if confidence >= .30 and ("直播" in value and "影音" in value or value.strip() == "直播"):
            center = np.mean(np.asarray(points, dtype=float), axis=0)
            return int(x1 + center[0] / scale), max(0, int(y1 + center[1] / scale - height * .025))
    return None


def find_startup_promo_close(screen: np.ndarray) -> tuple[int, int] | None:
    """Locate the circular close X on Shopee's orange startup promotion."""
    height, width = screen.shape[:2]
    central = screen[int(height * .22):int(height * .65), int(width * .13):int(width * .87)]
    orange = cv2.inRange(cv2.cvtColor(central, cv2.COLOR_BGR2HSV), (0, 110, 120), (20, 255, 255))
    if cv2.countNonZero(orange) / orange.size < .40:
        return None
    x1, x2 = int(width * .75), int(width * .98)
    y1, y2 = int(height * .15), int(height * .30)
    gray = cv2.GaussianBlur(cv2.cvtColor(screen[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY), (7, 7), 1)
    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=max(20, int(width * .05)),
        param1=100, param2=30, minRadius=max(10, int(width * .02)),
        maxRadius=max(25, int(width * .06)),
    )
    if circles is None:
        return None
    candidates = [(int(round(x + x1)), int(round(y + y1))) for x, y, _ in circles[0]]
    best = min(candidates, key=lambda point: abs(point[0] - width * .88) + abs(point[1] - height * .21))
    return best if abs(best[0] - width * .88) < width * .07 and abs(best[1] - height * .21) < height * .06 else None


def find_checkin_popup_close(screen: np.ndarray, serial: str) -> tuple[int, int] | None:
    """Find the close X of the orange '馬上簽到' promotion, only after verifying its text."""
    height, width = screen.shape[:2]
    x1, x2 = int(width * .16), int(width * .84)
    y1, y2 = int(height * .25), int(height * .73)
    crop = screen[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    orange = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (0, 120, 130), (16, 255, 255))
    if cv2.countNonZero(orange) / orange.size < .40:
        return None
    scale = min(2.5, max(1.0, 650 / max(1, crop.shape[1])))
    enlarged = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale > 1 else crop
    with _inference_lock:
        lines = _get_reader().readtext(enlarged)
    words = "".join(str(value).replace(" ", "") for _, value, confidence in lines if confidence >= .45)
    if "馬上簽到" not in words or "領蝦幣或抽優惠券" not in words:
        return None
    manual = button_point(serial, "checkin_close", width, height)
    if manual:
        return manual
    close_x1, close_x2 = int(width * .32), int(width * .68)
    close_y1, close_y2 = int(height * .72), int(height * .85)
    gray = cv2.GaussianBlur(cv2.cvtColor(screen[close_y1:close_y2, close_x1:close_x2], cv2.COLOR_BGR2GRAY), (5, 5), 1)
    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=max(15, int(width * .05)),
        param1=80, param2=18, minRadius=max(5, int(width * .025)),
        maxRadius=max(12, int(width * .065)),
    )
    if circles is not None:
        candidates = [(int(round(x + close_x1)), int(round(y + close_y1))) for x, y, _ in circles[0]]
        return min(candidates, key=lambda point: abs(point[0] - width * .5) + abs(point[1] - height * .79))
    return int(width * .5), int(height * .79)


def _read_card(screen: np.ndarray, box: tuple[int, int, int, int], coin: tuple[int, int, int]) -> RewardObservation:
    x1, y1, x2, y2 = box
    image = screen[y1:y2, x1:x2]
    with _inference_lock:
        results = _get_reader().readtext(image)
    entries = []
    for points, text, confidence in results:
        text = str(text).strip()
        if not text or confidence < 0.25:
            continue
        center = np.mean(np.asarray(points, dtype=float), axis=0)
        entries.append((text, float(confidence), int(x1 + center[0]), int(y1 + center[1])))
    joined = " ".join(item[0] for item in entries)
    compact = joined.replace(" ", "")
    has_title = "直播間蝦幣" in compact or "直播间虾币" in compact
    coin_x, coin_y, _ = coin
    buttons = [(x, y) for text, confidence, x, y in entries
               if confidence >= 0.55 and ("領取" in text or "领取" in text)
               and x > coin_x + (x2 - x1) * 0.35 and abs(y - coin_y) < (y2 - y1) * 0.28]
    amounts = []
    for text, confidence, x, y in entries:
        if confidence < 0.40 or not (coin_x + 18 <= x < coin_x + (x2 - x1) * 0.55):
            continue
        if abs(y - coin_y) > (y2 - y1) * 0.28:
            continue
        match = _amount_re.search(text)
        if match and 0 < float(match.group(1)) <= 10:
            amounts.append(match.group(1))
    timers = [parse_timer(text) for text, confidence, x, y in entries
              if confidence >= .35 and abs(y - coin_y) < (y2 - y1) * .42]
    timers = [seconds for seconds in timers if seconds is not None]
    state = "unknown"
    collect_point = None
    if has_title and any(word in compact for word in ("明日再來", "明天再來", "限定", "上限")):
        state = "limit"
    elif has_title and timers:
        state = "waiting"
    elif has_title and buttons and len(amounts) == 1:
        state = "collect"
        collect_point = buttons[0]
    return RewardObservation(state, box, (coin_x, coin_y), collect_point, joined,
                             amounts[0] if len(amounts) == 1 else None,
                             timers[0] if len(timers) == 1 else None)


def inspect_reward(screen: np.ndarray, serial: str) -> RewardObservation:
    """Classify only the blue coin card; the red timer is reported separately."""
    box = locate_blue_box(screen, serial)
    if box is None:
        return RewardObservation("absent")
    lines = _read_box_text(screen, box, 400)
    blue_seconds = _time_from_lines(lines)
    height, width = screen.shape[:2]
    amount = _blue_amount(screen, box, lines)
    text = " ".join(value for value, confidence in lines if confidence >= .20)
    has_limit = any(word in text for word in ("明日再來", "明天再來", "限定", "上限"))
    button = (_blue_collect_button(screen, serial, box)
              if not has_limit and not (blue_seconds is not None and blue_seconds > 0) else None)
    if amount is None and blue_seconds is None and button is None and not has_limit:
        return RewardObservation("absent")
    red_seconds = _red_timer(screen, serial, box)
    if blue_seconds is not None and blue_seconds > 0:
        state, button = "waiting", None
    elif button and amount:
        state = "collect"
    elif has_limit:
        state, button = "limit", None
    else:
        state, button = "unknown", None
    coin = (box[0] + int((box[2] - box[0]) * .18),
            box[1] + int((box[3] - box[1]) * .68))
    observation = RewardObservation(state, box, coin, button, text, amount,
                                    blue_seconds, red_seconds)
    if state in ("waiting", "collect", "limit"):
        _save_region(serial, width, height, observation)
    return observation
